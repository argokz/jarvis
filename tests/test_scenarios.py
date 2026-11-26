import asyncio
import importlib
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from jarvis import pc, scenarios
from jarvis.agent import Reply, check_path, integrations_hint, scenario_tools
from jarvis.config import Settings
from jarvis.notify import Notifier
from jarvis.router import VoiceRouter
from jarvis.scenarios import Failure, ScenarioError, Scenarios, clock, duration, key, parse, parse_step, parse_times
from jarvis.schedule import Scheduler
from jarvis.store import Store
from jarvis.voice import VoiceService, VoiceSession
from jarvis.wake import WakeWords

NOW = datetime(2026, 11, 18, 14, 0)  # среда

HOME = """---
phrases: ["я дома", "Я пришёл"]
steps:
  - on: Торшер
  - off: {name: Свет, area: Зал}
  - volume: 30
  - say: С возвращением
---
# Я дома
"""


class FakeHome:
    tools = [{"name": "intent__HassTurnOn"}, {"name": "intent__HassTurnOff"}, {"name": "light__HassLightSet"}]

    def __init__(self, fail=(), down=False):
        self.calls, self.fail, self.down = [], set(fail), down

    async def context(self):
        return None if self.down else (self.tools, "обзор")

    async def call(self, name, args):
        self.calls.append((name, args))
        return ("No entities matched", True) if args.get("name") in self.fail else ("ok", False)


class FakePC:
    def __init__(self):
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        if args.get("query") == "сломано":
            raise pc.PCError("yt-dlp не ответил вовремя")
        return "ok"


class FakeBrain:
    def __init__(self, reply=Reply("Открыто три задачи.")):
        self.calls, self.reply, self.notes = [], reply, []

    async def ask(self, user, text, channel="api"):
        self.calls.append((text, channel))
        return self.reply

    def journal_note(self, line):
        self.notes.append(line)


class Recorder(Notifier):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def send(self, text, title="Jarvis", **kw):
        self.sent.append((title, text))


class Confirm:
    def __init__(self, answer=True):
        self.answer, self.asked = answer, []

    async def ask(self, title, details="", channel="", **kw):
        self.asked.append((title, details, channel))
        return self.answer


class World:
    """Сценарии в пустом vault с поддельными домом, компьютером и агентом и ручными часами."""

    def __init__(self, tmp_path):
        self.vault = tmp_path / "vault"
        (self.vault / scenarios.DIR).mkdir(parents=True)
        self.now = NOW.timestamp()
        self.store = Store(tmp_path / "j.db")
        self.home, self.pc, self.brain, self.notifier, self.confirm = FakeHome(), FakePC(), FakeBrain(), Recorder(), Confirm()
        self.sched = Scheduler(self.store, clock=lambda: self.now)
        self.s = Scenarios(self.vault, self.store, home=self.home, computer=self.pc, scheduler=self.sched,
                           brain=self.brain, notifier=self.notifier, confirm=self.confirm, clock=lambda: self.now)
        self.notes, self.said = [], []
        self.s.journal = self.notes.append

    def write(self, name, text, age=3600.0):
        """Заметка сценария; age — сколько секунд назад её правили (по ручным часам)."""
        path = self.vault / scenarios.DIR / f"{name}.md"
        path.write_text(text, encoding="utf-8")
        import os
        os.utime(path, (self.now - age, self.now - age))
        return path

    def at(self, hour, minute, second=0, day=18):
        self.now = datetime(2026, 11, day, hour, minute, second).timestamp()

    async def tick(self):
        await self.s.tick()
        await asyncio.gather(*self.s._jobs)

    def journal(self, kind):
        return [(e["channel"], e["name"], e["payload"]) for e in reversed(self.store.recent()) if e["kind"] == kind]


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


async def collect(gen):
    return [x async for x in gen]


# --- разбор заметки ------------------------------------------------------------------------------

def test_steps_are_parsed_and_described_for_people():
    # YAML читает незакавыченные on и off как «да» и «нет» — это всё равно шаги дома
    on, off = parse_step({True: "Торшер"}), parse_step({False: {"name": "Свет", "area": "Зал", "domain": "light"}})
    assert on.kind == "on" and on.args == {"tool": "HassTurnOn", "args": {"name": "Торшер"}}
    assert on.text == "включить: Торшер" and off.text == "выключить: Свет (Зал)"
    assert off.args == {"tool": "HassTurnOff", "args": {"name": "Свет", "area": "Зал", "domain": ["light"]}}
    light = parse_step({"home": {"tool": "HassLightSet", "name": "Торшер", "brightness": 30}})
    assert light.args == {"tool": "HassLightSet", "args": {"name": "Торшер", "brightness": 30}}
    assert light.text == "дом, HassLightSet: Торшер, brightness 30"
    assert parse_step({"volume": 30}).args == {"action": "set", "value": 30}
    assert parse_step({"volume": "mute"}).text == "выключить звук"
    assert parse_step({"media": "next"}).text == "следующий трек"
    assert parse_step({"youtube": " лофи  радио "}).args == {"query": "лофи радио"}
    assert parse_step({"open": "music.yandex.ru"}).args == {"url": "https://music.yandex.ru"}
    assert parse_step({"say": "С возвращением"}).text == "сказать: С возвращением"
    assert parse_step({"ask": "Что в планах?"}).kind == "ask"


def test_timers_alarms_and_reminders_become_scheduler_calls():
    assert parse_step({"timer": "10m"}).args == {"kind": "timer", "in_seconds": 600}
    assert parse_step({"timer": 5}).text == "таймер на 5 минут"                    # число без единицы — минуты
    assert parse_step({"timer": {"in": "1ч 30м", "text": "пирог"}}).text == "таймер на 1 час 30 минут: пирог"
    assert parse_step({"alarm": "07:00"}).args == {"kind": "alarm", "at": "7:00"}
    # незакавыченное 7:30 YAML читает как число 450, а 23:00 — как 1380
    assert parse_step({"alarm": 450}).args["at"] == "7:30" and parse_step({"alarm": 1380}).args["at"] == "23:00"
    weekly = parse_step({"alarm": {"at": "7:00", "repeat": "weekdays"}})
    assert weekly.args == {"kind": "alarm", "at": "7:00", "repeat": "weekdays"}
    assert weekly.text == "будильник на 7:00, по будням"
    remind = parse_step({"remind": {"in": "30m", "text": "выпить таблетки"}})
    assert remind.args == {"kind": "reminder", "in_seconds": 1800, "text": "выпить таблетки"}
    assert remind.text == "напомнить через 30 минут: выпить таблетки"
    assert duration("90s") == 90 and duration("2 часа") == 7200 and duration("15") == 900
    assert clock("7.05") == (7, 5) and clock(7) == (7, 0)


@pytest.mark.parametrize("bad", [
    "say: привет", {"say": "a", "volume": 3}, {"свет": "да"}, {"on": ""}, {"on": {"entity": "x"}},
    {"home": {"name": "x"}}, {"volume": 130}, {"volume": True}, {"media": "rewind"}, {"open": "file:///etc/passwd"},
    {"timer": "скоро"}, {"timer": {"at": "7:00"}}, {"alarm": "25:00"}, {"alarm": {"in": "5m", "repeat": "daily"}},
    {"alarm": {"at": "7:00", "repeat": "иногда"}}, {"remind": {"at": "8:00"}}, {"say": ""}, {"say": "я" * 501},
])
def test_bad_steps_are_refused_with_a_reason(bad):
    with pytest.raises(ScenarioError):
        parse_step(bad)


def test_schedule_days_and_times():
    assert parse_times("пн-пт 07:30") == [(frozenset(range(5)), 7, 30)]
    assert parse_times("сб,вс 10:00") == parse_times("выходные в 10:00") == [(frozenset({5, 6}), 10, 0)]
    assert parse_times("23:00") == parse_times(1380) == parse_times("каждый день в 23:00") == [(frozenset(range(7)), 23, 0)]
    assert parse_times("по будням 8:00") == parse_times("weekdays 08:00") == [(frozenset(range(5)), 8, 0)]
    assert parse_times("пт-пн 9:00") == [(frozenset({4, 5, 6, 0}), 9, 0)]            # через выходные
    assert parse_times(["mon, среда 6:15", "вс 12:00"]) == [(frozenset({0, 2}), 6, 15), (frozenset({6}), 12, 0)]
    for bad in ("утром", "пн-пт", "ежедневно 24:00", "каждый понедельни 7:00x", "абв 7:00"):
        with pytest.raises(ScenarioError):
            parse_times(bad)
    sc = parse("Утро", {"schedule": "пн-пт 07:30", "steps": [{"say": "Доброе утро"}]})
    assert sc.schedule_text == "по будням в 7:30"
    assert sc.previous(NOW) == datetime(2026, 11, 18, 7, 30)                          # сегодня, среда
    assert sc.previous(datetime(2026, 11, 16, 7, 0)) == datetime(2026, 11, 13, 7, 30)  # утро понедельника → пятница


def test_scenario_needs_steps_and_sane_phrases():
    sc = parse("Я дома", {"phrases": "я дома", "steps": [{"say": "Привет"}]})   # одна фраза строкой — тоже список
    assert sc.phrases == ["я дома"] and sc.enabled and not sc.without_name and sc.times == []
    for meta in ({}, {"steps": []}, {"steps": "say"}, {"steps": [{"say": "a"}] * 21},
                 {"phrases": ["!!!"], "steps": [{"say": "a"}]}, {"phrases": [1], "steps": [{"say": "a"}]}):
        with pytest.raises(ScenarioError):
            parse("x", meta)


# --- заметки в vault и сверка фраз -----------------------------------------------------------------

def test_phrase_is_matched_whole_and_before_any_model(w):
    w.write("Я дома", HOME)
    w.write("_черновик", HOME)                              # с «_» — не сценарий
    assert [s.id for s in w.s.load()] == ["Я дома"]
    for said in ("Я дома!", "я  дома, пожалуйста", "я пришел.", "Я ПРИШЁЛ"):
        assert w.s.match(said).id == "Я дома"
    for said in ("я дома или нет", "я не дома", "дома", "", "..."):
        assert w.s.match(said) is None
    assert key("Включи кино") != key("Выключи кино")        # похожие по буквам команды не путаются
    assert w.s.get("я дома").id == w.s.get("Я пришёл").id == "Я дома" and w.s.get("нет такого") is None
    assert w.s.summary() == "- Я дома: «я дома», «Я пришёл»"
    assert w.s.public(w.s.load()[0])["steps"] == ["включить: Торшер", "выключить: Свет (Зал)", "громкость 30%",
                                                 "сказать: С возвращением"]


def test_without_the_name_only_marked_scenarios_and_only_long_phrases(w):
    w.write("Я дома", HOME)
    w.write("Ночь", "---\nphrases: [спокойной ночи, отбой]\nwithout_name: true\nsteps:\n  - say: Спокойной ночи\n---\n")
    assert w.s.match("Спокойной ночи.", bare=True).id == "Ночь"
    assert w.s.match("отбой", bare=True) is None            # одно слово без обращения — слишком легко задеть
    assert w.s.match("отбой").id == "Ночь"
    assert w.s.match("я дома", bare=True) is None           # сценарий не просил слушать без имени


def test_broken_disabled_and_duplicate_notes_do_not_run(w):
    w.write("А", HOME)
    w.write("Б", HOME.replace("Я пришёл", "я вернулся"))    # «я дома» уже занята
    w.write("В", "---\nphrases: [отбой]\nenabled: false\nsteps:\n  - say: Ночь\n---\n")
    w.write("Г", "---\nsteps: [oops\n---\n")
    w.write("Д", "---\nphrases: [утро]\nsteps:\n  - свет: да\n---\n")
    by = {s.id: s for s in w.s.load()}
    assert not by["А"].error and "уже занята сценарием «А»" in by["Б"].error
    assert by["Г"].error.startswith("frontmatter:") and "неизвестный шаг" in by["Д"].error
    assert w.s.match("я дома").id == "А" and w.s.match("я вернулся") is None and w.s.match("отбой") is None
    assert w.s.summary() == "- А: «я дома», «Я пришёл»"
    w.write("Б", HOME.replace('"я дома", "Я пришёл"', "я вернулся"), age=10)   # поправили — заметка перечитана
    assert w.s.match("я вернулся").id == "Б"


# --- выполнение ----------------------------------------------------------------------------------

async def test_steps_run_in_order_and_only_say_is_spoken(w):
    w.write("Я дома", HOME)
    assert await collect(w.s.run(w.s.get("Я дома"), "voice")) == ["С возвращением."]
    assert w.home.calls == [("intent__HassTurnOn", {"name": "Торшер"}),
                            ("intent__HassTurnOff", {"name": "Свет", "area": "Зал"})]
    assert w.pc.calls == [("pc_volume", {"target": "system", "action": "set", "value": 30})]
    assert w.notes == ["сценарий «Я дома» (voice)"]


async def test_failed_step_does_not_stop_the_rest_and_is_reported(w):
    w.home.fail = {"Торшер"}
    w.write("Ночь", "---\nsteps:\n  - off: Торшер\n  - youtube: сломано\n  - off: Свет\n  - timer: 10m\n"
                    "  - alarm: \"07:00\"\n  - say: Спокойной ночи\n---\n")
    out = await collect(w.s.run(w.s.get("Ночь"), "voice"))
    assert out[:3] == ["Поставил таймер на 10 минут.", "Будильник сработает завтра в 7 часов.", "Спокойной ночи."]
    assert isinstance(out[3], Failure) and not isinstance(out[2], Failure)
    assert out[3] == ("Не получилось: выключить: Торшер — No entities matched; "
                      "YouTube: сломано — yt-dlp не ответил вовремя.")
    assert w.home.calls[-1] == ("intent__HassTurnOff", {"name": "Свет"})          # следующий шаг выполнен
    assert [(i["kind"], i["channel"]) for i in w.sched.active()] == [("timer", "voice"), ("alarm", "voice")]


async def test_missing_integrations_are_named(w):
    w.write("Всё", "---\nsteps:\n  - on: Торшер\n  - home: {tool: HassVacuumStart, name: Пылесос}\n  - volume: 10\n"
                   "  - timer: 1m\n  - ask: Что нового?\n---\n")
    w.s.computer = w.s.scheduler = w.s.brain = None
    out = await collect(w.s.run(w.s.get("Всё"), "app"))
    assert len(out) == 1 and "нет инструмента HassVacuumStart" in out[0] and "JARVIS_PC_CONTROL" in out[0]
    assert "планировщик не подключён" in out[0] and "агент не подключён" in out[0]
    w.home.down = True
    assert "Home Assistant не подключён или недоступен" in (await collect(w.s.run(w.s.get("Всё"), "app")))[0]


async def test_ask_goes_to_the_agent_unless_the_agent_itself_started_it(w):
    w.write("Утро", "---\nsteps:\n  - ask: Какие задачи открыты?\n---\n")
    assert await w.s.execute(w.s.get("Утро"), "voice") == "Открыто три задачи."
    assert w.brain.calls[0][0].startswith("Какие задачи открыты?") and w.brain.calls[0][1] == "voice-agent"
    # из хода агента второй ход начать нельзя (общая сессия занята) — поручение возвращается ему же
    assert await w.s.execute(w.s.get("Утро"), "telegram", agent=True) == \
        "Поручение из сценария, выполни его: Какие задачи открыты?"
    assert len(w.brain.calls) == 1
    w.brain.reply = Reply("Лимит исчерпан", is_error=True)
    assert await w.s.execute(w.s.get("Утро"), "voice") == "Не получилось: поручить агенту: Какие задачи открыты? — Лимит исчерпан."


async def test_disabled_broken_and_running_scenarios_refuse(w):
    w.write("Выкл", "---\nenabled: false\nsteps:\n  - say: а\n---\n")
    w.write("Битый", "---\nsteps:\n  - свет: да\n---\n")
    w.write("Тихий", "---\nsteps:\n  - volume: 10\n---\n")
    for name, why in (("Выкл", "выключен"), ("Битый", "ошибка")):
        with pytest.raises(ScenarioError, match=why):
            await w.s.execute(w.s.get(name), "app")
    quiet = w.s.get("Тихий")
    assert await w.s.execute(quiet, "app") == "Готово."       # сказать нечего — инструменту всё равно нужен ответ
    w.s._running.add("Тихий")
    with pytest.raises(ScenarioError, match="уже выполняется"):
        await w.s.execute(quiet, "app")


async def test_phrase_from_a_text_channel_is_logged_like_a_tool_call(w):
    w.write("Я дома", HOME)
    assert await w.s.handle("owner", "как дела?", "telegram") is None and w.store.recent() == []
    assert await w.s.handle("owner", "Я дома!", "telegram") == "С возвращением."
    assert w.store.history("owner") == [("user", "Я дома!"), ("assistant", "С возвращением.")]
    assert w.journal("tool") == [("telegram", "scenario_run", '{"name": "Я дома"}')]
    assert w.journal("tool_result") == [("telegram", "scenario_run", '{"text": "С возвращением."}')]
    # кнопка в приложении: реплики пользователя нет, в чат ничего не пишется
    w.write("Тихий", "---\nsteps:\n  - volume: 10\n---\n")
    assert await collect(w.s.respond("owner", w.s.get("Тихий"), "app")) == ["Готово."]
    assert len(w.store.history("owner")) == 2
    w.s._running.add("Тихий")
    assert await collect(w.s.respond("owner", w.s.get("Тихий"), "app")) == ["Не получилось: сценарий «Тихий» уже выполняется."]
    assert '"is_error": true' in w.journal("tool_result")[-1][2]


# --- запуск по времени ---------------------------------------------------------------------------

MORNING = "---\nschedule: \"пн-пт 07:30\"\nsteps:\n  - volume: 20\n  - say: Доброе утро\n---\n"


async def test_scheduled_scenario_runs_once_at_its_time(w):
    w.write("Утро", MORNING, age=86400)
    heard = []

    async def announce(text):
        heard.append(text)
        return True

    w.s.announce = announce
    w.at(7, 29, 58)
    await w.tick()
    assert w.pc.calls == [] and heard == []                   # вчерашнее 7:30 было больше десяти минут назад
    w.at(7, 30, 3)
    await w.tick()
    assert w.pc.calls == [("pc_volume", {"target": "system", "action": "set", "value": 20})] and heard == ["Доброе утро."]
    assert w.notifier.sent == []                              # фразу услышали — сообщение не нужно
    assert w.journal("tool") == [("schedule", "scenario_run", '{"name": "Утро"}')]
    for second in (8, 13, 59):
        w.at(7, 30, second)
        await w.tick()
    w.at(7, 31)
    await w.tick()
    assert len(w.pc.calls) == 1 and heard == ["Доброе утро."]
    w.at(7, 30, 1, day=21)                                    # суббота — не будни
    await w.tick()
    assert len(w.pc.calls) == 1
    w.at(7, 30, 1, day=23)                                    # понедельник
    await w.tick()
    assert len(w.pc.calls) == 2


async def test_scheduled_scenario_is_not_run_late_or_retroactively(w):
    w.write("Утро", MORNING, age=86400)
    w.at(7, 39)                                               # ядро запустили в 7:39 — ещё можно
    await w.tick()
    assert len(w.pc.calls) == 1
    w.at(9, 0, day=19)                                        # в 7:30 ПК спал
    await w.tick()
    assert len(w.pc.calls) == 1
    w.at(7, 35, day=20)
    w.write("Утро", MORNING, age=60)                          # заметку правили в 7:34 — уже после срока
    await w.tick()
    assert len(w.pc.calls) == 1
    w.at(7, 30, 2, day=23)
    w.write("Утро", MORNING.replace("---\nschedule", "---\nenabled: false\nschedule"), age=86400)
    await w.tick()
    assert len(w.pc.calls) == 1                               # выключенный не запускается


async def test_scheduled_scenario_reports_by_message_when_nobody_hears(w):
    w.home.fail = {"Торшер"}
    w.write("Утро", "---\nschedule: \"07:30\"\nsteps:\n  - on: Торшер\n  - say: Доброе утро\n---\n", age=86400)
    w.at(7, 30, 1)
    await w.tick()                                            # голосового клиента нет
    assert w.notifier.sent == [("Сценарий «Утро»", "Доброе утро. Не получилось: включить: Торшер — No entities matched.")]

    async def announce(text):
        return True

    w.s.announce, w.notifier.sent = announce, []
    w.at(7, 30, 1, day=19)
    await w.tick()                                            # фразу сказали вслух, но о сбое — ещё и сообщением
    assert w.notifier.sent == [("Сценарий «Утро»", "Не получилось: включить: Торшер — No entities matched.")]


async def test_scheduler_ticks_scenarios_and_survives_their_errors(w):
    seen = []

    async def boom():
        seen.append("boom")
        raise RuntimeError("сломалось")

    w.sched.hooks += [boom, w.s.tick]
    w.write("Утро", MORNING, age=86400)
    w.at(7, 30, 1)
    await w.sched.tick()
    await asyncio.gather(*w.s._jobs)
    assert seen == ["boom"] and len(w.pc.calls) == 1


# --- сохранение агентом --------------------------------------------------------------------------

NIGHT = {"name": "Спокойной ночи", "phrases": ["спокойной ночи"], "without_name": True,
         "steps": [{"off": {"name": "Свет", "area": "Зал"}}, {"alarm": "07:00"}, {"say": "Спокойной ночи"}]}


async def test_agent_saves_a_scenario_only_after_confirmation(w):
    out = await w.s.save(NIGHT, "telegram")
    assert out == "Сценарий «Спокойной ночи» сохранён: 50_Scenarios/Спокойной ночи.md"
    title, details, channel = w.confirm.asked[0]
    assert title == "Создать сценарий «Спокойной ночи»?" and channel == "telegram"
    assert details == ("Фразы: «спокойной ночи» — срабатывают и без обращения по имени\n"
                       "1. выключить: Свет (Зал)\n2. будильник на 7:00\n3. сказать: Спокойной ночи")
    sc = w.s.match("Спокойной ночи", bare=True)
    assert sc.id == "Спокойной ночи" and [s.kind for s in sc.steps] == ["off", "alarm", "say"]
    # замена: тот же сценарий, текст заметки под frontmatter остаётся
    path = w.vault / sc.file
    path.write_text(path.read_text(encoding="utf-8") + "\nМои пометки.\n", encoding="utf-8")
    await w.s.save({"name": "спокойной ночи", "schedule": "23:00", "enabled": False, "steps": [{"say": "Отбой"}]}, "app")
    assert w.confirm.asked[1][0] == "Изменить сценарий «Спокойной ночи»?"
    assert "По времени: каждый день в 23:00\nВыключен\n1. сказать: Отбой" == w.confirm.asked[1][1]
    assert [s.id for s in w.s.load()] == ["Спокойной ночи"] and "Мои пометки." in path.read_text(encoding="utf-8")
    assert w.s.load()[0].enabled is False and w.s.load()[0].phrases == []


async def test_refused_or_invalid_scenario_is_not_written(w):
    w.write("Я дома", HOME)
    folder = w.vault / scenarios.DIR
    cases = [
        ({**NIGHT, "name": "../etc"}, "name"),
        ({**NIGHT, "name": "_скрытый"}, "name"),
        ({**NIGHT, "steps": [{"свет": "да"}]}, "неизвестный шаг"),
        ({**NIGHT, "phrases": ["Я дома"]}, "уже есть у сценария «Я дома»"),
        ({**NIGHT, "steps": [{"home": {"tool": "HassVacuumStart", "name": "Пылесос"}}]}, "нет инструмента"),
    ]
    for args, why in cases:
        with pytest.raises(ScenarioError, match=why):
            await w.s.save(args, "telegram")
    assert w.confirm.asked == []                              # до вопроса пользователю дело не дошло
    w.confirm.answer = False
    with pytest.raises(ScenarioError, match="не подтвердил"):
        await w.s.save(NIGHT, "telegram")
    w.s.confirm = None
    with pytest.raises(ScenarioError, match="подтверждения"):
        await w.s.save(NIGHT, "telegram")
    w.s.confirm, w.home.down = Confirm(), True
    with pytest.raises(ScenarioError, match="Home Assistant"):
        await w.s.save(NIGHT, "telegram")
    assert sorted(p.name for p in folder.iterdir()) == ["Я дома.md"]


def test_switching_off_touches_one_line_and_keeps_comments(w):
    path = w.write("Я дома", HOME.replace("steps:", "# мой комментарий\nsteps:"))
    assert w.s.set_enabled("Я дома", False).enabled is False and w.s.match("я дома") is None
    text = path.read_text(encoding="utf-8")
    assert "# мой комментарий\n" in text and "enabled: false\n---\n# Я дома\n" in text
    assert w.s.set_enabled("Я дома", True).enabled is True and w.s.match("я дома").id == "Я дома"
    assert path.read_text(encoding="utf-8") == text.replace("enabled: false", "enabled: true")
    with pytest.raises(ScenarioError):
        w.s.set_enabled("нет такого", True)


async def test_agent_tools_and_vault_guard(w, tmp_path):
    w.write("Я дома", HOME)
    listing, run, save = scenario_tools(w.s, "telegram")
    assert [t.name for t in (listing, run, save)] == ["scenario_list", "scenario_run", "scenario_save"]
    text = (await listing.handler({}))["content"][0]["text"]
    assert text.startswith("Я дома\nФразы: «я дома», «Я пришёл»\n1. включить: Торшер")
    assert await run.handler({"name": "я дома"}) == {"content": [{"type": "text", "text": "С возвращением."}]}
    bad = await run.handler({"name": "нет такого"})
    assert bad["is_error"] is True and "scenario_list" in bad["content"][0]["text"]
    ok = await save.handler(NIGHT)
    assert "сохранён" in ok["content"][0]["text"] and w.confirm.asked[0][2] == "telegram"
    # напрямую заметки сценариев агент не правит: иначе шаги появились бы без проверки и подтверждения
    vault = w.vault.resolve()
    for tool in ("Write", "Edit"):
        assert "scenario_save" in check_path(vault, tool, {"file_path": "50_Scenarios/Я дома.md"})
    assert check_path(vault, "Read", {"file_path": "50_Scenarios/Я дома.md"}) is None
    assert "scenario_save" in integrations_hint(Settings(), scenes=True)
    assert "scenario_save" not in integrations_hint(Settings())


# --- быстрый роутер ------------------------------------------------------------------------------

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


def use(name, args, id_="tu_1"):
    return NS(type="tool_use", id=id_, name=name, input=args)


def router(w, client):
    s = replace(Settings(), vault=w.vault.resolve(), hass_url="", hass_token="", notion_token="", open_roots=())
    return VoiceRouter(s, w.brain, w.store, Notifier(), client=client, scenes=w.s)


async def say(r, text):
    return "".join([x async for x in r.respond("owner", text)])


async def test_router_runs_a_scenario_by_meaning_in_one_model_call(w):
    w.write("Я дома", HOME)
    client = Client(([], "tool_use", [use("scenario_run", {"name": "Я дома"})]))
    r = router(w, client)
    assert await say(r, "я вернулся домой") == "С возвращением."
    assert len(client.calls) == 1 and len(w.home.calls) == 2
    kw = client.calls[0]
    assert [t["name"] for t in kw["tools"]] == ["delegate", "scenario_run"]
    assert "- Я дома: «я дома», «Я пришёл»" in kw["system"][0]["text"] and "не вызвав его" in kw["system"][0]["text"]
    # вызов и то, что сценарий сказал, остаются в истории следующих реплик
    past = [b for m in r.history("owner", {"scenario_run"}) if not isinstance(m["content"], str) for b in m["content"]]
    assert past[0]["input"] == {"name": "Я дома"} and past[1]["content"] == "С возвращением."


async def test_router_explains_a_missing_scenario_and_hides_the_tool_when_there_are_none(w):
    w.write("Я дома", HOME)
    client = Client(([], "tool_use", [use("scenario_run", {"name": "Кино"})]), (["Такого сценария нет."], "end_turn", []))
    assert await say(router(w, client), "включи сценарий кино") == "Такого сценария нет."
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    (w.vault / scenarios.DIR / "Я дома.md").unlink()
    client = Client((["Привет."], "end_turn", []))
    await say(router(w, client), "привет")
    assert [t["name"] for t in client.calls[0]["tools"]] == ["delegate"] and "Сценарии" not in client.calls[0]["system"][0]["text"]


# --- голос ---------------------------------------------------------------------------------------

class Speech:
    enabled, tts_rate, local = True, 16000, True

    def __init__(self, heard=""):
        self.heard = heard

    async def recognize(self, audio, fmt="oggopus", sample_rate=None, hint=""):
        return self.heard

    async def synthesize(self, text):
        return text.encode()


class ModelRouter:
    def __init__(self):
        self.seen = []

    async def respond(self, user, text, channel="voice"):
        self.seen.append(text)
        yield "Ответ модели."


def voice_app(w, speech, model):
    app = FastAPI()
    svc = VoiceService(model, speech, wake=WakeWords(["клод"]), scenarios=w.s)

    @app.websocket("/ws")
    async def ep(ws: WebSocket):
        await ws.accept()
        await VoiceSession(ws, svc).run()

    return TestClient(app), svc


def turn(ws, heard=None, speech=None, text=None):
    if text is not None:
        ws.send_json({"type": "text", "text": text})
    else:
        speech.heard = heard
        ws.send_json({"type": "passive"})
        ws.send_bytes(b"\0\0" * 1600)
    events = []
    while True:
        msg = ws.receive()
        if msg.get("bytes") is not None:
            continue
        import json
        events.append(json.loads(msg["text"]))
        if events[-1]["type"] == "done":
            return events


def test_voice_runs_a_scenario_phrase_without_the_model(w):
    w.write("Я дома", HOME)
    w.write("Ночь", "---\nphrases: [спокойной ночи]\nwithout_name: true\nsteps:\n  - off: Свет\n  - say: Спокойной ночи\n---\n")
    speech, model = Speech(), ModelRouter()
    client, _ = voice_app(w, speech, model)
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ev = turn(ws, "Клод, я дома.", speech)
        assert ev[1] == {"type": "transcript", "turn": 1, "text": "Клод, я дома.", "wake": True}
        assert [e["text"] for e in ev if e["type"] == "text"] == ["С возвращением."]
        assert len(w.home.calls) == 2 and model.seen == []
        assert [e["type"] for e in turn(ws, "Я дома.", speech)] == ["start", "ignored", "done"]   # без имени — мимо
        ev = turn(ws, "Спокойной ночи!", speech)                                              # этому имя не нужно
        assert ev[1]["wake"] is True and [e["text"] for e in ev if e["type"] == "text"] == ["Спокойной ночи."]
        assert w.home.calls[-1] == ("intent__HassTurnOff", {"name": "Свет"})
        ev = turn(ws, text="я пришёл")                                                        # продолжение разговора
        assert [e["text"] for e in ev if e["type"] == "text"] == ["С возвращением."]
        ev = turn(ws, "Клод, я дома или нет?", speech)                                        # не фраза — к модели
        assert [e["text"] for e in ev if e["type"] == "text"] == ["Ответ модели."] and model.seen == ["я дома или нет?"]
    assert w.store.history("owner")[:2] == [("user", "я дома."), ("assistant", "С возвращением.")]


def test_voice_announces_a_scheduled_phrase_to_subscribed_clients(w):
    client, svc = voice_app(w, Speech(), ModelRouter())
    with client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        assert client.portal.call(svc.announce, "Доброе утро.") is False      # клиент не подписан — сказать некому
        ws.send_json({"type": "subscribe"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        assert client.portal.call(svc.announce, "Доброе утро.") is True
        assert ws.receive_json() == {"type": "say", "id": 0, "text": "Доброе утро.", "chime": False}
        assert ws.receive_json() == {"type": "audio", "alert": 0, "bytes": len("Доброе утро.".encode())}
        assert ws.receive_bytes() == "Доброе утро.".encode()


# --- API -----------------------------------------------------------------------------------------

@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "t0k")
    monkeypatch.setenv("JARVIS_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "j.db"))
    for name in ("TELEGRAM_BOT_TOKEN", "NTFY_URL", "HASS_URL", "HASS_TOKEN", "JARVIS_PC_CONTROL"):
        monkeypatch.setenv(name, "")
    import jarvis.api as api

    api = importlib.reload(api)
    with TestClient(api.app) as client:
        yield api, client


H = {"Authorization": "Bearer t0k"}


def test_api_lists_runs_and_switches_scenarios(core, tmp_path):
    api, client = core
    assert client.get("/scenarios").status_code == 401
    assert client.get("/scenarios", headers=H).json() == []       # шаблон из vault сценарием не считается
    folder = tmp_path / "vault" / "50_Scenarios"
    assert (folder / "_template.md").exists()
    (folder / "Таймер чая.md").write_text(
        "---\nphrases: [завари чай]\nsteps:\n  - timer: 4m\n  - say: Засёк четыре минуты\n---\n", encoding="utf-8")
    item, = client.get("/scenarios", headers=H).json()
    assert item == {"id": "Таймер чая", "phrases": ["завари чай"], "schedule": "", "enabled": True,
                    "without_name": False, "steps": ["таймер на 4 минуты", "сказать: Засёк четыре минуты"],
                    "error": "", "file": "50_Scenarios/Таймер чая.md"}
    ran = client.post("/scenarios/Таймер чая/run", headers=H).json()
    assert ran == {"ok": True, "text": "Поставил таймер на 4 минуты. Засёк четыре минуты."}
    assert [i["title"] for i in client.get("/schedule", headers=H).json()] == ["Таймер на 4 минуты"]
    assert client.post("/scenarios/Нет такого/run", headers=H).status_code == 404
    # фраза из чата приложения выполняется сразу, без агента
    api.brain.ask = None
    said = client.post("/chat", headers=H, json={"text": "Завари чай!", "channel": "app"}).json()
    assert said["text"] == "Поставил таймер на 4 минуты. Засёк четыре минуты." and said["is_error"] is False
    off = client.post("/scenarios/Таймер чая/enabled", headers=H, json={"enabled": False}).json()
    assert off["enabled"] is False
    assert client.post("/scenarios/Таймер чая/run", headers=H).json()["text"] == "Не получилось: сценарий «Таймер чая» выключен."
    assert client.post("/scenarios/Нет такого/enabled", headers=H, json={"enabled": True}).status_code == 404
