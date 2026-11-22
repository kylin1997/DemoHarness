"""DemoHarness 内核 · 存储层（core/storage.py）。

唯一碰 SQL 的地方。分层原则：storage.py 不懂业务语义（不知道什么是
reset），session.py 不写 SQL——本层只提供 schema、读写原语、事务，
由 session 层在此之上组装六动词与重放等语义。

关键约束（见 docs/design/storage-session.md）：

- 单连接是正确性前提：进程内对同一 db 文件只允许一个 Storage 实例存活，
  重复打开抛 :class:`StorageAlreadyOpen`。两个连接 = 两套写事务交错 =
  id 发号重号 + FIFO 失效。
- 只增不改：本层不提供任何 UPDATE/DELETE entry 的通道——树只增不改，
  entry 创建后即不可变（架构不变量 2）。
- id 应用层发号（meta 表），发号与插入同事务，中途 kill 一起回滚、
  重启后重发同号无害（该号从未被占用）。
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .task import (
    Action,
    ActionState,
    Budget,
    CheckEvidence,
    Delivery,
    FileCheck,
    Input,
    InputReceipt,
    Run,
    RunReason,
    Task,
    TaskState,
    Verdict,
)
from .tools import ToolCall, ToolResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    bot_id        TEXT NOT NULL,
    parent_id     TEXT,
    fork_at       TEXT,
    active_leaf   TEXT,
    model         TEXT,
    title         TEXT,
    created_at    REAL NOT NULL,
    origin_parent_id TEXT,
    background     INTEGER NOT NULL DEFAULT 0,
    restart_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entries (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    parent_id     TEXT REFERENCES entries(id),
    kind          TEXT NOT NULL,
    content       TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(session_id, id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# id 应用层发号器在 meta 表中的键。
_NEXT_SESSION_SEQ = "next_session_seq"
_NEXT_ENTRY_SEQ = "next_entry_seq"


class StorageAlreadyOpen(Exception):
    """同 db 文件已有 Storage 实例存活时再次打开触发。"""


@dataclass(frozen=True)
class SessionRow:
    id: str
    bot_id: str
    parent_id: str | None
    fork_at: str | None
    active_leaf: str | None
    model: str | None
    title: str | None
    created_at: float
    origin_parent_id: str | None
    background: bool
    restart_count: int


@dataclass(frozen=True)
class EntryRow:
    id: str
    session_id: str
    parent_id: str | None
    kind: str
    content: str
    created_at: float


# 进程级登记：path -> Storage，防止同一 db 文件被第二个实例打开。
_open_paths: dict[str, Storage] = {}


def _numeric_id_key(s: str) -> tuple[int, str]:
    """把 ``e_42`` / ``s_7`` 转成数值序键，纯 ``e_``/``s_`` 前缀。

    TEXT 的 `ORDER BY id` 是字典序（e_1, e_10, e_2 …），与"全局单调"的
    语义序不一致；全部读原语据此按数值序排序。
    """
    for prefix in ("e_", "s_"):
        if s.startswith(prefix):
            try:
                return (int(s[len(prefix):]), s)
            except ValueError:
                break
    return (0, s)


def _row_of(cur: sqlite3.Cursor, table: str, row: tuple) -> Any:
    """按 SELECT 列序把表行转成对应 dataclass。"""
    if table == "sessions":
        return SessionRow(
            id=row[0],
            bot_id=row[1],
            parent_id=row[2],
            fork_at=row[3],
            active_leaf=row[4],
            model=row[5],
            title=row[6],
            created_at=row[7],
            origin_parent_id=row[8],
            background=bool(row[9]),
            restart_count=row[10],
        )
    return EntryRow(
        id=row[0],
        session_id=row[1],
        parent_id=row[2],
        kind=row[3],
        content=row[4],
        created_at=row[5],
    )


_SESSION_COLS = (
    "id, bot_id, parent_id, fork_at, active_leaf, model, title, "
    "created_at, origin_parent_id, background, restart_count"
)
_ENTRY_COLS = "id, session_id, parent_id, kind, content, created_at"


class Storage:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        if self.path in _open_paths:
            raise StorageAlreadyOpen(self.path)
        _open_paths[self.path] = self

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：SDK 的后台任务可能在事件循环线程使用 Storage，
        # 而 Storage 在装配线程创建，会跨线程使用。安全由三层保证：(1) 进程内单实例
        # 单连接（StorageAlreadyOpen）；(2) 所有写经 BEGIN IMMEDIATE 单写者；
        # (3) sqlite3 模块自身的连接级互斥。教学规模足够。
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        _open_paths.pop(self.path, None)
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """唯一写通道：BEGIN IMMEDIATE → yield conn → commit/rollback。

        本层不提供任何 UPDATE/DELETE entry 的通道——树只增不改。
        """
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ── 读原语（WAL 下走快照，无事务） ──────────────────────────

    def session(self, sid: str) -> SessionRow | None:
        cur = self._conn.execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?", (sid,)
        )
        row = cur.fetchone()
        return _row_of(cur, "sessions", row) if row else None

    def entries(self, sid: str) -> list[EntryRow]:
        cur = self._conn.execute(
            f"SELECT {_ENTRY_COLS} FROM entries WHERE session_id = ?", (sid,)
        )
        rows = [_row_of(cur, "entries", r) for r in cur.fetchall()]
        rows.sort(key=lambda e: _numeric_id_key(e.id))
        return rows

    def entry(self, eid: str) -> EntryRow | None:
        cur = self._conn.execute(
            f"SELECT {_ENTRY_COLS} FROM entries WHERE id = ?", (eid,)
        )
        row = cur.fetchone()
        return _row_of(cur, "entries", row) if row else None

    def sessions_of_bot(self, bot_id: str) -> list[SessionRow]:
        cur = self._conn.execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE bot_id = ?", (bot_id,)
        )
        rows = [_row_of(cur, "sessions", r) for r in cur.fetchall()]
        rows.sort(key=lambda s: _numeric_id_key(s.id))
        return rows

    def sessions_all(self) -> list[SessionRow]:
        cur = self._conn.execute(f"SELECT {_SESSION_COLS} FROM sessions")
        rows = [_row_of(cur, "sessions", r) for r in cur.fetchall()]
        rows.sort(key=lambda s: _numeric_id_key(s.id))
        return rows

    # ── 写原语（必须在 transaction() 内调用；storage 持 SQL，session 层给语义） ──

    def insert_session(self, conn: sqlite3.Connection, row: SessionRow) -> None:
        conn.execute(
            "INSERT INTO sessions "
            f"({_SESSION_COLS}) VALUES ({', '.join('?' * 11)})",
            (
                row.id,
                row.bot_id,
                row.parent_id,
                row.fork_at,
                row.active_leaf,
                row.model,
                row.title,
                row.created_at,
                row.origin_parent_id,
                int(row.background),
                row.restart_count,
            ),
        )

    def insert_entry(self, conn: sqlite3.Connection, row: EntryRow) -> None:
        conn.execute(
            "INSERT INTO entries "
            f"({_ENTRY_COLS}) VALUES ({', '.join('?' * 6)})",
            (
                row.id,
                row.session_id,
                row.parent_id,
                row.kind,
                row.content,
                row.created_at,
            ),
        )

    def update_active_leaf(self, conn: sqlite3.Connection, sid: str, leaf_id: str) -> None:
        conn.execute(
            "UPDATE sessions SET active_leaf = ? WHERE id = ?", (leaf_id, sid)
        )

    def update_session_model(
        self, conn: sqlite3.Connection, sid: str, model: str | None
    ) -> None:
        """会话层模型覆盖的唯一 SQL 写入通道。

        config 三层链（任务 > 会话 > 全局）的「会话」层由此落库；调用方是
        session.py 的 SetModelOp（经会话 FIFO），门面不再直接发 SQL。
        只改 sessions 的可变配置列，不触碰 entry（架构不变量 2：树只增不改）。
        """
        conn.execute("UPDATE sessions SET model = ? WHERE id = ?", (model, sid))

    def delete_session_tree(self, conn: sqlite3.Connection, sids: list[str]) -> None:
        """一个事务内删除多个会话及其全部 entry（delete 级联用）。"""
        if not sids:
            return
        marks = ",".join("?" * len(sids))
        conn.execute(f"DELETE FROM entries WHERE session_id IN ({marks})", sids)
        conn.execute(f"DELETE FROM sessions WHERE id IN ({marks})", sids)

    def mint_entry_id(self, conn: sqlite3.Connection) -> str:
        return self._mint(conn, _NEXT_ENTRY_SEQ, "e_")

    def mint_session_id(self, conn: sqlite3.Connection) -> str:
        return self._mint(conn, _NEXT_SESSION_SEQ, "s_")

    def _mint(self, conn: sqlite3.Connection, key: str, prefix: str) -> str:
        cur = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        n = int(row[0]) + 1 if row else 1
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(n)),
        )
        return f"{prefix}{n}"


_TASK_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    project TEXT NOT NULL,
    goal TEXT NOT NULL,
    acceptance TEXT NOT NULL,
    workspace TEXT NOT NULL,
    workspace_kind TEXT,
    base_commit TEXT,
    workspace_ready INTEGER NOT NULL DEFAULT 0,
    capabilities TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT,
    detail TEXT,
    delivery TEXT,
    model_calls INTEGER NOT NULL CHECK (model_calls >= 0),
    tool_calls INTEGER NOT NULL CHECK (tool_calls >= 0),
    verifications INTEGER NOT NULL CHECK (verifications >= 0),
    compactions INTEGER NOT NULL CHECK (compactions >= 0)
);
CREATE TABLE IF NOT EXISTS inputs (
    id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    text TEXT NOT NULL,
    PRIMARY KEY (agent_id, id)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    config_version TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(task_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_entries ON entries(task_id, id);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    response_id INTEGER NOT NULL REFERENCES entries(id),
    position INTEGER NOT NULL,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    params TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    intent TEXT,
    result TEXT,
    UNIQUE (response_id, position)
);
CREATE INDEX IF NOT EXISTS task_actions ON actions(task_id, response_id, position);
"""


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    task_id: str
    run_id: str | None
    kind: str
    data: dict


class TaskStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lock = (root / "host.lock").open("a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise StorageAlreadyOpen(f"An AgentRuntime already owns {root}") from None
        try:
            self.db = sqlite3.connect(root / "agent.sqlite3")
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.executescript(_TASK_SCHEMA)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._lock.close()
            raise

    @property
    def lock_fd(self) -> int:
        return self._lock.fileno()

    def close(self) -> None:
        try:
            self.db.close()
        finally:
            self._lock.close()

    def _entry(self, task_id: str, run_id: str | None, kind: str, **data: Any) -> int:
        return self.db.execute(
            "INSERT INTO entries(task_id, run_id, kind, data) VALUES (?, ?, ?, ?)",
            (task_id, run_id, kind, json.dumps(data, ensure_ascii=False)),
        ).lastrowid

    def submit(
        self, agent_id: str, project: Path, goal: str, acceptance: tuple[FileCheck, ...],
        budget: Budget, capabilities: frozenset[str], input_id: str,
    ) -> InputReceipt:
        previous = self.input(agent_id, input_id)
        if previous:
            return InputReceipt(previous.id, previous.task_id)
        task_id = uuid4().hex
        with self.db:
            self.db.execute(
                """INSERT INTO tasks (
                    id, agent_id, project, goal, acceptance, workspace, capabilities, state,
                    model_calls, tool_calls, verifications, compactions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (task_id, agent_id, str(project), goal, json.dumps([asdict(check) for check in acceptance]),
                 str(self.root / "workspaces" / task_id), json.dumps(sorted(capabilities)),
                 budget.model_calls, budget.tool_calls, budget.verifications, budget.compactions),
            )
            self.db.execute("INSERT INTO inputs VALUES (?, ?, ?, ?)", (input_id, agent_id, task_id, goal))
            self._entry(task_id, None, "input", input_id=input_id)
        return InputReceipt(input_id, task_id)

    def input(self, agent_id: str, input_id: str) -> Input | None:
        row = self.db.execute("SELECT * FROM inputs WHERE agent_id = ? AND id = ?", (agent_id, input_id)).fetchone()
        return Input(**dict(row)) if row else None

    def task(self, task_id: str) -> Task:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        delivery = json.loads(row["delivery"]) if row["delivery"] else None
        if delivery:
            checks = tuple(_check_evidence(check) for check in delivery["checks"])
            delivery = Delivery(delivery["text"], checks, delivery["diff"])
        return Task(
            id=row["id"], agent_id=row["agent_id"], project=Path(row["project"]), goal=row["goal"],
            acceptance=tuple(FileCheck(**check) for check in json.loads(row["acceptance"])),
            workspace=Path(row["workspace"]), state=TaskState(row["state"]),
            budget=Budget(**{name: row[name] for name in Budget.__dataclass_fields__}),
            capabilities=frozenset(json.loads(row["capabilities"])), session_id=task_id,
            base_commit=row["base_commit"], workspace_kind=row["workspace_kind"],
            workspace_ready=bool(row["workspace_ready"]),
            reason=RunReason(row["reason"]) if row["reason"] else None,
            detail=row["detail"], delivery=delivery,
        )

    def queued(self, agent_id: str) -> list[str]:
        rows = self.db.execute("SELECT id FROM tasks WHERE agent_id = ? AND state = 'queued' ORDER BY rowid", (agent_id,))
        return [row["id"] for row in rows]

    def history(self, task_id: str, after: int = 0) -> list[HistoryEntry]:
        rows = self.db.execute(
            """SELECT e.*, i.text FROM entries e JOIN tasks t ON t.id = e.task_id
               LEFT JOIN inputs i ON i.agent_id = t.agent_id AND i.id = json_extract(e.data, '$.input_id')
               WHERE e.task_id = ? AND e.id > ? ORDER BY e.id""",
            (task_id, after),
        )
        entries = []
        for row in rows:
            data = json.loads(row["data"])
            if row["kind"] == "input":
                data["text"] = row["text"]
            entries.append(HistoryEntry(row["id"], task_id, row["run_id"], row["kind"], data))
        return entries

    def runs(self, task_id: str) -> list[Run]:
        rows = self.db.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY rowid", (task_id,))
        return [_run_row(row) for row in rows]

    def claim(self, task_id: str, config_version: str) -> Run | None:
        run = Run(uuid4().hex, task_id, config_version, time.time())
        with self.db:
            claimed = self.db.execute(
                "UPDATE tasks SET state = 'running', reason = NULL, detail = NULL WHERE id = ? AND state = 'queued'",
                (task_id,),
            ).rowcount
            if not claimed:
                return None
            self.db.execute(
                "INSERT INTO runs(id, task_id, config_version, started_at) VALUES (?, ?, ?, ?)",
                (run.id, task_id, config_version, run.started_at),
            )
            self._entry(task_id, run.id, "run_started", config_version=config_version)
        return run

    def recover(self) -> None:
        runs = self.db.execute("SELECT * FROM runs WHERE ended_at IS NULL").fetchall()
        for row in runs:
            run = _run_row(row)
            with self.db:
                self._record_interrupted_model(run)
                self._end(run.task_id, run.id, TaskState.QUEUED, RunReason.INTERRUPTED, "Previous host exited")

    def _record_interrupted_model(self, run: Run) -> None:
        started = self.db.execute(
            """SELECT id FROM entries WHERE run_id = ? AND kind = 'model_started' AND NOT EXISTS (
                   SELECT 1 FROM entries result WHERE result.run_id = entries.run_id AND
                   result.kind IN ('model_result', 'model_interrupted') AND
                   json_extract(result.data, '$.request_id') = entries.id
               )""",
            (run.id,),
        ).fetchall()
        for row in started:
            self._entry(run.task_id, run.id, "model_interrupted", request_id=row["id"])

    def _end(
        self, task_id: str, run_id: str | None, state: TaskState, reason: RunReason, detail: str = "",
        delivery: Delivery | None = None, checks: tuple[CheckEvidence, ...] = (),
    ) -> None:
        if run_id:
            self.db.execute(
                "UPDATE runs SET ended_at = ?, reason = ? WHERE id = ? AND ended_at IS NULL",
                (time.time(), reason, run_id),
            )
        self.db.execute(
            "UPDATE tasks SET state = ?, reason = ?, detail = ?, delivery = ? WHERE id = ?",
            (state, reason, detail or None, json.dumps(asdict(delivery), ensure_ascii=False) if delivery else None,
             task_id),
        )
        kind = reason if state is TaskState.QUEUED else state
        self._entry(task_id, run_id, kind, reason=reason, detail=detail, checks=[asdict(check) for check in checks])

    def end(
        self, run: Run, state: TaskState, reason: RunReason, detail: str = "", delivery: Delivery | None = None,
        checks: tuple[CheckEvidence, ...] = (),
    ) -> None:
        if self.task(run.task_id).state is TaskState.RUNNING:
            with self.db:
                self._record_interrupted_model(run)
                self._end(run.task_id, run.id, state, reason, detail, delivery, checks)

    def stop(self, task_id: str) -> None:
        if self.task(task_id).state not in (TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING):
            return
        active = self.db.execute("SELECT * FROM runs WHERE task_id = ? AND ended_at IS NULL", (task_id,)).fetchone()
        with self.db:
            if active:
                self._record_interrupted_model(_run_row(active))
            self._end(task_id, active["id"] if active else None, TaskState.STOPPED, RunReason.STOPPED, "User stopped task")

    def enqueue(self, task_id: str, budget: Budget | None) -> None:
        with self.db:
            if budget is not None:
                self.db.execute(
                    """UPDATE tasks SET model_calls = model_calls + ?, tool_calls = tool_calls + ?,
                       verifications = verifications + ?, compactions = compactions + ? WHERE id = ?""",
                    (budget.model_calls, budget.tool_calls, budget.verifications, budget.compactions, task_id),
                )
            self.db.execute("UPDATE tasks SET state = 'queued', reason = NULL, detail = NULL WHERE id = ?", (task_id,))
            self._entry(task_id, None, "resumed", budget=asdict(budget) if budget else None)

    def workspace_started(self, run: Run, workspace_kind: str, base_commit: str | None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE tasks SET workspace_kind = ?, base_commit = ? WHERE id = ?", (workspace_kind, base_commit, run.task_id),
            )
            self._entry(run.task_id, run.id, "workspace_started", workspace_kind=workspace_kind, base_commit=base_commit)

    def workspace_ready(self, run: Run) -> None:
        with self.db:
            self.db.execute("UPDATE tasks SET workspace_ready = 1 WHERE id = ?", (run.task_id,))
            self._entry(run.task_id, run.id, "workspace_ready")

    def _charge(self, task_id: str, kind: str) -> bool:
        if kind not in Budget.__dataclass_fields__:
            raise ValueError(kind)
        return bool(self.db.execute(
            f"UPDATE tasks SET {kind} = {kind} - 1 WHERE id = ? AND state = 'running' AND {kind} > 0",
            (task_id,),
        ).rowcount)

    def begin(self, run: Run, budget: str, kind: str, **data: Any) -> int | None:
        with self.db:
            if self._charge(run.task_id, budget):
                return self._entry(run.task_id, run.id, kind, **data)
        return None

    def record(self, run: Run, kind: str, **data: Any) -> None:
        with self.db:
            self._entry(run.task_id, run.id, kind, **data)

    def model_response(
        self, run: Run, request_id: int, stop: str, text: str, calls: list[ToolCall],
        usage: dict | None, error: str | None,
    ) -> None:
        with self.db:
            self._entry(run.task_id, run.id, "model_result", request_id=request_id, stop=stop, usage=usage, error=error)
            if stop != "ok" or not (text or calls):
                return
            response_id = self._entry(run.task_id, run.id, "assistant", text=text, proposal=not calls)
            for position, call in enumerate(calls):
                action_id = f"{run.id}:{response_id}:{position}"
                self.db.execute(
                    """INSERT INTO actions(id, task_id, run_id, response_id, position, call_id, name, params)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (action_id, run.task_id, run.id, response_id, position, call.call_id, call.name, json.dumps(call.params)),
                )
                self._entry(run.task_id, run.id, "action_pending", action_id=action_id)

    def actions(self, task_id: str) -> list[Action]:
        rows = self.db.execute("SELECT * FROM actions WHERE task_id = ? ORDER BY response_id, position", (task_id,))
        return [_action_row(row) for row in rows]

    def start_action(self, run: Run, action: Action, intent: dict | None) -> bool:
        with self.db:
            if not self._charge(run.task_id, "tool_calls"):
                return False
            self.db.execute(
                "UPDATE actions SET state = 'started', attempts = attempts + 1, intent = ? WHERE id = ?",
                (json.dumps(intent), action.id),
            )
            self._entry(run.task_id, run.id, "action_started", action_id=action.id)
        return True

    def result(self, run: Run, action: Action, result: ToolResult) -> None:
        with self.db:
            saved = self.db.execute(
                "UPDATE actions SET state = 'result', result = ? WHERE id = ? AND state != 'result'",
                (json.dumps(asdict(result), ensure_ascii=False), action.id),
            ).rowcount
            if saved:
                self._entry(run.task_id, run.id, "action_result", action_id=action.id)


def _check_evidence(data: dict) -> CheckEvidence:
    return CheckEvidence(data["id"], data["path"], Verdict(data["verdict"]), data["detail"], data["sha256"])


def _run_row(row: sqlite3.Row) -> Run:
    reason = RunReason(row["reason"]) if row["reason"] else None
    return Run(row["id"], row["task_id"], row["config_version"], row["started_at"], row["ended_at"], reason)


def _action_row(row: sqlite3.Row) -> Action:
    return Action(
        id=row["id"], task_id=row["task_id"], run_id=row["run_id"], response_id=row["response_id"], position=row["position"],
        call=ToolCall(row["call_id"], row["name"], json.loads(row["params"])), state=ActionState(row["state"]),
        attempts=row["attempts"], intent=json.loads(row["intent"]) if row["intent"] else None,
        result=ToolResult(**json.loads(row["result"])) if row["result"] else None,
    )
