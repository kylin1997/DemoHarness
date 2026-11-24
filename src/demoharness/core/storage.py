"""当前运行时使用 TaskStore 将 Task、Run、Session 历史和动作存入 SQLite。

TaskStore 负责认领 Task、记录工具动作、保存验收结果并恢复中断的 Run。
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .plan import Plan
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


class StorageAlreadyOpen(Exception):
    """Raised when another runtime owns the state directory."""


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
    allowed_write_paths TEXT NOT NULL DEFAULT '[]',
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
    reason TEXT,
    delegation_id TEXT REFERENCES actions(id)
);
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
CREATE TABLE IF NOT EXISTS task_plans (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id), plan TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    seq INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, message_id TEXT,
    task_id TEXT, kind TEXT NOT NULL, text TEXT NOT NULL
);
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
            self._migrate_run_streams()
            self._migrate_write_scope()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._lock.close()
            raise

    def _migrate_run_streams(self) -> None:
        with self.db:
            self.db.execute("BEGIN")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(runs)")}
            if "delegation_id" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN delegation_id TEXT REFERENCES actions(id)")
                self.db.execute("DROP INDEX IF EXISTS one_active_run")
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_task_run ON runs(task_id) "
                "WHERE ended_at IS NULL AND delegation_id IS NULL"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS delegation_runs ON runs(delegation_id)")
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_delegation_run ON runs(delegation_id) "
                "WHERE ended_at IS NULL AND delegation_id IS NOT NULL"
            )

    def _migrate_write_scope(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}
        if "allowed_write_paths" in columns:
            return
        with self.db:
            self.db.execute("ALTER TABLE tasks ADD COLUMN allowed_write_paths TEXT NOT NULL DEFAULT '[]'")

    @property
    def lock_fd(self) -> int:
        return self._lock.fileno()

    def close(self) -> None:
        try:
            self.db.close()
        finally:
            self._lock.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("SAVEPOINT task_write")
        try:
            yield
            self.db.execute("RELEASE task_write")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK TO task_write")
                self.db.execute("RELEASE task_write")
            raise

    def _entry(self, task_id: str, run_id: str | None, kind: str, **data: Any) -> int:
        return self.db.execute(
            "INSERT INTO entries(task_id, run_id, kind, data) VALUES (?, ?, ?, ?)",
            (task_id, run_id, kind, json.dumps(data, ensure_ascii=False)),
        ).lastrowid

    def submit(
        self, agent_id: str, project: Path, goal: str, acceptance: tuple[FileCheck, ...],
        budget: Budget, capabilities: frozenset[str], input_id: str, input_text: str | None = None,
        *, base_commit: str | None = None, allowed_write_paths: frozenset[str] = frozenset(),
    ) -> InputReceipt:
        previous = self.input(agent_id, input_id)
        if previous:
            return InputReceipt(previous.id, previous.task_id)
        task_id = uuid4().hex
        with self.transaction():
            self.db.execute(
                """INSERT INTO tasks (
                    id, agent_id, project, goal, acceptance, workspace, capabilities, allowed_write_paths, state,
                    model_calls, tool_calls, verifications, compactions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (task_id, agent_id, str(project), goal, json.dumps([asdict(check) for check in acceptance]),
                 str(self.root / "agents" / agent_id / "workspaces" / task_id), json.dumps(sorted(capabilities)),
                 json.dumps(sorted(allowed_write_paths), ensure_ascii=False),
                 budget.model_calls, budget.tool_calls, budget.verifications, budget.compactions),
            )
            self.db.execute("INSERT INTO inputs VALUES (?, ?, ?, ?)",
                            (input_id, agent_id, task_id, goal if input_text is None else input_text))
            self._entry(task_id, None, "input", input_id=input_id, source="observation" if base_commit else "user",
                        allowed_write_paths=sorted(allowed_write_paths))
            self.db.execute("INSERT INTO task_plans VALUES (?, ?)",
                (task_id, json.dumps(asdict(Plan().revise([goal.strip()[:500]])), ensure_ascii=False)))
            if base_commit:
                self.db.execute("UPDATE tasks SET workspace_kind = 'git', base_commit = ? WHERE id = ?",
                                (base_commit, task_id))
        return InputReceipt(input_id, task_id)

    def follow_up(self, task: Task, input_id: str, text: str, *, observed: bool = False,
                  acceptance: tuple[FileCheck, ...] | None = None, goal: str | None = None,
                  allowed_write_paths: frozenset[str] | None = None) -> InputReceipt:
        previous = self.input(task.agent_id, input_id)
        if previous:
            if previous.task_id != task.id:
                raise ValueError("This input is already attached to another Task")
            return InputReceipt(input_id, task.id)
        with self.transaction():
            self.db.execute("INSERT INTO inputs VALUES (?, ?, ?, ?)", (input_id, task.agent_id, task.id, text))
            self._entry(task.id, None, "input", input_id=input_id, source="observation" if observed else "user")
            if not observed and allowed_write_paths is not None and allowed_write_paths != task.allowed_write_paths:
                paths = sorted(allowed_write_paths)
                self.db.execute("UPDATE tasks SET allowed_write_paths = ? WHERE id = ?",
                                (json.dumps(paths, ensure_ascii=False), task.id))
                self._entry(task.id, None, "write_scope_revised", input_id=input_id,
                            previous_paths=sorted(task.allowed_write_paths), allowed_write_paths=paths)
            if goal is not None and not observed:
                self.db.execute("UPDATE tasks SET goal = ? WHERE id = ?", (goal, task.id))
                self._entry(task.id, None, "goal_revised", input_id=input_id, goal=goal)
            if acceptance is not None and not observed:
                checks = [asdict(check) for check in acceptance]
                self.db.execute("UPDATE tasks SET acceptance = ? WHERE id = ?", (json.dumps(checks), task.id))
                self._entry(task.id, None, "acceptance_revised", input_id=input_id, checks=checks)
            if task.state is TaskState.COMPLETED and not observed:
                budget = Budget()
                self.db.execute("""UPDATE tasks SET state = 'queued', reason = NULL, detail = NULL, delivery = NULL,
                    model_calls = ?, tool_calls = ?, verifications = ?, compactions = ? WHERE id = ?""",
                    (budget.model_calls, budget.tool_calls, budget.verifications, budget.compactions, task.id))
                self._entry(task.id, None, "reopened", input_id=input_id)
            elif (task.state is TaskState.WAITING and not observed
                  and task.reason not in {RunReason.BUDGET_EXHAUSTED, RunReason.UNKNOWN_EFFECT}):
                self.db.execute("UPDATE tasks SET state = 'queued', reason = NULL, detail = NULL WHERE id = ?",
                                (task.id,))
                self._entry(task.id, None, "unblocked", input_id=input_id)
        return InputReceipt(input_id, task.id)

    def task_ids(self, agent_id: str) -> list[str]:
        rows = self.db.execute("SELECT id FROM tasks WHERE agent_id = ? ORDER BY rowid DESC", (agent_id,))
        return [row[0] for row in rows]

    def delivered_task(self, agent_id: str, project: Path, fingerprint: str) -> str | None:
        row = self.db.execute("""
            SELECT e.task_id FROM entries e JOIN tasks t ON t.id = e.task_id
            WHERE t.agent_id = ? AND t.project = ? AND e.kind = 'completed'
                AND json_extract(e.data, '$.delivery.change_fingerprint') = ?
            ORDER BY e.id DESC LIMIT 1
        """, (agent_id, str(project), fingerprint)).fetchone()
        return row[0] if row else None

    def latest_input(self, task_id: str) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id), 0) FROM entries WHERE task_id = ? AND run_id IS NULL AND kind = 'input'",
                               (task_id,)).fetchone()[0]

    def _plan(self, task_id: str) -> Plan:
        row = self.db.execute("SELECT plan FROM task_plans WHERE task_id = ?", (task_id,)).fetchone()
        return Plan.from_record(json.loads(row[0])) if row else Plan()

    def plan_for_action(self, task_id: str, action_id: str) -> Plan | None:
        row = self.db.execute("""
            SELECT data FROM entries WHERE task_id = ? AND kind = 'plan_revised'
            AND json_extract(data, '$.action_id') = ?
        """, (task_id, action_id)).fetchone()
        return Plan.from_record(json.loads(row[0])["plan"]) if row else None

    def set_plan(self, run: Run, expected_revision: int, update: Callable[[Plan], Plan],
                 reason: str, action_id: str) -> Plan:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self._plan(run.task_id)
            if expected_revision != current.revision:
                raise ValueError(f"Plan revision {expected_revision} is stale. Current revision is {current.revision}")
            plan = update(current)
            if plan == current:
                return current
            plan_data = asdict(plan)
            self.db.execute("""INSERT INTO task_plans VALUES (?, ?) ON CONFLICT(task_id)
                DO UPDATE SET plan = excluded.plan""", (run.task_id, json.dumps(plan_data, ensure_ascii=False)))
            self._entry(run.task_id, run.id, "plan_revised", plan=plan_data, reason=reason,
                        expected_revision=expected_revision, action_id=action_id)
        return plan

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
            delivery = Delivery(delivery["text"], checks, delivery["diff"], delivery.get("change_fingerprint"))
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
            plan=self._plan(task_id),
            allowed_write_paths=frozenset(json.loads(row["allowed_write_paths"])),
        )

    def queued(self, agent_id: str) -> list[str]:
        rows = self.db.execute("SELECT id FROM tasks WHERE agent_id = ? AND state = 'queued' ORDER BY rowid", (agent_id,))
        return [row["id"] for row in rows]

    def history(
        self, task_id: str, after: int = 0, *, delegation_id: str | None = None, limit: int = -1,
    ) -> list[HistoryEntry]:
        rows = self.db.execute(
            """SELECT e.*, i.text FROM entries e JOIN tasks t ON t.id = e.task_id
               LEFT JOIN runs r ON r.id = e.run_id
               LEFT JOIN inputs i ON i.agent_id = t.agent_id AND i.id = json_extract(e.data, '$.input_id')
               WHERE e.task_id = ? AND e.id > ? AND r.delegation_id IS ? ORDER BY e.id LIMIT ?""",
            (task_id, after, delegation_id, limit),
        )
        entries = []
        for row in rows:
            data = json.loads(row["data"])
            if row["kind"] == "input" and row["text"] is not None:
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

    def claim_delegation(self, parent: Run, action: Action, prompt: str) -> Run:
        run = Run(uuid4().hex, parent.task_id, parent.config_version, time.time(), delegation_id=action.id)
        with self.db:
            prior = self.db.execute("SELECT 1 FROM runs WHERE delegation_id = ?", (action.id,)).fetchone()
            self.db.execute(
                "INSERT INTO runs(id, task_id, config_version, started_at, delegation_id) VALUES (?, ?, ?, ?, ?)",
                (run.id, run.task_id, run.config_version, run.started_at, action.id),
            )
            self._entry(run.task_id, run.id, "run_started", config_version=run.config_version)
            if prior is None:
                self._entry(run.task_id, run.id, "input", text=prompt)
        return run

    def delegation_outcome(self, task_id: str, delegation_id: str) -> dict | None:
        row = self.db.execute(
            """SELECT e.data FROM runs r JOIN entries e ON e.run_id = r.id
               WHERE r.task_id = ? AND r.delegation_id = ? AND e.kind = 'delegation_finished'""",
            (task_id, delegation_id),
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def recover(self) -> None:
        runs = self.db.execute("SELECT * FROM runs WHERE ended_at IS NULL AND delegation_id IS NULL").fetchall()
        for row in runs:
            run = _run_row(row)
            with self.db:
                self._record_interrupted_model(run)
                self._close_delegations(run.task_id, RunReason.INTERRUPTED, "Previous host exited")
                self._end(run.task_id, run.id, TaskState.QUEUED, RunReason.INTERRUPTED, "Previous host exited")

    def _close_delegations(self, task_id: str, reason: RunReason, detail: str) -> None:
        rows = self.db.execute(
            "SELECT * FROM runs WHERE task_id = ? AND ended_at IS NULL AND delegation_id IS NOT NULL",
            (task_id,),
        ).fetchall()
        for row in rows:
            run = _run_row(row)
            self._record_interrupted_model(run)
            self.db.execute("UPDATE runs SET ended_at = ?, reason = ? WHERE id = ?", (time.time(), reason, run.id))
            self._entry(task_id, run.id, reason.value, reason=reason, detail=detail)

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
        self._entry(task_id, run_id, kind, reason=reason, detail=detail, checks=[asdict(check) for check in checks],
                    delivery=asdict(delivery) if delivery else None)
        if state in {TaskState.COMPLETED, TaskState.WAITING, TaskState.FAILED, TaskState.STOPPED}:
            text = delivery.text if delivery else detail or reason.value
            self.db.execute("""INSERT INTO notices(agent_id, task_id, kind, text)
                SELECT agent_id, id, ?, ? FROM tasks WHERE id = ?""", (state, text, task_id))

    def end(
        self, run: Run, state: TaskState, reason: RunReason, detail: str = "", delivery: Delivery | None = None,
        checks: tuple[CheckEvidence, ...] = (),
    ) -> None:
        if self.task(run.task_id).state is TaskState.RUNNING:
            with self.db:
                self._record_interrupted_model(run)
                if run.delegation_id is not None:
                    finished = self.db.execute(
                        "UPDATE runs SET ended_at = ?, reason = ? WHERE id = ? AND ended_at IS NULL",
                        (time.time(), reason, run.id),
                    ).rowcount
                    if finished:
                        self._entry(run.task_id, run.id, "delegation_finished", status=state, reason=reason,
                                    summary=delivery.text if delivery else "", detail=detail)
                else:
                    self._close_delegations(run.task_id, reason, detail)
                    self._end(run.task_id, run.id, state, reason, detail, delivery, checks)

    def stop(self, task_id: str) -> None:
        if self.task(task_id).state not in (TaskState.QUEUED, TaskState.RUNNING, TaskState.WAITING):
            return
        active = self.db.execute(
            "SELECT * FROM runs WHERE task_id = ? AND ended_at IS NULL AND delegation_id IS NULL", (task_id,),
        ).fetchone()
        with self.db:
            if active:
                self._record_interrupted_model(_run_row(active))
            self._close_delegations(task_id, RunReason.STOPPED, "User stopped task")
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
            if run.delegation_id is None and self.latest_input(run.task_id) > request_id:
                self._entry(run.task_id, run.id, "stale_response", request_id=request_id)
                return
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

    def actions(self, task_id: str, *, delegation_id: str | None = None) -> list[Action]:
        rows = self.db.execute(
            """SELECT a.* FROM actions a JOIN runs r ON r.id = a.run_id
               WHERE a.task_id = ? AND r.delegation_id IS ? ORDER BY a.response_id, a.position""",
            (task_id, delegation_id),
        )
        return [_action_row(row) for row in rows]

    def start_action(self, run: Run, action: Action, intent: dict | None) -> bool:
        with self.db:
            if self.db.execute("SELECT 1 FROM runs WHERE delegation_id = ?", (action.id,)).fetchone():
                return True
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
    return Run(row["id"], row["task_id"], row["config_version"], row["started_at"], row["ended_at"], reason,
               row["delegation_id"])


def _action_row(row: sqlite3.Row) -> Action:
    return Action(
        id=row["id"], task_id=row["task_id"], run_id=row["run_id"], response_id=row["response_id"], position=row["position"],
        call=ToolCall(row["call_id"], row["name"], json.loads(row["params"])), state=ActionState(row["state"]),
        attempts=row["attempts"], intent=json.loads(row["intent"]) if row["intent"] else None,
        result=ToolResult(**json.loads(row["result"])) if row["result"] else None,
    )
