import asyncio
import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import registry
from jarvis.agent import needs_confirm
from jarvis.confirm import Confirmations
from jarvis.events import EventBus
from jarvis.notify import Notifier
from jarvis.store import Store


class Recorder(Notifier):
    def __init__(self):
        super().__init__()
        self.confirms = []

    async def confirm(self, cid, title, details):
        self.confirms.append((cid, title))


async def test_confirmation_approve_and_timeout(tmp_path):
    bus, store, note = EventBus(), Store(tmp_path / "j.db"), Recorder()
    q = bus.subscribe()
    c = Confirmations(bus, note, store)

    task = asyncio.create_task(c.ask("Перезаписать?", "детали", timeout=5, channel="voice"))
    await asyncio.sleep(0.01)
    asked = await q.get()
    assert asked["type"] == "confirm" and asked["title"] == "Перезаписать?"
    assert note.confirms and c.pending()[0]["id"] == asked["id"]
    assert c.resolve(asked["id"], True, by="app") is True
    assert await task is True
    assert c.resolve(asked["id"], False, by="app") is False  # повторно — уже закрыто
    assert c.pending() == []

    assert await c.ask("Долго?", timeout=0.05) is False      # молчание = «нет»
    kinds = [e["kind"] for e in store.recent(10)]
    assert kinds.count("confirm_resolved") == 2


def test_needs_confirm(tmp_path):
    vault = tmp_path.resolve()
    (vault / "10_Projects").mkdir()
    (vault / "10_Projects" / "a.md").write_text("x")
    assert needs_confirm(vault, "Write", {"file_path": "10_Projects/a.md"}) == "10_Projects/a.md"
    assert needs_confirm(vault, "Write", {"file_path": "10_Projects/new.md"}) is None   # новый файл
    assert needs_confirm(vault, "Edit", {"file_path": "10_Projects/a.md"}) is None      # точечная правка
    assert needs_confirm(vault, "Write", {"file_path": "00_Inbox/a.md"}) is None


async def test_ntfy_payload_has_no_secrets():
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), req.headers.get("authorization"), json.loads(req.content)))
        return httpx.Response(200)

    n = Notifier("https://ntfy.example/", "jarvis-x", "tk", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await n.confirm("ID123", "Деплой в прод?", "3 коммита")
    url, auth, body = seen[0]
    assert url == "https://ntfy.example" and auth == "Bearer tk"
    assert body["topic"] == "jarvis-x" and body["priority"] == 5
    assert "ID123" not in json.dumps(body)  # в push нет ничего, чем можно подтвердить
    assert n.where() == "push"


def test_bus_publish_from_other_thread():
    bus = EventBus()

    async def main():
        q = bus.subscribe()
        await asyncio.to_thread(bus.publish, {"x": 1})
        return await asyncio.wait_for(q.get(), 1)

    assert asyncio.run(main()) == {"x": 1}


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "t0k")
    monkeypatch.setenv("JARVIS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "j.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("NTFY_URL", "")
    import jarvis.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        yield api, client


H = {"Authorization": "Bearer t0k"}


def test_api_messages_and_live_events(core):
    api, client = core
    assert client.get("/messages").status_code == 401
    with client.websocket_connect("/events/ws?token=t0k") as ws:
        assert ws.receive_json() == {"type": "hello", "pending": []}
        api.store.log("owner", "telegram", "user", payload="привет из телеграма")
        api.store.log("owner", "voice-agent", "user", payload="внутреннее")
        api.store.log("owner", "telegram", "assistant", payload={"text": "привет!", "cost_usd": 0.01})
        ev = [ws.receive_json() for _ in range(3)]
    assert [e["kind"] for e in ev] == ["user", "user", "assistant"]
    assert ev[0]["text"] == "привет из телеграма" and ev[2]["text"] == "привет!"
    feed = client.get("/journal", headers=H).json()
    assert [e["kind"] for e in feed] == ["assistant", "user", "user"] and feed[0]["text"] == "привет!"
    msgs = client.get("/messages", headers=H).json()
    assert [(m["role"], m["text"], m["channel"]) for m in msgs] == [
        ("user", "привет из телеграма", "telegram"), ("assistant", "привет!", "telegram")]


def test_api_confirmation_roundtrip(core):
    api, client = core
    with client.websocket_connect("/events/ws?token=t0k") as ws:
        ws.receive_json()
        fut = client.portal.start_task_soon(lambda: api.confirms.ask("Перезаписать заметку?", "текст", 5, "app"))
        ev = ws.receive_json()
        while ev["type"] != "confirm":
            ev = ws.receive_json()
        assert client.get("/confirmations", headers=H).json()[0]["id"] == ev["id"]
        r = client.post(f"/confirmations/{ev['id']}", headers=H, json={"approve": True})
        assert r.status_code == 200
        assert fut.result(timeout=2) is True
        assert client.post(f"/confirmations/{ev['id']}", headers=H, json={"approve": True}).status_code == 404


def test_events_ws_rejects_bad_token(core):
    _, client = core
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect("/events/ws?token=nope") as ws:
            ws.receive_json()
    assert e.value.code == 4401


def test_cors_allows_tauri_origin(core):
    _, client = core
    r = client.options("/messages", headers={"Origin": "http://tauri.localhost", "Access-Control-Request-Method": "GET",
                                             "Access-Control-Request-Headers": "authorization"})
    assert r.headers.get("access-control-allow-origin") == "http://tauri.localhost"
    r = client.options("/messages", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers
