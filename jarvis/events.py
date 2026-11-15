"""Шина событий: всё, что происходит в ядре, сразу видно всем подключённым клиентам."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("jarvis.events")


class EventBus:
    def __init__(self, queue_size: int = 500):
        self._subs: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}
        self._size = queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._size)
        self._subs[q] = asyncio.get_running_loop()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.pop(q, None)

    def publish(self, event: dict) -> None:
        """Не блокирует и безопасна из любого потока. Медленный подписчик теряет события."""
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        for q, loop in list(self._subs.items()):
            if loop is current:
                self._put(q, event)
            elif not loop.is_closed():
                loop.call_soon_threadsafe(self._put, q, event)

    @staticmethod
    def _put(q: asyncio.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("подписчик не успевает, событие пропущено")
