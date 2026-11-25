"""Gateway: один API для всех клиентов. Запуск: uv run uvicorn jarvis.api:app"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import music, pc, registry, wake
from .agent import Brain
from .backup import Backup
from .config import Settings
from .confirm import Confirmations
from .events import EventBus
from .home import Home
from .homepanel import Panel, PanelError
from .notify import Notifier
from .router import VoiceRouter
from .scenarios import ScenarioError, Scenarios
from .schedule import ScheduleError, Scheduler
from .speech import make_speech
from .status import Status, Watch
from .store import Store, _text
from .voice import VoiceService, VoiceSession
from .wake import WakeWords

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis")


class HideToken(logging.Filter):
    """uvicorn пишет в журнал адрес WebSocket целиком, а браузеры передают токен в ?token= — вырезаем его."""
    TOKEN = re.compile(r"(token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if "token=" in text:
            record.msg, record.args = self.TOKEN.sub(r"\1***", text), ()
        return True


for _name in ("uvicorn.error", "uvicorn.access"):
    logging.getLogger(_name).addFilter(HideToken())
WEB_DIST = Path(__file__).resolve().parent.parent / "app" / "dist"

settings = Settings()
store = Store(settings.db)
bus = EventBus()
notifier = Notifier(settings.ntfy_url, settings.ntfy_topic, settings.ntfy_token)
confirms = Confirmations(bus, notifier, store)
scheduler = Scheduler(store, notifier, bus)
brain = Brain(settings, store, confirms, scheduler=scheduler)
scheduler.journal = brain.journal_note
speech = make_speech(settings)
home = Home(settings.hass_url, settings.hass_token, settings.hass_mcp_path)
# Музыка: на этом ПК — YouTube и Яндекс Музыка (если разрешено управление ПК), на Станции — через HA
tunes = music.Music(
    sources=[music.YouTube(), music.Yandex(settings.yandex_music_token)] if settings.pc_control else [],
    station=music.Station(settings.hass_url, settings.hass_token, settings.station_entity),
    default=settings.music_source or ("yandex" if settings.yandex_music_token else "youtube"),
    level_file=settings.db.parent / "music_volume.json")
computer = pc.PC(player=tunes.player) if settings.pc_control else None
scenarios = Scenarios(settings.vault, store, home=home, computer=computer, scheduler=scheduler, brain=brain,
                      notifier=notifier, confirm=confirms, tunes=tunes)
scenarios.journal = brain.journal_note
brain.scenes, brain.computer, brain.tunes = scenarios, computer, tunes
router = VoiceRouter(settings, brain, store, notifier, home=home, computer=computer, scheduler=scheduler,
                     scenes=scenarios, tunes=tunes)
voice = VoiceService(router, speech, scheduler, WakeWords(wake.parse(settings.wake_words)), scenarios,
                     player=tunes.player if tunes.sources else None, computer=computer, duck=settings.media_duck)
scenarios.announce = voice.announce
scheduler.sinks.append(voice.on_alert)
scheduler.has_voice = lambda: bool(voice.sessions)
scheduler.hooks.append(scenarios.tick)
panel = Panel(settings.hass_url, settings.hass_token)
panel.changed = lambda: setattr(home, "_cache", None)  # открыли устройство ассистенту — голос видит его сразу
backup = Backup(settings.vault, settings.db, settings.backup_dir, settings.backup_keep, settings.backup_hours)
status = Status(settings, store, speech, home, tunes, scenarios, scheduler, voice, notifier, web=brain.web,
                backup=backup)
watch = Watch(status, notifier, bus, interval=settings.watch_min * 60)


def journal_event(entry: dict) -> dict:
    chat = entry["kind"] in ("user", "assistant")
    text = _text(entry["payload"]) if chat else (entry["payload"] or "")[:300]
    return {"type": "journal", "id": entry["id"], "ts": entry["ts"], "channel": entry["channel"],
            "kind": entry["kind"], "name": entry["name"], "text": text}


store.listeners.append(lambda e: bus.publish(journal_event(e)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    clock = asyncio.create_task(scheduler.run())
    if settings.telegram_token:
        from .telegram_bot import build

        bot, dp = build(settings, brain, speech, confirms, scheduler, scenarios)
        notifier.use_telegram(bot, settings.telegram_allowed)
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        log.info("Telegram-бот запущен, разрешённые id: %s", sorted(settings.telegram_allowed) or "НЕТ")
    else:
        log.info("TELEGRAM_BOT_TOKEN не задан — бот выключен")
    log.info("push через ntfy: %s", "включён" if notifier.ntfy else "выключен")
    side = [asyncio.create_task(watch.run())] if settings.watch_min > 0 else []
    if backup.enabled:
        side.append(asyncio.create_task(backup.run()))
    if panel.enabled and settings.home_live:
        side.append(asyncio.create_task(panel.live(bus.publish)))
    log.info("сторож: %s; резервная копия: %s", f"раз в {settings.watch_min:g} мин" if side and settings.watch_min > 0
             else "выключен", backup.target or "выключена")
    yield
    tunes.player.stop()  # иначе ffplay доиграет очередь уже без ядра
    clock.cancel()
    for job in side:
        job.cancel()
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


class SnoozeIn(BaseModel):
    minutes: int = 10


class EnabledIn(BaseModel):
    enabled: bool


class HomeActIn(BaseModel):
    action: str
    value: float | None = None


class ExposeIn(BaseModel):
    exposed: bool


class FlowIn(BaseModel):
    data: dict = {}


class DeviceIn(BaseModel):
    name: str | None = None
    area_id: str | None = None
    new_area: str | None = None
    aliases: list[str] | None = None


class PhrasesIn(BaseModel):
    phrases: list[str]
    without_name: bool | None = None


class ScenarioIn(BaseModel):
    name: str
    phrases: list[str] = []
    steps: list = []
    without_name: bool = False


@app.get("/")
async def root():
    return RedirectResponse("/app/") if WEB_DIST.is_dir() else {"ok": True, "docs": "/docs"}


@app.get("/health")
async def health():
    return {"ok": True, "model": settings.model, "vault": str(settings.vault)}


@app.get("/status")
async def status_get(user: str = Depends(auth)):
    """Что умеет ассистент и что из этого сейчас работает — для экрана «Функции»."""
    return await status.collect()


@app.post("/status/speech")
async def status_speech(user: str = Depends(auth)):
    """Проверка речи без микрофона: фраза синтезируется и распознаётся обратно."""
    return await status.speech_check()


@app.post("/chat")
async def chat(body: ChatIn, user: str = Depends(auth)):
    scene = await scenarios.handle(user, body.text, body.channel)  # фраза сценария — без модели
    if scene is not None:
        return {"text": scene, "session_id": None, "cost_usd": None, "is_error": False}
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


@app.get("/schedule")
async def schedule_list(user: str = Depends(auth)):
    """Активные таймеры, напоминания и будильники; звенящие — первыми."""
    return [scheduler.public(i) for i in scheduler.active()]


@app.post("/schedule/{item_id}/stop")
async def schedule_stop(item_id: int, user: str = Depends(auth)):
    if not await scheduler.stop(item_id, by="app"):
        raise HTTPException(404, "запись не звенит")
    return {"ok": True}


@app.post("/schedule/{item_id}/snooze")
async def schedule_snooze(item_id: int, body: SnoozeIn, user: str = Depends(auth)):
    if not 1 <= body.minutes <= 720 or not await scheduler.snooze(item_id, body.minutes, by="app"):
        raise HTTPException(404, "запись нельзя отложить")
    return {"ok": True}


@app.delete("/schedule/{item_id}")
async def schedule_cancel(item_id: int, user: str = Depends(auth)):
    try:
        await scheduler.call("schedule_cancel", {"id": item_id}, "app")
    except ScheduleError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}


@app.get("/scenarios")
async def scenario_list(user: str = Depends(auth)):
    return [scenarios.public(s) for s in scenarios.load()]


def _scenario(name: str):
    scene = next((s for s in scenarios.load() if s.id == name), None)
    if scene is None:
        raise HTTPException(404, "нет такого сценария")
    return scene


@app.post("/scenarios")
async def scenario_create(body: ScenarioIn, user: str = Depends(auth)):
    """Своя голосовая команда из приложения: фразы и шаги. Владелец собрал её сам — подтверждение не нужно."""
    try:
        text = await scenarios.save(body.model_dump(), channel="app", by_owner=True)
    except ScenarioError as e:
        raise HTTPException(400, str(e)) from e
    store.log(user, "app", "tool", "scenario_save", {"name": body.name, "phrases": body.phrases})
    return {"ok": True, "text": text}


@app.post("/scenarios/{name}/phrases")
async def scenario_phrases(name: str, body: PhrasesIn, user: str = Depends(auth)):
    _scenario(name)
    try:
        return scenarios.public(scenarios.set_phrases(name, body.phrases, body.without_name))
    except (ScenarioError, OSError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/scenarios/{name}/run")
async def scenario_run(name: str, user: str = Depends(auth)):
    """Запуск кнопкой. Возвращает то, что сценарий сказал; сбой шага — в тексте, а не в коде ответа."""
    scene = _scenario(name)
    text = "".join([p async for p in scenarios.respond(user, scene, "app")]).strip()
    return {"ok": True, "text": text}


@app.post("/scenarios/{name}/enabled")
async def scenario_enabled(name: str, body: EnabledIn, user: str = Depends(auth)):
    _scenario(name)
    try:
        return scenarios.public(scenarios.set_enabled(name, body.enabled))
    except (ScenarioError, OSError) as e:
        raise HTTPException(409, str(e)) from e


async def _panel(call):
    try:
        return await call
    except PanelError as e:
        raise HTTPException(502 if "недоступен" in str(e) or "ответил" in str(e) else 400, str(e)) from e


@app.get("/home")
async def home_overview(user: str = Depends(auth)):
    """Вкладка «Дом»: устройства по комнатам, службы производителей, найденное в сети."""
    return await _panel(panel.overview())


@app.post("/home/entity/{entity_id}")
async def home_act(entity_id: str, body: HomeActIn, user: str = Depends(auth)):
    store.log(user, "app", "tool", "home_panel", {"entity_id": entity_id, "action": body.action, "value": body.value})
    return await _panel(panel.act(entity_id, body.action, body.value))


@app.post("/home/entity/{entity_id}/expose")
async def home_expose(entity_id: str, body: ExposeIn, user: str = Depends(auth)):
    store.log(user, "app", "tool", "home_expose", {"entity_id": entity_id, "exposed": body.exposed})
    return await _panel(panel.expose(entity_id, body.exposed))


@app.post("/home/entity/{entity_id}/settings")
async def home_settings(entity_id: str, body: DeviceIn, user: str = Depends(auth)):
    """Название, комната и имена для голоса: правится запись устройства в Home Assistant."""
    store.log(user, "app", "tool", "home_settings", {"entity_id": entity_id, **body.model_dump(exclude_none=True)})
    return await _panel(panel.update(entity_id, body.name, body.area_id, body.new_area, body.aliases))


@app.post("/home/integrations/{entry_id}/reload")
async def home_reload(entry_id: str, user: str = Depends(auth)):
    return await _panel(panel.reload(entry_id))


@app.get("/home/flows/{flow_id}")
async def home_flow(flow_id: str, user: str = Depends(auth)):
    return await _panel(panel.flow(flow_id))


@app.post("/home/flows/{flow_id}")
async def home_flow_answer(flow_id: str, body: FlowIn, user: str = Depends(auth)):
    store.log(user, "app", "tool", "home_add_device", {"flow_id": flow_id, "fields": sorted(body.data)})
    return await _panel(panel.flow(flow_id, body.data))


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
