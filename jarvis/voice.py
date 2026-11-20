"""Голосовой WebSocket: аудио → STT → роутер → озвучка по фразам.

Протокол (клиент → сервер):
  binary                        — реплика целиком: PCM16 little-endian, моно, 16 кГц (до 30 с)
  {"type": "text", "text": ...} — реплика текстом (отладка, клиенты без микрофона)
  {"type": "cancel"}            — перебивание: остановить текущий ответ
  {"type": "ping"}
  {"type": "subscribe"}         — клиент сам звенит и говорит: присылать ему ring / say / stop;
                                  "followup": true — клиент понимает "followup": false в done
  {"type": "passive"}           — следующая binary-реплика поймана без сигнала к разговору: ответ будет,
                                  только если в ней есть слово активации («Клод, …»), либо это «стоп» /
                                  «отложи» звенящему будильнику, либо фраза сценария, которому обращение
                                  не нужно; остальное — посторонняя речь
  {"type": "dismiss"}           — выключить всё, что звенит (Enter в клиенте)

Сервер → клиент:
  {"type": "hello", "tts": bool, "stt": bool, "sample_rate": int, "wake": [слова активации]}
                                  wake пуст — ядро не разбирает обращения в тексте (речь в облаке)
  {"type": "start", "turn": n}                      — начало ответа на реплику
  {"type": "transcript", "turn": n, "text": ...}    — что распознано; "wake": true — в passive-реплике
                                                      было обращение, дальше обычный разговор
  {"type": "wake", "turn": n}                       — в passive-реплике только позвали: слушать команду
  {"type": "ignored", "turn": n, "text": ...}       — passive-реплика без обращения (для отладки)
  {"type": "text", "turn": n, "text": ...}          — очередная фраза ответа
  {"type": "audio", "turn": n, "bytes": k}, затем binary k байт PCM16 моно sample_rate
  {"type": "error", "turn": n, "text": ...}
  {"type": "done", "turn": n}                       — "followup": false — продолжения не слушать: после
                                                      ответа заиграет музыка, и микрофон услышит её
По инициативе ядра, только подписанным клиентам (subscribe):
  {"type": "ring", "id": i, "kind": "timer" | "alarm", "text": ...} — звенеть, пока не придёт stop
  {"type": "say", "id": i, "text": ..., "chime": bool}              — напоминание: сигнал и фраза
  {"type": "stop", "id": i}                                         — перестать звенеть
  {"type": "audio", "alert": i, "bytes": k}, затем binary           — фраза к ring или say
Новая реплика во время ответа отменяет старый ответ (как и cancel).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re

from fastapi import WebSocket

from .router import VoiceRouter
from .schedule import STT_HINT, ScheduleError
from .speech import MAX_STT_BYTES, SpeechError, SpeechKit
from .wake import WakeWords

log = logging.getLogger("jarvis.voice")
IN_RATE = 16000
OWNER = "owner"
# Музыка ассистента молчит, пока он слушает и говорит. Сколько ещё молчать после ответа:
QUIET_GAP = 0.4        # клиент сам не слушает продолжение под музыку — только доиграть ответ
FOLLOWUP_GAP = 6.5     # прочие клиенты ещё 5 секунд слушают продолжение: музыка попала бы в реплику
LISTEN_GAP = 9.0       # позвали по имени: клиент подал сигнал и ждёт команду
SAID_GAP = 7.0         # напоминание: сигнал и фраза
RING_HOLD = 620.0      # будильник звенит не дольше десяти минут

_END = re.compile(r"([.!?…]+[»\")]*)(\s+)")
_MARKUP = re.compile(r"[*_`#>|]+")


class SentenceSplitter:
    """Режет поток текста на фразы, чтобы озвучивать первую, пока генерируется вторая."""

    def __init__(self, soft_limit: int = 220):
        self.buf = ""
        self.soft_limit = soft_limit

    def feed(self, chunk: str) -> list[str]:
        self.buf += chunk
        out = []
        while True:
            m = _END.search(self.buf)
            if m:
                out.append(self.buf[: m.end(1)])
                self.buf = self.buf[m.end():]
                continue
            if len(self.buf) > self.soft_limit:  # длинная фраза без точки — режем по запятой/пробелу
                cut = max(self.buf.rfind(", ", 0, self.soft_limit), self.buf.rfind(" ", 0, self.soft_limit))
                if cut > 0:
                    out.append(self.buf[: cut + 1])
                    self.buf = self.buf[cut + 1:]
                    continue
            break
        return [s for s in (clean(x) for x in out) if s]

    def flush(self) -> list[str]:
        rest, self.buf = clean(self.buf), ""
        return [rest] if rest else []


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", _MARKUP.sub("", text)).strip()


class VoiceService:
    def __init__(self, router: VoiceRouter, speech: SpeechKit, scheduler=None, wake: WakeWords | None = None,
                 scenarios=None, player=None):
        self.router, self.speech, self.scheduler, self.scenarios = router, speech, scheduler, scenarios
        self.player = player  # music.Player или None: музыка, которую надо заглушать на время разговора
        self._release: asyncio.TimerHandle | None = None
        # Обращение ищется в тексте каждой услышанной фразы — только когда распознавание своё.
        self.wake = wake if wake and speech.enabled and getattr(speech, "local", False) else None
        self.sessions: set[VoiceSession] = set()  # подписанные клиенты: сами звенят и говорят
        self._jobs: set[asyncio.Task] = set()

    # --- по инициативе ядра: таймеры, напоминания, будильники ---------------------------------
    async def on_alert(self, event: dict) -> None:
        """Событие планировщика → подписанным клиентам. Фраза синтезируется в фоне."""
        state, item_id = event["state"], event["id"]
        if state in ("stopped", "snoozed"):
            self._resume_music(0.5)
            return await self._broadcast({"type": "stop", "id": item_id})
        if state == "ringing":
            if self.sessions:  # звонок должно быть слышно: музыка молчит, пока его не выключат
                self._hush_music(RING_HOLD)
            await self._broadcast({"type": "ring", "id": item_id, "kind": event["kind"], "text": event["text"]})
        elif state == "said":
            if self.sessions:
                self._hush_music()
                self._resume_music(SAID_GAP)
            await self._broadcast({"type": "say", "id": item_id, "text": event["text"], "chime": True})
        else:
            return
        if self.sessions and self.speech.enabled:
            job = asyncio.create_task(self._speak(item_id, event["speech"]))
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)

    async def _speak(self, item_id: int, text: str) -> None:
        try:
            pcm = await self.speech.synthesize(text)
        except SpeechError as e:
            return log.warning("TTS: %s", e)
        for session in list(self.sessions):
            with contextlib.suppress(Exception):  # клиент отвалился — уберётся сам в run()
                await session.send_audio(pcm, alert=item_id)

    async def announce(self, text: str) -> bool:
        """Фраза по инициативе ядра (сценарий по времени). False — подписанных клиентов нет, сказать некому."""
        if not self.sessions:
            return False
        await self._broadcast({"type": "say", "id": 0, "text": text, "chime": False})
        if self.speech.enabled:
            await self._speak(0, text)
        return True

    def _hush_music(self, limit: float | None = None) -> None:
        if self.player is None:
            return
        if self._release:
            self._release.cancel()
            self._release = None
        self.player.hold(*([limit] if limit else []))

    def _resume_music(self, delay: float) -> None:
        if self.player is None:
            return
        if self._release:
            self._release.cancel()
        self._release = asyncio.get_running_loop().call_later(delay, self.player.release)

    async def _broadcast(self, data: dict) -> None:
        for session in list(self.sessions):
            with contextlib.suppress(Exception):
                await session.send_json(data)

    async def subscribe(self, session: "VoiceSession") -> None:
        self.sessions.add(session)
        for item in self.scheduler.ringing() if self.scheduler else []:  # уже звенит — догоняем
            await session.send_json({"type": "ring", "id": item["id"], "kind": item["kind"],
                                     "text": self.scheduler.title(item)})

    async def dismiss(self) -> None:
        if self.scheduler:
            with contextlib.suppress(ScheduleError):  # ничего не звенит — нечего и выключать
                await self.scheduler.dismiss(by="voice")

    # --- ход разговора --------------------------------------------------------------------------
    async def run_turn(self, session: "VoiceSession", turn: int, text: str | None, audio: bytes | None,
                       passive: bool = False) -> None:
        """passive — фраза, пойманная без сигнала: отвечаем на обращение и на команду будильнику."""
        await session.send_json({"type": "start", "turn": turn})
        attended = called = False  # обратились к ассистенту; только позвали по имени

        def attend() -> None:
            nonlocal attended
            attended = True
            self._hush_music()

        try:
            if audio is not None:
                if not self.speech.enabled:
                    raise SpeechError("Распознавание речи не настроено: задайте YANDEX_SPEECHKIT_API_KEY.")
                if len(audio) > MAX_STT_BYTES:
                    raise SpeechError("Слишком длинная реплика — до 30 секунд.")
                hint = {"hint": STT_HINT} if self.scheduler and self.scheduler.ringing() else {}
                text = await self.speech.recognize(audio, fmt="lpcm", sample_rate=IN_RATE, **hint)
                if not passive:
                    await session.send_json({"type": "transcript", "turn": turn, "text": text})
            text = (text or "").strip()
            quick = await self.scheduler.quick(text) if text and self.scheduler else None
            if quick is not None:  # «стоп» или «отложи» звенящему будильнику — без модели
                if passive:
                    await session.send_json({"type": "transcript", "turn": turn, "text": text})
                attend()
                await self._say(session, turn, quick)
            elif text and not passive:
                attend()
                await self._answer(session, turn, text)
            elif text:
                command = self.wake.match(text) if self.wake else None
                # фраза сценария, которому обращение по имени не нужно («спокойной ночи»)
                bare = self.scenarios.match(text, bare=True) if self.wake and self.scenarios and command is None else None
                if bare is not None:
                    await session.send_json({"type": "transcript", "turn": turn, "text": text, "wake": True})
                    attend()
                    await self._answer(session, turn, text)
                elif command is None:
                    await session.send_json({"type": "ignored", "turn": turn, "text": text})
                elif not command:  # только позвали — клиент подаст сигнал и запишет команду
                    attend()
                    called = True
                    await session.send_json({"type": "wake", "turn": turn})
                else:
                    await session.send_json({"type": "transcript", "turn": turn, "text": text, "wake": True})
                    quick = await self.scheduler.quick(command) if self.scheduler else None
                    attend()  # после quick: выключенный звонок сам отпускает музыку, а нам ещё отвечать
                    if quick is not None:  # «Клод, стоп» звенящему будильнику — тоже без модели
                        await self._say(session, turn, quick)
                    else:
                        await self._answer(session, turn, command)
        except asyncio.CancelledError:
            raise
        except SpeechError as e:
            await session.send_json({"type": "error", "turn": turn, "text": str(e)})
        except Exception as e:
            log.exception("ошибка голосового хода")
            await session.send_json({"type": "error", "turn": turn, "text": f"Ошибка: {e}"})
        finally:
            resumes = attended and self.player is not None and self.player.state == "playing"
            if attended:  # музыка продолжит, когда клиент доиграет ответ и перестанет слушать
                gap = LISTEN_GAP if called else QUIET_GAP if session.quiet_followup else FOLLOWUP_GAP
                self._resume_music(session.audio_left() + gap)
        await session.send_json({"type": "done", "turn": turn, **({"followup": False} if resumes and not called else {})})

    async def _say(self, session: "VoiceSession", turn: int, phrase: str) -> None:
        await session.send_json({"type": "text", "turn": turn, "text": phrase})
        if self.speech.enabled:
            await session.send_audio(await self.speech.synthesize(phrase), turn=turn)

    async def _answer(self, session: "VoiceSession", turn: int, text: str) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        synth: list[asyncio.Task] = []

        async def sender():
            while (item := await queue.get()) is not None:
                phrase, job = item
                await session.send_json({"type": "text", "turn": turn, "text": phrase})
                if job is None:
                    continue
                try:
                    pcm = await job
                except SpeechError as e:
                    log.warning("TTS: %s", e)
                    continue
                await session.send_audio(pcm, turn=turn)

        def put(phrase: str):
            job = asyncio.create_task(self.speech.synthesize(phrase)) if self.speech.enabled else None
            if job:
                synth.append(job)
            queue.put_nowait((phrase, job))

        splitter = SentenceSplitter()
        out = asyncio.create_task(sender())
        scene = self.scenarios.match(text) if self.scenarios else None  # фраза сценария — без модели
        source = (self.scenarios.respond(OWNER, scene, "voice", text) if scene
                  else self.router.respond(OWNER, text, channel="voice"))
        try:
            async for chunk in source:
                for phrase in splitter.feed(chunk):
                    put(phrase)
                if out.done():  # отправка упала — дальше генерировать незачем
                    break
            for phrase in splitter.flush():
                put(phrase)
            queue.put_nowait(None)
            await out
        finally:
            for t in [out, *synth]:
                if not t.done():
                    t.cancel()
            for t in [out, *synth]:
                with contextlib.suppress(BaseException):
                    await t


class VoiceSession:
    def __init__(self, ws: WebSocket, service: VoiceService):
        self.ws, self.service = ws, service
        self.turn = 0
        self.task: asyncio.Task | None = None
        self.passive_next = False  # клиент предупредил: следующая реплика — без слова активации
        self.quiet_followup = False  # клиент не слушает продолжение, если done пришёл с "followup": false
        self.audio_until = 0.0     # когда клиент доиграет отправленный ему ответ (время цикла событий)
        self._lock = asyncio.Lock()

    async def send_json(self, data: dict) -> None:
        async with self._lock:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False))

    async def send_audio(self, pcm: bytes, turn: int | None = None, alert: int | None = None) -> None:
        """Звук ответа на ход turn либо фраза к сработавшей записи alert."""
        head = {"type": "audio", **({"alert": alert} if turn is None else {"turn": turn}), "bytes": len(pcm)}
        now = asyncio.get_running_loop().time()
        self.audio_until = max(self.audio_until, now) + len(pcm) / 2 / max(1, self.service.speech.tts_rate)
        async with self._lock:  # заголовок и данные должны идти подряд
            await self.ws.send_text(json.dumps(head))
            await self.ws.send_bytes(pcm)

    def audio_left(self) -> float:
        return max(0.0, self.audio_until - asyncio.get_running_loop().time())

    async def run(self) -> None:
        sp = self.service.speech
        wake = self.service.wake.phrases if self.service.wake else []
        await self.send_json({"type": "hello", "tts": sp.enabled, "stt": sp.enabled, "sample_rate": sp.tts_rate,
                              "wake": wake})
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    passive, self.passive_next = self.passive_next, False
                    await self._start(audio=msg["bytes"], passive=passive)
                    continue
                try:
                    data = json.loads(msg.get("text") or "")
                except ValueError:
                    await self.send_json({"type": "error", "text": "ожидался JSON"})
                    continue
                kind = data.get("type")
                if kind == "text" and str(data.get("text", "")).strip():
                    await self._start(text=str(data["text"]))
                elif kind == "cancel":
                    await self._cancel()
                elif kind == "ping":
                    await self.send_json({"type": "pong"})
                elif kind == "subscribe":
                    self.quiet_followup = data.get("followup") is True
                    await self.service.subscribe(self)
                elif kind == "passive":
                    self.passive_next = True
                elif kind == "dismiss":
                    await self.service.dismiss()
        finally:
            self.service.sessions.discard(self)
            await self._cancel()

    async def _cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.task = None

    async def _start(self, text: str | None = None, audio: bytes | None = None, passive: bool = False) -> None:
        await self._cancel()
        self.turn += 1
        self.task = asyncio.create_task(self.service.run_turn(self, self.turn, text, audio, passive))
