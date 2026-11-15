"""Уведомления пользователю вне текущего канала (пока — Telegram)."""
from __future__ import annotations

import logging

log = logging.getLogger("jarvis.notify")


class Notifier:
    def __init__(self):
        self._bot = None
        self._chats: list[int] = []

    def use_telegram(self, bot, chat_ids) -> None:
        self._bot, self._chats = bot, list(chat_ids)

    @property
    def available(self) -> bool:
        return bool(self._bot and self._chats)

    async def send(self, text: str) -> None:
        if not self.available:
            log.info("уведомление некуда отправить: %s", text[:200])
            return
        for chat in self._chats:
            try:
                for i in range(0, len(text), 4000):
                    await self._bot.send_message(chat, text[i:i + 4000])
            except Exception:
                log.exception("не удалось отправить уведомление в Telegram")
