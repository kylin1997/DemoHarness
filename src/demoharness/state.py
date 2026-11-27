"""Persist the chat transcript, model context, and session-scoped decisions."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

DEFAULT_OPTIONS = {"profile": "demo", "reasoning": "default", "mode": "react"}


class Store:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                options TEXT NOT NULL,
                context_window TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS messages (
                position INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                position INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory (
                position INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trusted_commands (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                signature TEXT NOT NULL,
                PRIMARY KEY (session_id, signature)
            );
            CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id);
            CREATE INDEX IF NOT EXISTS memory_session ON memory(session_id);
            """
        )

    def close(self) -> None:
        self._db.close()

    def create_session(self, title: str = "新会话") -> dict:
        session_id = uuid.uuid4().hex
        now = time.time()
        with self._db:
            self._db.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at, options) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, title, now, now, json.dumps(DEFAULT_OPTIONS)),
            )
        return self.session(session_id)

    def list_sessions(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, title, created_at, updated_at, options FROM sessions "
            "ORDER BY updated_at DESC, rowid DESC"
        )
        return [self._session_dict(row) for row in rows]

    def session(self, session_id: str) -> dict:
        row = self._db.execute(
            "SELECT id, title, created_at, updated_at, options FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return self._session_dict(row)

    @staticmethod
    def _session_dict(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["options"] = json.loads(result["options"])
        return result

    def update_options(self, session_id: str, options: dict) -> dict:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            updated = self.session(session_id)["options"] | options
            self._db.execute(
                "UPDATE sessions SET options = ?, updated_at = ? WHERE id = ?",
                (json.dumps(updated), time.time(), session_id),
            )
        return self.session(session_id)

    def rename(self, session_id: str, title: str) -> None:
        with self._db:
            result = self._db.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
                (title, time.time(), session_id),
            )
            if result.rowcount == 0:
                raise KeyError(session_id)

    def add_message(self, session_id: str, message: dict) -> None:
        self._append("messages", session_id, message)

    def messages(self, session_id: str) -> list[dict]:
        return self._read("messages", session_id)

    def save_window(self, session_id: str, messages: list[dict]) -> None:
        with self._db:
            result = self._db.execute(
                "UPDATE sessions SET context_window = ?, updated_at = ? WHERE id = ?",
                (json.dumps(messages), time.time(), session_id),
            )
            if result.rowcount == 0:
                raise KeyError(session_id)

    def window(self, session_id: str) -> list[dict]:
        row = self._db.execute(
            "SELECT context_window FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return json.loads(row["context_window"])

    def append_event(self, session_id: str, event: dict) -> None:
        self._append("events", session_id, event)

    def events(self, session_id: str) -> list[dict]:
        return self._read("events", session_id)

    def remember(self, session_id: str, text: str) -> None:
        self._append("memory", session_id, text)

    def recall(self, session_id: str) -> list[str]:
        return self._read("memory", session_id)

    def is_trusted(self, session_id: str, signature: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM trusted_commands WHERE session_id = ? AND signature = ?",
            (session_id, signature),
        ).fetchone()
        return row is not None

    def trust(self, session_id: str, signature: str) -> None:
        with self._db:
            self._touch(session_id)
            self._db.execute(
                "INSERT INTO trusted_commands (session_id, signature) VALUES (?, ?) "
                "ON CONFLICT (session_id, signature) DO NOTHING",
                (session_id, signature),
            )

    def _append(self, table: str, session_id: str, value: dict | str) -> None:
        with self._db:
            self._touch(session_id)
            # The table names come from the fixed Store methods above.
            self._db.execute(
                f"INSERT INTO {table} (session_id, payload) VALUES (?, ?)",
                (session_id, json.dumps(value)),
            )

    def _read(self, table: str, session_id: str) -> list:
        rows = self._db.execute(
            f"SELECT payload FROM {table} WHERE session_id = ? ORDER BY position",
            (session_id,),
        )
        return [json.loads(row["payload"]) for row in rows]

    def _touch(self, session_id: str) -> None:
        result = self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
        )
        if result.rowcount == 0:
            raise KeyError(session_id)
