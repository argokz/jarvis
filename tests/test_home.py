import json
from dataclasses import replace
from types import SimpleNamespace as NS

import httpx

from jarvis.agent import Reply
from jarvis.config import Settings
from jarvis.home import Home
from jarvis.notify import Notifier
from jarvis.router import VoiceRouter
from jarvis.store import Store

TOOLS = [
    {"name": "intent__HassTurnOn", "description": "Turns on", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "homeassistant__GetLiveContext", "description": "State", "inputSchema": {"type": "object"}},
    {"name": "llm__GetDateTime", "description": "Time", "inputSchema": {"type": "object"}},
]
OVERVIEW = "This device is not able to start timers.\nStatic Context:\n- names: Лампочка\n  domain: light\n  areas: Зал"


def ha(calls, tool_result=None, fail=False):
    """Поддельный MCP-сервер Home Assistant."""
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append((body["method"], body.get("params"), req.headers.get("authorization"), str(req.url)))
        if fail:
            raise httpx.ConnectError("refused")
        result = {
            "tools/list": {"tools": TOOLS},
            "prompts/list": {"prompts": [{"name": "Device control"}]},
            "prompts/get": {"messages": [{"role": "assistant", "content": {"type": "text", "text": OVERVIEW}}]},
            "tools/call": tool_result or {"content": [{"type": "text", "text": '{"response_type": "action_done"}'}],
                                          "isError": False},
        }[body["method"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- клиент Home Assistant ----------------------------------------------------------------------

async def test_context_lists_tools_and_overview_and_is_cached():
    calls, now = [], [0.0]
    home = Home("http://ha:8123/", "tok", client=ha(calls), clock=lambda: now[0])
    tools, overview = await home.context()
    assert [t["name"] for t in tools] == ["intent__HassTurnOn", "homeassistant__GetLiveContext"]  # без GetDateTime
    assert tools[0]["input_schema"] == {"type": "object", "properties": {}} and "Лампочка" in overview
    assert "timers" not in overview  # таймеры ставит сам Jarvis: фраза HA о своих таймерах сбивает модель
    assert calls[0][2] == "Bearer tok" and calls[0][3] == "http://ha:8123/api/mcp/assist"
    await home.context()
    assert len(calls) == 3  # второй раз — из кэша
    now[0] = 301
    await home.context()
    assert len(calls) == 6


async def test_context_is_none_when_ha_is_down_or_not_configured():
    calls, now = [], [0.0]
    home = Home("http://ha:8123", "tok", client=ha(calls, fail=True), clock=lambda: now[0])
    assert await home.context() is None
    assert await home.context() is None and len(calls) == 1  # повторная проверка — не раньше чем через 30 с
    now[0] = 31
    await home.context()
    assert len(calls) == 2
    assert await Home("", "", client=ha(calls)).context() is None and len(calls) == 2


async def test_call_returns_text_and_error_flag():
    calls = []
    home = Home("http://ha", "tok", client=ha(calls))
    assert await home.call("intent__HassTurnOn", {"name": "Лампочка"}) == ('{"response_type": "action_done"}', False)
    assert calls[-1][:2] == ("tools/call", {"name": "intent__HassTurnOn", "arguments": {"name": "Лампочка"}})
    bad = {"content": [{"type": "text", "text": "Error calling tool: No entities matched"}], "isError": True}
    assert await Home("http://ha", "tok", client=ha([], bad)).call("x", {}) == ("Error calling tool: No entities matched", True)
    text, err = await Home("http://ha", "tok", client=ha([], fail=True)).call("x", {})
    assert err and "недоступен" in text


# --- роутер с инструментами дома ----------------------------------------------------------------

class Stream:
    def __init__(self, texts, final):
        self.texts, self.final = texts, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for t in self.texts:
                yield NS(type="text", text=t)
        return gen()

    async def get_final_message(self):
        return self.final


class Client:
    """Отдаёт заранее заданные ответы модели по очереди и запоминает запросы."""

    def __init__(self, *rounds):
        self.rounds, self.calls = list(rounds), []
        self.messages = NS(stream=self._stream)

    def _stream(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        texts, stop, content = self.rounds.pop(0)
        return Stream(texts, NS(stop_reason=stop, content=content))


class Brain:
    def __init__(self):
        self.notes, self.calls = [], []

    def journal_note(self, line):
        self.notes.append(line)

    async def ask(self, user, text, channel="api"):
        self.calls.append(text)
        return Reply("Ответ агента.")


def use(name, args, id_="tu_1"):
    return NS(type="tool_use", id=id_, name=name, input=args)


def router(tmp_path, client, home_client):
    s = replace(Settings(), vault=(tmp_path / "vault").resolve(), hass_url="http://ha", hass_token="tok",
                notion_token="", open_roots=())
    store, brain = Store(tmp_path / "j.db"), Brain()
    r = VoiceRouter(s, brain, store, Notifier(), client=client, home=Home("http://ha", "tok", client=home_client))
    return r, store, brain


async def say(r, text):
    return "".join([x async for x in r.respond("owner", text)])


async def test_router_turns_light_on_without_agent_in_one_model_call(tmp_path):
    ha_calls = []
    on = use("intent__HassTurnOn", {"name": "Лампочка", "area": "Зал"})
    client = Client((["Включаю лампочку в зале."], "tool_use", [on]))
    r, store, brain = router(tmp_path, client, ha(ha_calls))
    assert await say(r, "включи лампочку в зале") == "Включаю лампочку в зале."
    assert len(client.calls) == 1 and brain.calls == []
    kw = client.calls[0]
    assert [t["name"] for t in kw["tools"]] == ["delegate", "intent__HassTurnOn", "homeassistant__GetLiveContext"]
    assert "Умным домом управляй сам" in kw["system"] and "Лампочка" in kw["system"]
    assert "управлению умным домом" not in kw["system"]  # дом больше не уходит агенту
    assert ha_calls[-1][:2] == ("tools/call", {"name": "intent__HassTurnOn", "arguments": {"name": "Лампочка", "area": "Зал"}})
    tool = [e for e in store.recent() if e["kind"] == "tool"]
    assert tool[0]["name"] == "intent__HassTurnOn" and "Лампочка" in tool[0]["payload"]
    assert brain.notes and "умный дом, intent__HassTurnOn" in brain.notes[0]
    assert store.history("owner")[-1] == ("assistant", "Включаю лампочку в зале.")


async def test_router_says_done_when_model_acted_silently(tmp_path):
    client = Client(([], "tool_use", [use("intent__HassTurnOn", {"name": "Лампочка"})]))
    r, _, _ = router(tmp_path, client, ha([]))
    assert await say(r, "свет") == "Готово."


async def test_router_answers_state_question_from_tool_result(tmp_path):
    live = {"content": [{"type": "text", "text": "Лампочка: state on"}], "isError": False}
    client = Client((["Сейчас посмотрю."], "tool_use", [use("homeassistant__GetLiveContext", {})]),
                    (["Лампочка в зале включена."], "end_turn", []))
    r, _, brain = router(tmp_path, client, ha([], live))
    assert await say(r, "свет в зале горит?") == "Сейчас посмотрю. Лампочка в зале включена."
    assert brain.notes == []  # чтение состояния в дневной журнал не пишется
    second = client.calls[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-1]["role"] == "user"
    assert second[-1]["content"] == [{"type": "tool_result", "tool_use_id": "tu_1", "content": "Лампочка: state on"}]


async def test_router_explains_failed_action_instead_of_done(tmp_path):
    bad = {"content": [{"type": "text", "text": "Error calling tool: No entities matched"}], "isError": True}
    client = Client((["Включаю гирлянду."], "tool_use", [use("intent__HassTurnOn", {"name": "Гирлянда"})]),
                    (["Гирлянды среди устройств нет."], "end_turn", []))
    r, _, _ = router(tmp_path, client, ha([], bad))
    assert await say(r, "включи гирлянду") == "Включаю гирлянду. Гирлянды среди устройств нет."
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True


async def test_router_last_round_forbids_tools(tmp_path):
    bad = {"content": [{"type": "text", "text": "Error"}], "isError": True}
    on = lambda: use("intent__HassTurnOn", {"name": "X"})  # noqa: E731
    client = Client(([], "tool_use", [on()]), ([], "tool_use", [on()]), (["Не получилось."], "end_turn", []))
    r, _, _ = router(tmp_path, client, ha([], bad))
    assert await say(r, "включи x") == "Не получилось."
    assert "tool_choice" not in client.calls[0] and client.calls[2]["tool_choice"] == {"type": "none"}


async def test_router_runs_home_action_and_delegates_the_rest(tmp_path):
    ha_calls = []
    both = [use("intent__HassTurnOn", {"name": "Лампочка"}), use("delegate", {"task": "Запиши идею про теплицу"}, "tu_2")]
    r, _, brain = router(tmp_path, Client((["Включаю свет, секунду."], "tool_use", both)), ha(ha_calls))
    assert await say(r, "включи свет и запиши идею") == "Включаю свет, секунду. Ответ агента."
    assert ha_calls[-1][0] == "tools/call" and brain.calls[0].startswith("Запиши идею про теплицу")


async def test_router_falls_back_to_agent_when_ha_is_down(tmp_path):
    client = Client((["Секунду."], "tool_use", [use("delegate", {"task": "Включи лампочку в зале"})]))
    r, _, brain = router(tmp_path, client, ha([], fail=True))
    assert await say(r, "включи лампочку") == "Секунду. Ответ агента."
    kw = client.calls[0]
    assert [t["name"] for t in kw["tools"]] == ["delegate"] and "управлению умным домом" in kw["system"]
