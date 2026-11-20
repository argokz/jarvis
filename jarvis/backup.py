"""Резервная копия памяти ассистента: vault целиком и согласованный снимок базы — в один архив.

Копия делается раз в сутки в папку JARVIS_BACKUP_DIR; старые архивы сверх JARVIS_BACKUP_KEEP удаляются.
Папка на том же диске спасает от случайного удаления и испорченной базы, но не от поломки диска —
для этого укажите другой диск или папку, которую синхронизирует облако.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("jarvis.backup")
PREFIX, SUFFIX = "jarvis-", ".tar.gz"
CHECK_EVERY = 3600.0  # как часто смотреть, не пора ли


class Backup:
    def __init__(self, vault: Path, db: Path, target: Path | None, keep: int = 14, hours: float = 24.0,
                 clock=time.time, sleep=asyncio.sleep):
        self.vault, self.db, self.target = vault, db, target
        self.keep, self.period = max(1, keep), hours * 3600
        self._clock, self._sleep = clock, sleep

    @property
    def enabled(self) -> bool:
        return self.target is not None

    def copies(self) -> list[Path]:
        """Свои архивы в папке, от новых к старым. Чужие файлы там не трогаем."""
        if not self.enabled or not self.target.is_dir():
            return []
        return sorted((p for p in self.target.glob(f"{PREFIX}*{SUFFIX}") if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)

    def due(self) -> bool:
        copies = self.copies()
        return self.enabled and (not copies or self._clock() - copies[0].stat().st_mtime >= self.period)

    def make(self) -> Path:
        """Архив пишется под временным именем и переименовывается: оборванная копия не выглядит готовой."""
        self.target.mkdir(parents=True, exist_ok=True)
        name = f"{PREFIX}{datetime.fromtimestamp(self._clock()):%Y%m%d-%H%M%S}{SUFFIX}"
        final, partial = self.target / name, self.target / (name + ".part")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / self.db.name
            if self.db.is_file():  # снимок через сам SQLite: копия файла во время записи была бы битой
                src, dst = sqlite3.connect(self.db), sqlite3.connect(snapshot)
                try:
                    src.backup(dst)
                finally:
                    src.close()
                    dst.close()
            with tarfile.open(partial, "w:gz") as tar:
                if self.vault.is_dir():
                    tar.add(self.vault, arcname="vault")
                if snapshot.is_file():
                    tar.add(snapshot, arcname=f"data/{self.db.name}")
        partial.replace(final)
        for old in self.copies()[self.keep:]:
            old.unlink()
        return final

    def summary(self) -> str:
        copies = self.copies()
        if not copies:
            return "Копий пока нет."
        newest = copies[0]
        when = datetime.fromtimestamp(newest.stat().st_mtime)
        return f"Копий: {len(copies)}, последняя — {when:%d.%m %H:%M}, {newest.stat().st_size / 1024:.0f} КБ, в {self.target}."

    async def run(self) -> None:
        while True:
            try:
                if self.due():
                    made = await asyncio.to_thread(self.make)
                    log.info("резервная копия: %s", made)
            except Exception:
                log.exception("резервная копия не удалась")
            await self._sleep(CHECK_EVERY)
