"""Telegram — первый тонкий клиент: текст и голосовые сообщения."""
from __future__ import annotations

import io
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from . import registry
from .speech import MAX_STT_SECONDS, SpeechError, SpeechKit
from .agent import Brain
from .config import Settings

log = logging.getLogger("jarvis.telegram")
OWNER = "owner"  # один пользователь: все каналы ведут в одну сессию
LIMIT = 4000


def chunks(text: str, limit: int = LIMIT):
    while text:
        if len(text) <= limit:
            yield text
            return
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def build(settings: Settings, brain: Brain, speech: SpeechKit, confirms=None) -> tuple[Bot, Dispatcher]:
    bot = Bot(settings.telegram_token)
    dp = Dispatcher()

    def allowed(m: Message) -> bool:
        return bool(m.from_user) and m.from_user.id in settings.telegram_allowed

    async def answer(m: Message, text: str) -> None:
        async with ChatActionSender.typing(bot=bot, chat_id=m.chat.id):
            reply = await brain.ask(OWNER, text, channel="telegram")
        for part in chunks(reply.text):
            await m.answer(part)

    @dp.callback_query(F.data.startswith("cf:"))
    async def on_confirm(q: CallbackQuery):
        if not q.from_user or q.from_user.id not in settings.telegram_allowed or confirms is None:
            return await q.answer("Нет доступа")
        _, cid, verdict = q.data.split(":", 2)
        ok = confirms.resolve(cid, verdict == "y", by="telegram")
        await q.answer("Принято" if ok else "Уже неактуально")
        if q.message:
            mark = ("✅ Подтверждено" if verdict == "y" else "❌ Отклонено") if ok else "⌛ Неактуально"
            await q.message.edit_text(f"{q.message.text}\n\n{mark}")

    @dp.message(~F.func(allowed))
    async def stranger(m: Message):
        uid = m.from_user.id if m.from_user else "?"
        log.warning("отказ: telegram id %s", uid)
        await m.answer(f"Нет доступа. Ваш Telegram ID: {uid} — добавьте его в TELEGRAM_ALLOWED_IDS.")

    @dp.message(Command("start", "help"))
    async def start(m: Message):
        await m.answer("Джарвис на связи. Пишите или присылайте голосовые.\n"
                       "/projects — реестр проектов\n/new — начать разговор заново")

    @dp.message(Command("new"))
    async def new(m: Message):
        brain.new_session(OWNER)
        await m.answer("Начал новый разговор. Память в vault сохранена.")

    @dp.message(Command("projects"))
    async def projects(m: Message):  # без модели: прямо из реестра
        await m.answer(registry.summary(registry.load_projects(settings.vault)))

    @dp.message(F.voice)
    async def voice(m: Message):
        if not speech.enabled:
            return await m.answer("Голосовые не настроены: задайте YANDEX_SPEECHKIT_API_KEY.")
        if m.voice.duration > MAX_STT_SECONDS:
            return await m.answer(f"Пока понимаю голосовые до {MAX_STT_SECONDS} секунд.")
        buf = io.BytesIO()
        await bot.download(m.voice, destination=buf)
        try:
            text = await speech.recognize(buf.getvalue(), fmt="oggopus")
        except SpeechError as e:
            return await m.answer(str(e))
        if not text:
            return await m.answer("Не расслышал, повторите.")
        await m.answer(f"🎙 {text}")
        await answer(m, text)

    @dp.message(F.text)
    async def text(m: Message):
        await answer(m, m.text)

    return bot, dp
