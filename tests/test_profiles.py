"""Профили: владелец и «простой» (гости, дети, планшет на стене) — что кому можно."""
import importlib

import pytest
from fastapi.testclient import TestClient

OWNER = {"Authorization": "Bearer t0k"}
SIMPLE = {"Authorization": "Bearer guest"}
ITEMS = [
    {"entity_id": "light.lamp", "control": True, "exposed": True, "secondary": False, "area": "Зал", "name": "Лампа"},
    {"entity_id": "switch.boiler", "control": True, "exposed": False, "secondary": False, "area": "", "name": "Котёл"},
    {"entity_id": "switch.lamp_mode", "control": True, "exposed": True, "secondary": True, "area": "Зал", "name": "Режим"},
    {"entity_id": "sensor.t", "control": False, "exposed": True, "secondary": False, "area": "Зал", "name": "Температура"},
]


class FakePanel:
    enabled = True

    def __init__(self):
        self.acted, self.said = [], []
        self._index = {i["entity_id"]: ({}, {}, i["area"], i["exposed"]) for i in ITEMS}

    async def overview(self):
        return {"url": "http://ha", "areas": ["Зал"], "rooms": [{"id": "hall", "name": "Зал"}], "items": list(ITEMS),
                "integrations": [{"entry_id": "E", "title": "me@example.com"}], "discovered": [{"flow_id": "F"}]}

    def allowed(self, entity_id):
        known = self._index.get(entity_id)
        return bool(known and known[3])

    async def act(self, entity_id, action, value=None):
        self.acted.append((entity_id, action))
        return {"ok": True}

    async def say(self, entity_id, text):
        self.said.append((entity_id, text))
        return {"ok": True}


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "t0k")
    monkeypatch.setenv("JARVIS_SIMPLE_TOKEN", "guest")
    monkeypatch.setenv("JARVIS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "j.db"))
    for name in ("TELEGRAM_BOT_TOKEN", "NTFY_URL", "HASS_URL", "HASS_TOKEN", "JARVIS_PC_CONTROL"):
        monkeypatch.setenv(name, "")
    import jarvis.api as api

    api = importlib.reload(api)
    api.panel = FakePanel()
    with TestClient(api.app) as client:
        yield api, client


def test_each_token_learns_its_role(core):
    _, client = core
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/me", headers=OWNER).json()["role"] == "owner"
    assert client.get("/me", headers=SIMPLE).json()["role"] == "simple"


def test_simple_profile_gets_a_home_without_settings_and_accounts(core):
    _, client = core
    full = client.get("/home", headers=OWNER).json()
    assert len(full["items"]) == 4 and full["integrations"] and full["url"]
    simple = client.get("/home", headers=SIMPLE).json()
    assert [i["entity_id"] for i in simple["items"]] == ["light.lamp"]   # открытое ассистенту, без настроек и датчиков
    assert simple["integrations"] == [] and simple["discovered"] == [] and simple["url"] == ""
    assert "example.com" not in str(simple)


def test_simple_profile_switches_only_what_it_sees(core):
    api, client = core
    assert client.post("/home/entity/light.lamp", json={"action": "toggle"}, headers=SIMPLE).status_code == 200
    denied = client.post("/home/entity/switch.boiler", json={"action": "on"}, headers=SIMPLE)
    assert denied.status_code == 403 and api.panel.acted == [("light.lamp", "toggle")]
    assert client.post("/home/entity/switch.boiler", json={"action": "on"}, headers=OWNER).status_code == 200
    assert [e["user_id"] for e in api.store.recent(5) if e["name"] == "home_panel"] == ["owner", "simple"]  # кто нажал — в журнале


@pytest.mark.parametrize("method, path, body", [
    ("post", "/chat", {"text": "привет"}),
    ("get", "/messages", None),
    ("get", "/journal", None),
    ("get", "/status", None),
    ("get", "/scenarios", None),
    ("post", "/scenarios", {"name": "Х", "phrases": ["а б"], "steps": [{"say": "в"}]}),
    ("post", "/home/entity/light.lamp/expose", {"exposed": False}),
    ("post", "/home/entity/light.lamp/settings", {"name": "Моя"}),
    ("post", "/home/entity/light.lamp/say", {"text": "бу"}),
    ("post", "/home/integrations/E/reload", None),
    ("get", "/home/flows/F", None),
    ("post", "/session/new", None),
    ("get", "/confirmations", None),
])
def test_everything_else_is_closed_to_the_simple_profile(core, method, path, body):
    api, client = core
    r = getattr(client, method)(path, headers=SIMPLE, **({"json": body} if body is not None else {}))
    assert r.status_code == 403, (path, r.status_code)
    assert not api.panel.said


def test_simple_profile_hears_only_its_devices_and_no_conversations(core):
    api, client = core
    with client.websocket_connect("/events/ws?token=guest") as ws:
        assert ws.receive_json() == {"type": "hello", "pending": []}
        api.store.log("owner", "telegram", "user", payload="личное сообщение")
        api.bus.publish({"type": "home", "item": {"entity_id": "switch.boiler", "exposed": False, "control": True}})
        api.bus.publish({"type": "health", "text": "сервер речи упал"})
        api.bus.publish({"type": "home", "item": {"entity_id": "light.lamp", "exposed": True, "control": True, "on": False}})
        assert ws.receive_json()["item"]["entity_id"] == "light.lamp"   # всё остальное до него не дошло
    with pytest.raises(Exception):
        with client.websocket_connect("/voice/ws?token=guest"):
            pass                                                        # голос — только владельцу


def test_help_button_reaches_the_owner_once_per_half_minute(core):
    api, client = core
    assert client.post("/help").status_code == 401
    with client.websocket_connect("/events/ws?token=t0k") as owner:
        owner.receive_json()
        first = client.post("/help", headers=SIMPLE).json()
        assert first == {"ok": True, "repeated": False}
        seen = owner.receive_json()
        while seen.get("type") != "health":
            seen = owner.receive_json()
        assert "зовут" in seen["text"]
    assert client.post("/help", headers=SIMPLE).json() == {"ok": True, "repeated": True}   # не заспамить владельца


def test_owner_can_speak_through_the_station(core):
    api, client = core
    assert client.post("/home/entity/media_player.station/say", json={"text": "Ужин готов"}, headers=OWNER).status_code == 200
    assert api.panel.said == [("media_player.station", "Ужин готов")]


def test_without_a_simple_token_there_is_no_simple_profile(core, monkeypatch):
    monkeypatch.setenv("JARVIS_SIMPLE_TOKEN", "")
    import jarvis.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        assert client.get("/me", headers=SIMPLE).status_code == 401
        assert client.get("/me", headers={"Authorization": "Bearer "}).status_code == 401   # пустой токен — не пропуск
