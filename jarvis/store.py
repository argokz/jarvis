"""SQLite: единая сессия на пользователя (не на устройство) + журнал действий."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions(
                user_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS journal(
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, user_id TEXT NOT NULL,
                channel TEXT, kind TEXT NOT NULL, name TEXT, payload TEXT);
            """
        )

    def get_session(self, user_id: str) -> str | None:
        row = self.db.execute("SELECT session_id FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

    def set_session(self, user_id: str, session_id: str) -> None:
        self.db.execute(
            "INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "session_id=excluded.session_id, updated_at=excluded.updated_at",
            (user_id, session_id, time.time()),
        )

    def drop_session(self, user_id: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def log(self, user_id: str, channel: str, kind: str, name: str = "", payload=None) -> None:
        text = json.dumps(payload, ensure_ascii=False, default=str)[:4000] if payload is not None else None
        self.db.execute(
            "INSERT INTO journal(ts,user_id,channel,kind,name,payload) VALUES(?,?,?,?,?,?)",
            (time.time(), user_id, channel, kind, name, text),
        )

    def recent(self, limit: int = 50) -> list[dict]:
        cur = self.db.execute(
            "SELECT ts,user_id,channel,kind,name,payload FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(zip(("ts", "user_id", "channel", "kind", "name", "payload"), r)) for r in cur]
