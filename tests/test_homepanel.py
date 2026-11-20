"""Вкладка «Дом»: список устройств по комнатам, команды, что открыто ассистенту, добавление найденного."""
import asyncio
import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.homepanel import Panel, PanelError, describe, field, item, service_call

AREAS = [{"area_id": "hall", "name": "Зал"}, {"area_id": "kitchen", "name": "Кухня"}]
DEVICES = [{"id": "d1", "name": "Лампа", "name_by_user": None, "area_id": "hall"},
           {"id": "d2", "name": "Аэрогриль", "name_by_user": "Гриль", "area_id": "kitchen"}]
ENTITIES = [
    {"entity_id": "light.lamp", "device_id": "d1", "area_id": None, "config_entry_id": "E1"},
    {"entity_id": "switch.grill", "device_id": "d2", "area_id": None, "config_entry_id": "E2"},
    {"entity_id": "switch.grill_keep_warm", "device_id": "d2", "area_id": None, "config_entry_id": "E2", "entity_category": "config"},
    {"entity_id": "switch.hidden", "device_id": "d2", "hidden_by": "user", "config_entry_id": "E2"},
    {"entity_id": "sensor.grill_rssi", "device_id": "d2", "config_entry_id": "E2"},
    {"entity_id": "sensor.temp", "device_id": None, "area_id": "hall", "config_entry_id": "E3"},
]
STATES = [
    {"entity_id": "light.lamp", "state": "on", "attributes": {"friendly_name": "Лампочка", "brightness": 128, "supported_color_modes": ["hs"]}},
    {"entity_id": "switch.grill", "state": "off", "attributes": {"friendly_name": "Гриль Питание"}},
    {"entity_id": "switch.grill_keep_warm", "state": "on", "attributes": {"friendly_name": "Гриль Подогрев"}},
    {"entity_id": "switch.hidden", "state": "on", "attributes": {}},
    {"entity_id": "sensor.grill_rssi", "state": "-60", "attributes": {"device_class": "signal_strength"}},
    {"entity_id": "sensor.temp", "state": "21.5", "attributes": {"friendly_name": "Температура", "device_class": "temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "lock.door", "state": "locked", "attributes": {"friendly_name": "Замок"}},
    {"entity_id": "sun.sun", "state": "below_horizon", "attributes": {}},
]
ENTRIES = [{"entry_id": "E1", "domain": "tuya", "title": "Tuya", "state": "loaded"},
           {"entry_id": "E2", "domain": "xiaomi_home", "title": "Xiaomi", "state": "loaded"},
           {"entry_id": "E9", "domain": "sun", "title": "Sun", "state": "loaded"}]
FLOWS = [{"flow_id": "F1", "handler": "upnp", "context": {"source": "ssdp", "title_placeholders": {"name": "Роутер"}}},
         {"flow_id": "F2", "handler": "upnp", "context": {"source": "ssdp", "title_placeholders": {"name": "Роутер"}}},
         {"flow_id": "F3", "handler": "tuya", "context": {"source": "reauth"}}]
WS = {"config/area_registry/list": AREAS, "config/device_registry/list": DEVICES, "config/entity_registry/list": ENTITIES,
      "homeassistant/expose_entity/list": {"exposed_entities": {"light.lamp": {"conversation": True}}},
      "config_entries/flow/progress": FLOWS, "homeassistant/expose_entity": None,
      "config/entity_registry/update": {"entity_entry": {}}, "config/area_registry/create": {"area_id": "new_room", "name": "Кабинет"},
      "frontend/get_translations": {"resources": {
          "component.androidtv_remote.config.step.pair.title": "Сопряжение",
          "component.androidtv_remote.config.step.pair.description": "Введите код с экрана {name}.",
          "component.androidtv_remote.config.step.pair.data.pin": "Код",
          "component.androidtv_remote.config.error.invalid_auth": "Неверный код",
          "component.androidtv_remote.config.abort.already_configured": "Уже добавлено"}}}


class Socket:
    """Home Assistant по WebSocket: вход по токену, ответ на каждую команду по её типу."""
    def __init__(self, sent, token_ok=True):
        self.sent, self.token_ok, self.inbox = sent, token_ok, [json.dumps({"type": "auth_required"})]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        return self.inbox.pop(0)

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "auth":
            self.inbox.append(json.dumps({"type": "auth_ok" if self.token_ok and msg["access_token"] == "tok" else "auth_invalid"}))
        else:
            self.inbox.append(json.dumps({"id": msg["id"], "type": "event"}))  # чужие сообщения пропускаются
            self.inbox.append(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": WS[msg["type"]]}))


def make(rest=None, token="tok"):
    sent, calls = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == f"Bearer {token}"
        if rest:
            got = rest(request.method, request.url.path, body)
            if got is not None:
                return got
        if request.url.path == "/api/states":
            return httpx.Response(200, json=STATES)
        if request.url.path == "/api/config/config_entries/entry":
            return httpx.Response(200, json=ENTRIES)
        return httpx.Response(200, json=[])

    panel = Panel("http://ha.test:8123/", token, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                  connect=lambda address, **kw: sent.append({"address": address}) or Socket(sent))
    return panel, sent, calls


def test_overview_groups_by_room_and_hides_noise():
    panel, sent, _ = make()
    got = asyncio.run(panel.overview())
    assert sent[0] == {"address": "ws://ha.test:8123/api/websocket"}
    assert got["areas"] == ["Зал", "Кухня"]
    rows = {i["entity_id"]: i for i in got["items"]}
    assert set(rows) == {"light.lamp", "switch.grill", "switch.grill_keep_warm", "sensor.temp"}  # замок, скрытое, сигнал — нет
    lamp = rows["light.lamp"]
    assert (lamp["area"], lamp["device"], lamp["on"], lamp["brightness"], lamp["dimmable"], lamp["exposed"]) == ("Зал", "Лампа", True, 50, True, True)
    assert rows["switch.grill"]["device"] == "Гриль" and rows["switch.grill"]["area"] == "Кухня" and not rows["switch.grill"]["exposed"]
    assert rows["switch.grill_keep_warm"]["secondary"] and not rows["switch.grill"]["secondary"]
    assert rows["sensor.temp"]["control"] is False and rows["sensor.temp"]["unit"] == "°C" and rows["sensor.temp"]["area"] == "Зал"
    assert [i["entity_id"] for i in got["items"]][:2] == ["light.lamp", "sensor.temp"]   # в комнате: сначала управляемое


def test_overview_lists_vendor_services_and_found_devices_once():
    got = asyncio.run(make()[0].overview())
    assert [(i["domain"], i["devices"]) for i in got["integrations"]] == [("tuya", 1), ("xiaomi_home", 2)]  # солнце — не служба устройств
    assert got["discovered"] == [{"flow_id": "F1", "handler": "upnp", "title": "Роутер"}]  # повтор и переавторизация убраны


def test_commands_map_to_home_assistant_services():
    assert service_call("light.lamp", "on") == ("light", "turn_on", {"entity_id": "light.lamp"})
    assert service_call("light.lamp", "brightness", 40) == ("light", "turn_on", {"entity_id": "light.lamp", "brightness_pct": 40})
    assert service_call("media_player.tv", "volume", 35) == ("media_player", "volume_set", {"entity_id": "media_player.tv", "volume_level": 0.35})
    assert service_call("cover.blind", "off")[1] == "close_cover" and service_call("button.fire", "on")[1] == "press"
    assert service_call("climate.ac", "temperature", 22.5)[2]["temperature"] == 22.5


@pytest.mark.parametrize("entity, action, value", [
    ("lock.door", "on", None),              # замки — только с подтверждением, не кнопкой
    ("light.lamp", "explode", None),
    ("light.lamp", "brightness", 400),
    ("light.lamp", "brightness", "ярко"),
    ("light.lamp", "volume", 10),           # громкость не у лампы
    ("scene.movie", "off", None),
    ("light.x/../../config", "on", None),
    ("light.", "on", None),
])
def test_commands_outside_the_whitelist_are_refused(entity, action, value):
    with pytest.raises(PanelError):
        service_call(entity, action, value)


def test_act_calls_the_service_with_the_token():
    panel, _, calls = make()
    assert asyncio.run(panel.act("switch.grill", "toggle")) == {"ok": True, "service": "switch.toggle"}
    assert calls == [("POST", "/api/services/switch/toggle", {"entity_id": "switch.grill"})]


def test_home_assistant_errors_become_readable():
    panel, _, _ = make(rest=lambda m, p, b: httpx.Response(500, text="boom"))
    with pytest.raises(PanelError, match="ответил 500"):
        asyncio.run(panel.act("switch.grill", "on"))
    off = Panel("", "")
    with pytest.raises(PanelError, match="не подключён"):
        asyncio.run(off.overview())
    sent = []
    wrong = Panel("http://ha.test", "bad", connect=lambda a, **kw: Socket(sent))
    with pytest.raises(PanelError, match="не принял токен"):
        asyncio.run(wrong.overview())


def test_exposing_to_the_assistant_resets_what_voice_knows():
    panel, sent, _ = make()
    reset = []
    panel.changed = lambda: reset.append(1)
    assert asyncio.run(panel.expose("switch.grill", True)) == {"ok": True, "exposed": True}
    assert sent[-1] == {"id": 1, "type": "homeassistant/expose_entity", "assistants": ["conversation"],
                        "entity_ids": ["switch.grill"], "should_expose": True}
    assert reset == [1]


def test_reload_asks_the_vendor_service_again():
    panel, _, calls = make()
    asyncio.run(panel.reload("E1"))
    assert calls == [("POST", "/api/config/config_entries/entry/E1/reload", None)]
    with pytest.raises(PanelError):
        asyncio.run(panel.reload("../x"))


def test_buttons_are_pressable_though_their_state_is_unknown():
    made = item({"entity_id": "button.fire", "state": "unknown", "attributes": {"friendly_name": "Камин"}}, {}, {}, "", False)
    assert made["available"] and made["on"] is None and made["state"] == ""
    gone = item({"entity_id": "switch.x", "state": "unavailable", "attributes": {}}, {}, {}, "", False)
    assert not gone["available"] and gone["state"] == "недоступно" and gone["on"] is None


def test_wizard_step_gets_labels_from_home_assistant_translations():
    step = {"flow_id": "F8", "handler": "androidtv_remote", "type": "form", "step_id": "pair",
            "description_placeholders": {"name": "TV"}, "errors": {"base": "invalid_auth"},
            "data_schema": [{"name": "pin", "type": "string", "required": True}]}
    answers = []

    def rest(method, path, body):
        if path == "/api/config/config_entries/flow/F8":
            answers.append((method, body))
            return httpx.Response(200, json=step if method == "GET" else
                                  {"flow_id": "F8", "handler": "androidtv_remote", "type": "create_entry", "title": "TV"})

    panel, _, _ = make(rest=rest)
    reset = []
    panel.changed = lambda: reset.append(1)
    got = asyncio.run(panel.flow("F8"))
    assert (got["title"], got["text"], got["errors"]) == ("Сопряжение", "Введите код с экрана TV.", ["Неверный код"])
    assert got["fields"] == [{"name": "pin", "label": "Код", "kind": "password", "required": True, "default": None, "options": []}]
    assert not reset
    done = asyncio.run(panel.flow("F8", {"pin": "123456"}))
    assert answers == [("GET", None), ("POST", {"pin": "123456"})]
    assert done["type"] == "create_entry" and done["title"] == "TV" and reset == [1]   # новое устройство — голос перечитает дом


def test_wizard_fields_menus_and_aborts():
    assert field({"name": "mode", "selector": {"select": {"options": ["a", {"value": "b", "label": "Б"}]}}}, "")["options"] == [
        {"value": "a", "label": "a"}, {"value": "b", "label": "Б"}]
    assert field({"name": "port", "type": "integer", "default": 80}, "Порт") == {
        "name": "port", "label": "Порт", "kind": "number", "required": False, "default": 80, "options": []}
    assert field({"name": "x", "type": "expandable"}, "")["kind"] == "text" and field({"name": "on", "type": "boolean"}, "")["kind"] == "boolean"
    menu = describe({"handler": "h", "type": "menu", "step_id": "user", "menu_options": ["cloud", "local"]}, {
        "component.h.config.step.user.menu_options.cloud": "Через облако"})
    assert menu["choices"] == [{"value": "cloud", "label": "Через облако"}, {"value": "local", "label": "local"}]
    stop = describe({"handler": "androidtv_remote", "type": "abort", "reason": "already_configured"}, WS["frontend/get_translations"]["resources"])
    assert stop["text"] == "Уже добавлено"
    assert describe({"handler": "upnp", "type": "form", "step_id": "s"}, {"component.upnp.config.flow_title": "{name}"})["title"] == "upnp"


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "t0k")
    monkeypatch.setenv("JARVIS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "j.db"))
    for name in ("TELEGRAM_BOT_TOKEN", "NTFY_URL", "HASS_URL", "HASS_TOKEN"):
        monkeypatch.setenv(name, "")
    import jarvis.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        yield api, client


def test_home_endpoints_need_the_token_and_explain_a_missing_home(core):
    api, client = core
    h = {"Authorization": "Bearer t0k"}
    assert client.get("/home").status_code == 401
    assert client.post("/home/entity/light.lamp", json={"action": "on"}).status_code == 401
    r = client.get("/home", headers=h)
    assert r.status_code == 400 and "не подключён" in r.json()["detail"]
    assert client.post("/home/entity/lock.door", json={"action": "on"}, headers=h).status_code == 400
    assert any(e["name"] == "home_panel" for e in api.store.recent(5))   # команда с вкладки видна в журнале


# --- живые состояния -------------------------------------------------------------------------------

class LiveSocket(Socket):
    """После подписки отдаёт заранее записанные события Home Assistant, потом связь рвётся."""
    def __init__(self, sent, events):
        super().__init__(sent)
        self.events = list(events)

    async def recv(self):
        await asyncio.sleep(0)          # даём шанс отложенному перечитыванию реестров
        if self.inbox:
            return self.inbox.pop(0)
        if self.events:
            return json.dumps({"type": "event", "event": self.events.pop(0)})
        for _ in range(50):             # тишина после событий: отложенное перечитывание успевает отработать
            await asyncio.sleep(0)
        raise OSError("связь оборвалась")

    async def send(self, raw):
        msg = json.loads(raw)
        if msg["type"] == "subscribe_events":
            self.sent.append(msg)
            self.inbox.append(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": None}))
        else:
            await super().send(raw)


def changed(entity_id, state, **attrs):
    new = None if state is None else {"entity_id": entity_id, "state": state, "attributes": attrs}
    return {"event_type": "state_changed", "data": {"entity_id": entity_id, "new_state": new}}


def run_live(events):
    panel, sent, _ = make()
    sockets = iter([Socket(sent), LiveSocket(sent, events)])   # первый — снимок реестров, второй — подписка
    panel._connect = lambda address, **kw: next(sockets, None) or Socket(sent)
    out, pauses = [], []

    async def sleep(seconds):
        pauses.append(seconds)
        if seconds == 1.0:              # пауза перед переподключением — на этом тест заканчиваем
            raise asyncio.CancelledError
        for _ in range(10):             # пауза «пока стихнет» длится несколько оборотов цикла
            await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(panel.live(out.append, sleep=sleep))
    return panel, sent, out, pauses


def test_live_turns_state_changes_into_tab_rows():
    _, sent, out, _ = run_live([
        changed("light.lamp", "off", friendly_name="Лампочка", supported_color_modes=["hs"]),
        changed("sensor.grill_rssi", "-70", device_class="signal_strength"),   # на вкладке его нет — молчим
        changed("sensor.unknown_new", "1"),
        changed("switch.grill", "on", friendly_name="Гриль Питание"),
    ])
    subscribed = [m["event_type"] for m in sent if m.get("type") == "subscribe_events"]
    assert subscribed == ["state_changed", "entity_registry_updated", "device_registry_updated", "area_registry_updated"]
    assert out[0] == {"type": "home", "reload": True}          # связь установлена — клиент перечитывает снимок
    rows = [e["item"] for e in out[1:]]
    assert [(r["entity_id"], r["on"]) for r in rows] == [("light.lamp", False), ("switch.grill", True)]
    lamp = rows[0]
    assert (lamp["area"], lamp["device"], lamp["exposed"], lamp["name"]) == ("Зал", "Лампа", True, "Лампочка")  # из реестров


def test_live_asks_to_reload_when_the_home_itself_changes():
    panel, _, out, pauses = run_live([
        {"event_type": "entity_registry_updated", "data": {"action": "create", "entity_id": "light.new"}},
        {"event_type": "device_registry_updated", "data": {}},   # перезагрузка службы шлёт их пачкой
        {"event_type": "entity_registry_updated", "data": {}},
        changed("switch.grill", None),                           # сущность удалена
    ])
    assert out.count({"type": "home", "reload": True}) == 3      # связь, пачка реестров разом, удаление
    assert pauses[0] == 1.5 and pauses[-1] == 1.0                # подождали, пока стихнет; потом переподключение


def test_live_keeps_the_voice_mark_after_exposing():
    panel, _, _ = make()
    asyncio.run(panel.overview())
    assert panel._index["switch.grill"][3] is False
    asyncio.run(panel.expose("switch.grill", True))
    assert panel._index["switch.grill"][3] is True               # следующее событие придёт уже с отметкой


# --- название, комната и имена для голоса -----------------------------------------------------------

def test_overview_carries_what_the_editor_needs():
    got = asyncio.run(make()[0].overview())
    assert got["rooms"] == [{"id": "hall", "name": "Зал"}, {"id": "kitchen", "name": "Кухня"}]
    lamp = next(i for i in got["items"] if i["entity_id"] == "light.lamp")
    assert (lamp["area_id"], lamp["aliases"], lamp["renamed"]) == ("hall", [], False)


def test_rename_move_and_voice_names_go_to_the_registry():
    panel, sent, _ = make()
    reset = []
    panel.changed = lambda: reset.append(1)
    got = asyncio.run(panel.update("light.lamp", name="  Торшер  у дивана ", area_id="kitchen", aliases=["лампа", " Лампа ", "ночник", ""]))
    assert got == {"ok": True, "changed": ["aliases", "area_id", "name"]}
    update = next(m for m in sent if m.get("type") == "config/entity_registry/update")
    assert update == {"id": 1, "type": "config/entity_registry/update", "entity_id": "light.lamp",
                      "name": "Торшер у дивана", "area_id": "kitchen", "aliases": ["Лампа", "лампа", "ночник"]}
    assert reset == [1]                                          # голос сразу узнаёт новые имена


def test_empty_name_restores_the_vendor_one_and_a_new_room_is_created_first():
    panel, sent, _ = make()
    asyncio.run(panel.update("light.lamp", name="", new_area="Кабинет"))
    kinds = [m["type"] for m in sent if m.get("type", "").startswith("config/") and "list" not in m["type"]]
    assert kinds == ["config/area_registry/create", "config/entity_registry/update"]
    update = next(m for m in sent if m.get("type") == "config/entity_registry/update")
    assert update["name"] is None and update["area_id"] == "new_room"
    asyncio.run(panel.update("light.lamp", area_id=""))          # пустая комната — «без комнаты»
    assert [m for m in sent if m.get("type") == "config/entity_registry/update"][-1]["area_id"] is None


@pytest.mark.parametrize("kw", [
    {},                                         # нечего менять
    {"name": "я" * 81},
    {"area_id": "hall; drop"},
    {"aliases": ["а"] * 11},
    {"aliases": "лампа"},
    {"aliases": [5]},
])
def test_bad_edits_are_refused_before_home_assistant_sees_them(kw):
    panel, sent, _ = make()
    with pytest.raises(PanelError):
        asyncio.run(panel.update("light.lamp", **kw))
    assert not any(m.get("type") == "config/entity_registry/update" for m in sent)
    with pytest.raises(PanelError, match="нет такого устройства"):
        asyncio.run(panel.update("lock.door", name="Замок"))     # того, чего нет на вкладке, не трогаем


# --- сказать на колонке и «простой» профиль -----------------------------------------------------------

def test_say_speaks_only_on_speakers_that_can():
    panel, _, calls = make()
    panel._index = {"media_player.station": ({"platform": "yandex_station"}, {}, "Зал", True),
                    "media_player.tv": ({"platform": "cast"}, {}, "", True),
                    "light.lamp": ({"platform": "yandex_station"}, {}, "Зал", True)}
    assert asyncio.run(panel.say("media_player.station", "  Ужин   готов ")) == {"ok": True}
    assert calls == [("POST", "/api/services/media_player/play_media",
                      {"entity_id": "media_player.station", "media_content_type": "text", "media_content_id": "Ужин готов"})]
    for entity, text in (("media_player.tv", "привет"), ("light.lamp", "привет"), ("media_player.station", "   "),
                         ("media_player.station", "я" * 301)):
        with pytest.raises(PanelError):
            asyncio.run(panel.say(entity, text))
    assert len(calls) == 1
    made = item({"entity_id": "media_player.station", "state": "idle", "attributes": {}}, {"platform": "yandex_station"}, {}, "", True)
    assert made["speaks"] and not item({"entity_id": "media_player.tv", "state": "off", "attributes": {}}, {"platform": "cast"}, {}, "", True)["speaks"]


def test_simple_profile_sees_only_what_the_owner_opened_to_the_assistant():
    from jarvis.homepanel import simple_view

    panel, _, _ = make()
    full = asyncio.run(panel.overview())
    got = simple_view(full)
    assert [i["entity_id"] for i in got["items"]] == ["light.lamp"]          # открыта только лампа
    assert got["integrations"] == [] and got["discovered"] == [] and got["url"] == "" and got["rooms"] == []
    assert panel.allowed("light.lamp") and not panel.allowed("switch.grill") and not panel.allowed("lock.door")
