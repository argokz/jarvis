"""Быстрый голосовой контур: Haiku отвечает сам или передаёт задачу агенту (Brain)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime

import anthropic

from . import registry
from .agent import Brain
from .config import Settings
from .notify import Notifier
from .store import Store

log = logging.getLogger("jarvis.router")

SYSTEM = """Ты — голосовой интерфейс личного ассистента. Тебя слушают, а не читают: отвечай одной–тремя \
короткими фразами разговорным языком пользователя (обычно русским), без списков, разметки, ссылок и \
эмодзи. Числа, даты и сокращения пиши так, чтобы их было удобно произнести. Не произноси своё имя.

Отвечай сам, если хватает общего знания, контекста разговора или сводки реестра ниже: приветствия, \
простые вопросы и факты, арифметика, вопросы о проектах на уровне сводки.

Всё остальное передавай инструменту delegate — это полноценный агент с доступом к Obsidian-vault \
(заметки, память о пользователе, подробности проектов, журнал){capabilities}. Обязательно делегируй, \
когда нужно что-то записать, запомнить или найти в заметках; узнать о проекте больше, чем есть в \
сводке; вспомнить факты о пользователе{open_hint}. Не выдумывай содержимое заметок, памяти и файлов — \
если не знаешь, делегируй. Перед вызовом delegate скажи короткую фразу вроде «Секунду, посмотрю.» и \
сразу вызывай инструмент. Агент не слышит разговор, поэтому в task сформулируй самодостаточную задачу \
со всем нужным контекстом.

Текущее время пользователя указано в квадратных скобках в начале его реплики.

Сводка реестра проектов:
{registry}
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

VOICE_SUFFIX = "\n\n(Голосовой запрос: ответь одной–тремя короткими фразами без разметки — ответ будет озвучен.)"
HISTORY_TURNS = 12


class VoiceRouter:
    def __init__(self, settings: Settings, brain: Brain, store: Store, notifier: Notifier,
                 client: anthropic.AsyncAnthropic | None = None):
        self.s, self.brain, self.store, self.notifier = settings, brain, store, notifier
        self._client = client
        self._background: set[asyncio.Task] = set()

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:  # ленивое создание: без ключа ядро всё равно стартует
            self._client = anthropic.AsyncAnthropic()
        return self._client

    def system(self) -> str:
        has_open = bool(self.s.open_roots)
        return SYSTEM.format(
            capabilities=", а также к поиску и открытию файлов на компьютере" if has_open else "",
            open_hint="; найти или открыть файл, документ, таблицу на компьютере" if has_open else "",
            registry=registry.summary(registry.load_projects(self.s.vault)),
        )

    def history(self, user_id: str) -> list[dict]:
        """Последние реплики из журнала всех каналов: разговор общий для Telegram, голоса и API."""
        msgs: list[dict] = []
        for role, text in self.store.history(user_id, HISTORY_TURNS * 2):
            text = text[:1500]
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + text
            else:
                msgs.append({"role": role, "content": text})
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        while msgs and msgs[-1]["role"] != "assistant":  # следом пойдёт новая реплика пользователя
            msgs.pop()
        return msgs

    async def respond(self, user_id: str, text: str, channel: str = "voice") -> AsyncIterator[str]:
        """Отдаёт фрагменты ответа по мере генерации — их сразу можно озвучивать."""
        messages = self.history(user_id) + [{"role": "user", "content": f"[{datetime.now():%Y-%m-%d %H:%M, %a}] {text}"}]
        self.store.log(user_id, channel, "user", payload=text)
        spoken: list[str] = []
        try:
            task: str | None = None
            final = None
            try:
                async with self.client.messages.stream(
                    model=self.s.router_model,
                    max_tokens=2048,
                    system=self.system(),
                    tools=[DELEGATE],
                    output_config={"effort": self.s.router_effort},
                    cache_control={"type": "ephemeral"},
                    messages=messages,
                ) as stream:
                    async for event in stream:
                        if event.type == "text" and event.text:
                            spoken.append(event.text)
                            yield event.text
                    final = await stream.get_final_message()
            except ValueError:
                # ввод инструмента не разобрался как JSON — отдаём агенту исходную реплику
                task = text
            except (anthropic.APIError, TypeError) as e:
                if isinstance(e, TypeError) and "authentication" not in str(e):
                    raise
                log.error("роутер: ошибка API: %r", e)
                msg = ("Не задан ключ Anthropic API." if isinstance(e, TypeError)
                       else "Не получилось связаться с моделью, попробуйте ещё раз.")
                spoken.append(msg)
                yield msg
                return

            if final is not None:
                if final.stop_reason == "refusal":
                    msg = "С этим я помочь не могу."
                    spoken.append(msg)
                    yield msg
                    return
                for block in final.content:
                    if block.type == "tool_use" and block.name == "delegate":
                        args = block.input if isinstance(block.input, dict) else {}
                        t = args.get("task")
                        ok = isinstance(t, str) and t.strip() and final.stop_reason != "max_tokens"
                        task = t.strip() if ok else text
                        break

            if task:
                if not "".join(spoken).strip():
                    filler = "Секунду. "
                    spoken.append(filler)
                    yield filler
                async for chunk in self._delegate(user_id, task, channel):
                    spoken.append(chunk)
                    yield chunk
        finally:
            self.store.log(user_id, channel, "assistant", payload={"text": "".join(spoken).strip()})

    async def _delegate(self, user_id: str, task: str, channel: str) -> AsyncIterator[str]:
        job = asyncio.create_task(self.brain.ask(user_id, task + VOICE_SUFFIX, channel=f"{channel}-agent"))
        try:
            done, _ = await asyncio.wait({job}, timeout=self.s.voice_agent_timeout)
        except asyncio.CancelledError:  # пользователь перебил — агента останавливаем
            job.cancel()
            raise
        if done:
            yield " " + job.result().text
            return
        where = "Пришлю результат в Telegram." if self.notifier.available else "Результат будет в журнале."
        yield " Это займёт больше времени. " + where
        self._background.add(job)
        job.add_done_callback(lambda j: self._finished_later(j, task))

    def _finished_later(self, job: asyncio.Task, task: str) -> None:
        self._background.discard(job)
        if job.cancelled():
            return
        exc = job.exception()
        text = f"Ошибка: {exc}" if exc else job.result().text
        t = asyncio.get_running_loop().create_task(self.notifier.send(f"Готово: {task[:100]}\n\n{text}"))
        self._background.add(t)
        t.add_done_callback(self._background.discard)

