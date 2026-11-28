"""Import the previous release's SQLite store on first use of the file store."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from .state import (
    HISTORY_TYPES,
    SESSION_ID,
    _encode_json,
    _sync_directory,
    _write_state,
)

_HISTORY = (
    ("message", "SELECT payload FROM messages WHERE session_id = ? ORDER BY position"),
    ("event", "SELECT payload FROM events WHERE session_id = ? ORDER BY position"),
    ("memory", "SELECT payload FROM memory WHERE session_id = ? ORDER BY position"),
)


def migrate_sqlite(source: Path, destination: Path) -> None:
    """Read and stage a complete import before publishing the sessions directory."""
    try:
        with (
            closing(
                sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db,
            TemporaryDirectory(prefix=".import-", dir=destination.parent) as temp,
        ):
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            staged = Path(temp) / destination.name
            staged.mkdir(mode=0o700)
            for row in db.execute(
                "SELECT id, title, created_at, updated_at, options, context_window "
                "FROM sessions ORDER BY rowid"
            ):
                _import_session(db, row, staged)
            _sync_directory(staged)
            staged.rename(destination)
            _sync_directory(destination.parent)
    except (sqlite3.Error, TypeError, ValueError, OSError) as error:
        raise ValueError(f"无法从 {source.name} 导入会话：{error}") from error


def _import_session(db: sqlite3.Connection, row: sqlite3.Row, staged: Path) -> None:
    session_id = row["id"]
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("旧会话 ID 格式无效。")
    folder = staged / session_id
    folder.mkdir(mode=0o700)
    trusted = [
        command["signature"]
        for command in db.execute(
            "SELECT signature FROM trusted_commands WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        )
    ]
    if not all(isinstance(signature, str) for signature in trusted):
        raise TypeError("旧会话的命令信任记录格式无效。")
    snapshot = {
        "id": session_id,
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "options": json.loads(row["options"]),
        "context_window": json.loads(row["context_window"]),
        "trusted_commands": trusted,
    }
    _write_state(folder, snapshot)

    with (folder / "history.jsonl").open("xb") as fp:
        # SQLite preserved the order within each table and stored one timestamp per session.
        at = row["updated_at"]
        for kind, query in _HISTORY:
            for record in db.execute(query, (session_id,)):
                value = json.loads(record["payload"])
                if not isinstance(value, HISTORY_TYPES[kind]):
                    raise TypeError(f"旧会话 {session_id} 的 {kind} 记录格式无效。")
                fp.write(_encode_json({"kind": kind, "at": at, "value": value}))
        fp.flush()
        os.fsync(fp.fileno())
    _sync_directory(folder)
