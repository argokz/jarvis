"""Умный дом для вкладки «Дом» в приложении: устройства по комнатам, команды, новые устройства.

Это пульт владельца, а не инструмент модели: приложение ходит сюда со своим токеном, а агент и быстрая
модель по-прежнему видят только то, что открыто голосовым ассистентам (см. home.py). Список комнат,
устройств и того, что открыто ассистенту, Home Assistant отдаёт только по WebSocket; команды — по REST.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit

import httpx
import websockets

log = logging.getLogger("jarvis.homepanel")
TIMEOUT = 10.0   # облачные устройства отвечают не сразу
ASSISTANT = "conversation"   # так Home Assistant называет встроенный Assist; его же видит MCP-сервер

# Чем можно управлять с вкладки. Замков и сигнализаций здесь нет: им нужно подтверждение, а не кнопка.
CONTROLS = frozenset({"light", "switch", "fan", "cover", "climate", "media_player", "vacuum", "humidifier",
                      "scene", "script", "button", "input_boolean"})
# Что стоит показывать из датчиков: остальное (сила сигнала, версии прошивки) — шум.
SENSORS = frozenset({"temperature", "humidity", "battery", "power", "energy", "door", "window", "motion",
                     "moisture", "smoke", "gas", "occupancy", "illuminance", "pm25", "carbon_dioxide"})
ON_STATES = frozenset({"on", "open", "opening", "playing", "paused", "idle", "cleaning", "heat", "cool",
                       "heat_cool", "auto", "dry", "fan_only", "buffering"})
# действие → (служба, из чего собрать данные вызова); домен берётся из сущности
ACTIONS = {
    "on": {"cover": "open_cover", "vacuum": "start", "button": "press", "*": "turn_on"},
    "off": {"cover": "close_cover", "vacuum": "return_to_base", "*": "turn_off"},
    "toggle": {"*": "toggle"},
    "brightness": {"light": "turn_on"},
    "volume": {"media_player": "volume_set"},
    "temperature": {"climate": "set_temperature"},
    "position": {"cover": "set_cover_position"},
    "play_pause": {"media_player": "media_play_pause"},
}
NO_TOGGLE = frozenset({"scene", "script", "button", "climate", "vacuum"})  # у них нет «переключить»


class PanelError(Exception):
    pass


def number(value, low: float, high: float, what: str) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise PanelError(f"{what}: нужно число") from None
    if not low <= n <= high:
        raise PanelError(f"{what}: от {low:g} до {high:g}")
    return n


def service_call(entity_id: str, action: str, value=None) -> tuple[str, str, dict]:
    """Что вызвать в Home Assistant: (домен, служба, данные). Чужие домены и действия не пропускаются."""
    domain, _, name = entity_id.partition(".")
    if domain not in CONTROLS or not name or not all(c.isalnum() or c == "_" for c in name):
        raise PanelError(f"этим с вкладки управлять нельзя: {entity_id}")
    by_domain = ACTIONS.get(action)
    if by_domain is None:
        raise PanelError(f"неизвестное действие: {action}")
    service = by_domain.get(domain) or by_domain.get("*")
    if service is None or (action == "toggle" and domain in NO_TOGGLE) or (action == "off" and domain in ("scene", "button")):
        raise PanelError(f"«{action}» не подходит для {domain}")
    data: dict = {"entity_id": entity_id}
    if action == "brightness":
        data["brightness_pct"] = round(number(value, 1, 100, "яркость"))
    elif action == "volume":
        data["volume_level"] = round(number(value, 0, 100, "громкость") / 100, 2)
    elif action == "temperature":
        data["temperature"] = number(value, 5, 35, "температура")
    elif action == "position":
        data["position"] = round(number(value, 0, 100, "положение"))
    return domain, service, data


def item(state: dict, entry: dict, device: dict, area: str, exposed: bool) -> dict | None:
    """Строка вкладки из состояния сущности и записей реестров. None — показывать нечего."""
    entity_id = state["entity_id"]
    domain = entity_id.split(".")[0]
    attrs = state.get("attributes") or {}
    kind = attrs.get("device_class") or ""
    control = domain in CONTROLS
    if not control and not (domain in ("sensor", "binary_sensor") and kind in SENSORS):
        return None
    if entry.get("hidden_by") or entry.get("disabled_by"):
        return None
    value = state.get("state")
    press = domain in ("button", "scene", "script")  # у них состояние — время последнего нажатия или «unknown»
    available = value not in (("unavailable", None) if press else ("unavailable", "unknown", None))
    out = {
        "entity_id": entity_id, "domain": domain, "kind": kind,
        "name": attrs.get("friendly_name") or entry.get("name") or entry.get("original_name") or entity_id,
        "device": device.get("name_by_user") or device.get("name") or "",
        "area": area, "control": control, "available": available, "exposed": exposed,
        # настройки и диагностика устройства (десяток тумблеров аэрогриля) — под отдельной галочкой
        "secondary": bool(entry.get("entity_category")),
        "state": ("" if press else value) if available else "недоступно",
        "on": (value in ON_STATES) if control and available and not press else None,
        "entry_id": entry.get("config_entry_id") or "",
        "unit": attrs.get("unit_of_measurement") or "",
    }
    if domain == "light" and attrs.get("brightness") is not None:
        out["brightness"] = max(1, round(attrs["brightness"] / 255 * 100))
    if domain == "light":
        out["dimmable"] = any(m != "onoff" for m in attrs.get("supported_color_modes") or [])
    if domain == "media_player" and attrs.get("volume_level") is not None:
        out["volume"] = round(attrs["volume_level"] * 100)
    if domain == "climate":
        out["temperature"], out["current"] = attrs.get("temperature"), attrs.get("current_temperature")
    if domain == "cover" and attrs.get("current_position") is not None:
        out["position"] = attrs["current_position"]
    return out


class Panel:
    def __init__(self, url: str, token: str, client: httpx.AsyncClient | None = None, connect=websockets.connect):
        self.url, self.token = url.rstrip("/"), token
        self.enabled = bool(url and token)
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT)
        self._connect = connect
        self.changed = lambda: None  # ядро подставит сброс кэша у home.Home: голос должен сразу увидеть новое

    async def _ws(self, *commands: dict) -> list:
        """Одно соединение: вход по токену, команды по очереди, результаты в том же порядке."""
        if not self.enabled:
            raise PanelError("Home Assistant не подключён: задайте HASS_URL и HASS_TOKEN")
        parts = urlsplit(self.url)
        address = f"{'wss' if parts.scheme == 'https' else 'ws'}://{parts.netloc}/api/websocket"
        try:
            async with self._connect(address, open_timeout=TIMEOUT, max_size=16 * 2**20) as ws:
                await ws.recv()  # auth_required
                await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                if json.loads(await ws.recv()).get("type") != "auth_ok":
                    raise PanelError("Home Assistant не принял токен")
                out = []
                for i, command in enumerate(commands, 1):
                    await ws.send(json.dumps({"id": i, **command}))
                    while True:
                        reply = json.loads(await ws.recv())
                        if reply.get("id") == i and reply.get("type") == "result":
                            break
                    if not reply.get("success"):
                        raise PanelError((reply.get("error") or {}).get("message") or "Home Assistant отказал")
                    out.append(reply.get("result"))
                return out
        except (OSError, TimeoutError, websockets.WebSocketException, ValueError) as e:
            raise PanelError(f"Home Assistant недоступен: {e}") from e

    async def _rest(self, method: str, path: str, **kw):
        try:
            r = await self._client.request(method, self.url + path, headers={"Authorization": f"Bearer {self.token}"}, **kw)
        except httpx.HTTPError as e:
            raise PanelError(f"Home Assistant недоступен: {e}") from e
        if r.status_code >= 400:
            raise PanelError(f"Home Assistant ответил {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    async def overview(self) -> dict:
        """Устройства по комнатам, подключённые службы производителей и то, что найдено, но не добавлено."""
        areas, devices, entities, exposed, flows = await self._ws(
            {"type": "config/area_registry/list"}, {"type": "config/device_registry/list"},
            {"type": "config/entity_registry/list"}, {"type": "homeassistant/expose_entity/list"},
            {"type": "config_entries/flow/progress"})
        states = await self._rest("GET", "/api/states")
        entries = await self._rest("GET", "/api/config/config_entries/entry")
        area_name = {a["area_id"]: a["name"] for a in areas}
        device_by = {d["id"]: d for d in devices}
        entry_by = {e["entity_id"]: e for e in entities}
        shown = (exposed or {}).get("exposed_entities") or {}
        items = []
        for state in states:
            entry = entry_by.get(state["entity_id"], {})
            device = device_by.get(entry.get("device_id"), {})
            area = area_name.get(entry.get("area_id") or device.get("area_id"), "")
            made = item(state, entry, device, area, bool((shown.get(state["entity_id"]) or {}).get(ASSISTANT)))
            if made:
                items.append(made)
        items.sort(key=lambda i: (i["area"] == "", i["area"], not i["control"], i["secondary"], i["name"].lower()))
        used: dict[str, int] = {}  # служба производителя → сколько её строк на вкладке
        for i in items:
            if i["entry_id"]:
                used[i["entry_id"]] = used.get(i["entry_id"], 0) + 1
        found: dict[tuple, dict] = {}  # роутер объявляет себя по UPnP много раз — показываем один
        for f in flows or []:
            context = f.get("context") or {}
            if context.get("source") in ("user", "reauth", "reconfigure"):
                continue
            title = (context.get("title_placeholders") or {}).get("name") or f.get("handler", "")
            found.setdefault((f.get("handler"), title), {"flow_id": f["flow_id"], "handler": f.get("handler", ""), "title": title})
        return {
            "url": self.url,
            "areas": sorted({i["area"] for i in items if i["area"]}),
            "items": items,
            # службы производителей, через которые приходят устройства: их можно опросить заново
            "integrations": [{"entry_id": e["entry_id"], "domain": e["domain"], "title": e.get("title") or e["domain"],
                              "state": e.get("state", ""), "devices": used[e["entry_id"]]}
                             for e in entries if e["entry_id"] in used],
            "discovered": list(found.values()),
        }

    async def act(self, entity_id: str, action: str, value=None) -> dict:
        domain, service, data = service_call(entity_id, action, value)
        await self._rest("POST", f"/api/services/{domain}/{service}", json=data)
        return {"ok": True, "service": f"{domain}.{service}"}

    async def expose(self, entity_id: str, exposed: bool) -> dict:
        """Открыть или закрыть сущность голосовому ассистенту: от этого зависит, чем можно управлять голосом."""
        if "." not in entity_id:
            raise PanelError(f"нет такой сущности: {entity_id}")
        await self._ws({"type": "homeassistant/expose_entity", "assistants": [ASSISTANT],
                        "entity_ids": [entity_id], "should_expose": bool(exposed)})
        self.changed()
        return {"ok": True, "exposed": bool(exposed)}

    async def reload(self, entry_id: str) -> dict:
        """Опросить службу производителя заново: так появляются устройства, добавленные в её приложении."""
        if not entry_id.isalnum():
            raise PanelError("неверный идентификатор службы")
        await self._rest("POST", f"/api/config/config_entries/entry/{entry_id}/reload")
        self.changed()
        return {"ok": True}

    # --- добавление найденного устройства: шаги мастера Home Assistant ---------------------------------

    async def flow(self, flow_id: str, answer: dict | None = None) -> dict:
        """Текущий шаг добавления (answer=None) или следующий после ответа. Подписи полей — из переводов HA."""
        if not flow_id.isalnum():
            raise PanelError("неверный идентификатор шага")
        path = f"/api/config/config_entries/flow/{flow_id}"
        step = await (self._rest("GET", path) if answer is None else self._rest("POST", path, json=answer))
        handler = step.get("handler") or ""
        texts: dict = {}
        if isinstance(handler, str) and handler:
            try:
                (got,) = await self._ws({"type": "frontend/get_translations", "language": "ru", "category": "config",
                                         "integration": [handler]})
                texts = (got or {}).get("resources") or {}
            except PanelError as e:  # без подписей шаг всё равно можно пройти
                log.warning("переводы шага %s не получены: %s", handler, e)
        if step.get("type") in ("create_entry", "abort"):
            self.changed()
        return describe(step, texts)


def field(raw: dict, label: str) -> dict:
    """Поле формы Home Assistant в простом виде: text, password, number, boolean или select."""
    selector = raw.get("selector") or {}
    kind, options = raw.get("type") or "", raw.get("options")
    if "select" in selector:
        kind, options = "select", selector["select"].get("options")
    elif "boolean" in selector:
        kind = "boolean"
    elif "number" in selector:
        kind = "number"
    elif "text" in selector:
        kind = "password" if (selector["text"] or {}).get("type") == "password" else "text"
    kind = {"string": "text", "integer": "number", "float": "number", "positive_int": "number"}.get(kind, kind)
    if kind not in ("text", "password", "number", "boolean", "select"):
        kind = "text"
    if raw.get("name") in ("password", "pin", "token", "api_key") and kind == "text":
        kind = "password"
    choices = [{"value": o[0], "label": str(o[1])} if isinstance(o, (list, tuple)) else
               {"value": o.get("value"), "label": str(o.get("label", o.get("value")))} if isinstance(o, dict) else
               {"value": o, "label": str(o)} for o in options or []] if kind == "select" else []
    default = raw.get("default", (raw.get("description") or {}).get("suggested_value"))
    return {"name": raw.get("name", ""), "label": label or raw.get("name", ""), "kind": kind,
            "required": bool(raw.get("required")), "default": default, "options": choices}


def describe(step: dict, texts: dict) -> dict:
    """Шаг мастера для приложения: что показать и что спросить."""
    handler, step_id, kind = step.get("handler") or "", step.get("step_id") or "", step.get("type") or ""
    places = step.get("description_placeholders") or {}

    def text(key: str) -> str:
        raw = texts.get(f"component.{handler}.config.{key}", "")
        for name, value in places.items():
            raw = raw.replace("{" + name + "}", str(value))
        return raw

    out = {"flow_id": step.get("flow_id", ""), "handler": handler, "type": kind, "fields": [], "choices": [],
           "title": text(f"step.{step_id}.title") or text("flow_title") or handler, "text": text(f"step.{step_id}.description"),
           "errors": [text(f"error.{e}") or str(e) for e in (step.get("errors") or {}).values()], "url": step.get("url", "")}
    if "{" in out["title"]:  # имя устройства приложение уже знает из списка найденного
        out["title"] = handler
    if kind == "form":
        out["fields"] = [field(f, text(f"step.{step_id}.data.{f.get('name')}")) for f in step.get("data_schema") or []]
    elif kind == "menu":
        options = step.get("menu_options") or []
        pairs = options.items() if isinstance(options, dict) else [(o, "") for o in options]
        out["choices"] = [{"value": k, "label": v or text(f"step.{step_id}.menu_options.{k}") or k} for k, v in pairs]
    elif kind == "create_entry":
        out["title"], out["text"] = step.get("title") or handler, "Добавлено. Откройте устройство ассистенту, чтобы управлять им голосом."
    elif kind == "abort":
        out["text"] = text(f"abort.{step.get('reason')}") or f"Не добавлено: {step.get('reason')}"
    elif kind not in ("external", "external_done", "progress", "show_progress"):
        out["text"] = out["text"] or "Этот шаг нужно пройти в самом Home Assistant."
    return out
