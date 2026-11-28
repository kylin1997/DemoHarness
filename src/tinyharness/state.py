"""会话持久化：以 JSONL 追加消息、事件和记忆，以 JSON 快照保存配置及模型窗口。

目录锁限制单进程写入，原子替换保护状态快照，重开存储时清理日志末尾的未完成行。
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

DEFAULT_OPTIONS = {"profile": "demo", "reasoning": "default", "mode": "react"}
SESSION_ID = re.compile(r"[0-9a-f]{32}")
HISTORY_TYPES = {"message": dict, "event": dict, "memory": str}
SESSION_FIELDS = ("id", "title", "created_at", "updated_at", "options")


def _encode_json(value: dict, *, indent: int | None = None) -> bytes:
    encoded = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=indent) + "\n"
    )
    return encoded.encode("utf-8", errors="backslashreplace")


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _is_timestamp(value: object) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _validate_state(state: object, path: Path) -> None:
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("title"), str)
        or not _is_timestamp(state.get("created_at"))
        or not _is_timestamp(state.get("updated_at"))
        or not isinstance(state.get("options"), dict)
        or not isinstance(state.get("context_window"), list)
    ):
        raise ValueError(f"Invalid session state: {path}")


def _load_state(path: Path) -> dict:
    state_path = path / "state.json"
    state = json.loads(
        state_path.read_text(encoding="utf-8"), parse_constant=_invalid_constant
    )
    _validate_state(state, state_path)
    if state.get("id") != path.name:
        raise ValueError(f"Session ID does not match directory: {state_path}")
    trusted = state.get("trusted_commands")
    if not isinstance(trusted, list) or not all(
        isinstance(signature, str) for signature in trusted
    ):
        state["trusted_commands"] = []
    return state


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_state(path: Path, state: dict) -> None:
    state_path = path / "state.json"
    _validate_state(state, state_path)
    data = _encode_json(state, indent=2)
    with tempfile.NamedTemporaryFile(
        dir=path, prefix=".state-", delete=False
    ) as stream:
        staged = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            staged.replace(state_path)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    _sync_directory(path)


def _history(path: Path, *, recover_tail: bool = False) -> Iterator[dict]:
    with path.open("r+b" if recover_tail else "rb") as stream:
        for number, line in enumerate(stream, start=1):
            if recover_tail and not line.endswith(b"\n"):
                # A newline is the commit boundary, even if the partial JSON parses.
                stream.truncate(stream.tell() - len(line))
                os.fsync(stream.fileno())
                break
            try:
                row = json.loads(line.decode("utf-8"), parse_constant=_invalid_constant)
                kind = row.get("kind") if isinstance(row, dict) else None
                if (
                    not line.endswith(b"\n")
                    or not isinstance(kind, str)
                    or kind not in HISTORY_TYPES
                    or not isinstance(row.get("value"), HISTORY_TYPES[kind])
                    or not _is_timestamp(row.get("at"))
                ):
                    raise ValueError("Invalid history record")
            except ValueError as exc:
                raise ValueError(f"Invalid history record: {path}:{number}") from exc
            yield row


class Store:
    """管理会话目录，分别提供原始历史、模型上下文窗口和命令信任记录的读写入口。

    每个会话使用 history.jsonl 与 state.json；压缩窗口不删除原始消息。
    实例持有存储目录的写入锁，使用结束后调用 close() 释放。
    """

    def __init__(self, root: Path):
        self._root = Path(root).resolve()
        self._root.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._root.parent / f".{self._root.name}.lock"
        self._lock = lock_path.open("ab")
        self._history_updates: dict[str, float] = {}
        try:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    f"Session store is already open: {self._root}"
                ) from exc
            if not self._root.exists():
                self._root.mkdir()
                _sync_directory(self._root.parent)
            for path in self._root.iterdir():
                if not SESSION_ID.fullmatch(path.name):
                    continue
                if not path.is_dir() or path.is_symlink():
                    raise ValueError(f"Invalid session directory: {path}")
                _load_state(path)
                self._history_updates[path.name] = max(
                    (
                        row["at"]
                        for row in _history(path / "history.jsonl", recover_tail=True)
                    ),
                    default=-math.inf,
                )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._lock.close()

    def create_session(self, title: str = "新会话") -> dict:
        self._ensure_open()
        session_id = uuid.uuid4().hex
        path = self._root / session_id
        if path.exists() or path.is_symlink():
            raise FileExistsError(path)
        now = time.time()
        state = {
            "id": session_id,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "options": DEFAULT_OPTIONS,
            "context_window": [],
            "trusted_commands": [],
        }
        with tempfile.TemporaryDirectory(
            dir=self._root, prefix=".session-"
        ) as directory:
            staged = Path(directory)
            (staged / "history.jsonl").touch()
            _write_state(staged, state)
            staged.rename(path)
        self._history_updates[session_id] = -math.inf
        _sync_directory(self._root)
        return self.session(session_id)

    def list_sessions(self) -> list[dict]:
        self._ensure_open()
        return sorted(
            (self.session(session_id) for session_id in self._history_updates),
            key=lambda session: (
                session["updated_at"],
                session["created_at"],
                session["id"],
            ),
            reverse=True,
        )

    def session(self, session_id: str) -> dict:
        state = _load_state(self._session_path(session_id))
        session = {field: state[field] for field in SESSION_FIELDS}
        session["updated_at"] = max(
            session["updated_at"], self._history_updates[session_id]
        )
        return session

    def update_options(self, session_id: str, options: dict) -> dict:
        path = self._session_path(session_id)
        state = _load_state(path)
        state["options"] = state["options"] | options
        self._save_state(path, state)
        return self.session(session_id)

    def rename(self, session_id: str, title: str) -> None:
        path = self._session_path(session_id)
        state = _load_state(path)
        state["title"] = title
        self._save_state(path, state)

    def add_message(self, session_id: str, message: dict) -> None:
        self._append("message", session_id, message)

    def messages(self, session_id: str) -> list[dict]:
        return self._read("message", session_id)

    def save_window(self, session_id: str, messages: list[dict]) -> None:
        path = self._session_path(session_id)
        state = _load_state(path)
        state["context_window"] = messages
        self._save_state(path, state)

    def window(self, session_id: str) -> list[dict]:
        return _load_state(self._session_path(session_id))["context_window"]

    def append_event(self, session_id: str, event: dict) -> None:
        self._append("event", session_id, event)

    def events(self, session_id: str) -> list[dict]:
        return self._read("event", session_id)

    def remember(self, session_id: str, text: str) -> None:
        self._append("memory", session_id, text)

    def recall(self, session_id: str) -> list[str]:
        return self._read("memory", session_id)

    def is_trusted(self, session_id: str, signature: str) -> bool:
        try:
            path = self._session_path(session_id)
        except KeyError:
            return False
        return signature in _load_state(path)["trusted_commands"]

    def trust(self, session_id: str, signature: str) -> None:
        path = self._session_path(session_id)
        state = _load_state(path)
        if not isinstance(signature, str):
            raise TypeError("A command signature must be a string")
        if signature not in state["trusted_commands"]:
            state["trusted_commands"].append(signature)
        self._save_state(path, state)

    def _ensure_open(self) -> None:
        if self._lock.closed:
            raise RuntimeError("Session store is closed")

    def _session_path(self, session_id: str) -> Path:
        self._ensure_open()
        if session_id not in self._history_updates:
            raise KeyError(session_id)
        return self._root / session_id

    @staticmethod
    def _save_state(path: Path, state: dict) -> None:
        state["updated_at"] = time.time()
        _write_state(path, state)

    def _append(self, kind: str, session_id: str, value: dict | str) -> None:
        path = self._session_path(session_id) / "history.jsonl"
        if not isinstance(value, HISTORY_TYPES[kind]):
            raise TypeError(f"Invalid history value for {kind}")
        now = time.time()
        data = _encode_json({"kind": kind, "at": now, "value": value})
        with path.open("r+b", buffering=0) as stream:
            offset = stream.seek(0, os.SEEK_END)
            if offset:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    raise ValueError(f"Incomplete history record: {path}")
            try:
                if stream.write(data) != len(data):
                    raise OSError(f"Incomplete history write: {path}")
                os.fsync(stream.fileno())
            except BaseException:
                stream.truncate(offset)
                os.fsync(stream.fileno())
                raise
        self._history_updates[session_id] = max(now, self._history_updates[session_id])

    def _read(self, kind: str, session_id: str) -> list:
        try:
            path = self._session_path(session_id)
        except KeyError:
            return []
        return [
            row["value"]
            for row in _history(path / "history.jsonl")
            if row["kind"] == kind
        ]
