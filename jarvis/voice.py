"""Голосовой WebSocket: аудио → STT → роутер → озвучка по фразам.

Протокол (клиент → сервер):
  binary                        — реплика целиком: PCM16 little-endian, моно, 16 кГц (до 30 с)
  {"type": "text", "text": ...} — реплика текстом (отладка, клиенты без микрофона)
  {"type": "cancel"}            — перебивание: остановить текущий ответ
  {"type": "ping"}

Сервер → клиент:
  {"type": "hello", "tts": bool, "stt": bool, "sample_rate": int}
  {"type": "start", "turn": n}                      — начало ответа на реплику
  {"type": "transcript", "turn": n, "text": ...}    — что распознано
  {"type": "text", "turn": n, "text": ...}          — очередная фраза ответа
  {"type": "audio", "turn": n, "bytes": k}, затем binary k байт PCM16 моно sample_rate
  {"type": "error", "turn": n, "text": ...}
  {"type": "done", "turn": n}
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
from .speech import MAX_STT_BYTES, SpeechError, SpeechKit

log = logging.getLogger("jarvis.voice")
IN_RATE = 16000
OWNER = "owner"

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
    def __init__(self, router: VoiceRouter, speech: SpeechKit):
        self.router, self.speech = router, speech

    async def run_turn(self, session: "VoiceSession", turn: int, text: str | None, audio: bytes | None) -> None:
        await session.send_json({"type": "start", "turn": turn})
        try:
            if audio is not None:
                if not self.speech.enabled:
                    raise SpeechError("Распознавание речи не настроено: задайте YANDEX_SPEECHKIT_API_KEY.")
                if len(audio) > MAX_STT_BYTES:
                    raise SpeechError("Слишком длинная реплика — до 30 секунд.")
                text = await self.speech.recognize(audio, fmt="lpcm", sample_rate=IN_RATE)
                await session.send_json({"type": "transcript", "turn": turn, "text": text})
            if text and text.strip():
                await self._answer(session, turn, text.strip())
        except asyncio.CancelledError:
            raise
        except SpeechError as e:
            await session.send_json({"type": "error", "turn": turn, "text": str(e)})
        except Exception as e:
            log.exception("ошибка голосового хода")
            await session.send_json({"type": "error", "turn": turn, "text": f"Ошибка: {e}"})
        await session.send_json({"type": "done", "turn": turn})

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
                await session.send_audio(turn, pcm)

        def put(phrase: str):
            job = asyncio.create_task(self.speech.synthesize(phrase)) if self.speech.enabled else None
            if job:
                synth.append(job)
            queue.put_nowait((phrase, job))

        splitter = SentenceSplitter()
        out = asyncio.create_task(sender())
        try:
            async for chunk in self.router.respond(OWNER, text, channel="voice"):
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
        self._lock = asyncio.Lock()

    async def send_json(self, data: dict) -> None:
        async with self._lock:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False))

    async def send_audio(self, turn: int, pcm: bytes) -> None:
        async with self._lock:  # заголовок и данные должны идти подряд
            await self.ws.send_text(json.dumps({"type": "audio", "turn": turn, "bytes": len(pcm)}))
            await self.ws.send_bytes(pcm)

    async def run(self) -> None:
        sp = self.service.speech
        await self.send_json({"type": "hello", "tts": sp.enabled, "stt": sp.enabled, "sample_rate": sp.tts_rate})
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await self._start(audio=msg["bytes"])
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
        finally:
            await self._cancel()

    async def _cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.task = None

    async def _start(self, text: str | None = None, audio: bytes | None = None) -> None:
        await self._cancel()
        self.turn += 1
        self.task = asyncio.create_task(self.service.run_turn(self, self.turn, text, audio))
