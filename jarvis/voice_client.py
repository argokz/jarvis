"""Голосовой клиент для ПК: wake word → запись реплики → ядро → озвучка ответа.

Запуск:  uv run --extra voice python -m jarvis.voice_client
Управление: «Hey Jarvis» или Enter — начать слушать; «Hey Jarvis» во время ответа — перебить;
строка текста + Enter — отправить текстом; Ctrl+C — выход.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
import threading
import time
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


# --- логика разговора (без звуковых устройств, тестируется) ----------------------------------

IDLE, LISTENING, WAITING = "idle", "listening", "waiting"


class Conversation:
    def __init__(self, wake, vad, player, send, cfg: Config | None = None, log=print):
        self.wake, self.vad, self.player, self.send = wake, vad, player, send
        self.cfg = cfg or Config()
        self.log = log
        self.state = IDLE
        self.turn: int | None = None       # номер хода, ответ на который ждём
        self.requests = 0                  # сервер нумерует ходы по порядку запросов: n-й запрос = ход n
        self.turn_done = False
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
        if beep:
            self.player.beep()
        self.state = LISTENING
        self._reset_listen(self.cfg.listen_timeout_ms if timeout_ms is None else timeout_ms)
        self.log("🎙  слушаю…")

    async def interrupt(self) -> None:
        self.player.stop()
        if self.state == WAITING and not self.turn_done:
            await self.send(json.dumps({"type": "cancel"}))
        self.turn, self.turn_done = None, False

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
        score = self.wake.score(frame)  # модель должна видеть непрерывный поток
        thr = self.cfg.wake_threshold_playing if playing else self.cfg.wake_threshold
        woke = score >= thr
        if woke:
            self.wake.reset()

        if self.state == IDLE:
            if woke:
                self.listen()
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
                if self.cfg.followup_ms > 0:
                    self.listen(beep=False, timeout_ms=self.cfg.followup_ms)
                else:
                    self.state = IDLE
            return

        # LISTENING
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
        if self.silence_ms >= self.cfg.end_silence_ms or duration >= self.cfg.max_utterance_ms:
            pcm = np.concatenate(self.utterance).astype(np.int16).tobytes()
            self.log(f"⏳ отправляю {duration / 1000:.1f} с")
            await self.send(pcm)
            self._waiting()

    # события сервера
    async def on_message(self, msg) -> None:
        if isinstance(msg, bytes):
            if self._audio_turn is not None and self._audio_turn == self.turn and self.state == WAITING:
                self.player.play(msg)
            self._audio_turn = None
            return
        data = json.loads(msg)
        kind, turn = data.get("type"), data.get("turn")
        if kind in ("hello", "start", "pong"):
            return
        if turn is None or turn != self.turn:
            if kind == "audio":
                self._audio_turn = None
            return  # хвост отменённого хода
        if kind == "transcript":
            self.log(f"вы: {data['text'] or '(не расслышал)'}")
        elif kind == "text":
            self.log(f"   {data['text']}")
        elif kind == "audio":
            self._audio_turn = turn
        elif kind == "error":
            self.log(f"⚠  {data['text']}")
        elif kind == "done":
            self.turn_done = True


# --- запуск ----------------------------------------------------------------------------------

async def run(args) -> None:
    import sounddevice as sd
    from websockets.asyncio.client import connect

    cfg = Config(wake_threshold=args.threshold, barge_in=args.barge_in, followup_ms=args.followup * 1000)
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
    wake, vad = WakeWord(model_path=args.wake_model), Vad()

    async with connect(args.url, additional_headers=headers, max_size=2**24) as ws:
        hello = json.loads(await ws.recv())
        if not hello.get("tts"):
            print("⚠  На сервере не настроен SpeechKit — ответы будут только текстом.")
        player = Player(hello.get("sample_rate", 48000))
        conv = Conversation(wake, vad, player, ws.send, cfg)

        async def reader():
            async for msg in ws:
                await conv.on_message(msg)

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
                else:
                    await conv.interrupt()
                    conv.listen()

        print("Готов. Скажите «Hey Jarvis» или нажмите Enter. Можно набрать вопрос текстом.")
        tasks = [asyncio.create_task(c()) for c in (reader, mic, keyboard)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()
        finally:
            for t in tasks:
                t.cancel()
            player.close()


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    p = argparse.ArgumentParser(description="Голосовой клиент Jarvis")
    p.add_argument("--url", default=os.getenv("JARVIS_VOICE_URL", "ws://127.0.0.1:8000/voice/ws"))
    p.add_argument("--token", default=os.getenv("JARVIS_API_TOKEN", ""))
    p.add_argument("--threshold", type=float, default=float(os.getenv("JARVIS_WAKE_THRESHOLD", "0.5")))
    p.add_argument("--wake-model", default=os.getenv("JARVIS_WAKE_MODEL") or None,
                   help="путь к своей .onnx-модели wake word (по умолчанию hey_jarvis)")
    p.add_argument("--barge-in", choices=["wake", "vad"], default=os.getenv("JARVIS_BARGE_IN", "wake"))
    p.add_argument("--followup", type=int, default=5, help="секунд слушать продолжение после ответа")
    p.add_argument("--device", default=None, help="микрофон: номер или часть имени (список: --list-devices)")
    p.add_argument("--list-devices", action="store_true")
    args = p.parse_args()
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return
    if args.device and args.device.isdigit():
        args.device = int(args.device)
    if not args.token:
        sys.exit("Нужен JARVIS_API_TOKEN (в .env или --token).")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
