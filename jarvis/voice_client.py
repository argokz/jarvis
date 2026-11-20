"""Голосовой клиент для ПК: wake word → запись реплики → ядро → озвучка ответа.

Запуск:  uv run --extra voice python -m jarvis.voice_client
Управление: «Hey Jarvis» или Enter — начать слушать; «Hey Jarvis» во время ответа — перебить;
строка текста + Enter — отправить текстом; Ctrl+C — выход.
Проверка микрофона: … -m jarvis.voice_client --mic-test (уровень, перегруз, слово активации, распознавание).
Пока звенит таймер или будильник: «стоп», «хватит», «отложи на 10 минут» без «Hey Jarvis» или Enter.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np

RATE = 16000
FRAME = 1280            # 80 мс — размер кадра, который ждёт openWakeWord
FRAME_MS = FRAME * 1000 // RATE


@dataclass
class Config:
    wake_threshold: float = 0.5
    wake_threshold_playing: float = 0.75   # пока звучит ответ — строже, чтобы не реагировать на эхо
    vad_threshold: float = 0.5
    listen_timeout_ms: int = 5000          # ждать начала речи после wake word
    followup_ms: int = 5000                # после ответа слушать продолжение без wake word (0 — выкл.)
    end_silence_ms: int = 800              # тишина, после которой реплика считается законченной
    max_utterance_ms: int = 25000
    preroll_frames: int = 4                # ~320 мс до начала речи, чтобы не съедать первый слог
    barge_in: str = "wake"                 # wake — перебивать только wake word; vad — любой речью (гарнитура)
    barge_in_ms: int = 400
    passive_ms: int = 240                  # столько речи подряд, пока звенит, — слушаем «стоп» без wake word
    passive_delay_ms: int = 2000           # первые секунды звонка «стоп» не ждём: сначала он должен прозвучать
    passive_end_silence_ms: int = 500
    passive_max_ms: int = 3000
    # Пауза после непонятой реплики; с каждой следующей подряд — втрое дольше, до минуты. Иначе шумная
    # комната, телевизор или неисправный микрофон заглушат будильник: на время записи звонок молчит.
    passive_cooldown_ms: int = 3000
    passive_cooldown_max_ms: int = 60000
    # Слово активации в тексте («Джарвис, включи свет»): каждая услышанная фраза уходит ядру, отвечает
    # оно только на обращение. Работает, когда ядро прислало список слов в hello (своё распознавание).
    phrase_wake: bool = False
    phrase_end_silence_ms: int = 700       # пауза после «Джарвис» перед командой не должна обрывать фразу
    # Детектор речи замечает фразу с опозданием, а имя стоит в самом её начале: на живой проверке от
    # «Джарвис, который час» доходило «Кторый час». Поэтому к фразе прикладывается звук до неё.
    phrase_preroll_frames: int = 20        # ~1,6 с
    phrase_max_ms: int = 15000
    debug: bool = False                    # печатать и фразы без обращения


# --- детекторы (обёртки, чтобы подменять в тестах) ---------------------------------------------

class WakeWord:
    def __init__(self, name: str = "hey_jarvis", model_path: str | None = None):
        import openwakeword
        from openwakeword import Model

        path = model_path or openwakeword.models[name]["model_path"]
        self.model = Model(wakeword_model_paths=[path])

    def score(self, frame: np.ndarray) -> float:
        return float(max(self.model.predict(frame).values()))

    def reset(self) -> None:
        self.model.reset()


class NoWake:
    """Вместо модели wake word, когда обращение ищется в распознанном тексте."""

    def score(self, frame: np.ndarray) -> float:
        return 0.0

    def reset(self) -> None:
        pass


class Vad:
    def __init__(self):
        from openwakeword import VAD

        self.vad = VAD()

    def prob(self, frame: np.ndarray) -> float:
        return float(self.vad.predict(frame, frame_size=640))


class Player:
    """Потоковое воспроизведение PCM16 моно; stop() мгновенно обрывает звук."""

    def __init__(self, rate: int):
        import sounddevice as sd

        self.rate = rate
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stream = sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16", callback=self._cb)
        self._stream.start()

    def _cb(self, outdata, frames, _time, _status):
        n = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        outdata[: len(chunk)] = chunk
        if len(chunk) < n:
            outdata[len(chunk):] = b"\0" * (n - len(chunk))

    def play(self, pcm: bytes) -> None:
        with self._lock:
            self._buf += pcm

    def beep(self, hz: int = 880, ms: int = 120) -> None:
        t = np.arange(int(self.rate * ms / 1000)) / self.rate
        tone = (np.sin(2 * np.pi * hz * t) * 6000 * np.hanning(t.size)).astype(np.int16)
        self.play(tone.tobytes())

    def stop(self) -> None:
        with self._lock:
            self._buf.clear()

    @property
    def playing(self) -> bool:
        with self._lock:
            return bool(self._buf)

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


def load_sound(path: str, rate: int, log=print, max_s: int = 60, which=shutil.which):
    """Звуковой файл → массив моно с частотой rate. Через ffmpeg — любой формат, без него — WAV 16 бит."""
    path = os.path.expanduser(path)
    try:
        ffmpeg = which("ffmpeg")
        if ffmpeg:
            out = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-t", str(max_s), "-f", "s16le", "-ac", "1",
                                  "-ar", str(rate), "-"], capture_output=True, timeout=30, check=True).stdout
            data = np.frombuffer(out, dtype=np.int16).astype(np.float32)
        else:
            with wave.open(path, "rb") as w:
                if w.getsampwidth() != 2:
                    raise ValueError("без ffmpeg читается только WAV 16 бит")
                src = w.getframerate()
                raw = np.frombuffer(w.readframes(min(w.getnframes(), src * max_s)), dtype=np.int16)
                data = raw.astype(np.float32).reshape(-1, w.getnchannels()).mean(axis=1)
            if src != rate and data.size:
                n = int(data.size * rate / src)
                data = np.interp(np.linspace(0, data.size - 1, n), np.arange(data.size), data).astype(np.float32)
        if data.size < rate // 10:
            raise ValueError("в файле нет звука")
    except (OSError, ValueError, EOFError, wave.Error, subprocess.SubprocessError) as e:
        log(f"⚠  Звук будильника не загружен ({path}): {e}. Будет встроенный сигнал.")
        return None
    return np.concatenate([data, np.zeros(rate // 2, dtype=np.float32)])  # пауза между повторами


class RingSound:
    """Звук таймера и будильника: встроенные сигналы или свой файл для будильника (JARVIS_ALARM_SOUND)."""

    RAMP_S = 20.0  # за столько секунд будильник набирает полную громкость
    START = 0.2    # с какой доли громкости начинает

    def __init__(self, rate: int, path: str | None = None, log=print):
        self.rate = rate
        self.sounds = {"timer": self._pattern(1320, 3, 120, 80, 900, 14000),
                       "alarm": self._pattern(880, 4, 150, 100, 700, 20000)}
        custom = load_sound(path, rate, log) if path else None
        if custom is not None:
            self.sounds["alarm"] = custom

    def _tone(self, hz: int, ms: int, amp: int) -> np.ndarray:
        n = int(self.rate * ms / 1000)
        i = np.arange(n)
        fade = np.minimum(1.0, np.minimum(i, n - i) / (self.rate * 0.01))  # 10 мс, чтобы не щёлкало
        return (np.sin(2 * np.pi * hz * i / self.rate) * amp * fade).astype(np.float32)

    def _pattern(self, hz: int, beeps: int, ms: int, gap_ms: int, rest_ms: int, amp: int) -> np.ndarray:
        gap = np.zeros(int(self.rate * gap_ms / 1000), dtype=np.float32)
        rest = np.zeros(int(self.rate * rest_ms / 1000), dtype=np.float32)
        return np.concatenate([*[np.concatenate([self._tone(hz, ms, amp), gap]) for _ in range(beeps)], rest])

    def cycle(self, kind: str, elapsed: float) -> bytes:
        """Один повтор сигнала. Будильник нарастает: elapsed — сколько секунд он уже звенит."""
        sound = self.sounds.get(kind, self.sounds["timer"])
        if kind == "alarm":
            t = elapsed + np.arange(sound.size) / self.rate
            sound = sound * np.clip(self.START + (1 - self.START) * t / self.RAMP_S, self.START, 1.0)
        return sound.astype(np.int16).tobytes()

    def chime(self) -> bytes:
        """Короткий сигнал перед напоминанием."""
        return np.concatenate([self._tone(660, 150, 9000), self._tone(880, 250, 9000)]).astype(np.int16).tobytes()


# --- логика разговора (без звуковых устройств, тестируется) ----------------------------------

IDLE, LISTENING, WAITING = "idle", "listening", "waiting"


class Conversation:
    def __init__(self, wake, vad, player, send, cfg: Config | None = None, log=print,
                 ring: RingSound | None = None, clock=time.monotonic):
        self.wake, self.vad, self.player, self.send = wake, vad, player, send
        self.cfg = cfg or Config()
        self.log = log
        self.ring, self.clock = ring, clock    # без ring клиент не звенит
        self.ringing: dict = {}                # что звенит сейчас: id → сообщение ring от ядра
        self.ring_since = 0.0
        self.passive = False                   # текущая реплика поймана без сигнала: обращение, «стоп» или шум
        self.passive_after = 0.0               # раньше этого момента «стоп» без wake word не слушаем
        self.passive_misses = 0                # сколько реплик подряд оказались не командой
        self.cycle_until = 0.0                 # когда доиграет поставленный в очередь сигнал
        # Звук перед фразой без сигнала; копится и пока ядро разбирает предыдущую фразу.
        self.heard: collections.deque = collections.deque(maxlen=self.cfg.phrase_preroll_frames)
        self._alert_audio = False
        self.state = IDLE
        self.turn: int | None = None       # номер хода, ответ на который ждём
        self.requests = 0                  # сервер нумерует ходы по порядку запросов: n-й запрос = ход n
        self.turn_done = False
        self.no_followup = False           # ядро попросило не слушать продолжение после этого ответа
        self._audio_turn: int | None = None
        self._reset_listen(0)

    # состояния
    def _reset_listen(self, timeout_ms: int) -> None:
        self.preroll: collections.deque = collections.deque(maxlen=self.cfg.preroll_frames)
        self.utterance: list[np.ndarray] = []
        self.started = False
        self.silence_ms = 0
        self.waited_ms = 0
        self.timeout_ms = timeout_ms
        self.speech_run_ms = 0

    def listen(self, beep: bool = True, timeout_ms: int | None = None) -> None:
        if self.ringing:
            self.player.stop()  # звонок не должен попасть в запись
        if beep:
            self.player.beep()
        self.state = LISTENING
        self.passive = False
        self._reset_listen(self.cfg.listen_timeout_ms if timeout_ms is None else timeout_ms)
        self.log("🎙  слушаю…")

    async def interrupt(self) -> None:
        self.player.stop()
        if self.state == WAITING and not self.turn_done:
            await self.send(json.dumps({"type": "cancel"}))
        self.turn, self.turn_done = None, False
        self.passive = False

    async def dismiss(self) -> None:
        """Enter, пока звенит: выключить без слов. Звук смолкнет, когда ядро пришлёт stop."""
        await self.send(json.dumps({"type": "dismiss"}))

    def _idle_tick(self, frame: np.ndarray, playing: bool) -> None:
        """Разговора нет: ловим фразу без сигнала — обращение по имени или «стоп» звонку — и звеним."""
        self.preroll.append(frame)
        own = playing and not self.ringing  # напоминание говорит само — свой голос за обращение не принимаем
        if self.clock() >= self.passive_after and not own:
            speech = self.vad.prob(frame) >= self.cfg.vad_threshold
            self.speech_run_ms = self.speech_run_ms + FRAME_MS if speech else 0
            if self.speech_run_ms >= self.cfg.passive_ms:
                # Со звонком берём короткое начало: в длинном был бы сам звонок.
                frames = list(self.preroll) if self.ringing else list(self.heard)
                self.heard.clear()  # этот звук ушёл во фразу; в следующую он попасть не должен
                if self.ringing:
                    self.player.stop()
                self.state = LISTENING
                self._reset_listen(self.cfg.listen_timeout_ms)
                self.passive, self.started, self.utterance = True, True, frames
                return
        elif own:
            self.speech_run_ms = 0
        if not self.ringing:
            return
        if self.ring and not playing:
            kind = "alarm" if any(r.get("kind") == "alarm" for r in self.ringing.values()) else "timer"
            pcm = self.ring.cycle(kind, self.clock() - self.ring_since)
            self.player.play(pcm)
            self.cycle_until = self.clock() + self._seconds(pcm)

    def _seconds(self, pcm: bytes) -> float:
        return len(pcm) / 2 / (getattr(self.ring, "rate", None) or 22050)

    async def send_text(self, text: str) -> None:
        await self.interrupt()
        await self.send(json.dumps({"type": "text", "text": text}, ensure_ascii=False))
        self._waiting()

    def _waiting(self) -> None:
        self.requests += 1
        self.state = WAITING
        self.turn, self.turn_done = self.requests, False
        self.speech_run_ms = 0

    # кадры с микрофона
    async def on_frame(self, frame: np.ndarray) -> None:
        playing = self.player.playing
        if self.state != LISTENING and not playing:  # записанное в фразу и свой голос сюда не попадают
            self.heard.append(frame)
        elif playing:
            self.heard.clear()
        score = self.wake.score(frame)  # модель должна видеть непрерывный поток
        thr = self.cfg.wake_threshold_playing if playing else self.cfg.wake_threshold
        woke = score >= thr
        if woke:
            self.wake.reset()

        if self.state == IDLE:
            if woke:
                self.listen()
            elif self.ringing or self.cfg.phrase_wake:
                self._idle_tick(frame, playing)
            return

        if self.state == WAITING:
            if woke:
                await self.interrupt()
                self.listen()
                return
            if self.cfg.barge_in == "vad" and playing:
                speech = self.vad.prob(frame) >= self.cfg.vad_threshold
                self.speech_run_ms = self.speech_run_ms + FRAME_MS if speech else 0
                if self.speech_run_ms >= self.cfg.barge_in_ms:
                    await self.interrupt()
                    self.listen(beep=False)
                    return
            if self.turn_done and not playing:
                if self.passive:  # ответили на «стоп» или промолчали на шум — продолжения не ждём
                    self.passive = False
                    self.passive_misses = self.passive_misses + 1 if self.ringing else 0
                    # Пауза нужна звонку: запись его заглушает. Без звонка слушаем дальше сразу.
                    pause = min(self.cfg.passive_cooldown_max_ms,
                                self.cfg.passive_cooldown_ms * 3 ** max(0, self.passive_misses - 1))
                    self.passive_after = self.clock() + (pause / 1000 if self.ringing else 0)
                    self.state = IDLE
                elif self.cfg.followup_ms > 0 and not self.no_followup:
                    self.listen(beep=False, timeout_ms=self.cfg.followup_ms)
                else:
                    self.state = IDLE
            return

        # LISTENING
        if woke and self.passive:
            self.listen()  # позвали по имени — это обычная реплика
            return
        if woke and not self.started:
            return
        speech = self.vad.prob(frame) >= self.cfg.vad_threshold
        if not self.started:
            self.preroll.append(frame)
            if speech:
                self.started = True
                self.utterance = list(self.preroll)
            else:
                self.waited_ms += FRAME_MS
                if self.waited_ms >= self.timeout_ms:
                    self.state = IDLE
                    self.log("…тишина, жду wake word")
            return
        self.utterance.append(frame)
        self.silence_ms = 0 if speech else self.silence_ms + FRAME_MS
        duration = len(self.utterance) * FRAME_MS
        if not self.passive:
            end_ms, max_ms = self.cfg.end_silence_ms, self.cfg.max_utterance_ms
        elif self.ringing:  # ждём короткое «стоп»: длинная запись заглушает звонок
            end_ms, max_ms = self.cfg.passive_end_silence_ms, self.cfg.passive_max_ms
        else:               # ждём «Джарвис, <команда>» одной фразой
            end_ms, max_ms = self.cfg.phrase_end_silence_ms, self.cfg.phrase_max_ms
        if self.silence_ms >= end_ms or duration >= max_ms:
            pcm = np.concatenate(self.utterance).astype(np.int16).tobytes()
            if self.passive:
                await self.send(json.dumps({"type": "passive"}))
            else:
                self.log(f"⏳ отправляю {duration / 1000:.1f} с")
            await self.send(pcm)
            self._waiting()

    # события сервера
    async def on_message(self, msg) -> None:
        if isinstance(msg, bytes):
            if self._alert_audio:  # фраза к таймеру, напоминанию или будильнику
                self._alert_audio = False
                if self.ringing:  # свою же фразу из колонок за команду не принимаем: пока звучит — не слушаем
                    start = max(self.clock(), self.cycle_until)
                    self.passive_after = max(self.passive_after, start + self._seconds(msg) + 0.3)
                self.player.play(msg)
                return
            if self._audio_turn is not None and self._audio_turn == self.turn and self.state == WAITING:
                self.player.play(msg)
            self._audio_turn = None
            return
        data = json.loads(msg)
        kind, turn = data.get("type"), data.get("turn")
        if kind in ("hello", "start", "pong"):
            return
        if kind == "ring":
            if not self.ringing:
                self.ring_since, self.speech_run_ms, self.passive_misses = self.clock(), 0, 0
                self.passive_after = self.ring_since + self.cfg.passive_delay_ms / 1000
            self.ringing[data.get("id")] = data
            self.log(f"🔔 {data.get('text', '')} — скажите «стоп» или «отложи на 10 минут», либо нажмите Enter")
            return
        if kind == "stop":
            if self.ringing.pop(data.get("id"), None) is not None and not self.ringing and self.state == IDLE:
                self.player.stop()
            return
        if kind == "say":
            self.log(f"🔔 {data.get('text', '')}")
            if data.get("chime") and self.ring:
                self.player.play(self.ring.chime())
            return
        if kind == "audio" and "alert" in data:
            self._alert_audio = True
            return
        if turn is None or turn != self.turn:
            if kind == "audio":
                self._audio_turn = None
            return  # хвост отменённого хода
        if kind == "transcript":
            if data.get("wake"):  # обратились по имени — дальше обычный разговор с продолжением
                self.passive = False
            self.log(f"вы: {data['text'] or '(не расслышал)'}")
        elif kind == "wake":      # только позвали: сигнал и запись команды
            self.listen()
        elif kind == "ignored":
            if self.cfg.debug:
                self.log(f"   (мимо: {data.get('text', '')})")
        elif kind == "text":
            self.log(f"   {data['text']}")
        elif kind == "audio":
            self._audio_turn = turn
        elif kind == "error":
            self.log(f"⚠  {data['text']}")
        elif kind == "done":
            self.turn_done = True
            self.no_followup = data.get("followup") is False  # после ответа заиграет музыка: её слушать незачем


# --- запуск ----------------------------------------------------------------------------------

RETRY_S = (1, 2, 5, 10)  # паузы между попытками переподключения; последняя повторяется


async def keep_connected(session, sleep=asyncio.sleep, log=print) -> None:
    """Держит сеанс с ядром: при обрыве или перезапуске ядра подключается заново.

    session(connected) — один сеанс; connected() он вызывает после приветствия ядра.
    Возврата нет: выход только по отмене или если ядро отклонило токен.
    """
    from websockets.exceptions import ConnectionClosed, InvalidHandshake

    attempt = 0

    def connected() -> None:
        nonlocal attempt
        attempt = 0

    while True:
        try:
            await session(connected)
            reason = "ядро закрыло соединение"
        except InvalidHandshake as e:
            if getattr(getattr(e, "response", None), "status_code", None) in (401, 403):
                raise SystemExit("Ядро отклонило токен — проверьте JARVIS_API_TOKEN.") from e
            reason = f"ядро не приняло соединение: {e}"
        except (OSError, ConnectionClosed, TimeoutError) as e:
            reason = str(e) or type(e).__name__
        delay = RETRY_S[min(attempt, len(RETRY_S) - 1)]
        attempt += 1
        log(f"Нет связи с ядром ({reason}). Повтор через {delay} с…")
        await sleep(delay)


async def run(args) -> None:
    import sounddevice as sd
    from websockets.asyncio.client import connect

    cfg = Config(wake_threshold=args.threshold, barge_in=args.barge_in, followup_ms=args.followup * 1000,
                 debug=args.debug)
    headers = {"Authorization": f"Bearer {args.token}"}
    loop = asyncio.get_running_loop()
    frames: asyncio.Queue = asyncio.Queue(maxsize=200)
    lines: asyncio.Queue = asyncio.Queue()

    def mic_cb(indata, _frames, _time, status):
        if status:
            print(f"[микрофон] {status}", file=sys.stderr)
        loop.call_soon_threadsafe(_put, indata[:, 0].copy())

    def _put(frame):
        if not frames.full():
            frames.put_nowait(frame)

    def stdin_reader():
        for line in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, line.rstrip("\n"))

    threading.Thread(target=stdin_reader, daemon=True).start()
    print("Загружаю модели…")
    wake = NoWake() if args.wake == "phrase" else WakeWord(model_path=args.wake_model)
    vad = Vad()
    rings: dict[int, RingSound] = {}

    async def session(connected) -> None:
        async with connect(args.url, additional_headers=headers, max_size=2**24) as ws:
            hello = json.loads(await ws.recv())
            connected()
            if not hello.get("tts"):
                print("⚠  На сервере не настроена речь (JARVIS_STT_URL/JARVIS_TTS_URL или SpeechKit) — ответы будут только текстом.")
            player = Player(hello.get("sample_rate", 48000))
            if player.rate not in rings:  # свой звук читается один раз, а не при каждом переподключении
                rings[player.rate] = RingSound(player.rate, args.alarm_sound)
            names = hello.get("wake") or []
            cfg.phrase_wake = args.wake != "model" and bool(names)
            if args.wake != "model" and not names:
                print("⚠  Ядро не разбирает обращение в тексте (нужно своё распознавание, JARVIS_STT_URL). "
                      + ("Остаётся «Hey Jarvis»." if args.wake == "both" else "Запустите с --wake model."))
            conv = Conversation(wake, vad, player, ws.send, cfg, ring=rings[player.rate])
            # таймеры, напоминания, будильники — сюда; followup — понимаем «продолжения не слушать»
            await ws.send(json.dumps({"type": "subscribe", "followup": True}))
            while not frames.empty():  # звук, накопленный без связи, ядру не нужен
                frames.get_nowait()

            async def reader():
                async for msg in ws:
                    await conv.on_message(msg)
                raise ConnectionError("ядро закрыло соединение")

            async def mic():
                with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
                                    device=args.device, callback=mic_cb):
                    while True:
                        await conv.on_frame(await frames.get())

            async def keyboard():
                while True:
                    line = (await lines.get()).strip()
                    if line:
                        await conv.send_text(line)
                    elif conv.ringing:
                        await conv.dismiss()
                    else:
                        await conv.interrupt()
                        conv.listen()

            call = [f"«{names[0].capitalize()}, …»"] if cfg.phrase_wake else []
            call += ["«Hey Jarvis»"] if args.wake != "phrase" else []
            print(f"Готов. Скажите {' или '.join(call)} либо нажмите Enter. Можно набрать вопрос текстом."
                  if call else "Готов. Нажмите Enter и говорите. Можно набрать вопрос текстом.")
            tasks = [asyncio.create_task(c()) for c in (reader, mic, keyboard)]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                for t in done:
                    t.result()
            finally:
                for t in tasks:
                    t.cancel()
                player.close()

    await keep_connected(session)


# --- проверка микрофона ----------------------------------------------------------------------

CLIP = 32700  # отсчёт на пределе 16 бит


def highpass(x: np.ndarray, fc: float = 80.0) -> np.ndarray:
    """Убирает постоянную составляющую и гул ниже fc: в них нет речи, а уровень они завышают."""
    spec = np.fft.rfft(x)
    spec[np.fft.rfftfreq(x.size, 1 / RATE) < fc] = 0
    return np.fft.irfft(spec, x.size)


def level_stats(frames: list, probs: list[float], threshold: float = 0.5) -> dict:
    """Уровень сигнала и доля кадров, которые детектор считает речью.

    floor и loud — обычная и самая громкая громкость кадра без гула: по их отношению между замером
    тишины и замером речи видно, насколько голос громче фона.
    """
    x = np.concatenate(frames).astype(np.float64) if frames else np.zeros(FRAME)
    per = np.sqrt(np.mean(highpass(x)[: x.size // FRAME * FRAME].reshape(-1, FRAME) ** 2, axis=1))
    return {"rms": float(np.sqrt(np.mean(x ** 2))), "peak": int(np.abs(x).max()),
            "clip": float(np.mean(np.abs(x) >= CLIP) * 100),
            "floor": float(np.median(per)), "loud": float(np.percentile(per, 95)),
            "speech": float(np.mean(np.array(probs) >= threshold) * 100) if probs else 0.0}


def voice_ratio(noise: dict, speech: dict) -> float:
    """Во сколько раз голос громче фона."""
    return speech.get("loud", 0.0) / max(noise.get("floor", 0.0), 1.0)


def mic_verdict(noise: dict, speech: dict) -> list[str]:
    """Что не так с микрофоном, по замеру тишины и замеру речи. Пусто — всё в порядке."""
    out = []
    if max(noise["clip"], speech["clip"]) > 1:
        out.append("Перегруз: сигнал упирается в предел. Убавьте усиление микрофона.")
    if noise["speech"] > 20:
        out.append("Фон принимается за речь: реплика не закончится сама, а «стоп» без слова активации "
                   "будет срабатывать впустую. Убавьте усиление или уберите источник шума.")
    if "loud" in speech and voice_ratio(noise, speech) < 4:
        out.append(f"Голос едва громче фона (в {voice_ratio(noise, speech):.1f} раза): усиление тут не "
                   "поможет, микрофон должен быть ближе — гарнитура или колонка с микрофоном.")
    elif speech["speech"] < 10:
        out.append("Речь не обнаружена: говорите ближе к микрофону или прибавьте усиление.")
    elif speech["peak"] < 1500 and not out:
        out.append("Речь слышна, но тихо: прибавьте усиление микрофона.")
    return out


def mic_test(args) -> None:
    """Пять секунд тишины и шесть секунд речи: уровни, слово активации и, если речь своя, распознавание."""
    import queue

    import sounddevice as sd

    print("Загружаю модели…")
    from .wake import WakeWords, parse

    names = WakeWords(parse(os.getenv("JARVIS_WAKE_WORDS", "джарвис, jarvis")))
    by_text = args.wake != "model" and bool(os.getenv("JARVIS_STT_URL"))
    call = names.phrases[0].capitalize() if by_text and names.phrases else "Hey Jarvis"
    wake = NoWake() if args.wake == "phrase" and by_text else WakeWord(model_path=args.wake_model)
    vad, q = Vad(), queue.Queue()

    def measure(seconds: float) -> tuple[list, dict, float]:
        frames, probs, score = [], [], 0.0
        while not q.empty():
            q.get_nowait()
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            frame = q.get()
            frames.append(frame)
            probs.append(vad.prob(frame))
            score = max(score, wake.score(frame))
        return frames, level_stats(frames, probs), score

    def show(name: str, s: dict) -> None:
        print(f"  {name}: уровень {s['rms']:.0f}, пик {s['peak']} из 32767, на пределе {s['clip']:.1f}%, "
              f"детектор речи сработал в {s['speech']:.0f}% кадров")

    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, device=args.device,
                        callback=lambda indata, _f, _t, _s: q.put(indata[:, 0].copy())):
        print(f"Микрофон: {sd.query_devices(args.device, 'input')['name']}. Готовлю…")
        measure(3)  # встроенный микрофон после включения секунды две выдаёт сигнал на пределе
        print("1/2. Помолчите 5 секунд…")
        quiet, noise, _ = measure(5)
        show("тишина", noise)
        print(f"2/2. Скажите обычным голосом: «{call}, как меня слышно?» — 6 секунд…")
        frames, speech, score = measure(6)
        show("речь", speech)
    print(f"  голос громче фона в {voice_ratio(noise, speech):.1f} раза")
    if not isinstance(wake, NoWake):
        heard = "сработало бы" if score >= args.threshold else "НЕ сработало бы"
        print(f"  «Hey Jarvis»: {score:.2f} при пороге {args.threshold} — {heard}")
    stt = os.getenv("JARVIS_STT_URL")
    if stt:  # своё распознавание: звук не уходит дальше этого сервера
        import httpx

        from .speech import pcm_to_wav

        data = {"language": os.getenv("JARVIS_SPEECH_LANG", "ru-RU").split("-")[0], "response_format": "json"}
        if os.getenv("JARVIS_STT_MODEL"):
            data["model"] = os.environ["JARVIS_STT_MODEL"]
        wav = pcm_to_wav(np.concatenate(frames).astype(np.int16).tobytes(), RATE)
        try:
            t0 = time.monotonic()
            r = httpx.post(stt, data=data, files={"file": ("mic.wav", wav, "audio/wav")}, timeout=60)
            took = time.monotonic() - t0
            if r.status_code == 200:
                text = (r.json().get("text") or "").strip()
                print(f"  распознано за {took:.1f} с: «{text}»")
                if by_text:
                    command = names.match(text)
                    print(f"  обращение «{call}»: " + ("НЕ услышано — ассистент бы не ответил" if command is None
                                                      else f"услышано, команда: «{command}»"))
            else:
                print(f"  распознавание вернуло {r.status_code}")
        except (httpx.HTTPError, ValueError) as e:
            print(f"  распознавание недоступно: {e}")
    if args.mic_save:  # запись остаётся на этом ПК: по ней подбираются усиление и шумоподавление
        from pathlib import Path

        from .speech import pcm_to_wav as to_wav

        path = Path(args.mic_save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(to_wav(np.concatenate(quiet + frames).astype(np.int16).tobytes(), RATE))
        print(f"  запись сохранена: {path}")
    problems = mic_verdict(noise, speech)
    print("\n".join(["Итог:", *[f"  ⚠  {p}" for p in problems]]) if problems else "Итог: микрофон в порядке.")


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    p = argparse.ArgumentParser(description="Голосовой клиент Jarvis")
    p.add_argument("--url", default=os.getenv("JARVIS_VOICE_URL", "ws://127.0.0.1:8000/voice/ws"))
    p.add_argument("--token", default=os.getenv("JARVIS_API_TOKEN", ""))
    p.add_argument("--wake", choices=["phrase", "model", "both"], default=os.getenv("JARVIS_WAKE_MODE", "phrase"),
                   help="phrase — «Джарвис, …» по распознанному тексту (слова задаёт ядро: JARVIS_WAKE_WORDS); "
                        "model — «Hey Jarvis» (openWakeWord); both — и то и другое")
    p.add_argument("--debug", action="store_true", help="печатать и фразы, в которых обращения не было")
    p.add_argument("--threshold", type=float, default=float(os.getenv("JARVIS_WAKE_THRESHOLD", "0.5")))
    p.add_argument("--wake-model", default=os.getenv("JARVIS_WAKE_MODEL") or None,
                   help="путь к своей .onnx-модели wake word (по умолчанию hey_jarvis)")
    p.add_argument("--barge-in", choices=["wake", "vad"], default=os.getenv("JARVIS_BARGE_IN", "wake"))
    p.add_argument("--followup", type=int, default=5, help="секунд слушать продолжение после ответа")
    p.add_argument("--device", default=os.getenv("JARVIS_MIC_DEVICE") or None, help="микрофон: номер или часть имени (список: --list-devices)")
    p.add_argument("--alarm-sound", default=os.getenv("JARVIS_ALARM_SOUND") or None,
                   help="свой звук будильника: файл wav, mp3, ogg (по умолчанию — встроенный сигнал)")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--mic-test", action="store_true", help="проверить микрофон: уровень, перегруз, слово активации")
    p.add_argument("--mic-save", default=os.getenv("JARVIS_MIC_SAVE", "data/mic-test.wav"),
                   help="куда --mic-test сохраняет запись (тишина и речь) для разбора; пусто — не сохранять")
    args = p.parse_args()
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return
    if args.device and args.device.isdigit():
        args.device = int(args.device)
    if args.mic_test:
        return mic_test(args)
    if not args.token:
        sys.exit("Нужен JARVIS_API_TOKEN (в .env или --token).")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
