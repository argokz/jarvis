"""Быстрый голосовой контур: Haiku отвечает сам или передаёт задачу агенту (Brain)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime

import anthropic

from . import browser, music, pc, pricing, printing, registry, scenarios, schedule
from .agent import Brain
from .config import Settings
from .home import READ_ONLY, Home
from .notify import Notifier
from .store import Store, _text

log = logging.getLogger("jarvis.router")

SYSTEM = """Ты — голосовой интерфейс личного ассистента. Тебя слушают, а не читают: отвечай одной–тремя \
короткими фразами разговорным языком пользователя (обычно русским), без списков, разметки, ссылок и \
эмодзи. Числа, даты и сокращения пиши так, чтобы их было удобно произнести. Тебя зовут {name}; без вопроса имя не произноси.

Отвечай сам, если хватает общего знания, контекста разговора или сводки реестра ниже: приветствия, \
простые вопросы и факты, арифметика, вопросы о проектах на уровне сводки.

Всё остальное передавай инструменту delegate — это полноценный агент с доступом к Obsidian-vault \
(заметки, память о пользователе, подробности проектов, журнал){capabilities}. Обязательно делегируй, \
когда нужно что-то записать, запомнить или найти в заметках; узнать о проекте больше, чем есть в \
сводке; вспомнить факты о пользователе{open_hint}. Не выдумывай содержимое заметок, памяти и файлов — \
если не знаешь, делегируй. Перед вызовом delegate скажи короткую фразу вроде «Секунду, посмотрю.» и \
сразу вызывай инструмент. Агент не слышит разговор, поэтому в task сформулируй самодостаточную задачу \
со всем нужным контекстом.

В истории есть и ответы агента из других каналов (чат, Telegram): он получал их своими инструментами — интернетом, заметками, файлами, — а вызовы этих инструментов тебе не показаны. Такие ответы верны: не объявляй их выдуманными и не поправляй себя за них.

Текущее время пользователя указано в квадратных скобках в начале его реплики.
{home}
Сводка реестра проектов:
{registry}
"""

OWN_TOOLS = """
Действие происходит, только когда ты вызываешь инструмент: не говори, что сделал, не вызвав его. Выполняй \
только то, о чём просит последняя реплика. Прошлые просьбы из истории уже выполнены, даже если вызова рядом \
не видно (их выполнял агент или другой канал): не повторяй и не перепроверяй их.
"""

SCHEDULE_HINT = """
Таймеры, напоминания и будильники ставь сам, без delegate: schedule_set — поставить, schedule_list — что \
запланировано, schedule_cancel — отменить, schedule_dismiss — выключить или отложить то, что звенит. \
«Через N минут» — это in_seconds; время суток для at бери из реплики и из текущего времени в квадратных \
скобках. Перед schedule_set, schedule_cancel и schedule_dismiss ничего не говори: подтверждение \
прозвучит само.
"""

PC_HINT = """
Компьютером управляй сам, без delegate: pc_volume — громкость (по умолчанию музыки и видео, твой голос \
она не меняет; общую — target system, только если пользователь так и сказал), \
pc_media — пауза, продолжить, следующий и предыдущий трек, выключить музыку; pc_open_url — открыть сайт \
в браузере; pc_youtube — найти и открыть видео на YouTube в браузере; pc_notify — показать уведомление \
на экране. Устройство не названо — речь о компьютере{other}.
"""
MUSIC_HINT = """
Музыку включай сам инструментом music_play: песня, исполнитель, жанр, «что-нибудь спокойное». Перед \
music_play ничего не говори: что включилось, прозвучит само. pc_youtube — только когда просят видео.
"""
PC_OTHER = "; инструменты Hass для громкости и воспроизведения — только когда названа колонка или телевизор"

SCENARIO_HINT = """
Сценарии — цепочки действий, которые настроил пользователь. Если последняя реплика по смыслу — название или \
фраза сценария из списка, вызови scenario_run и ничего не говори: сценарий ответит сам. Создать, изменить \
или выключить сценарий — через delegate.

{list}
"""

HOME = """
Умным домом управляй сам, без delegate: инструменты Hass включают, выключают и настраивают устройства, \
homeassistant__GetLiveContext сообщает их текущее состояние и показания датчиков. В name и area передавай \
названия точно как в списке ниже, в именительном падеже. Перед действием скажи короткую фразу вроде \
«Включаю свет в зале.» и сразу вызывай инструмент. Нужного устройства в списке нет — так и скажи, \
ничего не вызывая.

{overview}
"""

DELEGATE = {
    "name": "delegate",
    "description": "Передать задачу полноценному агенту с доступом к заметкам, памяти и файлам. "
                   "Агент вернёт короткий ответ, который будет озвучен пользователю.",
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {"task": {"type": "string", "description": "Самодостаточная формулировка задачи."}},
        "required": ["task"],
    },
}

READY = schedule.SPOKEN | {"scenario_run", "music_play"}  # результат — фраза, которую остаётся произнести
MAX_ROUNDS = 3  # запросов к модели на одну реплику: своё действие → при необходимости ответ по результату
VOICE_SUFFIX = "\n\n(Голосовой запрос: ответь одной–тремя короткими фразами без разметки — ответ будет озвучен.)"
HISTORY_TURNS = 12
HISTORY_ROWS = HISTORY_TURNS * 4  # записей журнала в истории: реплики, вызовы инструментов и их результаты


def stamp(ts: float, text: str) -> str:
    """Реплика пользователя с её временем. Вид один и тот же в своём ходе и потом в истории — иначе кэш промахнётся."""
    return f"[{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M, %a}] {text}"


class VoiceRouter:
    def __init__(self, settings: Settings, brain: Brain, store: Store, notifier: Notifier,
                 client: anthropic.AsyncAnthropic | None = None, home: Home | None = None,
                 computer: pc.PC | None = None, scheduler: schedule.Scheduler | None = None,
                 scenes: scenarios.Scenarios | None = None, tunes: music.Music | None = None):
        self.s, self.brain, self.store, self.notifier = settings, brain, store, notifier
        self.home = home            # None — дом только через агента (delegate)
        self.computer = computer    # None — громкость и плееры на ПК выключены
        self.scheduler = scheduler  # None — таймеров, напоминаний и будильников нет
        self.scenes = scenes        # None — сценариев нет
        self.tunes = tunes if tunes is not None and tunes.available else None  # None — музыки нет
        self._client = client
        self._background: set[asyncio.Task] = set()
        self._anchor: dict[str, int] = {}  # с какой записи журнала начинается история (см. _window)
        self._warm: tuple[str, float] | None = None  # (отпечаток начала запроса, когда его последний раз читали)
        self._warm_lock = asyncio.Lock()
        self.last_usage: dict[str, int] = {}

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:  # ленивое создание: без ключа ядро всё равно стартует
            self._client = anthropic.AsyncAnthropic()
        return self._client

    def system(self, home_overview: str | None = None, scenes: str = "") -> str:
        """home_overview — обзор устройств, когда инструменты дома выданы самому роутеру; scenes — список сценариев."""
        has_open = bool(self.s.open_roots)
        via_agent = home_overview is None and bool(self.s.hass_url and self.s.hass_token)
        caps = [(has_open, "поиску и открытию файлов на компьютере", "найти или открыть файл, документ, таблицу на компьютере"),
                (has_open and printing.available(), "печати файлов", "напечатать файл"),
                (self.s.screen, "снимку экрана компьютера", "посмотреть, прочитать или объяснить то, что сейчас на экране"),
                (self.s.browser and (self.s.browser_dir / browser.CLI).is_file(), "интернету через браузер",
                 "найти и прочитать что-то в интернете, проверить цену, расписание или новости на сайте"),
                (via_agent, "управлению умным домом",
                 "включить, выключить или узнать состояние света, розеток, климата и других устройств в доме"),
                (bool(self.s.notion_token), "страницам Notion", "найти, прочитать или дополнить страницу в Notion")]
        on = [(c, h) for enabled, c, h in caps if enabled]
        return SYSTEM.format(
            name=self.s.assistant_name,
            capabilities=", а также к " + ", ".join(c for c, _ in on) if on else "",
            open_hint="".join(f"; {h}" for _, h in on),
            home=(OWN_TOOLS if self.scheduler or self.computer or scenes or self.tunes or home_overview is not None
                  else "")
            + (SCHEDULE_HINT if self.scheduler else "")
            + (MUSIC_HINT if self.tunes else "")
            + (SCENARIO_HINT.format(list=scenes) if scenes else "")
            + (PC_HINT.format(other="" if home_overview is None else PC_OTHER) if self.computer else "")
            + ("" if home_overview is None else HOME.format(overview=home_overview.strip())),
            registry=registry.summary(registry.load_projects(self.s.vault)),
        )

    def history(self, user_id: str, own: frozenset[str] | set[str] = frozenset()) -> list[dict]:
        """Последние реплики из журнала всех каналов: разговор общий для Telegram, голоса и API.

        Прошлые вызовы инструментов из набора own восстанавливаются блоками tool_use с их результатами —
        и свои, и те же инструменты у агента (mcp__jarvis__*, mcp__home__*). Без них модель видит
        «Поставил таймер» без вызова и либо начинает так же «делать» словами, либо объявляет, что прошлые
        действия не выполнялись, либо выполняет их повторно; без результатов — что прошлые ответы выдуманы.
        """
        msgs: list[dict] = []
        calls: list[dict] = []  # вызовы после последней реплики пользователя: {"use": блок, "result": блок}
        for row in self._window(user_id):
            if row["kind"].startswith("tool"):
                try:
                    data = json.loads(row["payload"] or "{}")
                except ValueError:  # запись обрезана журналом
                    continue
                name = (row["name"] or "").removeprefix("mcp__jarvis__").removeprefix("mcp__home__")
                if name not in own or not isinstance(data, dict):
                    continue
                if row["kind"] == "tool":
                    use = {"type": "tool_use", "id": f"hist_{row['id']}", "name": name, "input": data}
                    calls.append({"use": use, "result": {"type": "tool_result", "tool_use_id": use["id"],
                                                         "content": "(результат этого вызова в журнале не сохранён)"}})
                    continue
                call = next((c for c in reversed(calls) if c["use"]["name"] == name and "is_saved" not in c), None)
                if call:  # результат относится к последнему вызову этого инструмента
                    call["is_saved"] = True
                    call["result"] = {"type": "tool_result", "tool_use_id": call["use"]["id"],
                                      "content": str(data.get("text") or "(пусто)"),
                                      **({"is_error": True} if data.get("is_error") else {})}
                continue
            role, text = row["kind"], _text(row["payload"])[:1500]
            if not text.strip():
                continue
            if role == "user":
                calls = []
                text = stamp(row["ts"], text)  # тем же видом, каким реплика ушла модели в своём ходе
            elif calls and msgs and msgs[-1]["role"] == "user":
                msgs += [{"role": "assistant", "content": [c["use"] for c in calls]},
                         {"role": "user", "content": [c["result"] for c in calls]}]
                calls = []
            if msgs and msgs[-1]["role"] == role and isinstance(msgs[-1]["content"], str):
                msgs[-1]["content"] += "\n" + text
            else:
                msgs.append({"role": role, "content": text})
        while msgs and not (msgs[0]["role"] == "user" and isinstance(msgs[0]["content"], str)):
            msgs.pop(0)
        while msgs and msgs[-1]["role"] != "assistant":  # следом пойдёт новая реплика пользователя
            msgs.pop()
        return msgs

    def _window(self, user_id: str) -> list[dict]:
        """Записи журнала для истории. Окно растёт от HISTORY_ROWS до двух таких и только потом сдвигается.

        Кэш запросов сравнивает начало запроса байт в байт. Окно «последние N записей» съезжало на каждой
        реплике, начало истории менялось, и вся история оплачивалась заново с наценкой за запись в кэш.
        """
        rows = self.store.dialogue(user_id, HISTORY_ROWS * 2)
        ids = [r["id"] for r in rows]
        if self._anchor.get(user_id) not in ids:
            self._anchor[user_id] = ids[-HISTORY_ROWS] if len(ids) > HISTORY_ROWS else (ids[0] if ids else 0)
        return rows[ids.index(self._anchor[user_id]):] if ids else []

    async def _prefix(self) -> tuple[list[dict], list[dict], set[str], set[str]]:
        """Постоянное начало запроса: подсказка и инструменты. Возвращает (system, tools, own, home_names)."""
        home = await self.home.context() if self.home else None
        home_tools, overview = home or ([], None)
        pc_tools = pc.TOOLS if self.computer else []
        scenes = self.scenes.summary() if self.scenes else ""  # сценариев нет — нет и инструмента
        own_tools = [*(schedule.TOOLS if self.scheduler else []), *([scenarios.RUN_TOOL] if scenes else []),
                     *([self.tunes.tool()] if self.tunes else []), *pc_tools, *home_tools]
        # Инструменты и подсказка меняются редко — отдельная точка кэша: её читает любой следующий ход,
        # даже когда история уже другая. Вторая точка (cache_control запроса) идёт по концу истории.
        mark = {"type": "ephemeral", **({"ttl": "1h"} if self.s.router_cache_ttl == "1h" else {})}
        system = [{"type": "text", "text": self.system(overview, scenes), "cache_control": mark}]
        return system, [DELEGATE, *own_tools], {t["name"] for t in own_tools}, {t["name"] for t in home_tools}

    def _remember_warm(self, system: list[dict], tools: list[dict]) -> None:
        self._warm = (_digest(system, tools), time.monotonic())

    async def prewarm(self, user_id: str = "owner") -> bool:
        """Прогреть кэш, если начало запроса в нём, скорее всего, остыло. True — запрос был отправлен.

        Повод — начало речи или подключение голосового клиента: пока человек договаривает, кэш успевает
        записаться, и первый ответ после долгой паузы не читает ~10 тысяч токенов заново.
        """
        async with self._warm_lock:
            try:
                system, tools, _, _ = await self._prefix()
            except Exception:  # дом недоступен и т. п. — прогрев не нужен ответу
                log.exception("прогрев: не собралось начало запроса")
                return False
            ttl = 3600 if self.s.router_cache_ttl == "1h" else 300
            if self._warm and self._warm[0] == _digest(system, tools) and time.monotonic() - self._warm[1] < ttl - 60:
                return False
            try:
                resp = await self.client.messages.create(
                    model=self.s.router_model, max_tokens=0, system=system, tools=tools,
                    output_config={"effort": self.s.router_effort},
                    messages=[{"role": "user", "content": "."}])
            except (anthropic.APIError, TypeError) as e:
                log.warning("прогрев кэша не удался: %r", e)
                return False
            self._remember_warm(system, tools)
            used = pricing.add({}, getattr(resp, "usage", None))
            self.store.log(user_id, "voice", "prewarm", "", {"cost_usd": pricing.cost(self.s.router_model, used),
                                                             "tokens": used})
            return True

    async def respond(self, user_id: str, text: str, channel: str = "voice") -> AsyncIterator[str]:
        """Отдаёт фрагменты ответа по мере генерации — их сразу можно озвучивать."""
        system, tools, own, home_names = await self._prefix()  # own — что роутер выполняет сам
        # delegate — тоже в истории: иначе модель видит свой прошлый ответ «нашёл, записал» без вызова
        # рядом и в следующей реплике объявляет, что ничего не делала
        now = time.time()  # одно время и модели, и журналу: в истории реплика должна выглядеть так же
        messages = self.history(user_id, own | {"delegate"}) + [{"role": "user", "content": stamp(now, text)}]
        self.store.log(user_id, channel, "user", payload=text, ts=now)
        spoken: list[str] = []
        used: dict[str, int] = {}  # токены всех запросов этого хода
        try:
            task: str | None = None
            for round_ in range(MAX_ROUNDS):
                last = round_ == MAX_ROUNDS - 1
                if round_ and "".join(spoken).strip():
                    spoken.append(" ")  # фразы разных запросов не должны слипаться
                    yield " "
                try:
                    async with self.client.messages.stream(
                        model=self.s.router_model,
                        max_tokens=2048,
                        system=system,
                        tools=tools,
                        **({"tool_choice": {"type": "none"}} if last else {}),  # последний запрос — только ответ
                        output_config={"effort": self.s.router_effort},
                        cache_control={"type": "ephemeral"},
                        messages=messages,
                    ) as stream:
                        async for event in stream:
                            if event.type == "text" and event.text:
                                spoken.append(event.text)
                                yield event.text
                        final = await stream.get_final_message()
                    pricing.add(used, getattr(final, "usage", None))
                    self._remember_warm(system, tools)
                except ValueError:
                    # ввод инструмента не разобрался как JSON — отдаём агенту исходную реплику
                    task = text
                    break
                except (anthropic.APIError, TypeError) as e:
                    if isinstance(e, TypeError) and "authentication" not in str(e):
                        raise
                    log.error("роутер: ошибка API: %r", e)
                    msg = ("Не задан ключ Anthropic API." if isinstance(e, TypeError)
                           else "Не получилось связаться с моделью, попробуйте ещё раз.")
                    spoken.append(msg)
                    yield msg
                    return

                if final.stop_reason == "refusal":
                    msg = "С этим я помочь не могу."
                    spoken.append(msg)
                    yield msg
                    return
                uses = [b for b in final.content if b.type == "tool_use"]
                if uses and final.stop_reason == "max_tokens":  # ввод инструмента мог оборваться
                    task = text
                    break
                calls = [b for b in uses if b.name in own]
                results = [await self._call(user_id, channel, b, home_names) for b in calls]
                delegate = next((b for b in uses if b.name == "delegate"), None)
                if delegate is not None:
                    t = (delegate.input if isinstance(delegate.input, dict) else {}).get("task")
                    task = t.strip() if isinstance(t, str) and t.strip() else text
                    break
                if not calls:
                    break
                acted = not any(self._reads(b, home_names) for b in calls) and not any(r.get("is_error") for r in results)
                if acted:  # действие выполнено и уже объявлено — второй запрос к модели не нужен
                    said = "".join(spoken).strip()
                    # результат таймера, будильника или сценария — готовая фраза
                    ready = " ".join(r["content"] for b, r in zip(calls, results) if b.name in READY)
                    msg = (" " if said and ready else "") + (ready or ("" if said else "Готово."))
                    if msg:
                        spoken.append(msg)
                        yield msg
                    break
                messages += [{"role": "assistant", "content": final.content}, {"role": "user", "content": results}]

            if task:
                if not "".join(spoken).strip():
                    filler = "Секунду. "
                    spoken.append(filler)
                    yield filler
                async for chunk in self._delegate(user_id, task, channel):
                    spoken.append(chunk)
                    yield chunk
        finally:
            # цена и токены — перед текстом: запись журнала обрезается с конца
            spent = {"cost_usd": pricing.cost(self.s.router_model, used), "tokens": used} if any(used.values()) else {}
            self.last_usage = dict(used)  # для замера стадий голосового ответа
            self.store.log(user_id, channel, "assistant", payload={**spent, "text": "".join(spoken).strip()})

    @staticmethod
    def _reads(block, home_names: set[str]) -> bool:
        """По результату вызова модель должна ещё ответить: спросили состояние, нашли видео."""
        if block.name in home_names:
            return block.name in READ_ONLY
        if block.name in schedule.NAMES:
            return block.name not in schedule.SPOKEN
        if block.name in ("scenario_run", "music_play"):
            return False
        return pc.needs_answer(block.name, block.input if isinstance(block.input, dict) else {})

    async def _call(self, user_id: str, channel: str, block, home_names: set[str]) -> dict:
        """Выполняет свой инструмент роутера (дом, компьютер, расписание) и пишет вызов в журнал, как агент."""
        args = block.input if isinstance(block.input, dict) else {}
        self.store.log(user_id, channel, "tool", block.name, args)
        if block.name in home_names:
            if block.name not in READ_ONLY:
                self.brain.journal_note(f"умный дом, {block.name}: {args} ({channel})")
            text, is_error = await self.home.call(block.name, args)
        elif block.name in schedule.NAMES:
            try:
                text, is_error = await self.scheduler.call(block.name, args, channel), False
            except schedule.ScheduleError as e:
                text, is_error = str(e), True
        elif block.name == "scenario_run":
            scene = self.scenes.get(str(args.get("name", "")))
            try:
                if scene is None:
                    raise scenarios.ScenarioError(f"нет сценария «{args.get('name', '')}»")
                text, is_error = await self.scenes.execute(scene, channel), False
            except scenarios.ScenarioError as e:
                text, is_error = str(e), True
        elif block.name == "music_play":
            self.brain.journal_note(f"музыка: {args.get('query', '')} ({channel})")
            try:
                text, is_error = await self.tunes.play(args, channel), False
            except music.MusicError as e:
                text, is_error = str(e), True
        else:
            if block.name in ("pc_open_url", "pc_youtube"):
                self.brain.journal_note(f"браузер, {block.name}: {args.get('url') or args.get('query', '')} ({channel})")
            try:
                text, is_error = await self.computer.call(block.name, args), False
            except pc.PCError as e:
                text, is_error = str(e), True
        failed = {"is_error": True} if is_error else {}
        # результат — в журнал: по нему модель в следующих ходах видит, что и чем кончилось (см. history)
        self.store.log(user_id, channel, "tool_result", block.name, {"text": text[:1500], **failed})
        return {"type": "tool_result", "tool_use_id": block.id, "content": text, **failed}

    async def _delegate(self, user_id: str, task: str, channel: str) -> AsyncIterator[str]:
        self.store.log(user_id, channel, "tool", "delegate", {"task": task[:1500]})
        job = asyncio.create_task(self.brain.ask(user_id, task + VOICE_SUFFIX, channel=f"{channel}-agent"))
        try:
            done, _ = await asyncio.wait({job}, timeout=self.s.voice_agent_timeout)
        except asyncio.CancelledError:  # пользователь перебил — агента останавливаем
            job.cancel()
            raise
        if done:
            reply = job.result()
            self.store.log(user_id, channel, "tool_result", "delegate",
                           {"text": reply.text[:1500], **({"is_error": True} if reply.is_error else {})})
            yield " " + reply.text
            return
        self.store.log(user_id, channel, "tool_result", "delegate",
                       {"text": "Агент ещё работает; результат придёт пользователю отдельным сообщением."})
        where = f"Пришлю результат в {self.notifier.where()}." if self.notifier.available else "Результат будет в журнале."
        yield " Это займёт больше времени. " + where
        self._background.add(job)
        job.add_done_callback(lambda j: self._finished_later(j, task))

    def _finished_later(self, job: asyncio.Task, task: str) -> None:
        self._background.discard(job)
        if job.cancelled():
            return
        exc = job.exception()
        text = f"Ошибка: {exc}" if exc else job.result().text
        t = asyncio.get_running_loop().create_task(self.notifier.send(f"Готово: {task[:100]}\n\n{text}", title="Задача готова"))
        self._background.add(t)
        t.add_done_callback(self._background.discard)


def _digest(system: list[dict], tools: list[dict]) -> str:
    """Отпечаток начала запроса: другое начало — другая запись в кэше."""
    return hashlib.sha256(json.dumps([system, tools], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
