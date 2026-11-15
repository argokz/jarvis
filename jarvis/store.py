"""SQLite: единая сессия на пользователя (не на устройство) + журнал действий."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.listeners: list = []  # вызываются с каждой новой записью журнала
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
        ts = time.time()
        cur = self.db.execute(
            "INSERT INTO journal(ts,user_id,channel,kind,name,payload) VALUES(?,?,?,?,?,?)",
            (ts, user_id, channel, kind, name, text),
        )
        entry = {"id": cur.lastrowid, "ts": ts, "user_id": user_id, "channel": channel, "kind": kind,
                 "name": name, "payload": text}
        for fn in self.listeners:
            try:
                fn(entry)
            except Exception:  # слушатель не должен ломать журнал
                pass

    def messages(self, user_id: str, limit: int = 24, before: int | None = None) -> list[dict]:
        """Реплики разговора по порядку, без внутренних каналов «*-agent» и пустых."""
        cur = self.db.execute(
            "SELECT id, ts, channel, kind, payload FROM journal WHERE user_id=? AND kind IN ('user','assistant') "
            "AND (channel IS NULL OR channel NOT LIKE '%-agent') AND id < ? ORDER BY id DESC LIMIT ?",
            (user_id, before or 2**62, limit),
        )
        out = []
        for id_, ts, channel, kind, payload in reversed(cur.fetchall()):
            text = _text(payload)
            if text.strip():
                out.append({"id": id_, "ts": ts, "channel": channel, "role": kind, "text": text})
        return out

    def history(self, user_id: str, limit: int = 24) -> list[tuple[str, str]]:
        return [(m["role"], m["text"]) for m in self.messages(user_id, limit)]

    def recent(self, limit: int = 50) -> list[dict]:
        cur = self.db.execute(
            "SELECT id,ts,user_id,channel,kind,name,payload FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(zip(("id", "ts", "user_id", "channel", "kind", "name", "payload"), r)) for r in cur]


def _text(payload: str | None) -> str:
    if not payload:
        return ""
    try:
        data = json.loads(payload)
    except ValueError:
        return payload
    return str(data.get("text") or "") if isinstance(data, dict) else str(data)
