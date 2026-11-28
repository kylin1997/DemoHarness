import json
import sqlite3
from contextlib import closing

import pytest

from demoharness.application import Application

SESSION = "a" * 32
MESSAGE = {"role": "user", "content": "核对笔记", "run_id": "first-run"}
EVENT = {"type": "stage", "stage": "input", "run_id": "first-run", "at": 10.5}
OPTIONS = {"profile": "demo", "reasoning": "high", "mode": "plan"}
MEMORY = "笔记位于 notes.md"
SIGNATURE = '["printf","hello"]'


def _legacy_store(database):
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, created_at REAL,
                                   updated_at REAL, options TEXT, context_window TEXT);
            CREATE TABLE messages (position INTEGER PRIMARY KEY, session_id TEXT, payload TEXT);
            CREATE TABLE events (position INTEGER PRIMARY KEY, session_id TEXT, payload TEXT);
            CREATE TABLE memory (position INTEGER PRIMARY KEY, session_id TEXT, payload TEXT);
            CREATE TABLE trusted_commands (session_id TEXT, signature TEXT,
                                           PRIMARY KEY (session_id, signature));
        """)
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            (SESSION, "旧版会话", 10, 11, json.dumps(OPTIONS), json.dumps([MESSAGE])),
        )
        db.execute(
            "INSERT INTO messages VALUES (1, ?, ?)", (SESSION, json.dumps(MESSAGE))
        )
        db.execute("INSERT INTO events VALUES (1, ?, ?)", (SESSION, json.dumps(EVENT)))
        db.execute("INSERT INTO memory VALUES (1, ?, ?)", (SESSION, json.dumps(MEMORY)))
        db.execute("INSERT INTO trusted_commands VALUES (?, ?)", (SESSION, SIGNATURE))


def _app(state):
    return Application(state, None, state / "skills")


def test_application_imports_legacy_store_once(tmp_path):
    database = tmp_path / "harness.sqlite3"
    _legacy_store(database)
    original = database.read_bytes()
    folder = tmp_path / "sessions" / SESSION

    app = _app(tmp_path)
    try:
        assert app.store.session(SESSION)["options"] == OPTIONS
        assert app.store.messages(SESSION) == app.store.window(SESSION) == [MESSAGE]
        assert app.store.events(SESSION) == [EVENT]
        assert app.store.recall(SESSION) == [MEMORY]
        assert app.store.is_trusted(SESSION, SIGNATURE)
        snapshot = json.loads((folder / "state.json").read_text())
        assert snapshot["title"] == "旧版会话"
        assert snapshot["context_window"] == [MESSAGE]
        log = [
            json.loads(line)
            for line in (folder / "history.jsonl").read_text().splitlines()
        ]
        assert [(row["kind"], row["value"]) for row in log] == [
            ("message", MESSAGE),
            ("event", EVENT),
            ("memory", MEMORY),
        ]
        app.store.rename(SESSION, "迁移后继续")
    finally:
        app.store.close()

    assert database.read_bytes() == original
    reopened = _app(tmp_path)
    try:
        assert reopened.store.session(SESSION)["title"] == "迁移后继续"
        assert reopened.store.messages(SESSION) == [MESSAGE]
    finally:
        reopened.store.close()


def test_failed_import_can_be_retried_without_partial_sessions(tmp_path):
    database = tmp_path / "harness.sqlite3"
    _legacy_store(database)
    invalid = "b" * 32
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
            (invalid, "第二个旧版会话", 12, 12, "{}", "invalid JSON"),
        )

    with pytest.raises(ValueError, match="无法从 harness.sqlite3 导入会话"):
        _app(tmp_path)
    assert not (tmp_path / "sessions").exists()

    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE sessions SET context_window = '[]' WHERE id = ?", (invalid,))
    app = _app(tmp_path)
    try:
        assert {session["id"] for session in app.store.list_sessions()} == {
            SESSION,
            invalid,
        }
    finally:
        app.store.close()
