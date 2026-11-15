"""Уведомления вне текущего канала: Telegram и push через ntfy (работает на Android без GMS)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("jarvis.notify")


class Notifier:
    def __init__(self, ntfy_url: str = "", ntfy_topic: str = "", ntfy_token: str = "",
                 client: httpx.AsyncClient | None = None):
        self._bot = None
        self._chats: list[int] = []
        self.ntfy_url, self.ntfy_topic, self.ntfy_token = ntfy_url.rstrip("/"), ntfy_topic, ntfy_token
        self._http = client

    def use_telegram(self, bot, chat_ids) -> None:
        self._bot, self._chats = bot, list(chat_ids)

    @property
    def telegram(self) -> bool:
        return bool(self._bot and self._chats)

    @property
    def ntfy(self) -> bool:
        return bool(self.ntfy_url and self.ntfy_topic)

    @property
    def available(self) -> bool:
        return self.telegram or self.ntfy

    def where(self) -> str:
        names = [n for n, on in (("Telegram", self.telegram), ("push", self.ntfy)) if on]
        return " и ".join(names)

    async def send(self, text: str, title: str = "Jarvis", priority: int = 3) -> None:
        if not self.available:
            log.info("уведомление некуда отправить: %s", text[:200])
        await self._telegram(text)
        await self._push(text, title, priority, ["robot"])

    async def confirm(self, confirm_id: str, title: str, details: str) -> None:
        """Telegram — с кнопками. В push только напоминание: секретов и ссылок на действие в нём нет."""
        markup = None
        if self.telegram:
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data=f"cf:{confirm_id}:y"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"cf:{confirm_id}:n"),
            ]])
        body = f"Нужно подтверждение: {title}" + (f"\n\n{details[:1500]}" if details else "")
        await self._telegram(body, markup)
        await self._push(f"{title}\nОткройте приложение или Telegram, чтобы ответить.",
                         "Нужно подтверждение", 5, ["warning"])

    async def _telegram(self, text: str, markup=None) -> None:
        if not self.telegram:
            return
        for chat in self._chats:
            try:
                parts = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
                for i, part in enumerate(parts):
                    last = i == len(parts) - 1
                    await self._bot.send_message(chat, part, reply_markup=markup if last else None)
            except Exception:
                log.exception("не удалось отправить в Telegram")

    async def _push(self, text: str, title: str, priority: int, tags: list[str]) -> None:
        if not self.ntfy:
            return
        headers = {"Authorization": f"Bearer {self.ntfy_token}"} if self.ntfy_token else {}
        payload = {"topic": self.ntfy_topic, "message": text[:3500], "title": title,
                   "priority": priority, "tags": tags}
        try:
            client = self._http or httpx.AsyncClient(timeout=10)
            try:
                r = await client.post(self.ntfy_url, json=payload, headers=headers)
                if r.status_code >= 300:
                    log.error("ntfy вернул %s: %s", r.status_code, r.text[:200])
            finally:
                if self._http is None:
                    await client.aclose()
        except httpx.HTTPError as e:
            log.error("ntfy недоступен: %r", e)
