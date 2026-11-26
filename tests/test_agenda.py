import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from jarvis import agenda
from jarvis.agent import CALENDAR_HINT, calendar_on, integrations_hint
from jarvis.config import Settings


def fake_ha(events: dict, writable=("calendar.work",), log=None):
    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer tok"
        path = req.url.path
        if path == "/api/calendars":
            return httpx.Response(200, json=[{"entity_id": e, "name": e.split(".")[1].title()} for e in events])
        if path.startswith("/api/states/"):
            ent = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"attributes": {"supported_features": 7 if ent in writable else 0}})
        if path.startswith("/api/calendars/"):
            ent = path.rsplit("/", 1)[1]
            assert "start" in req.url.params and "end" in req.url.params
            return httpx.Response(200, json=events[ent])
        if path == "/api/services/calendar/create_event":
            log.append(json.loads(req.content))
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def cal(events, **kw):
    log = []
    return agenda.Calendars("http://ha:8123/", "tok", client=fake_ha(events, log=log, **kw)), log


EVENTS = {
    "calendar.work": [
        {"summary": "Созвон с клиникой", "start": {"dateTime": "2026-11-26T15:00:00+05:00"},
         "end": {"dateTime": "2026-11-26T16:00:00+05:00"}, "location": "Zoom"},
        {"summary": "Лекция Vue 3", "start": {"dateTime": "2026-11-26T09:30:00+05:00"},
         "end": {"dateTime": "2026-11-26T11:00:00+05:00"}, "description": "аудитория 204\nвзять ноутбук"},
    ],
    "calendar.family": [{"summary": "День рождения мамы", "start": {"date": "2026-11-26"}, "end": {"date": "2026-11-27"}}],
}


async def test_events_sorted_across_calendars():
    c, _ = cal(EVENTS)
    out = await c.events("2026-11-26", 1)
    lines = out.splitlines()
    assert lines[0] == "События за 26.11:"
    assert "весь день — День рождения мамы [Family]" in lines[1]  # события на весь день — первыми
    assert "09:30–11:00 — Лекция Vue 3 [Work]" in lines[2]
    assert "аудитория 204 взять ноутбук" in lines[3]
    assert "15:00–16:00 — Созвон с клиникой @ Zoom [Work]" in lines[4]


async def test_events_filter_and_calendar_choice():
    c, _ = cal(EVENTS)
    out = await c.events("2026-11-26", 3, calendar="work", query="клиник")
    assert "Созвон" in out and "Лекция" not in out and "26.11–28.11" in out
    assert "нет" in await c.events("2026-11-26", 1, query="отпуск")
    with pytest.raises(agenda.CalendarError, match="Есть: Work, Family"):
        await c.events("", 1, calendar="личный")


async def test_add_timed_defaults_to_one_hour():
    c, log = cal(EVENTS)
    out = await c.add("Стоматолог", "2026-11-27T10:00", location="ул. Абая 1")
    assert out == "Добавлено в «Work»: 27.11 10:00–11:00 — Стоматолог"
    sent = log[0]
    assert sent["entity_id"] == "calendar.work" and sent["location"] == "ул. Абая 1"
    start = datetime.fromisoformat(sent["start_date_time"])
    assert start.tzinfo is not None and start.hour == 10
    assert datetime.fromisoformat(sent["end_date_time"]) - start == timedelta(hours=1)


async def test_add_all_day_end_is_exclusive():
    c, log = cal(EVENTS)
    out = await c.add("Отпуск", "2026-12-28", "2026-12-28")
    assert "28.12, весь день" in out
    assert log[0]["start_date"] == "2026-12-28" and log[0]["end_date"] == "2026-12-29"


async def test_add_errors():
    c, _ = cal(EVENTS, writable=())
    with pytest.raises(agenda.CalendarError, match="чтение и запись"):
        await c.add("x", "2026-11-27T10:00")
    c, _ = cal(EVENTS)
    with pytest.raises(agenda.CalendarError, match="позже начала"):
        await c.add("x", "2026-11-27T10:00", "2026-11-27T09:00")
    with pytest.raises(agenda.CalendarError, match="не понял время"):
        await c.add("x", "завтра в 10")
    with pytest.raises(agenda.CalendarError, match="нет календаря"):
        await c.add("x", "2026-11-27T10:00", calendar="family")  # Family только на чтение


async def test_no_calendars():
    c, _ = cal({})
    with pytest.raises(agenda.CalendarError, match="Google Calendar"):
        await c.events()


def test_parse_when():
    assert agenda.parse_when("2026-11-26") == (date(2026, 11, 26), True)
    dt, all_day = agenda.parse_when("2026-11-26 15:30")
    assert not all_day and (dt.hour, dt.minute) == (15, 30) and dt.tzinfo is not None


def test_hint_only_with_home_assistant(monkeypatch):
    monkeypatch.setenv("HASS_URL", "")
    assert not calendar_on(Settings())
    monkeypatch.setenv("HASS_URL", "http://ha:8123")
    monkeypatch.setenv("HASS_TOKEN", "t")
    assert calendar_on(Settings()) and CALENDAR_HINT in integrations_hint(Settings())
    monkeypatch.setenv("JARVIS_CALENDAR", "0")
    assert not calendar_on(Settings())
