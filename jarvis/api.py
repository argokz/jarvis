"""Gateway: один API для всех клиентов. Запуск: uv run uvicorn jarvis.api:app"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import registry
from .agent import Brain
from .config import Settings
from .confirm import Confirmations
from .events import EventBus
from .notify import Notifier
from .router import VoiceRouter
from .speech import SpeechKit
from .store import Store, _text
from .voice import VoiceService, VoiceSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis")
WEB_DIST = Path(__file__).resolve().parent.parent / "app" / "dist"

settings = Settings()
store = Store(settings.db)
bus = EventBus()
notifier = Notifier(settings.ntfy_url, settings.ntfy_topic, settings.ntfy_token)
confirms = Confirmations(bus, notifier, store)
brain = Brain(settings, store, confirms)
speech = SpeechKit(settings.speechkit_key, settings.speechkit_lang, settings.speechkit_voice,
                   settings.speechkit_tts_rate, settings.speechkit_folder)
router = VoiceRouter(settings, brain, store, notifier)
voice = VoiceService(router, speech)


def journal_event(entry: dict) -> dict:
    chat = entry["kind"] in ("user", "assistant")
    text = _text(entry["payload"]) if chat else (entry["payload"] or "")[:300]
    return {"type": "journal", "id": entry["id"], "ts": entry["ts"], "channel": entry["channel"],
            "kind": entry["kind"], "name": entry["name"], "text": text}


store.listeners.append(lambda e: bus.publish(journal_event(e)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if settings.telegram_token:
        from .telegram_bot import build

        bot, dp = build(settings, brain, speech, confirms)
        notifier.use_telegram(bot, settings.telegram_allowed)
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        log.info("Telegram-бот запущен, разрешённые id: %s", sorted(settings.telegram_allowed) or "НЕТ")
    else:
        log.info("TELEGRAM_BOT_TOKEN не задан — бот выключен")
    log.info("push через ntfy: %s", "включён" if notifier.ntfy else "выключен")
    yield
    if task:
        task.cancel()


app = FastAPI(title="Jarvis core", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"],
                   allow_headers=["Authorization", "Content-Type"])
if WEB_DIST.is_dir():  # веб-клиент: тот же, что в Tauri; удобно открыть с телефона по HTTPS Tailscale
    app.mount("/app", StaticFiles(directory=WEB_DIST, html=True), name="app")


def token_ok(authorization: str) -> bool:
    return bool(settings.api_token) and hmac.compare_digest(authorization, f"Bearer {settings.api_token}")


def auth(authorization: str = Header(default="")) -> str:
    if not settings.api_token:
        raise HTTPException(503, "JARVIS_API_TOKEN не задан — HTTP API выключен")
    if not token_ok(authorization):
        raise HTTPException(401, "неверный токен")
    return "owner"


async def ws_auth(ws: WebSocket) -> bool:
    # токен — в заголовке Authorization или (браузеры не умеют заголовки у WebSocket) в ?token=
    header = ws.headers.get("authorization") or f"Bearer {ws.query_params.get('token', '')}"
    if not token_ok(header):
        await ws.close(code=4401)
        return False
    await ws.accept()
    return True


class ChatIn(BaseModel):
    text: str
    channel: str = "api"


class ConfirmIn(BaseModel):
    approve: bool


@app.get("/")
async def root():
    return RedirectResponse("/app/") if WEB_DIST.is_dir() else {"ok": True, "docs": "/docs"}


@app.get("/health")
async def health():
    return {"ok": True, "model": settings.model, "vault": str(settings.vault)}


@app.post("/chat")
async def chat(body: ChatIn, user: str = Depends(auth)):
    r = await brain.ask(user, body.text, channel=body.channel)
    return {"text": r.text, "session_id": r.session_id, "cost_usd": r.cost_usd, "is_error": r.is_error}


@app.post("/session/new")
async def session_new(user: str = Depends(auth)):
    brain.new_session(user)
    return {"ok": True}


@app.get("/messages")
async def messages(limit: int = 100, user: str = Depends(auth)):
    return store.messages(user, min(limit, 500))


@app.get("/projects")
async def projects(user: str = Depends(auth)):
    return registry.load_projects(settings.vault)


@app.get("/journal")
async def journal(limit: int = 50, user: str = Depends(auth)):
    return [journal_event(e) for e in store.recent(min(limit, 500))]


@app.get("/confirmations")
async def confirmations(user: str = Depends(auth)):
    return confirms.pending()


@app.post("/confirmations/{confirm_id}")
async def confirmation_resolve(confirm_id: str, body: ConfirmIn, user: str = Depends(auth)):
    if not confirms.resolve(confirm_id, body.approve, by="app"):
        raise HTTPException(404, "подтверждение не найдено или уже закрыто")
    return {"ok": True}


@app.websocket("/events/ws")
async def events_ws(ws: WebSocket):
    """Живая лента: реплики из всех каналов, вызовы инструментов, запросы подтверждения."""
    if not await ws_auth(ws):
        return
    q = bus.subscribe()
    try:
        await ws.send_text(json.dumps({"type": "hello", "pending": confirms.pending()}, ensure_ascii=False))

        async def pump():
            while True:
                await ws.send_text(json.dumps(await q.get(), ensure_ascii=False))

        async def drain():  # входящие игнорируем, ждём только закрытия
            while (await ws.receive())["type"] != "websocket.disconnect":
                pass

        tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
        _, rest = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in rest:
            t.cancel()
            with contextlib.suppress(BaseException):
                await t
    finally:
        bus.unsubscribe(q)


@app.websocket("/voice/ws")
async def voice_ws(ws: WebSocket):
    if await ws_auth(ws):
        await VoiceSession(ws, voice).run()
