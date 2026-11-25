"""Календари через Home Assistant: Google Calendar (интеграция HA) и любые другие календари HA.

OAuth Google проводит и хранит сам Home Assistant; ядро ходит в HA тем же HASS_TOKEN.
Умеет читать события за период и добавлять новые. Удалять и переносить не умеет намеренно:
это добавляющий доступ, как Notion и 00_Inbox.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import quote

import httpx

CREATE_EVENT = 1          # бит supported_features у сущности calendar.* в Home Assistant
MAX_DAYS = 62
MAX_EVENTS = 60
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


class CalendarError(Exception):
    pass


def _local(dt: datetime) -> datetime:
    """Время без зоны считаем местным временем этого компьютера."""
    return dt.astimezone() if dt.tzinfo is None else dt


def parse_when(text: str) -> tuple[datetime | date, bool]:
    """«2026-11-26», «2026-11-26T15:00», «2026-11-26 15:00» → (значение, весь_день)."""
    text = (text or "").strip()
    if not text:
        raise CalendarError("не указано время")
    try:
        if len(text) == 10:
            return date.fromisoformat(text), True
        return _local(datetime.fromisoformat(text.replace(" ", "T", 1))), False
    except ValueError as e:
        raise CalendarError(f"не понял время «{text[:40]}»: нужно ГГГГ-ММ-ДД или ГГГГ-ММ-ДДTЧЧ:ММ") from e


def _event_start(ev: dict) -> tuple[datetime, bool]:
    s = ev.get("start") or {}
    if "dateTime" in s:
        return _local(datetime.fromisoformat(s["dateTime"])), False
    d = date.fromisoformat(s.get("date", "1970-01-01"))
    return datetime(d.year, d.month, d.day).astimezone(), True


def _event_end(ev: dict) -> datetime | None:
    e = ev.get("end") or {}
    if "dateTime" in e:
        return _local(datetime.fromisoformat(e["dateTime"]))
    return None


def format_event(ev: dict, calendar: str) -> str:
    start, all_day = _event_start(ev)
    day = f"{WEEKDAYS[start.weekday()]} {start:%d.%m}"
    if all_day:
        when = f"{day}, весь день"
    else:
        end = _event_end(ev)
        when = f"{day} {start:%H:%M}" + (f"–{end:%H:%M}" if end and end.date() == start.date() else "")
    line = f"{when} — {ev.get('summary') or '(без названия)'}"
    if ev.get("location"):
        line += f" @ {ev['location']}"
    line += f" [{calendar}]"
    if ev.get("description"):
        line += "\n    " + " ".join(str(ev["description"]).split())[:200]
    return line


class Calendars:
    def __init__(self, url: str, token: str, default: str = "", client: httpx.AsyncClient | None = None):
        self.url, self.token, self.default = url.rstrip("/"), token, default
        self._client = client or httpx.AsyncClient(timeout=15)

    async def _req(self, method: str, path: str, **kw):
        try:
            r = await self._client.request(method, self.url + path,
                                           headers={"Authorization": f"Bearer {self.token}"}, **kw)
        except httpx.HTTPError as e:
            raise CalendarError(f"Home Assistant недоступен: {e}") from e
        if r.status_code >= 400:
            raise CalendarError(f"Home Assistant ответил {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    async def calendars(self) -> list[dict]:
        """[{entity_id, name, writable}] — все календари Home Assistant."""
        found = await self._req("GET", "/api/calendars") or []
        out = []
        for c in found:
            state = await self._req("GET", f"/api/states/{c['entity_id']}")
            feats = int(((state or {}).get("attributes") or {}).get("supported_features") or 0)
            out.append({"entity_id": c["entity_id"], "name": c.get("name") or c["entity_id"],
                        "writable": bool(feats & CREATE_EVENT)})
        if not out:
            raise CalendarError("в Home Assistant нет ни одного календаря — подключите интеграцию Google Calendar")
        return out

    def _pick(self, cals: list[dict], name: str) -> list[dict]:
        if not name:
            return cals
        low = name.strip().lower()
        hit = [c for c in cals if low in c["name"].lower() or low == c["entity_id"].lower()]
        if not hit:
            raise CalendarError(f"нет календаря «{name}». Есть: " + ", ".join(c["name"] for c in cals))
        return hit

    async def events(self, start: str = "", days: int = 1, calendar: str = "", query: str = "") -> str:
        """События за days дней с даты start (по умолчанию — с сегодняшнего дня)."""
        days = max(1, min(int(days or 1), MAX_DAYS))
        first = parse_when(start)[0] if start else date.today()
        if isinstance(first, datetime):
            first = first.date()
        begin = datetime(first.year, first.month, first.day).astimezone()
        end = begin + timedelta(days=days)
        cals = self._pick(await self.calendars(), calendar)
        rows: list[tuple[datetime, str]] = []
        q = query.strip().lower()
        for c in cals:
            params = {"start": begin.isoformat(), "end": end.isoformat()}
            for ev in await self._req("GET", f"/api/calendars/{quote(c['entity_id'])}", params=params) or []:
                text = f"{ev.get('summary', '')} {ev.get('description', '')} {ev.get('location', '')}".lower()
                if q and q not in text:
                    continue
                rows.append((_event_start(ev)[0], format_event(ev, c["name"])))
        rows.sort(key=lambda r: r[0])
        period = f"{begin:%d.%m}" + (f"–{end - timedelta(days=1):%d.%m}" if days > 1 else "")
        if not rows:
            return f"Событий за {period} нет" + (f" по запросу «{query}»" if q else "") + "."
        lines = [r[1] for r in rows[:MAX_EVENTS]]
        more = f"\n…и ещё {len(rows) - MAX_EVENTS}" if len(rows) > MAX_EVENTS else ""
        return f"События за {period}:\n" + "\n".join(lines) + more

    async def add(self, summary: str, start: str, end: str = "", calendar: str = "",
                  description: str = "", location: str = "") -> str:
        summary = " ".join((summary or "").split())[:200]
        if not summary:
            raise CalendarError("нет названия события")
        begin, all_day = parse_when(start)
        cals = [c for c in await self.calendars() if c["writable"]]
        if not cals:
            raise CalendarError("ни в один календарь нельзя записывать: в интеграции Google Calendar "
                                "в Home Assistant нужен доступ на чтение и запись")
        target = self._pick(cals, calendar or self.default)[0]
        data: dict = {"entity_id": target["entity_id"], "summary": summary}
        if description:
            data["description"] = description[:2000]
        if location:
            data["location"] = location[:200]
        if all_day:
            stop = parse_when(end)[0] if end else begin + timedelta(days=1)
            if isinstance(stop, datetime):
                stop = stop.date()
            if stop <= begin:  # в HA конец дня — исключительный: событие на 26.11 = с 26.11 по 27.11
                stop = begin + timedelta(days=1)
            data |= {"start_date": begin.isoformat(), "end_date": stop.isoformat()}
            when = f"{begin:%d.%m}, весь день"
        else:
            stop = parse_when(end)[0] if end else begin + timedelta(hours=1)
            if not isinstance(stop, datetime) or stop <= begin:
                raise CalendarError("конец события должен быть позже начала")
            data |= {"start_date_time": begin.isoformat(timespec="minutes"),
                     "end_date_time": stop.isoformat(timespec="minutes")}
            when = f"{begin:%d.%m %H:%M}–{stop:%H:%M}"
        await self._req("POST", "/api/services/calendar/create_event", json=data)
        return f"Добавлено в «{target['name']}»: {when} — {summary}"
