"""Home Assistant для быстрого голосового контура: инструменты Assist по MCP (JSON-RPC поверх HTTP).

Адрес тот же, что у агента (HASS_MCP_PATH, по умолчанию /api/mcp/assist): видны только сущности,
открытые голосовым ассистентам. Модель выбирает инструмент — здесь вызов только пересылается в HA.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("jarvis.home")

# Не нужны роутеру: время уже есть в реплике, таймеров и колонок-спутников HA нет.
SKIP = frozenset({"llm__GetDateTime", "assist_satellite__HassBroadcast", "intent__HassCancelAllTimers"})
# Только читают состояние: после них модель должна ответить по результату.
READ_ONLY = frozenset({"homeassistant__GetLiveContext", "todo__get_items"})
TTL = 300.0       # список устройств и инструментов меняется редко
RETRY = 30.0      # HA недоступен — не проверять на каждой реплике
LIST_TIMEOUT = 2.0
CALL_TIMEOUT = 10.0  # облачные устройства отвечают не сразу


class HomeError(Exception):
    pass


class Home:
    def __init__(self, url: str, token: str, path: str = "/api/mcp/assist",
                 client: httpx.AsyncClient | None = None, clock=time.monotonic):
        self.url, self.token = url.rstrip("/") + path, token
        self.enabled = bool(url and token)
        self._client = client or httpx.AsyncClient()
        self._clock = clock
        self._cache: tuple[float, tuple[list[dict], str] | None] | None = None
        self._id = 0

    async def _rpc(self, method: str, params: dict | None = None, timeout: float = LIST_TIMEOUT) -> dict:
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, **({"params": params} if params else {})}
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream"}
        try:
            r = await self._client.post(self.url, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException as e:
            raise HomeError("Home Assistant не ответил вовремя; неизвестно, дошла ли команда") from e
        except httpx.HTTPError as e:
            raise HomeError(f"Home Assistant недоступен: {e}") from e
        if r.status_code != 200:
            raise HomeError(f"Home Assistant вернул {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise HomeError("Home Assistant вернул не JSON") from e
        if "error" in data:
            raise HomeError(str(data["error"].get("message", data["error"])))
        return data.get("result") or {}

    async def context(self) -> tuple[list[dict], str] | None:
        """Инструменты в формате Anthropic и обзор устройств. None, если HA выключен или недоступен."""
        if not self.enabled:
            return None
        now = self._clock()
        if self._cache and now < self._cache[0]:
            return self._cache[1]
        try:
            listed = (await self._rpc("tools/list")).get("tools", [])
            tools = [{"name": t["name"], "description": t.get("description", ""), "input_schema": t["inputSchema"]}
                     for t in listed if t["name"] not in SKIP]
            prompts = (await self._rpc("prompts/list")).get("prompts", [])
            overview = ""
            if prompts:
                got = await self._rpc("prompts/get", {"name": prompts[0]["name"]})
                overview = "\n".join(m["content"].get("text", "") for m in got.get("messages", []))
                # HA дописывает «This device is not able to start timers»: это про его собственные таймеры,
                # а модель после такой строки отказывается ставить и наши (schedule_set).
                overview = "\n".join(line for line in overview.splitlines() if "timer" not in line.lower())
            value = (tools, overview) if tools else None
        except (HomeError, KeyError, TypeError, AttributeError) as e:
            log.warning("умный дом для роутера недоступен: %r", e)
            value = None
        self._cache = (now + (TTL if value else RETRY), value)
        return value

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Вызвать инструмент HA. Возвращает (текст результата, ошибка ли)."""
        try:
            res = await self._rpc("tools/call", {"name": name, "arguments": args}, timeout=CALL_TIMEOUT)
        except HomeError as e:  # модель должна сказать об этом, а не «готово»
            return str(e), True
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return text, bool(res.get("isError"))
