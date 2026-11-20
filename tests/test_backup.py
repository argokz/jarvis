"""Резервная копия: vault и согласованный снимок базы в одном архиве, раз в сутки, со сроком хранения."""
import asyncio
import os
import sqlite3
import tarfile

import pytest

from jarvis.backup import Backup
from jarvis.store import Store


@pytest.fixture
def made(tmp_path):
    vault = tmp_path / "vault"
    (vault / "10_Projects").mkdir(parents=True)
    (vault / "10_Projects" / "дом.md").write_text("# Дом\n", encoding="utf-8")
    store = Store(tmp_path / "data" / "jarvis.db")
    store.log("owner", "app", "user", payload="важная реплика")
    clock = [1_795_000_000.0]
    return Backup(vault, tmp_path / "data" / "jarvis.db", tmp_path / "copies", keep=3, clock=lambda: clock[0]), clock, tmp_path


def test_archive_holds_the_vault_and_a_readable_database(made):
    backup, _, tmp = made
    assert backup.due() and backup.copies() == []
    path = backup.make()
    assert path.parent == tmp / "copies" and path.name.startswith("jarvis-") and path.name.endswith(".tar.gz")
    with tarfile.open(path) as tar:
        assert {"vault/10_Projects/дом.md", "data/jarvis.db"} <= set(tar.getnames())
        tar.extract("data/jarvis.db", tmp / "restored", filter="data")
    rows = sqlite3.connect(tmp / "restored" / "data" / "jarvis.db").execute("select payload from journal").fetchall()
    assert rows == [('"важная реплика"',)]   # снимок сделан самим SQLite, а не копией файла посреди записи
    assert not list((tmp / "copies").glob("*.part"))


def test_copy_is_due_once_a_day_and_old_ones_are_removed(made):
    backup, clock, tmp = made
    (tmp / "copies").mkdir()
    (tmp / "copies" / "чужой-файл.txt").write_text("не трогать")
    names = []
    for day in range(5):
        clock[0] = 1_795_000_000.0 + day * 86400
        path = backup.make()
        os.utime(path, (clock[0], clock[0]))
        names.append(path.name)
        assert not backup.due()                      # сегодня копия уже есть
        clock[0] += 86400
        assert backup.due()                          # через сутки — пора
    assert [p.name for p in backup.copies()] == names[:1:-1]   # остались три последние
    assert (tmp / "copies" / "чужой-файл.txt").exists()
    assert "Копий: 3" in backup.summary()


def test_backup_is_off_without_a_folder(tmp_path):
    off = Backup(tmp_path / "vault", tmp_path / "j.db", None)
    assert not off.enabled and not off.due() and off.copies() == []


def test_loop_makes_a_copy_and_survives_a_failure(made):
    backup, _, tmp = made

    async def stop(_):
        raise asyncio.CancelledError

    backup._sleep = stop
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(backup.run())
    assert len(backup.copies()) == 1
    broken = Backup(tmp / "vault", tmp / "data" / "jarvis.db", tmp / "copies" / "jarvis-x.tar.gz" / "нельзя", sleep=stop)
    (tmp / "copies" / "jarvis-x.tar.gz").write_text("файл на месте папки")
    with pytest.raises(asyncio.CancelledError):   # сбой копии записан в журнал, цикл дошёл до ожидания
        asyncio.run(broken.run())


def test_tests_never_use_the_owners_backup_folder():
    from jarvis.config import Settings

    s = Settings()
    assert s.backup_dir is None and s.watch_min == 0   # см. conftest.py: иначе тест ядра пишет в настоящую папку
