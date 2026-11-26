"""Ускорение голоса: прогрев кэша по сигналу клиента и замер стадий ответа."""
import asyncio
import json

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from jarvis.store import Store
from jarvis.voice import VoiceService, VoiceSession


class Speech:
    enabled, tts_rate = True, 16000

    async def recognize(self, audio, fmt="oggopus", sample_rate=None, **kw):
        await asyncio.sleep(0.02)
        return "сколько времени"

    async def synthesize(self, text):
        await asyncio.sleep(0.01)
        return text.encode()


class Router:
    def __init__(self, store):
        self.store, self.warmed, self.last_usage = store, 0, {}

    async def prewarm(self, user_id="owner"):
        self.warmed += 1
        return True

    async def respond(self, user, text, channel="voice"):
        await asyncio.sleep(0.03)
        self.last_usage = {"cache_read_input_tokens": 9000, "cache_creation_input_tokens": 0}
        yield "Сейчас пять часов. "


def run_app(router):
    app = FastAPI()
    svc = VoiceService(router, Speech())

    @app.websocket("/ws")
    async def ep(ws: WebSocket):
        await ws.accept()
        await VoiceSession(ws, svc).run()

    return app


def test_warm_signal_and_stage_timings(tmp_path):
    store = Store(tmp_path / "j.db")
    router = Router(store)
    with TestClient(run_app(router)).websocket_connect("/ws") as ws:
        ws.receive_json()                                   # hello: при подключении прогрев тоже идёт
        ws.send_json({"type": "warm"})
        ws.send_bytes(b"\0\0" * 1600)
        while True:
            msg = ws.receive()
            if msg.get("text") and json.loads(msg["text"])["type"] == "done":
                break
    assert router.warmed == 2
    timing = [e for e in store.recent(20) if e["kind"] == "timing"]
    data = json.loads(timing[0]["payload"])
    assert data["stt_ms"] >= 20 and data["model_ms"] >= 30 and data["tts_ms"] >= 10
    assert data["total_ms"] >= data["stt_ms"] + data["model_ms"] and data["cache"] == "тёплый"


def test_router_without_prewarm_is_fine(tmp_path):
    class Plain(Router):
        prewarm = None
    with TestClient(run_app(Plain(Store(tmp_path / "j.db")))).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "warm"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_timings_summary(tmp_path):
    store = Store(tmp_path / "j.db")
    for stt, model, cache in ((200, 900, "тёплый"), (210, 1500, "записан"), (190, 700, "тёплый"), (0, 0, "сценарий")):
        store.log("owner", "voice", "timing", "", {"stt_ms": stt, "model_ms": model, "tts_ms": 130,
                                                   "total_ms": stt + model + 130, "cache": cache})
    store.log("owner", "voice", "timing", "", "битая запись")
    out = store.timings()
    assert out["count"] == 4 and out["median_ms"]["model_ms"] == 900 and out["median_ms"]["tts_ms"] == 130
    assert out["warm_share"] == 0.67                          # сценарии без модели в долю не входят
    assert out["last"][0]["cache"] == "сценарий"
