"""Gateway: один API для всех клиентов. Запуск: uv run uvicorn jarvis.api:app"""
from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import registry
from .agent import Brain
from .config import Settings
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis")

settings = Settings()
brain = Brain(settings, Store(settings.db))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if settings.telegram_token:
        from .telegram_bot import build

        bot, dp = build(settings, brain)
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        log.info("Telegram-бот запущен, разрешённые id: %s", sorted(settings.telegram_allowed) or "НЕТ")
    else:
        log.info("TELEGRAM_BOT_TOKEN не задан — бот выключен")
    yield
    if task:
        task.cancel()


app = FastAPI(title="Jarvis core", lifespan=lifespan)


def auth(authorization: str = Header(default="")) -> str:
    if not settings.api_token:
        raise HTTPException(503, "JARVIS_API_TOKEN не задан — HTTP API выключен")
    if not hmac.compare_digest(authorization, f"Bearer {settings.api_token}"):
        raise HTTPException(401, "неверный токен")
    return "owner"


class ChatIn(BaseModel):
    text: str
    channel: str = "api"


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


@app.get("/projects")
async def projects(user: str = Depends(auth)):
    return registry.load_projects(settings.vault)


@app.get("/journal")
async def journal(limit: int = 50, user: str = Depends(auth)):
    return brain.store.recent(min(limit, 500))
