"""Сессия агента: цена хода и когда разговор начинается с чистого листа."""
import asyncio
import sqlite3
import time
from dataclasses import replace

from jarvis.agent import Brain, Reply
from jarvis.config import Settings
from jarvis.store import Store

OFF = dict(hass_url="", hass_token="", notion_token="", notion_parent="", github_token="", open_roots=(),
           pc_control=False, screen=False)


def make(tmp_path, totals, **kw):
    """Агент с подменённым ходом: каждый ход возвращает очередной итог сессии, как это делает Agent SDK."""
    s = replace(Settings(), vault=(tmp_path / "vault").resolve(), db=tmp_path / "j.db", **{**OFF, **kw})
    b, calls, left = Brain(s, Store(s.db)), [], list(totals)

    async def run(user_id, text, channel, resume, recap=""):
        calls.append({"resume": resume, "recap": recap})
        total = left.pop(0)
        if isinstance(total, Exception):
            raise total
        return Reply("ок", "new" if resume is None else resume, total)

    b._run = run
    return b, calls


def age(b: Brain, seconds: float) -> None:
    b.store.db.execute("UPDATE sessions SET updated_at = updated_at - ?", (seconds,))


def ask(b: Brain, text: str = "привет") -> Reply:
    return asyncio.run(b.ask("owner", text, "api"))


def test_turn_cost_is_the_difference_between_session_totals(tmp_path):
    b, calls = make(tmp_path, [0.03, 0.05, 0.057])
    assert [ask(b).cost_usd for _ in range(3)] == [0.03, 0.02, 0.007]
    assert [c["resume"] for c in calls] == [None, "new", "new"]
    assert b.store.session("owner")[2] == 0.057                       # в базе — итог сессии
    costs = [e["payload"] for e in b.store.recent() if e["kind"] == "assistant"]
    assert '"cost_usd": 0.007' in costs[0] and '"cost_usd": 0.02' in costs[1]


def test_total_smaller_than_before_is_taken_as_the_turn_cost(tmp_path):
    b, _ = make(tmp_path, [0.05, 0.01])  # итог не продолжился — считаем его ценой хода, а не уходим в минус
    assert [ask(b).cost_usd for _ in range(2)] == [0.05, 0.01]


def test_long_pause_starts_a_new_session_with_a_recap(tmp_path):
    b, calls = make(tmp_path, [0.03, 0.06, 0.03])
    ask(b, "запиши идею про теплицу")
    age(b, 10 * 60)
    ask(b, "и ещё одну")                                   # пауза меньше порога — сессия та же, справки нет
    assert calls[1] == {"resume": "new", "recap": ""}
    age(b, 31 * 60)
    r = ask(b, "что я записывал?")
    assert calls[2]["resume"] is None and r.cost_usd == 0.03   # новая сессия: цена не вычитается из старого итога
    assert calls[2]["recap"].splitlines() == ["Пользователь: запиши идею про теплицу", "Ты: ок",
                                               "Пользователь: и ещё одну", "Ты: ок"]


def test_recap_skips_old_talk_and_is_absent_after_explicit_new(tmp_path):
    b, calls = make(tmp_path, [0.03, 0.03, 0.03, 0.03])
    ask(b, "старое")
    b.store.db.execute("UPDATE journal SET ts = ts - 7 * 3600")
    age(b, 7 * 3600)
    ask(b, "новое")
    assert calls[1] == {"resume": None, "recap": ""}       # вчерашнее не пересказываем
    b.new_session("owner")
    ask(b, "с нуля")
    assert calls[2] == {"resume": None, "recap": ""}       # «новый разговор» — без справки о прошлом
    age(b, 31 * 60)
    ask(b, "x" * 1000 + "  \n хвост")
    assert all(len(line) <= 320 for line in calls[3]["recap"].splitlines())


def test_idle_reset_can_be_switched_off(tmp_path):
    b, calls = make(tmp_path, [0.03, 0.06], session_idle=0)
    ask(b); age(b, 86400); ask(b)
    assert calls[1]["resume"] == "new"


def test_broken_session_restarts_and_cost_is_not_subtracted(tmp_path):
    b, calls = make(tmp_path, [0.2, RuntimeError("нет такой сессии"), 0.03])
    ask(b)
    r = ask(b)
    assert [c["resume"] for c in calls] == [None, "new", None] and r.cost_usd == 0.03 and not r.is_error


def test_old_database_gets_the_cost_column(tmp_path):
    db = sqlite3.connect(tmp_path / "j.db")
    db.execute("CREATE TABLE sessions(user_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at REAL NOT NULL)")
    db.execute("INSERT INTO sessions VALUES('owner', 'abc', ?)", (time.time(),))
    db.commit(); db.close()
    s = Store(tmp_path / "j.db")
    assert s.session("owner")[0] == "abc" and s.session("owner")[2] == 0
    s.set_session("owner", "abc", 0.4)
    assert s.get_session("owner") == "abc" and s.session("owner")[2] == 0.4
    Store(tmp_path / "j.db")                               # повторное открытие не падает
