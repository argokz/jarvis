"""Сценарии: фраза, время или кнопка → цепочка действий без модели.

Сценарий — заметка `50_Scenarios/<название>.md` с YAML-frontmatter, как проект в реестре:

    phrases: ["я дома", "я пришёл"]   # фразы запуска: сверяются целиком и до модели
    schedule: "пн-пт 07:30"           # необязательно: запуск по времени
    without_name: true                # необязательно: срабатывать и без обращения по имени
    enabled: false                    # необязательно: выключить, не удаляя
    steps:
      - on: Торшер                    # умный дом: on / off — имя или {name, area}
      - home: {tool: HassLightSet, name: Торшер, brightness: 30}
      - volume: 30                    # компьютер: volume, media, youtube, open
      - music: моя волна              # музыка: запрос или {query, where: station}
      - timer: 10m                    # расписание: timer, alarm, remind
      - say: С возвращением           # произнести
      - ask: Какие задачи открыты?    # поручить агенту и произнести его ответ

Дом управляется только инструментами Assist — теми же, что у модели: устройство, скрытое от
ассистента, сценарию тоже недоступно. Агент меняет сценарии одним инструментом scenario_save,
и только после подтверждения пользователя: сценарий — это действия, которые потом выполняются
без присмотра модели.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import frontmatter

from . import music, pc, schedule, wake

log = logging.getLogger("jarvis.scenarios")

DIR = "50_Scenarios"
MAX_STEPS = 20
LATE = 600.0        # запуск по времени, опоздавший сильнее (ядро не работало, ПК спал), пропускается
BARE_WORDS = 2      # без обращения по имени срабатывают только фразы не короче двух слов
POLITE = frozenset({"пожалуйста"})
NAME = re.compile(r"[^\W_][\w \-]{0,59}")
ASK_SUFFIX = "\n\n(Поручение из сценария: ответь одной–тремя короткими фразами без разметки — ответ будет озвучен.)"
HOME_TOOLS = {"on": "HassTurnOn", "off": "HassTurnOff"}
TARGET_KEYS = ("name", "area", "floor", "domain")
MEDIA = {"pause": "пауза", "play": "продолжить воспроизведение", "toggle": "пауза или продолжить",
         "next": "следующий трек", "previous": "предыдущий трек", "stop": "выключить музыку"}

STEPS_HELP = (
    "Шаг — объект с одним ключом: {\"on\": \"Торшер\"} или {\"off\": {\"name\": \"Свет\", \"area\": \"Зал\"}} — "
    "включить, выключить устройство дома (название точно как в Home Assistant); {\"home\": {\"tool\": "
    "\"HassLightSet\", \"name\": \"Торшер\", \"brightness\": 30}} — любой другой инструмент дома; "
    "{\"volume\": 30} или \"mute\" / \"unmute\" — громкость компьютера; {\"media\": \"pause\"} — play, pause, "
    "toggle, next, previous, stop; {\"music\": \"что включить\"} или {\"music\": {\"query\": \"…\", \"where\": "
    "\"station\"}} — музыка; {\"youtube\": \"запрос\"} — видео; {\"open\": \"https://…\"}; {\"notify\": \"текст\"} — "
    "уведомление на экране; {\"timer\": \"10m\"}; "
    "{\"alarm\": \"07:00\"} или {\"alarm\": {\"at\": \"07:00\", \"repeat\": \"weekdays\"}}; {\"remind\": "
    "{\"at\": \"08:00\", \"text\": \"…\"}} или {\"in\": \"30m\"}; {\"say\": \"фраза\"} — произнести; "
    "{\"ask\": \"задача\"} — поручить агенту и произнести ответ."
)
RUN_TOOL = {
    "name": "scenario_run",
    "description": "Запустить сценарий пользователя. name — название сценария. Возвращает то, что сценарий произнёс.",
    "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
}
LIST_TOOL = {
    "name": "scenario_list",
    "description": "Сценарии пользователя: названия, фразы запуска, расписание и шаги.",
    "input_schema": {"type": "object", "properties": {}},
}
SAVE_TOOL = {
    "name": "scenario_save",
    "description": "Создать сценарий или заменить существующий с тем же названием; пользователь увидит шаги и "
                   "должен подтвердить. name — короткое название; phrases — фразы запуска без обращения по "
                   "имени; schedule — запуск по времени: «07:30», «пн-пт 07:30», «сб,вс 10:00»; without_name: "
                   "true — срабатывать, даже когда к ассистенту не обратились (только если пользователь прямо "
                   "просил); enabled: false — выключить. " + STEPS_HELP,
    "input_schema": {
        "type": "object",
        "properties": {"name": {"type": "string"},
                       "phrases": {"type": "array", "items": {"type": "string"}},
                       "schedule": {"type": "string"},
                       "without_name": {"type": "boolean"},
                       "enabled": {"type": "boolean"},
                       "steps": {"type": "array", "items": {"type": "object"}}},
        "required": ["name", "steps"],
    },
}
TOOLS = [LIST_TOOL, RUN_TOOL, SAVE_TOOL]


class ScenarioError(Exception):
    pass


class Failure(str):
    """Фраза о том, что часть шагов не выполнена: её не только произносят, но и присылают уведомлением."""


@dataclass(frozen=True)
class Step:
    kind: str
    args: dict
    text: str  # как показать человеку: «включить: Торшер»


@dataclass
class Scenario:
    id: str                                   # название: имя файла без расширения
    phrases: list[str] = field(default_factory=list)
    keys: set[tuple[str, ...]] = field(default_factory=set)
    times: list[tuple[frozenset[int], int, int]] = field(default_factory=list)  # (дни недели, часы, минуты)
    steps: list[Step] = field(default_factory=list)
    enabled: bool = True
    without_name: bool = False
    error: str = ""
    file: str = ""
    mtime: float = 0.0

    @property
    def schedule_text(self) -> str:
        return "; ".join(f"{schedule.repeat_text(','.join(map(str, sorted(days))))} в {h}:{m:02d}"
                         for days, h, m in self.times)

    def previous(self, now: datetime) -> datetime | None:
        """Последний момент запуска по расписанию, не позже now."""
        best = None
        for days, h, m in self.times:
            for back in range(8):
                dt = (now - timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
                if dt <= now and dt.weekday() in days:
                    best = max(best, dt) if best else dt
                    break
        return best


# --- разбор заметки -------------------------------------------------------------------------

def key(text: str) -> tuple[str, ...]:
    """Фраза так, как она звучит: без регистра, знаков и «пожалуйста». «Включи» и «выключи» остаются разными."""
    return tuple(wake.norm(w) for w in wake.words(text) if w not in POLITE)


def sentence(text: str) -> str:
    text = " ".join(str(text).split())
    return text if text[-1:] in ".!?…" else text + "."


def clock(value) -> tuple[int, int]:
    """«7:30» → (7, 30). YAML читает незакавыченное 7:30 как число 450 (шестидесятеричная запись) — возвращаем."""
    if isinstance(value, bool):
        raise ScenarioError("время нужно как «ЧЧ:ММ»")
    if isinstance(value, int):
        h, m = (value, 0) if value < 24 else divmod(value, 60)
    else:
        found = re.fullmatch(r"(\d{1,2})[:.](\d{2})", str(value).strip())
        if not found:
            raise ScenarioError(f"время нужно как «ЧЧ:ММ», а не «{value}»")
        h, m = int(found[1]), int(found[2])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ScenarioError(f"такого времени не бывает: {value}")
    return h, m


_UNIT = re.compile(r"(\d+)\s*(час\w*|ч|h|мин\w*|м|min|m|сек\w*|с|sec|s)\.?\s*")


def duration(value) -> int:
    """«10m», «1ч 30м», «90 секунд» → секунды. Число без единицы — минуты."""
    if isinstance(value, bool):
        raise ScenarioError("длительность нужна как «10m», «1h30m» или «90s»")
    if isinstance(value, int):
        seconds = value * 60
    else:
        raw, seconds, pos = str(value).strip().lower(), 0, 0
        if raw.isdigit():
            seconds, pos = int(raw) * 60, len(raw)
        while (found := _UNIT.match(raw, pos)) and found.end() > pos:
            unit = found[2]
            seconds += int(found[1]) * (3600 if unit[0] in "чh" else 60 if unit[0] in "мm" else 1)
            pos = found.end()
        if pos != len(raw):
            raise ScenarioError(f"длительность нужна как «10m», «1h30m» или «90s», а не «{value}»")
    if not 1 <= seconds <= schedule.MAX_AHEAD:
        raise ScenarioError("длительность — от секунды до года")
    return seconds


_RU_DAYS = {"пн": 0, "пон": 0, "вт": 1, "ср": 2, "чт": 3, "чет": 3, "пт": 4, "пят": 4, "сб": 5, "суб": 5,
            "вс": 6, "вос": 6}


def _day(token: str) -> int:
    if token[:3] in schedule.DAYS:
        return schedule.DAYS.index(token[:3])
    for n in (3, 2):
        if token[:n] in _RU_DAYS:
            return _RU_DAYS[token[:n]]
    raise ScenarioError(f"не понял день недели «{token}»: пн, вт, ср, чт, пт, сб, вс, будни, выходные")


def parse_times(raw) -> list[tuple[frozenset[int], int, int]]:
    """«пн-пт 07:30», «сб,вс 10:00», «23:00» (каждый день) или список таких строк."""
    out = []
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, int) and not isinstance(item, bool):
            item = "%d:%02d" % clock(item)
        spec = str(item).strip().lower().replace("ё", "е")
        found = re.search(r"(\d{1,2})[:.](\d{2})$", spec)
        if not found:
            raise ScenarioError(f"schedule: в конце нужно время «ЧЧ:ММ», например «пн-пт 07:30», а не «{item}»")
        h, m = clock(found[0])
        rest = re.sub(r"\b(по|в|во|каждый|каждую|каждое|день)\b", " ", spec[:found.start()])
        days: set[int] = set()
        for token in filter(None, re.split(r"[,;\s]+", rest)):
            if token in ("ежедневно", "daily"):
                days.update(range(7))
            elif token.startswith("будн") or token == "weekdays":
                days.update(range(5))
            elif token.startswith("выходн") or token == "weekends":
                days.update((5, 6))
            elif re.fullmatch(r"\w+[-–—]\w+", token):
                a, b = (_day(t) for t in re.split(r"[-–—]", token))
                days.update((a + i) % 7 for i in range((b - a) % 7 + 1))  # «пт-пн» идёт через выходные
            else:
                days.add(_day(token))
        out.append((frozenset(days or range(7)), h, m))
    return out


def _text(value, what: str, limit: int) -> str:
    text = " ".join(str(value).split()) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
    if not text:
        raise ScenarioError(f"{what}: нужен текст")
    if len(text) > limit:
        raise ScenarioError(f"{what}: не длиннее {limit} знаков")
    return text


def _target(value, what: str) -> dict:
    if isinstance(value, str) and value.strip():
        return {"name": value.strip()}
    if isinstance(value, dict) and not set(value) - set(TARGET_KEYS):
        out = {k: value[k] for k in TARGET_KEYS if value.get(k)}
        if isinstance(out.get("domain"), str):
            out["domain"] = [out["domain"]]
        if out:
            return out
    raise ScenarioError(f"{what}: нужно название устройства или {{name, area}}")


def _where(args: dict) -> str:
    place = ", ".join(str(args[k]) for k in ("area", "floor") if args.get(k))
    return str(args.get("name") or "всё") + (f" ({place})" if place else "")


def _timed(kind: str, value) -> Step:
    v = value if isinstance(value, dict) else {"in" if kind == "timer" else "at": value}
    if set(v) - {"in", "at", "text", "repeat"}:
        raise ScenarioError(f"{kind}: допустимы in, at, text и repeat")
    args: dict = {"kind": "reminder" if kind == "remind" else kind}
    if v.get("in") is not None:
        args["in_seconds"] = duration(v["in"])
        when = "через " + schedule.span_text(args["in_seconds"])
    elif v.get("at") is not None and kind != "timer":
        args["at"] = "%d:%02d" % clock(v["at"])
        when = ("на " if kind == "alarm" else "в ") + args["at"]
    else:
        raise ScenarioError(f"{kind}: нужно время — " + ("in" if kind == "timer" else "at или in"))
    if v.get("repeat"):
        if kind == "timer" or "in_seconds" in args:
            raise ScenarioError(f"{kind}: repeat бывает только вместе с at")
        try:
            days = schedule.parse_repeat(v["repeat"])
        except schedule.ScheduleError as e:
            raise ScenarioError(f"{kind}: {e}") from e
        args["repeat"] = str(v["repeat"])
        when += ", " + schedule.repeat_text(days)
    if v.get("text"):
        args["text"] = _text(v["text"], kind, 300)
    elif kind == "remind":
        raise ScenarioError("remind: нужен text — о чём напомнить")
    tail = f": {args['text']}" if args.get("text") else ""
    if kind == "timer":
        return Step(kind, args, f"таймер на {schedule.span_text(args['in_seconds'])}{tail}")
    return Step(kind, args, ("будильник " if kind == "alarm" else "напомнить ") + when + tail)


def parse_step(raw) -> Step:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise ScenarioError(f"шаг — одна пара «действие: значение», а не {raw!r}")
    (kind, v), = raw.items()
    # YAML читает незакавыченные on и off как «да» и «нет»
    kind = {True: "on", False: "off"}[kind] if isinstance(kind, bool) else str(kind).strip().lower()
    if kind in HOME_TOOLS:
        args = _target(v, kind)
        return Step(kind, {"tool": HOME_TOOLS[kind], "args": args},
                    ("включить: " if kind == "on" else "выключить: ") + _where(args))
    if kind == "home":
        if not isinstance(v, dict) or not isinstance(v.get("tool"), str) or not v["tool"].strip():
            raise ScenarioError("home: нужен tool — название инструмента Home Assistant, например HassLightSet")
        args = {k: x for k, x in v.items() if k != "tool"}
        extra = ", ".join(f"{k} {x}" for k, x in args.items() if k not in TARGET_KEYS)
        return Step(kind, {"tool": v["tool"].strip(), "args": args},
                    f"дом, {v['tool'].strip()}: {_where(args)}" + (f", {extra}" if extra else ""))
    if kind == "volume":
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100:
            return Step(kind, {"action": "set", "value": v}, f"громкость {v}%")
        if v in ("mute", "unmute"):
            return Step(kind, {"action": v}, "выключить звук" if v == "mute" else "вернуть звук")
        raise ScenarioError("volume: число от 0 до 100, mute или unmute")
    if kind == "media":
        if v not in MEDIA:
            raise ScenarioError("media: " + ", ".join(MEDIA))
        return Step(kind, {"action": v}, MEDIA[v])
    if kind == "music":
        v = v if isinstance(v, dict) else {"query": v}
        if set(v) - {"query", "where", "source"} or v.get("where", "pc") not in music.PLACES \
                or v.get("source", "youtube") not in music.SOURCES:
            raise ScenarioError("music: запрос или {query, where: pc | station, source: youtube | yandex}")
        args = {"query": _text(v.get("query"), kind, 200), **{k: v[k] for k in ("where", "source") if v.get(k)}}
        return Step(kind, args, f"музыка: {args['query']}" + (" на Станции" if args.get("where") == "station" else ""))
    if kind == "youtube":
        query = _text(v, kind, 200)
        return Step(kind, {"query": query}, f"YouTube: {query}")
    if kind == "open":
        try:
            url = pc.clean_url(_text(v, kind, 500))
        except pc.PCError as e:
            raise ScenarioError(f"open: {e}") from e
        return Step(kind, {"url": url}, f"открыть {url}")
    if kind in ("timer", "alarm", "remind"):
        return _timed(kind, v)
    if kind == "say":
        text = _text(v, kind, 500)
        return Step(kind, {"text": text}, f"сказать: {text}")
    if kind == "notify":
        text = _text(v, kind, 500)
        return Step(kind, {"text": text}, f"уведомление на экране: {text}")
    if kind == "ask":
        text = _text(v, kind, 1000)
        return Step(kind, {"text": text}, f"поручить агенту: {text}")
    raise ScenarioError(f"неизвестный шаг «{kind}»: on, off, home, volume, media, music, youtube, open, notify, "
                        "timer, alarm, remind, say, ask")


def parse(name: str, meta: dict) -> Scenario:
    """Frontmatter заметки → сценарий. Ошибка в любом поле — ScenarioError с объяснением."""
    phrases = meta.get("phrases") or []
    phrases = [phrases] if isinstance(phrases, str) else phrases
    if not isinstance(phrases, list) or not all(isinstance(p, str) and key(p) for p in phrases):
        raise ScenarioError("phrases: список фраз, например [\"я дома\", \"я пришёл\"]")
    steps = meta.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ScenarioError("steps: нужен непустой список шагов")
    if len(steps) > MAX_STEPS:
        raise ScenarioError(f"steps: не больше {MAX_STEPS} шагов")
    return Scenario(
        id=name, phrases=[" ".join(p.split()) for p in phrases], keys={key(p) for p in phrases},
        times=parse_times(meta["schedule"]) if meta.get("schedule") not in (None, "", []) else [],
        steps=[parse_step(s) for s in steps],
        enabled=meta.get("enabled", True) is not False, without_name=meta.get("without_name") is True,
    )


def describe(sc: Scenario) -> str:
    """Что сценарий делает — человеку: в запросе подтверждения, в приложении и в ответе инструмента."""
    lines = []
    if sc.error:
        lines.append(f"Ошибка: {sc.error}")
    if sc.phrases:
        lines.append("Фразы: " + ", ".join(f"«{p}»" for p in sc.phrases)
                     + (" — срабатывают и без обращения по имени" if sc.without_name else ""))
    if sc.times:
        lines.append("По времени: " + sc.schedule_text)
    if not sc.enabled:
        lines.append("Выключен")
    return "\n".join(lines + [f"{i}. {s.text}" for i, s in enumerate(sc.steps, 1)])


# --- сценарии -------------------------------------------------------------------------------

class Scenarios:
    def __init__(self, vault: Path, store, home=None, computer=None, scheduler=None, brain=None, notifier=None,
                 confirm=None, clock=time.time, user: str = "owner", tunes=None):
        self.vault, self.store, self.user, self.clock = vault, store, user, clock
        self.home, self.computer, self.scheduler, self.brain = home, computer, scheduler, brain
        self.notifier, self.confirm = notifier, confirm
        self.tunes = tunes     # music.Music или None
        self.announce = None   # async fn(фраза) -> bool: произнести на голосовых клиентах; False — некому
        self.journal = None    # fn(строка): дневной журнал в vault
        self._sig: list | None = None
        self._cache: list[Scenario] = []
        self._running: set[str] = set()
        self._jobs: set[asyncio.Task] = set()
        store.db.execute("CREATE TABLE IF NOT EXISTS scenario_runs(name TEXT PRIMARY KEY, last REAL NOT NULL)")

    # --- чтение -----------------------------------------------------------------------------
    def load(self) -> list[Scenario]:
        """Все сценарии из vault. Заметки перечитываются, только когда изменились."""
        paths, sig = [], []
        with contextlib.suppress(OSError):
            for path in sorted((self.vault / DIR).glob("*.md")):
                if not path.name.startswith("_"):  # шаблон и черновики
                    stat = path.stat()
                    paths.append((path, stat.st_mtime))
                    sig.append((path.name, stat.st_mtime_ns, stat.st_size))
        if sig == self._sig:
            return self._cache
        out, taken = [], {}
        for path, mtime in paths:
            try:
                sc = parse(path.stem, frontmatter.load(path).metadata)
            except ScenarioError as e:
                sc = Scenario(id=path.stem, error=str(e))
            except Exception as e:  # битый YAML не должен ронять ядро
                sc = Scenario(id=path.stem, error=f"frontmatter: {e}")
            sc.file, sc.mtime = f"{DIR}/{path.name}", mtime
            for phrase in sc.phrases if sc.enabled and not sc.error else []:
                other = taken.setdefault(key(phrase), sc.id)
                if other != sc.id:
                    sc.error = f"фраза «{phrase}» уже занята сценарием «{other}»"
            out.append(sc)
        self._sig, self._cache = sig, out
        return out

    def get(self, name: str) -> Scenario | None:
        """По названию; модель может назвать сценарий и его фразой."""
        items, k = self.load(), key(str(name))
        return (next((s for s in items if s.id == name), None)
                or next((s for s in items if key(s.id) == k), None)
                or next((s for s in items if k and k in s.keys), None))

    def match(self, text: str, bare: bool = False) -> Scenario | None:
        """Сценарий, чья фраза совпала с репликой целиком. bare — к ассистенту не обращались по имени."""
        k = key(text)
        if not k or (bare and len(k) < BARE_WORDS):
            return None
        return next((s for s in self.load() if s.enabled and not s.error and k in s.keys
                     and (s.without_name or not bare)), None)

    def public(self, sc: Scenario) -> dict:
        return {"id": sc.id, "phrases": sc.phrases, "schedule": sc.schedule_text, "enabled": sc.enabled,
                "without_name": sc.without_name, "steps": [s.text for s in sc.steps], "error": sc.error,
                "file": sc.file}

    def summary(self) -> str:
        """Список для системного промпта: названия и фразы включённых сценариев."""
        lines = []
        for sc in self.load():
            if sc.enabled and not sc.error:
                hints = [", ".join(f"«{p}»" for p in sc.phrases), sc.schedule_text]
                lines.append(f"- {sc.id}" + (": " + "; ".join(h for h in hints if h) if any(hints) else ""))
        return "\n".join(lines)

    def listing(self) -> str:
        items = self.load()
        return "\n\n".join(f"{sc.id}\n{describe(sc)}" for sc in items) or "Сценариев пока нет."

    # --- выполнение -------------------------------------------------------------------------
    async def run(self, sc: Scenario, channel: str, agent: bool = False) -> AsyncIterator[str]:
        """Шаги по порядку. Отдаёт фразы, которые нужно произнести; сбой шага остальные не останавливает."""
        if sc.error:
            raise ScenarioError(f"в сценарии «{sc.id}» ошибка: {sc.error}")
        if not sc.enabled:
            raise ScenarioError(f"сценарий «{sc.id}» выключен")
        if sc.id in self._running:
            raise ScenarioError(f"сценарий «{sc.id}» уже выполняется")
        self._running.add(sc.id)
        try:
            if self.journal:
                with contextlib.suppress(Exception):
                    self.journal(f"сценарий «{sc.id}» ({channel})")
            failed = []
            for step in sc.steps:
                try:
                    phrase = await self._step(step, channel, agent)
                except ScenarioError as e:
                    log.warning("сценарий «%s», шаг «%s»: %s", sc.id, step.text, e)
                    failed.append(f"{step.text} — {e}")
                    continue
                if phrase:
                    yield sentence(phrase)
            if failed:
                yield Failure(sentence("Не получилось: " + "; ".join(failed)))
        finally:
            self._running.discard(sc.id)

    async def _step(self, step: Step, channel: str, agent: bool) -> str | None:
        kind, args = step.kind, step.args
        if kind == "say":
            return args["text"]
        if kind in ("on", "off", "home"):
            ctx = await self.home.context() if self.home is not None else None
            if ctx is None:
                raise ScenarioError("Home Assistant не подключён или недоступен")
            tool = next((t["name"] for t in ctx[0]
                         if t["name"] == args["tool"] or t["name"].endswith("__" + args["tool"])), None)
            if tool is None:
                raise ScenarioError(f"в Home Assistant нет инструмента {args['tool']}")
            text, is_error = await self.home.call(tool, args["args"])
            if is_error:
                raise ScenarioError(text.strip()[:200] or "Home Assistant отказал")
            return None
        if kind in ("volume", "media", "youtube", "open", "notify"):
            if self.computer is None:
                raise ScenarioError("управление компьютером выключено (JARVIS_PC_CONTROL)")
            tool = {"volume": "pc_volume", "media": "pc_media", "youtube": "pc_youtube", "open": "pc_open_url",
                    "notify": "pc_notify"}[kind]
            try:
                await self.computer.call(tool, args)
            except pc.PCError as e:
                raise ScenarioError(str(e)) from e
            return None
        if kind == "music":
            if self.tunes is None or not self.tunes.available:
                raise ScenarioError("музыка не подключена")
            try:
                await self.tunes.play(args, channel)
            except music.MusicError as e:
                raise ScenarioError(str(e)) from e
            return None
        if kind in ("timer", "alarm", "remind"):
            if self.scheduler is None:
                raise ScenarioError("планировщик не подключён")
            try:
                return await self.scheduler.call("schedule_set", args, channel)
            except schedule.ScheduleError as e:
                raise ScenarioError(str(e)) from e
        if kind == "ask":
            if agent:  # сценарий запустил сам агент: его ход ещё идёт, второй начать нельзя
                return f"Поручение из сценария, выполни его: {args['text']}"
            if self.brain is None:
                raise ScenarioError("агент не подключён")
            reply = await self.brain.ask(self.user, args["text"] + ASK_SUFFIX, channel=f"{channel}-agent")
            if reply.is_error:
                raise ScenarioError(reply.text[:200])
            return reply.text
        raise ScenarioError(f"неизвестный шаг «{kind}»")

    async def execute(self, sc: Scenario, channel: str, agent: bool = False) -> str:
        """Выполнить целиком и вернуть сказанное одной строкой — результат инструмента scenario_run."""
        return " ".join([p async for p in self.run(sc, channel, agent)]) or "Готово."

    async def respond(self, user: str, sc: Scenario, channel: str, text: str | None = None) -> AsyncIterator[str]:
        """Запуск фразой или кнопкой: шаги и запись в журнал в том же виде, что вызов инструмента моделью."""
        if text is not None:
            self.store.log(user, channel, "user", payload=text)
        self.store.log(user, channel, "tool", "scenario_run", {"name": sc.id})
        said: list[str] = []
        failed = False
        try:
            async for phrase in self.run(sc, channel):
                said.append(phrase)
                yield phrase + " "
            if not said:
                said.append("Готово.")
                yield said[0]
        except ScenarioError as e:
            failed = True
            said.append(sentence(f"Не получилось: {e}"))
            yield said[-1]
        finally:
            out = " ".join(said)
            self.store.log(user, channel, "tool_result", "scenario_run",
                           {"text": out, **({"is_error": True} if failed else {})})
            if text is not None:
                self.store.log(user, channel, "assistant", payload={"text": out})

    async def handle(self, user: str, text: str, channel: str) -> str | None:
        """Реплика из текстового канала: если это фраза сценария — выполнить и вернуть ответ, иначе None."""
        sc = self.match(text)
        if sc is None:
            return None
        return "".join([p async for p in self.respond(user, sc, channel, text)]).strip()

    # --- запуск по времени ------------------------------------------------------------------
    async def tick(self) -> None:
        """Вызывается планировщиком. Сценарии запускаются в фоне: будильники их не ждут."""
        now = self.clock()
        now_dt = datetime.fromtimestamp(now)
        for sc in self.load():
            if not sc.times or not sc.enabled or sc.error:
                continue
            prev = sc.previous(now_dt)
            row = self.store.db.execute("SELECT last FROM scenario_runs WHERE name=?", (sc.id,)).fetchone()
            # момент запуска должен быть позже и прошлого запуска, и правки заметки: перенос времени
            # на «десять минут назад» или включение сценария не запускают его задним числом
            if prev is None or prev.timestamp() <= max(row[0] if row else 0.0, sc.mtime):
                continue
            self.store.db.execute("INSERT INTO scenario_runs VALUES(?,?) ON CONFLICT(name) DO UPDATE SET "
                                  "last=excluded.last", (sc.id, now))
            if now - prev.timestamp() > LATE:
                log.info("сценарий «%s» пропущен: в %s ядро не работало", sc.id, f"{prev:%H:%M}")
                continue
            job = asyncio.get_running_loop().create_task(self._scheduled(sc))
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)

    async def _scheduled(self, sc: Scenario) -> None:
        self.store.log(self.user, "schedule", "tool", "scenario_run", {"name": sc.id})
        said, unheard = [], []
        try:
            async for phrase in self.run(sc, "schedule"):
                said.append(phrase)
                heard = bool(self.announce) and await self.announce(phrase)
                if not heard or isinstance(phrase, Failure):  # о сбое — ещё и сообщением: дома может никого не быть
                    unheard.append(phrase)
        except ScenarioError as e:
            unheard.append(sentence(f"Не выполнен: {e}"))
            said.append(unheard[-1])
        except Exception as e:
            log.exception("сценарий «%s» по расписанию", sc.id)
            unheard.append(sentence(f"Не выполнен: {e}"))
            said.append(unheard[-1])
        self.store.log(self.user, "schedule", "tool_result", "scenario_run", {"text": " ".join(said) or "Готово."})
        if unheard and self.notifier:
            await self.notifier.send(" ".join(unheard), title=f"Сценарий «{sc.id}»")

    # --- изменение --------------------------------------------------------------------------
    async def save(self, args: dict, channel: str = "") -> str:
        """Создать или заменить сценарий — после проверки шагов и подтверждения пользователя."""
        name = " ".join(str(args.get("name") or "").split())
        if not NAME.fullmatch(name):
            raise ScenarioError("name: короткое название из букв, цифр, пробелов и дефисов")
        meta = {k: args[k] for k in ("phrases", "schedule", "steps") if args.get(k)}
        if args.get("without_name"):
            meta["without_name"] = True
        if args.get("enabled") is False:
            meta["enabled"] = False
        sc = parse(name, meta)
        old = next((s for s in self.load() if key(s.id) == key(name)), None)  # то же название — заменяем
        for other in self.load():
            if other is not old and other.enabled and not other.error and other.keys & sc.keys:
                raise ScenarioError(f"такая фраза уже есть у сценария «{other.id}»")
        ctx = await self.home.context() if self.home is not None else None
        for step in sc.steps:
            if step.kind not in ("on", "off", "home"):
                continue
            if ctx is None:
                raise ScenarioError("в сценарии есть шаги умного дома, а Home Assistant не подключён или недоступен")
            if not any(t["name"] == step.args["tool"] or t["name"].endswith("__" + step.args["tool"]) for t in ctx[0]):
                raise ScenarioError(f"в Home Assistant нет инструмента {step.args['tool']}")
        if self.confirm is None:
            raise ScenarioError("сценарий сохраняется только с подтверждения пользователя, а оно не настроено")
        title = f"Изменить сценарий «{old.id}»?" if old else f"Создать сценарий «{name}»?"
        if not await self.confirm.ask(title, describe(sc), channel=channel):
            raise ScenarioError("пользователь не подтвердил сценарий")
        path = self.vault / (old.file if old else f"{DIR}/{name}.md")
        body = frontmatter.load(path).content if old else f"# {name}\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter.dumps(frontmatter.Post(body, **meta)) + "\n", encoding="utf-8")
        return f"Сценарий «{old.id if old else name}» сохранён: {path.relative_to(self.vault).as_posix()}"

    def set_enabled(self, name: str, enabled: bool) -> Scenario:
        """Включить или выключить: правится одна строка frontmatter, остальное в заметке не трогается."""
        sc = next((s for s in self.load() if s.id == name), None)
        if sc is None:
            raise ScenarioError(f"нет сценария «{name}»")
        path = self.vault / sc.file
        text = path.read_text(encoding="utf-8")
        found = re.match(r"(?s)\A(---[ \t]*\n)(.*?\n)?(---[ \t]*(?:\n|\Z))", text)
        if not found:
            raise ScenarioError("в заметке сценария нет frontmatter")
        line = f"enabled: {'true' if enabled else 'false'}\n"
        head, n = re.subn(r"(?m)^enabled[ \t]*:.*\n", line, found[2] or "")
        path.write_text(found[1] + (head if n else head + line) + found[3] + text[found.end():], encoding="utf-8")
        return next(s for s in self.load() if s.id == name)
