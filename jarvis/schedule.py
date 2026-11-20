"""Планировщик ядра: таймеры, напоминания и будильники.

Записи лежат в той же базе SQLite, что и журнал, поэтому переживают перезапуск ядра. Цикл run()
следит за сроками: напоминание произносится один раз, таймер и будильник звенят на голосовых
клиентах, пока их не выключат («стоп», Enter, кнопка в приложении или Telegram) или не отложат.
Инструменты TOOLS одинаковы для быстрого роутера и для агента.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from datetime import datetime, timedelta

from .store import Store
from .wake import distance

log = logging.getLogger("jarvis.schedule")

KINDS = ("timer", "reminder", "alarm")
KIND_NAMES = {"timer": "таймер", "reminder": "напоминание", "alarm": "будильник"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_GROUPS = {"daily": range(7), "weekdays": range(5), "weekends": (5, 6)}
RING_MAX = 600.0       # звенит не дольше: если дома никого, шуметь вечно незачем
MISSED_AFTER = 600.0   # таймер и будильник, опоздавшие сильнее (ядро не работало), уже не звенят
RECENT = 300.0         # «отложи» относится и к напоминанию, прозвучавшему не раньше
TICK = 5.0             # не спать дольше: после сна ПК часы уходят вперёд скачком
SNOOZE = 10            # минут, если не сказано иначе
# Подсказка распознаванию, пока звенит: одиночное «стоп» Whisper без неё слышит как «суп» или «топ».
# Именно в виде реплик: на 12 синтезах «стоп» узнан 12 раз, с описанием «команды: стоп, …» — 6.
STT_HINT = "Стоп. Хватит. Стоп! Выключи. Отложи на десять минут."
MAX_ACTIVE = 100
MAX_AHEAD = 366 * 86400

SET_TOOL = {
    "name": "schedule_set",
    "description": "Поставить таймер, напоминание или будильник. kind: timer — отсчёт, звенит, пока не "
                   "выключат; reminder — один раз произнесёт text в назначенное время; alarm — будильник, "
                   "звенит, пока не выключат. Время: in_seconds — через сколько секунд, или at — «ЧЧ:ММ» "
                   "(ближайшее такое время) либо «ГГГГ-ММ-ДД ЧЧ:ММ». repeat (для reminder и alarm): daily, "
                   "weekdays, weekends или дни через запятую: mon,tue,wed,thu,fri,sat,sun. text — о чём "
                   "напомнить; для таймера и будильника — только если пользователь сам дал название.",
    "input_schema": {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": list(KINDS)},
                       "in_seconds": {"type": "integer", "minimum": 1},
                       "at": {"type": "string"},
                       "repeat": {"type": "string"},
                       "text": {"type": "string"}},
        "required": ["kind"],
    },
}
LIST_TOOL = {
    "name": "schedule_list",
    "description": "Активные таймеры, напоминания и будильники с их id и временем срабатывания.",
    "input_schema": {"type": "object", "properties": {}},
}
CANCEL_TOOL = {
    "name": "schedule_cancel",
    "description": "Отменить таймер, напоминание или будильник. id — из schedule_list; если id неизвестен, "
                   "укажи kind и, при необходимости, слова из text. all: true — отменить все подходящие.",
    "input_schema": {
        "type": "object",
        "properties": {"id": {"type": "integer"},
                       "kind": {"type": "string", "enum": list(KINDS)},
                       "text": {"type": "string"},
                       "all": {"type": "boolean"}},
    },
}
DISMISS_TOOL = {
    "name": "schedule_dismiss",
    "description": "Выключить таймер или будильник, который звенит сейчас. snooze_minutes — не выключить, а "
                   "отложить на столько минут; так же откладывается только что прозвучавшее напоминание.",
    "input_schema": {
        "type": "object",
        "properties": {"snooze_minutes": {"type": "integer", "minimum": 1, "maximum": 720}},
    },
}
TOOLS = [SET_TOOL, LIST_TOOL, CANCEL_TOOL, DISMISS_TOOL]
NAMES = frozenset(t["name"] for t in TOOLS)
# Результат этих инструментов — готовая фраза: быстрый роутер произносит её сам, без второго запроса.
SPOKEN = frozenset({"schedule_set", "schedule_cancel", "schedule_dismiss"})


class ScheduleError(Exception):
    pass


# --- время словами --------------------------------------------------------------------------

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря")
ON_WEEKDAY = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")
BY_WEEKDAYS = ("понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def span_text(seconds: float) -> str:
    """Длительность в винительном падеже: «на 1 минуту», «через 2 часа 5 минут»."""
    total = max(1, round(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    parts = []
    if h:
        parts.append(f"{h} {plural(h, 'час', 'часа', 'часов')}")
    if m:
        parts.append(f"{m} {plural(m, 'минуту', 'минуты', 'минут')}")
    if s and not h:
        parts.append(f"{s} {plural(s, 'секунду', 'секунды', 'секунд')}")
    return " ".join(parts)


def clock_text(dt: datetime, spoken: bool = False) -> str:
    """«7:05» для экрана; «7 часов 5 минут» для озвучки — синтез читает «7:00» неразборчиво."""
    if not spoken:
        return f"{dt.hour}:{dt.minute:02d}"
    out = f"{dt.hour} {plural(dt.hour, 'час', 'часа', 'часов')}"
    return out + (f" {dt.minute} {plural(dt.minute, 'минуту', 'минуты', 'минут')}" if dt.minute else "")


def when_text(dt: datetime, now: datetime, spoken: bool = False) -> str:
    days = (dt.date() - now.date()).days
    date = f"{dt.day} {MONTHS[dt.month - 1]}"
    if 0 <= days <= 2:
        day = ("сегодня", "завтра", "послезавтра")[days]
    elif 2 < days < 7:
        day = f"{ON_WEEKDAY[dt.weekday()]}, {date},"
    else:
        day = date + (f" {dt.year} года" if dt.year != now.year else "")
    return f"{day} в {clock_text(dt, spoken)}"


def repeat_text(repeat: str) -> str:
    days = [int(x) for x in repeat.split(",") if x]
    if len(days) == 7:
        return "каждый день"
    if days == [0, 1, 2, 3, 4]:
        return "по будням"
    if days == [5, 6]:
        return "по выходным"
    return "по " + ", ".join(BY_WEEKDAYS[d] for d in days)


# --- разбор ввода ---------------------------------------------------------------------------

def parse_repeat(raw) -> str:
    """«weekdays» или «mon,wed» → «0,1,2,3,4» / «0,2» (понедельник — 0). Пусто — без повтора."""
    days: set[int] = set()
    for token in re.split(r"[,;\s]+", str(raw or "").strip().lower()):
        if not token:
            continue
        if token in DAY_GROUPS:
            days.update(DAY_GROUPS[token])
        elif token[:3] in DAYS:
            days.add(DAYS.index(token[:3]))
        else:
            raise ScheduleError("repeat: daily, weekdays, weekends или дни через запятую — mon,tue,wed,thu,fri,sat,sun")
    return ",".join(map(str, sorted(days)))


def parse_at(at: str, now: datetime) -> datetime:
    """«ЧЧ:ММ» — ближайшее такое время; «ГГГГ-ММ-ДД ЧЧ:ММ» — точная дата, она должна быть в будущем."""
    m = re.fullmatch(r"(?:(\d{4})-(\d{1,2})-(\d{1,2})[ T]+)?(\d{1,2})[:.](\d{2})(?::\d{2})?", str(at).strip())
    if not m:
        raise ScheduleError("at нужен как «ЧЧ:ММ» или «ГГГГ-ММ-ДД ЧЧ:ММ»")
    y, mo, d, h, mi = (int(x) if x else None for x in m.groups())
    try:
        if y is None:
            dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            return dt if dt > now else dt + timedelta(days=1)
        dt = datetime(y, mo, d, h, mi)
    except ValueError as e:
        raise ScheduleError(f"такого времени не бывает: {at}") from e
    if dt <= now:
        raise ScheduleError(f"это время уже прошло: сейчас {now:%Y-%m-%d %H:%M}")
    return dt


def next_day(dt: datetime, repeat: str, after: datetime) -> datetime:
    """Ближайшее время суток dt в разрешённый день недели, строго позже after."""
    days = {int(x) for x in repeat.split(",") if x}
    while dt <= after or dt.weekday() not in days:
        dt += timedelta(days=1)
    return dt


STOP = frozenset({"стоп", "стой", "хватит", "выключи", "выключай", "отключи", "останови", "замолчи",
                  "тихо", "достаточно", "stop"})
FILLER = frozenset({"будильник", "таймер", "напоминание", "звук", "его", "это", "все", "уже", "пожалуйста",
                    "спасибо", "джарвис", "ну", "да"})
LATER = frozenset({"позже", "попозже"})
NUMBERS = {"одну": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
           "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30,
           "сорок": 40, "пятьдесят": 50}
UNITS = frozenset({"на", "через", "еще", "минут", "минуту", "минуты", "минутку", "час", "часа", "часов", "полчаса"})


def parse_quick(text: str) -> tuple[str, int | None] | None:
    """Команда звенящему будильнику без модели: («stop», None), («snooze», минут) или None.

    Срабатывает, только если вся фраза состоит из слов команды: «выключи свет» сюда не попадёт.
    Вызывается, только пока что-то звенит, поэтому одиночное слово, похожее на «стоп», тоже принимается.
    """
    words = re.findall(r"\w+", text.lower().replace("ё", "е"))
    if not words or len(words) > 8:
        return None
    if len(words) == 1 and 3 <= len(words[0]) <= 5 and distance(words[0], "стоп") <= 2:
        return "stop", None  # короткое «стоп» распознаётся как «соп», «топ», «сопь» — пока звенит, это оно

    def snooze_word(w: str) -> bool:
        return w.startswith("отлож") or w in LATER

    if any(map(snooze_word, words)):
        if not all(snooze_word(w) or w.isdigit() or w in NUMBERS or w in UNITS or w in FILLER for w in words):
            return None
        n = next((int(w) if w.isdigit() else NUMBERS[w] for w in words if w.isdigit() or w in NUMBERS), None)
        if "полчаса" in words:
            minutes = 30
        elif any(w.startswith("час") for w in words):
            minutes = (n or 1) * 60
        else:
            minutes = n or SNOOZE
        return "snooze", max(1, min(720, minutes))
    if any(w in STOP for w in words) and all(w in STOP or w in FILLER for w in words):
        return "stop", None
    return None


# --- планировщик ----------------------------------------------------------------------------

COLUMNS = ("id", "kind", "text", "due", "span", "repeat", "state", "channel", "created", "fired")


class Scheduler:
    def __init__(self, store: Store, notifier=None, bus=None, clock=time.time, user: str = "owner"):
        self.store, self.db = store, store.db
        self.notifier, self.bus, self.clock, self.user = notifier, bus, clock, user
        self.sinks: list = []              # async fn(event): голосовые клиенты
        self.has_voice = lambda: False     # подключён ли клиент, который прозвенит сам
        self.journal = None                # fn(строка): дневной журнал в vault
        self.hooks: list = []              # async fn(): вызываются на каждом такте (сценарии по времени)
        self._wake = asyncio.Event()
        self._jobs: set[asyncio.Task] = set()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS schedule(
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', due REAL NOT NULL,
                span REAL, repeat TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
                channel TEXT, created REAL NOT NULL, fired REAL)"""
        )

    # --- записи -----------------------------------------------------------------------------
    def _rows(self, where: str, params: tuple = ()) -> list[dict]:
        cur = self.db.execute(f"SELECT {','.join(COLUMNS)} FROM schedule WHERE {where}", params)
        return [dict(zip(COLUMNS, r)) for r in cur]

    def get(self, item_id: int) -> dict | None:
        rows = self._rows("id=?", (item_id,))
        return rows[0] if rows else None

    def active(self) -> list[dict]:
        return self._rows("state IN ('pending','ringing') ORDER BY state DESC, due")

    def ringing(self) -> list[dict]:
        return self._rows("state='ringing' ORDER BY fired")

    def _set(self, item_id: int, **fields) -> None:
        self.db.execute(f"UPDATE schedule SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                        (*fields.values(), item_id))

    def add(self, kind: str, due: float, text: str = "", span: float | None = None, repeat: str = "",
            channel: str = "") -> dict:
        cur = self.db.execute(
            "INSERT INTO schedule(kind,text,due,span,repeat,channel,created) VALUES(?,?,?,?,?,?,?)",
            (kind, text, due, span, repeat, channel, self.clock()))
        self._wake.set()
        return self.get(cur.lastrowid)

    # --- как назвать запись -----------------------------------------------------------------
    @staticmethod
    def label(item: dict, spoken: bool = False) -> str:
        """«таймер на 10 минут», «будильник на 7:00», «напоминание: позвонить маме»."""
        text = f": {item['text']}" if item["text"] else ""
        if item["kind"] == "timer":
            return f"таймер на {span_text(item['span'] or 0)}{text}"
        if item["kind"] == "alarm":
            return f"будильник на {clock_text(datetime.fromtimestamp(item['due']), spoken)}{text}"
        return f"напоминание{text}"

    @classmethod
    def title(cls, item: dict, spoken: bool = False) -> str:
        """Что сказать и показать, когда запись сработала."""
        if item["kind"] == "reminder":
            return f"Напоминаю: {item['text']}" if item["text"] else "Напоминание"
        label = cls.label(item, spoken)
        return label[0].upper() + label[1:]

    def public(self, item: dict) -> dict:
        return {**{k: item[k] for k in ("id", "kind", "text", "due", "repeat", "state")}, "title": self.title(item)}

    def describe(self, item: dict, now: float) -> str:
        head = f"id {item['id']}: {KIND_NAMES[item['kind']]}"
        if item["kind"] == "timer":
            head += f" на {span_text(item['span'] or 0)}"
        if item["state"] == "ringing":
            tail = "звенит сейчас"
        elif item["kind"] == "timer":
            tail = f"сработает через {span_text(item['due'] - now)}"
        else:
            tail = when_text(datetime.fromtimestamp(item["due"]), datetime.fromtimestamp(now), spoken=True)
        if item["repeat"]:
            tail += f", {repeat_text(item['repeat'])}"
        return f"{head} — {tail}" + (f": {item['text']}" if item["text"] else "")

    # --- инструменты ------------------------------------------------------------------------
    async def call(self, name: str, args: dict, channel: str = "") -> str:
        """Выполняет инструмент и возвращает фразу-результат. Отказ — ScheduleError."""
        if name == "schedule_set":
            return self._tool_set(args, channel)
        if name == "schedule_list":
            now = self.clock()
            lines = [self.describe(i, now) for i in self.active()]
            return f"Сейчас {datetime.fromtimestamp(now):%Y-%m-%d %H:%M}.\n" + ("\n".join(lines) or "Ничего не запланировано.")
        if name == "schedule_cancel":
            return await self._tool_cancel(args, channel)
        if name == "schedule_dismiss":
            return await self.dismiss(args.get("snooze_minutes"), by=channel)
        raise ScheduleError(f"нет такого инструмента: {name}")

    def _tool_set(self, args: dict, channel: str) -> str:
        kind = args.get("kind")
        if kind not in KINDS:
            raise ScheduleError(f"kind должен быть одним из: {', '.join(KINDS)}")
        now = self.clock()
        now_dt = datetime.fromtimestamp(now)
        secs, at = args.get("in_seconds"), args.get("at")
        if secs is not None and (kind == "timer" or not at):
            try:
                secs = int(secs)
            except (TypeError, ValueError) as e:
                raise ScheduleError("in_seconds должен быть целым числом секунд") from e
            if not 1 <= secs <= MAX_AHEAD:
                raise ScheduleError("in_seconds — от одной секунды до года")
            due_dt, relative = now_dt + timedelta(seconds=secs), True
        elif at:
            due_dt, relative = parse_at(at, now_dt), False
        else:
            raise ScheduleError("нужно время: in_seconds или at")
        repeat = "" if kind == "timer" else parse_repeat(args.get("repeat"))
        if repeat:
            due_dt = next_day(due_dt, repeat, now_dt)
        if len(self.active()) >= MAX_ACTIVE:
            raise ScheduleError(f"уже запланировано {MAX_ACTIVE} записей — сначала отмени лишние")
        text = " ".join(str(args.get("text") or "").split()).rstrip(".!…")[:300]
        if text.lower().startswith(KIND_NAMES[kind]):
            text = ""  # модель повторила «таймер на 10 минут» — это не название
        due = due_dt.timestamp()
        item = self.add(kind, due, text, due - now if kind == "timer" else None, repeat, channel)
        self._note(f"поставлено: {self.label(item)}, {when_text(due_dt, now_dt)} ({channel})")
        if kind == "timer":
            return f"Поставил таймер на {span_text(due - now)}."
        when = f"через {span_text(due - now)}" if relative and not repeat else when_text(due_dt, now_dt, spoken=True)
        if kind == "alarm":
            if repeat:
                return (f"Будильник на {clock_text(due_dt, spoken=True)}, {repeat_text(repeat)}. "
                        f"Ближайший — {when_text(due_dt, now_dt, spoken=True)}.")
            return f"Будильник сработает {when}."
        tail = f": {text}." if text else "."
        if repeat:
            return (f"Буду напоминать {repeat_text(repeat)} в {clock_text(due_dt, spoken=True)}{tail} "
                    f"Ближайший раз — {when_text(due_dt, now_dt, spoken=True)}.")
        return f"Напомню {when}{tail}"

    async def _tool_cancel(self, args: dict, channel: str) -> str:
        items = self.active()
        if args.get("id") is not None:
            items = [i for i in items if i["id"] == args["id"]]
            if not items:
                raise ScheduleError(f"записи с id {args['id']} нет среди активных")
        else:
            words = str(args.get("text") or "").lower().split()
            items = [i for i in items if (not args.get("kind") or i["kind"] == args["kind"])
                     and all(w in i["text"].lower() for w in words)]
            if not items:
                raise ScheduleError("подходящих записей нет")
            if len(items) > 1 and not args.get("all"):
                now = self.clock()
                raise ScheduleError("подходит несколько, уточни id или передай all: true:\n"
                                    + "\n".join(self.describe(i, now) for i in items))
        for item in items:
            self._set(item["id"], state="cancelled")
            self._note(f"отменено: {self.label(item)} ({channel})")
            if item["state"] == "ringing":
                await self._emit(item, "stopped")
        self._wake.set()
        return "Отменил: " + ", ".join(self.label(i, spoken=True) for i in items) + "."

    # --- выключить и отложить ---------------------------------------------------------------
    async def stop(self, item_id: int, by: str = "") -> bool:
        item = self.get(item_id)
        if not item or item["state"] != "ringing":
            return False
        self._set(item_id, state="done")
        self._note(f"выключено: {self.label(item)} ({by})")
        await self._emit(item, "stopped")
        return True

    async def snooze(self, item_id: int, minutes: int = SNOOZE, by: str = "") -> bool:
        """Отложить то, что звенит или уже прозвучало: запись снова ждёт своего времени."""
        item = self.get(item_id)
        if not item or item["state"] not in ("ringing", "done", "missed"):
            return False
        was_ringing = item["state"] == "ringing"
        self._set(item_id, state="pending", due=self.clock() + minutes * 60, fired=None)
        self._note(f"отложено на {span_text(minutes * 60)}: {self.label(item)} ({by})")
        self._wake.set()
        if was_ringing:
            await self._emit(item, "snoozed")
        return True

    async def dismiss(self, snooze_minutes=None, by: str = "") -> str:
        """Всё, что звенит, выключить или отложить. Отложить можно и только что прозвучавшее напоминание."""
        items = self.ringing()
        if snooze_minutes is None:
            if not items:
                raise ScheduleError("сейчас ничего не звенит")
            for item in items:
                await self.stop(item["id"], by)
            return "Выключил."
        try:
            minutes = max(1, min(720, int(snooze_minutes)))
        except (TypeError, ValueError) as e:
            raise ScheduleError("snooze_minutes должен быть целым числом минут") from e
        if not items:
            items = self._rows("state='done' AND fired>=? ORDER BY fired DESC LIMIT 1", (self.clock() - RECENT,))
        if not items:
            raise ScheduleError("откладывать нечего: ничего не звенит и недавно не срабатывало")
        for item in items:
            await self.snooze(item["id"], minutes, by)
        return f"Отложил на {span_text(minutes * 60)}."

    async def quick(self, text: str, channel: str = "voice") -> str | None:
        """«Стоп», «хватит», «отложи на 10 минут», пока что-то звенит, — без модели. None — не команда."""
        if not self.ringing():
            return None
        command = parse_quick(text)
        if command is None:
            return None
        reply = await self.dismiss(command[1], by=channel)
        self.store.log(self.user, channel, "user", payload=text)
        self.store.log(self.user, channel, "assistant", payload={"text": reply})
        return reply

    # --- срабатывание -----------------------------------------------------------------------
    async def tick(self) -> None:
        now = self.clock()
        now_dt = datetime.fromtimestamp(now)
        for item in self._rows("state='pending' AND due<=? ORDER BY due", (now,)):
            late = now - item["due"]
            if item["repeat"]:  # следующий раз — отдельной записью, эта дальше живёт как разовая
                nxt = next_day(datetime.fromtimestamp(item["due"]), item["repeat"], now_dt)
                self.add(item["kind"], nxt.timestamp(), item["text"], item["span"], item["repeat"], item["channel"])
                self._set(item["id"], repeat="")
            title = self.title(item)
            if item["kind"] == "reminder":
                self._set(item["id"], state="done", fired=now)
                was = f" Было назначено на {clock_text(datetime.fromtimestamp(item['due']))}." if late > 120 else ""
                await self._emit(item, "said", suffix=was)
                self._notify(item, title + "." + was, priority=4)
            elif late > MISSED_AFTER:
                self._set(item["id"], state="missed", fired=now)
                self._notify(item, f"{title} — пропущен: в это время ядро не работало.", priority=4)
                self._note(f"пропущено: {self.label(item)} — ядро не работало")
                continue
            else:
                self._set(item["id"], state="ringing", fired=now)
                await self._emit(item, "ringing")
                if item["kind"] == "alarm" or not self.has_voice():
                    self._notify(item, title + ".", priority=5 if item["kind"] == "alarm" else 4, stop=True)
            self.store.log(self.user, "schedule", "alert", item["kind"], {"id": item["id"], "text": title})
            self._note(f"сработало: {self.label(item)}")
        for item in self._rows("state='ringing' AND fired<=?", (now - RING_MAX,)):
            self._set(item["id"], state="done")
            await self._emit(item, "stopped")
            if self.notifier:
                self._spawn(self.notifier.send(
                    f"{self.title(item)} — звенел {span_text(RING_MAX)}, никто не выключил. Остановил сам."))
        for hook in self.hooks:
            try:
                await hook()
            except Exception:
                log.exception("планировщик: ошибка в сценариях по времени")

    def _next_sleep(self) -> float:
        now = self.clock()
        due = self.db.execute("SELECT MIN(due) FROM schedule WHERE state='pending'").fetchone()[0]
        fired = self.db.execute("SELECT MIN(fired) FROM schedule WHERE state='ringing'").fetchone()[0]
        times = [t for t in (due, fired + RING_MAX if fired is not None else None) if t is not None]
        return max(0.05, min([TICK, *(t - now for t in times)]))

    async def run(self) -> None:
        """Цикл планировщика: работает, пока живо ядро."""
        while True:
            self._wake.clear()
            try:
                await self.tick()
            except Exception:
                log.exception("планировщик: ошибка при срабатывании")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._next_sleep())

    # --- кому сообщить ----------------------------------------------------------------------
    async def _emit(self, item: dict, state: str, suffix: str = "") -> None:
        event = {"type": "alert", "state": state, "id": item["id"], "kind": item["kind"], "due": item["due"],
                 "text": self.title(item) + suffix, "speech": self.title(item, spoken=True) + "."}
        if self.bus:
            self.bus.publish(event)
        for sink in self.sinks:
            try:
                await sink(event)
            except Exception:
                log.exception("планировщик: клиент не принял событие")

    def _notify(self, item: dict, text: str, priority: int, stop: bool = False) -> None:
        if self.notifier:
            self._spawn(self.notifier.alert(item["id"], text, priority=priority, stop=stop))

    def _spawn(self, coro) -> None:
        """Telegram и push не должны задерживать следующее срабатывание."""
        def done(t: asyncio.Task) -> None:
            self._jobs.discard(t)
            if not t.cancelled() and t.exception():
                log.error("планировщик: уведомление не отправлено: %r", t.exception())

        task = asyncio.get_running_loop().create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(done)

    def _note(self, line: str) -> None:
        if self.journal:
            with contextlib.suppress(Exception):
                self.journal(line)
