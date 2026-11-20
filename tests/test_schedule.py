import asyncio
import importlib
import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import schedule
from jarvis.agent import Reply, integrations_hint, schedule_tools
from jarvis.config import Settings
from jarvis.events import EventBus
from jarvis.notify import Notifier
from jarvis.router import VoiceRouter
from jarvis.schedule import ScheduleError, Scheduler, parse_at, parse_quick, parse_repeat
from jarvis.store import Store

NOW = datetime(2026, 11, 18, 14, 0)  # среда


class Recorder(Notifier):
    def __init__(self):
        super().__init__()
        self.alerts, self.sent = [], []

    async def alert(self, item_id, text, priority=4, stop=False):
        self.alerts.append((item_id, text, priority, stop))

    async def send(self, text, **kw):
        self.sent.append(text)


class World:
    """Планировщик с ручными часами и записью всего, что он разослал."""

    def __init__(self, tmp_path):
        self.now = NOW.timestamp()
        self.notes, self.events, self.notifier = [], [], Recorder()
        self.store = Store(tmp_path / "j.db")
        self.s = self.make()

    def make(self) -> Scheduler:
        s = Scheduler(self.store, self.notifier, EventBus(), clock=lambda: self.now)
        s.journal = self.notes.append

        async def sink(event):
            self.events.append((event["state"], event["id"]))

        s.sinks.append(sink)
        return s

    async def go(self, seconds=0.0, **delta):
        """Перевести часы и дать планировщику сработать; фоновые уведомления — дождаться."""
        self.now += seconds + timedelta(**delta).total_seconds()
        await self.s.tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def state(self, item_id):
        return self.s.get(item_id)["state"]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


# --- слова и разбор ------------------------------------------------------------------------------

def test_time_in_words():
    assert schedule.span_text(600) == "10 минут" and schedule.span_text(61) == "1 минуту 1 секунду"
    assert schedule.span_text(5400) == "1 час 30 минут" and schedule.span_text(7322) == "2 часа 2 минуты"
    assert schedule.span_text(22) == "22 секунды" and schedule.span_text(0.2) == "1 секунду"
    at = NOW.replace(hour=7, minute=5)
    assert schedule.clock_text(at) == "7:05" and schedule.clock_text(at, spoken=True) == "7 часов 5 минут"
    assert schedule.clock_text(NOW.replace(hour=21, minute=0), spoken=True) == "21 час"
    assert schedule.when_text(NOW.replace(hour=18, minute=30), NOW) == "сегодня в 18:30"
    assert schedule.when_text(at + timedelta(days=1), NOW, spoken=True) == "завтра в 7 часов 5 минут"
    assert schedule.when_text(at + timedelta(days=4), NOW) == "в воскресенье, 22 ноября, в 7:05"
    assert schedule.when_text(datetime(2027, 1, 9, 9, 0), NOW) == "9 января 2027 года в 9:00"
    assert schedule.repeat_text("0,1,2,3,4") == "по будням" and schedule.repeat_text("5,6") == "по выходным"
    assert schedule.repeat_text("0,1,2,3,4,5,6") == "каждый день" and schedule.repeat_text("0,2") == "по понедельникам, средам"


def test_parse_time_and_repeat():
    assert parse_at("18:30", NOW) == NOW.replace(hour=18, minute=30)
    assert parse_at("7:00", NOW) == NOW.replace(hour=7) + timedelta(days=1)   # сегодня уже прошло — завтра
    assert parse_at("2026-11-20 09:15", NOW) == datetime(2026, 11, 20, 9, 15)
    assert parse_at("2026-11-20T09:15:00", NOW) == datetime(2026, 11, 20, 9, 15)
    for bad in ("завтра в семь", "25:00", "2026-02-30 10:00"):
        with pytest.raises(ScheduleError):
            parse_at(bad, NOW)
    with pytest.raises(ScheduleError, match="уже прошло"):
        parse_at("2026-11-18 13:00", NOW)
    assert parse_repeat("weekdays") == "0,1,2,3,4" and parse_repeat("daily") == "0,1,2,3,4,5,6"
    assert parse_repeat("sat, Mon") == "0,5" and parse_repeat("") == "" and parse_repeat(None) == ""
    with pytest.raises(ScheduleError):
        parse_repeat("иногда")


def test_quick_commands_need_the_whole_phrase():
    assert parse_quick("Стоп!") == ("stop", None) and parse_quick("Всё, хватит, спасибо.") == ("stop", None)
    assert parse_quick("выключи будильник") == ("stop", None)
    assert parse_quick("Отложи на 10 минут") == ("snooze", 10) and parse_quick("отложи") == ("snooze", 10)
    assert parse_quick("отложи на полчаса") == ("snooze", 30) and parse_quick("Отложи на час.") == ("snooze", 60)
    assert parse_quick("ещё пять минут попозже") == ("snooze", 5)
    # чужие команды и обычная речь будильник не трогают
    assert parse_quick("выключи свет") is None and parse_quick("стоп машина") is None
    # короткое «стоп» распознаётся неточно (замерено на GigaAM); одно похожее слово — это оно
    for heard in ("Соп.", "Топ.", "Сопь.", "Stop.", "Стоoп."):
        assert parse_quick(heard) == ("stop", None), heard
    for other in ("Угу.", "Не.", "Да.", "Привет", "Супчик", "соп соп"):
        assert parse_quick(other) is None, other
    assert parse_quick("отложи встречу на завтра") is None and parse_quick("") is None
    assert parse_quick("ну вот тогда он и говорит мне хватит уже это делать") is None


# --- таймер ---------------------------------------------------------------------------------------

async def test_timer_rings_until_stopped(w):
    assert await w.s.call("schedule_set", {"kind": "timer", "in_seconds": 600}, "voice") == "Поставил таймер на 10 минут."
    item = w.s.active()[0]
    assert item["kind"] == "timer" and item["due"] == w.now + 600 and item["state"] == "pending"
    await w.go(599)
    assert w.events == []
    listing = await w.s.call("schedule_list", {})
    assert "Сейчас 2026-11-18 14:09." in listing and f"id {item['id']}: таймер на 10 минут — сработает через 1 секунду" in listing
    await w.go(1)
    assert w.events == [("ringing", item["id"])] and w.state(item["id"]) == "ringing"
    assert w.notifier.alerts == [(item["id"], "Таймер на 10 минут.", 4, True)]  # клиента с голосом нет — на телефон
    assert "звенит сейчас" in await w.s.call("schedule_list", {})
    await w.go(30)
    assert w.events == [("ringing", item["id"])]  # повторно не срабатывает
    assert await w.s.call("schedule_dismiss", {}, "voice") == "Выключил."
    assert w.events[-1] == ("stopped", item["id"]) and w.state(item["id"]) == "done" and w.s.active() == []
    assert w.notes == ["поставлено: таймер на 10 минут, сегодня в 14:10 (voice)", "сработало: таймер на 10 минут",
                       "выключено: таймер на 10 минут (voice)"]
    with pytest.raises(ScheduleError, match="ничего не звенит"):
        await w.s.call("schedule_dismiss", {})


async def test_timer_stays_off_the_phone_when_a_voice_client_rings(w):
    w.s.has_voice = lambda: True
    await w.s.call("schedule_set", {"kind": "timer", "in_seconds": 5, "text": "Паста."})
    await w.go(5)
    assert w.events[0][0] == "ringing" and w.notifier.alerts == []
    assert w.s.title(w.s.ringing()[0]) == "Таймер на 5 секунд: Паста"
    await w.s.call("schedule_set", {"kind": "timer", "in_seconds": 8, "text": "Таймер на 8 секунд"})
    assert w.s.active()[-1]["text"] == ""  # модель повторила вид записи вместо названия — отбрасываем


async def test_ringing_stops_by_itself_after_ten_minutes(w):
    await w.s.call("schedule_set", {"kind": "timer", "in_seconds": 60})
    await w.go(60)
    await w.go(schedule.RING_MAX - 1)
    assert w.s.ringing()
    await w.go(1)
    assert not w.s.ringing() and w.events[-1][0] == "stopped"
    assert "никто не выключил" in w.notifier.sent[0]


# --- напоминание ----------------------------------------------------------------------------------

async def test_reminder_is_said_once_and_can_be_snoozed(w):
    out = await w.s.call("schedule_set", {"kind": "reminder", "at": "18:30", "text": "позвонить маме"}, "telegram")
    assert out == "Напомню сегодня в 18 часов 30 минут: позвонить маме."
    item = w.s.active()[0]
    await w.go(hours=4, minutes=30)
    assert w.events == [("said", item["id"])] and w.state(item["id"]) == "done"
    assert w.notifier.alerts == [(item["id"], "Напоминаю: позвонить маме.", 4, False)]
    assert [e for e in w.store.recent() if e["kind"] == "alert"][0]["name"] == "reminder"
    # «отложи на 20 минут» сразу после напоминания
    assert await w.s.call("schedule_dismiss", {"snooze_minutes": 20}) == "Отложил на 20 минут."
    assert w.state(item["id"]) == "pending" and w.s.get(item["id"])["due"] == w.now + 1200
    await w.go(minutes=20)
    assert w.events == [("said", item["id"])] * 2
    await w.go(schedule.RECENT + 1)
    with pytest.raises(ScheduleError, match="откладывать нечего"):
        await w.s.call("schedule_dismiss", {"snooze_minutes": 5})


async def test_relative_reminder_and_validation(w):
    assert await w.s.call("schedule_set", {"kind": "reminder", "in_seconds": 1200, "text": "выключить духовку"}) \
        == "Напомню через 20 минут: выключить духовку."
    for bad in ({"kind": "siren", "in_seconds": 5}, {"kind": "alarm"}, {"kind": "timer", "in_seconds": 0},
                {"kind": "alarm", "at": "7 утра"}, {"kind": "alarm", "at": "07:00", "repeat": "иногда"}):
        with pytest.raises(ScheduleError):
            await w.s.call("schedule_set", bad)
    assert len(w.s.active()) == 1


async def test_late_reminder_is_still_said_but_late_alarm_is_not_rung(w, tmp_path):
    await w.s.call("schedule_set", {"kind": "reminder", "at": "15:00", "text": "таблетки"})
    await w.s.call("schedule_set", {"kind": "alarm", "at": "15:00"})
    reminder, alarm = sorted(w.s.active(), key=lambda i: i["id"])
    w.s = w.make()  # ядро перезапустили: записи читаются из той же базы
    assert len(w.s.active()) == 2
    await w.go(hours=3)  # …и запустили только в 17:00
    assert w.events == [("said", reminder["id"])]
    assert w.state(alarm["id"]) == "missed" and not w.s.ringing()
    texts = {a[0]: a[1] for a in w.notifier.alerts}
    assert texts[reminder["id"]] == "Напоминаю: таблетки. Было назначено на 15:00."
    assert "пропущен" in texts[alarm["id"]] and "Будильник на 15:00" in texts[alarm["id"]]


# --- будильник ------------------------------------------------------------------------------------

async def test_alarm_tomorrow_rings_goes_to_phone_and_snoozes(w):
    assert await w.s.call("schedule_set", {"kind": "alarm", "at": "07:00"}) == "Будильник сработает завтра в 7 часов."
    item = w.s.active()[0]
    w.s.has_voice = lambda: True
    await w.go(hours=17)
    assert w.events == [("ringing", item["id"])]
    assert w.notifier.alerts == [(item["id"], "Будильник на 7:00.", 5, True)]  # будильник — всегда и на телефон
    assert await w.s.quick("Отложи на 10 минут.") == "Отложил на 10 минут."
    assert w.events[-1] == ("snoozed", item["id"]) and w.state(item["id"]) == "pending"
    assert w.store.history("owner")[-2:] == [("user", "Отложи на 10 минут."), ("assistant", "Отложил на 10 минут.")]
    await w.go(minutes=10)
    assert w.events[-1] == ("ringing", item["id"])
    assert await w.s.stop(item["id"], by="telegram") is True and await w.s.stop(item["id"]) is False
    assert w.state(item["id"]) == "done"


async def test_repeating_alarm_schedules_the_next_day(w):
    out = await w.s.call("schedule_set", {"kind": "alarm", "at": "07:30", "repeat": "weekdays", "text": "на работу"})
    assert out == "Будильник на 7 часов 30 минут, по будням. Ближайший — завтра в 7 часов 30 минут."
    first = w.s.active()[0]
    assert "по будням: на работу" in await w.s.call("schedule_list", {})
    await w.go(hours=17, minutes=30)                      # четверг 7:30
    ringing, pending = w.s.active()
    assert ringing["id"] == first["id"] and ringing["state"] == "ringing" and ringing["repeat"] == ""
    assert datetime.fromtimestamp(pending["due"]) == datetime(2026, 11, 20, 7, 30) and pending["repeat"] == "0,1,2,3,4"
    await w.s.dismiss()
    await w.go(days=1)                                    # пятница 7:30 → следующий — понедельник
    nxt = [i for i in w.s.active() if i["state"] == "pending"][0]
    assert datetime.fromtimestamp(nxt["due"]) == datetime(2026, 11, 23, 7, 30)
    # повтор по выходным, поставленный в среду, начинается с субботы
    await w.s.call("schedule_set", {"kind": "reminder", "at": "10:00", "repeat": "weekends", "text": "полить цветы"})
    weekend = max(w.s.active(), key=lambda i: i["id"])
    assert datetime.fromtimestamp(weekend["due"]) == datetime(2026, 11, 21, 10, 0)


# --- отмена и быстрые команды ---------------------------------------------------------------------

async def test_cancel_by_id_kind_and_all(w):
    for secs in (60, 120):
        await w.s.call("schedule_set", {"kind": "timer", "in_seconds": secs})
    await w.s.call("schedule_set", {"kind": "reminder", "at": "20:00", "text": "Позвонить маме"})
    a, b, r = w.s.active()
    assert await w.s.call("schedule_cancel", {"kind": "reminder", "text": "маме"}) == "Отменил: напоминание: Позвонить маме."
    with pytest.raises(ScheduleError, match="несколько"):
        await w.s.call("schedule_cancel", {"kind": "timer"})
    assert await w.s.call("schedule_cancel", {"id": a["id"]}) == "Отменил: таймер на 1 минуту."
    with pytest.raises(ScheduleError):
        await w.s.call("schedule_cancel", {"id": a["id"]})
    await w.go(120)                                       # второй таймер звенит — отмена его выключает
    assert await w.s.call("schedule_cancel", {"kind": "timer", "all": True}) == "Отменил: таймер на 2 минуты."
    assert w.events == [("ringing", b["id"]), ("stopped", b["id"])] and w.s.active() == []
    with pytest.raises(ScheduleError, match="нет"):
        await w.s.call("schedule_cancel", {"kind": "alarm"})


async def test_quick_stop_only_while_ringing_and_only_for_commands(w):
    await w.s.call("schedule_set", {"kind": "timer", "in_seconds": 60})
    assert await w.s.quick("стоп") is None                # ещё не звенит — это обычная реплика
    await w.go(60)
    assert await w.s.quick("выключи свет в зале") is None and w.s.ringing()
    assert await w.s.quick("Хватит.") == "Выключил." and not w.s.ringing()


async def test_loop_fires_on_real_clock(tmp_path):
    s, fired = Scheduler(Store(tmp_path / "j.db")), asyncio.Event()

    async def sink(event):
        fired.set()

    s.sinks.append(sink)
    loop = asyncio.create_task(s.run())
    await asyncio.sleep(0.05)                              # цикл уже спит — новая запись должна его разбудить
    await s.call("schedule_set", {"kind": "timer", "in_seconds": 1})
    await asyncio.wait_for(fired.wait(), 3)
    assert s.ringing()
    loop.cancel()


# --- уведомления ----------------------------------------------------------------------------------

async def test_alert_goes_to_telegram_with_buttons_and_to_push():
    pushed, sent = [], []

    def handler(req: httpx.Request):
        pushed.append(json.loads(req.content))
        return httpx.Response(200)

    class Bot:
        async def send_message(self, chat, text, reply_markup=None):
            sent.append((chat, text, reply_markup))

    n = Notifier("https://ntfy.example", "jarvis-x", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    n.use_telegram(Bot(), [42])
    await n.alert(7, "Будильник на 7:00.", priority=5, stop=True)
    await n.alert(8, "Напоминаю: таблетки.")
    assert pushed[0]["priority"] == 5 and pushed[0]["message"] == "Будильник на 7:00." and pushed[1]["priority"] == 4
    chat, text, markup = sent[0]
    assert chat == 42 and text == "🔔 Будильник на 7:00."
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["al:7:s", "al:7:z"]
    assert [b.callback_data for b in sent[1][2].inline_keyboard[0]] == ["al:8:z"]  # напоминание не звенит — «стоп» не нужен


# --- быстрый роутер -------------------------------------------------------------------------------

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
    def __init__(self, *rounds):
        self.rounds, self.calls = list(rounds), []
        self.messages = NS(stream=self._stream)

    def _stream(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        texts, stop, content = self.rounds.pop(0)
        return Stream(texts, NS(stop_reason=stop, content=content))


class Brain:
    def journal_note(self, line):
        pass

    async def ask(self, user, text, channel="api"):
        return Reply("Ответ агента.")


def use(name, args):
    return NS(type="tool_use", id="tu_1", name=name, input=args)


def router(tmp_path, w, client):
    s = replace(Settings(), vault=(tmp_path / "vault").resolve(), hass_url="", hass_token="", notion_token="",
                open_roots=(), pc_control=False)
    return VoiceRouter(s, Brain(), w.store, Notifier(), client=client, scheduler=w.s)


async def say(r, text):
    return "".join([x async for x in r.respond("owner", text)])


async def test_router_sets_timer_in_one_model_call_and_speaks_the_result(tmp_path, w):
    client = Client(([], "tool_use", [use("schedule_set", {"kind": "timer", "in_seconds": 600})]))
    r = router(tmp_path, w, client)
    assert await say(r, "таймер на десять минут") == "Поставил таймер на 10 минут."
    kw = client.calls[0]
    assert len(client.calls) == 1 and w.s.active()[0]["channel"] == "voice"
    assert [t["name"] for t in kw["tools"]] == ["delegate", "schedule_set", "schedule_list", "schedule_cancel", "schedule_dismiss"]
    assert "Таймеры, напоминания и будильники ставь сам" in kw["system"][0]["text"]
    assert "не говори, что сделал, не вызвав его" in kw["system"][0]["text"] and "не повторяй" in kw["system"][0]["text"]
    assert [e["name"] for e in w.store.recent() if e["kind"] == "tool"] == ["schedule_set"]
    # модель всё же что-то сказала — результат добавляется, а не теряется
    client = Client((["Хорошо."], "tool_use", [use("schedule_set", {"kind": "alarm", "at": "07:00"})]))
    assert await say(router(tmp_path, w, client), "разбуди в семь") == "Хорошо. Будильник сработает завтра в 7 часов."


async def test_router_history_shows_its_own_past_tool_calls(tmp_path, w):
    """Без вызовов в истории модель видела «Поставил таймер» как пустые слова и отрекалась от действий."""
    w.store.log("owner", "telegram", "tool", "mcp__jarvis__capture_note", {"title": "x"})   # чужой вызов — до реплики
    w.store.log("owner", "voice", "assistant", payload={"text": "хвост прошлого разговора"})
    client = Client(([], "tool_use", [use("schedule_set", {"kind": "timer", "in_seconds": 600})]),
                    (["Да, десять минут."], "end_turn", []))
    r = router(tmp_path, w, client)
    await say(r, "таймер на десять минут")
    w.store.log("owner", "telegram", "user", payload="а это из телеграма")
    w.store.log("owner", "telegram", "tool", "mcp__jarvis__capture_note", {"title": "y"})   # инструмент агента
    w.store.log("owner", "telegram", "assistant", payload={"text": "Записал."})
    # тот же инструмент у агента: напоминание из Telegram и поручение роутера (канал voice-agent)
    w.store.log("owner", "telegram", "user", payload="напомни в девять про банк")
    w.store.log("owner", "telegram", "tool", "mcp__jarvis__schedule_set", {"kind": "reminder", "at": "09:00"})
    w.store.log("owner", "telegram", "tool_result", "mcp__jarvis__schedule_set", {"text": "Напомню завтра в 9 часов."})
    w.store.log("owner", "telegram", "assistant", payload={"text": "Напомню в девять."})
    w.store.log("owner", "voice", "user", payload="и ещё в десять про врача")
    w.store.log("owner", "voice-agent", "user", payload="служебная задача агенту")
    w.store.log("owner", "voice-agent", "tool", "mcp__jarvis__schedule_set", {"kind": "reminder", "at": "10:00"})
    w.store.log("owner", "voice-agent", "assistant", payload={"text": "служебный ответ агента"})
    w.store.log("owner", "voice", "assistant", payload={"text": "Секунду. Напомню в десять."})
    await say(r, "таймер стоит?")
    msgs = client.calls[1]["messages"]
    shown = [(m["content"][0]["name"], m["content"][0]["input"].get("at")) for m in msgs[6:]
             if m["role"] == "assistant" and not isinstance(m["content"], str)]
    assert shown == [("schedule_set", "09:00"), ("schedule_set", "10:00")]    # иначе модель ставила их повторно
    results = [m["content"][0]["content"] for m in msgs[6:] if m["role"] == "user" and not isinstance(m["content"], str)]
    assert results == ["Напомню завтра в 9 часов.", "(результат этого вызова в журнале не сохранён)"]
    assert not any("служебн" in str(m["content"]) for m in msgs)
    msgs = msgs[:6] + msgs[-1:]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert msgs[0]["content"].endswith("таймер на десять минут")       # история начинается с реплики пользователя
    call = msgs[1]["content"][0]
    assert call["type"] == "tool_use" and call["name"] == "schedule_set" and call["input"] == {"kind": "timer", "in_seconds": 600}
    assert msgs[2]["content"] == [{"type": "tool_result", "tool_use_id": call["id"], "content": "Поставил таймер на 10 минут."}]
    assert msgs[3]["content"] == "Поставил таймер на 10 минут."
    assert msgs[4]["content"].endswith("] а это из телеграма") and msgs[5]["content"] == "Записал."  # чужие инструменты не показываем
    # роутер без планировщика этих вызовов в историю не берёт: такого инструмента у модели сейчас нет
    r.scheduler = None
    assert all(isinstance(m["content"], str) for m in r.history("owner"))


async def test_router_lists_and_explains_errors_in_a_second_call(tmp_path, w):
    await w.s.call("schedule_set", {"kind": "timer", "in_seconds": 300})
    client = Client(([], "tool_use", [use("schedule_list", {})]), (["Один таймер, осталось пять минут."], "end_turn", []))
    assert await say(router(tmp_path, w, client), "какие таймеры?") == "Один таймер, осталось пять минут."
    assert "таймер на 5 минут" in client.calls[1]["messages"][-1]["content"][0]["content"]
    client = Client(([], "tool_use", [use("schedule_dismiss", {})]), (["Сейчас ничего не звенит."], "end_turn", []))
    assert await say(router(tmp_path, w, client), "выключи будильник") == "Сейчас ничего не звенит."
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    # и список, и отказ попадают в историю вместе с тем, что вернул инструмент: иначе модель считала свои
    # прошлые ответы выдуманными
    past = [b for m in router(tmp_path, w, client).history("owner", schedule.NAMES)
            if not isinstance(m["content"], str) for b in m["content"] if b["type"] == "tool_result"]
    assert "таймер на 5 минут" in past[0]["content"] and "is_error" not in past[0]
    assert past[1]["content"] == "сейчас ничего не звенит" and past[1]["is_error"] is True


def test_router_without_scheduler_has_no_schedule_tools(tmp_path, w):
    r = router(tmp_path, w, Client())
    r.scheduler = None
    assert "schedule_set" not in r.system() and "не вызвав его" not in r.system()


# --- агент и API ----------------------------------------------------------------------------------

async def test_agent_gets_the_same_tools(w):
    tools = schedule_tools(w.s, "telegram")
    assert [t.name for t in tools] == ["schedule_set", "schedule_list", "schedule_cancel", "schedule_dismiss"]
    ok = await tools[0].handler({"kind": "reminder", "at": "2026-11-19 09:00", "text": "созвон"})
    assert ok == {"content": [{"type": "text", "text": "Напомню завтра в 9 часов: созвон."}]}
    assert w.s.active()[0]["channel"] == "telegram"
    bad = await tools[3].handler({})
    assert bad["is_error"] is True and "ничего не звенит" in bad["content"][0]["text"]
    assert "schedule_set" in integrations_hint(Settings(), scheduler=True)
    assert "schedule_set" not in integrations_hint(replace(Settings(), pc_control=False), scheduler=False)


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


def test_api_lists_stops_snoozes_and_cancels(core):
    api, client = core
    assert client.get("/schedule").status_code == 401
    run = client.portal.call
    run(api.scheduler.call, "schedule_set", {"kind": "alarm", "at": "07:00", "text": "зарядка"})
    run(api.scheduler.call, "schedule_set", {"kind": "timer", "in_seconds": 1})
    alarm, timer = sorted(client.get("/schedule", headers=H).json(), key=lambda i: i["id"])
    assert alarm["title"] == "Будильник на 7:00: зарядка" and alarm["state"] == "pending"
    assert client.post(f"/schedule/{timer['id']}/stop", headers=H).status_code == 404   # ещё не звенит
    with client.websocket_connect("/events/ws?token=t0k") as ws:
        def next_alert():
            while (ev := ws.receive_json()).get("type") != "alert":
                pass
            return ev

        ev = next_alert()                                  # цикл планировщика в ядре сам доводит таймер до звонка
        assert ev["state"] == "ringing" and ev["id"] == timer["id"] and ev["text"] == "Таймер на 1 секунду"
        assert client.post(f"/schedule/{timer['id']}/snooze", headers=H, json={"minutes": 5}).status_code == 200
        assert next_alert()["state"] == "snoozed"
    assert [i["state"] for i in client.get("/schedule", headers=H).json()] == ["pending", "pending"]
    assert client.delete(f"/schedule/{alarm['id']}", headers=H).status_code == 200
    assert client.delete(f"/schedule/{alarm['id']}", headers=H).status_code == 404
    assert [i["id"] for i in client.get("/schedule", headers=H).json()] == [timer["id"]]
