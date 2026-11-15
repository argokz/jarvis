"""Подтверждения опасных действий: вопрос уходит во все каналы, ответ — из любого."""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass, field

from .events import EventBus
from .notify import Notifier
from .store import Store


@dataclass
class Pending:
    id: str
    title: str
    details: str
    channel: str
    created: float
    expires: float
    future: asyncio.Future = field(repr=False)

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "details": self.details, "channel": self.channel,
                "created": self.created, "expires": self.expires}


class Confirmations:
    def __init__(self, bus: EventBus, notifier: Notifier, store: Store, user_id: str = "owner"):
        self.bus, self.notifier, self.store, self.user = bus, notifier, store, user_id
        self.items: dict[str, Pending] = {}

    def pending(self) -> list[dict]:
        return [p.public() for p in self.items.values()]

    async def ask(self, title: str, details: str = "", timeout: float = 300.0, channel: str = "") -> bool:
        """Ждёт решения пользователя. Нет ответа за timeout — значит «нет»."""
        now = time.time()
        item = Pending(secrets.token_urlsafe(8), title, details[:2000], channel, now, now + timeout,
                       asyncio.get_running_loop().create_future())
        self.items[item.id] = item
        self.store.log(self.user, channel, "confirm_asked", title, {"id": item.id, "details": item.details})
        self.bus.publish({"type": "confirm", **item.public()})
        with contextlib.suppress(Exception):
            await self.notifier.confirm(item.id, title, item.details)
        try:
            return await asyncio.wait_for(asyncio.shield(item.future), timeout)
        except TimeoutError:
            self._close(item, False, "timeout")
            return False
        except asyncio.CancelledError:
            self._close(item, False, "cancelled")
            raise
        finally:
            self.items.pop(item.id, None)

    def resolve(self, confirm_id: str, approved: bool, by: str) -> bool:
        item = self.items.get(confirm_id)
        if item is None or item.future.done():
            return False
        item.future.set_result(approved)
        self._close(item, approved, by)
        return True

    def _close(self, item: Pending, approved: bool, by: str) -> None:
        self.store.log(self.user, by, "confirm_resolved", item.title, {"id": item.id, "approved": approved})
        self.bus.publish({"type": "confirm_resolved", "id": item.id, "approved": approved, "by": by})
