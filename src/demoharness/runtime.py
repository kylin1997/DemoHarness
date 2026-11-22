"""Assemble local Agents, schedule Tasks, and expose the Python API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from .coordination import CoordinationLog, Message, Notice, Route, route
from .core.blocking import run_io
from .core.git import change_fingerprint
from .core.loop import LoopRole, RoleResult, StreamFn, run_loop
from .core.storage import HistoryEntry, TaskStore
from .core.task import (
    Action,
    Budget,
    Delivery,
    FileCheck,
    Input,
    InputReceipt,
    Run,
    RunReason,
    Task,
    TaskState,
    Verdict,
    check_write_paths,
    write_paths,
)
from .discovery import ObservationError
from .discovery import observe as observe_project
from .memory import Memory, MemoryDraft, MemoryStore, learn_from_session
from .tools.delegation import DelegatingTools
from .tools.files import (
    FILE_CAPABILITIES,
    FileTools,
    Workspace,
    WorkspaceError,
    WriteScopeError,
)
from .tools.planning import PlanningTools


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    project: str | Path
    model: str
    complete: StreamFn
    system_prompt: str = ""
    capabilities: frozenset[str] = FILE_CAPABILITIES
    max_tokens: int | None = None
    context_messages: int = 16
    proactive_writes: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.id) or not self.model:
            raise ValueError("An Agent needs a simple id and a model")
        if self.context_messages <= 0 or self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("Context and output limits must be positive")
        capabilities = frozenset(self.capabilities)
        if capabilities - FILE_CAPABILITIES:
            raise ValueError(f"Unknown file capabilities: {sorted(capabilities - FILE_CAPABILITIES)}")
        object.__setattr__(self, "project", Path(self.project).resolve())
        object.__setattr__(self, "capabilities", capabilities)

    @property
    def version(self) -> str:
        definition = [self.id, str(self.project), self.model, self.system_prompt,
                      sorted(self.capabilities), self.max_tokens, self.context_messages, self.proactive_writes]
        return hashlib.sha256(json.dumps(definition).encode()).hexdigest()


@dataclass
class _Worker:
    job: asyncio.Task[None]
    cancel: asyncio.Event


class AgentRuntime:
    """Own one SQLite state directory and its in-process Task workers."""

    def __init__(self, store: TaskStore, definitions: dict[str, AgentDefinition]):
        self._store = store
        self._definitions = definitions
        self._workers: dict[str, _Worker] = {}
        self._coordination = CoordinationLog(store.db)
        store.db.execute("PRAGMA secure_delete = ON")
        self._memory = MemoryStore(store.db)
        self._coordinators: dict[str, asyncio.Task] = {}
        self._observers: list[asyncio.Task] = []
        self._learners: set[asyncio.Task] = set()
        self._closing = False

    @classmethod
    @asynccontextmanager
    async def open(cls, state_dir: str | Path, agents: Iterable[AgentDefinition], *, observe: bool = True,
                   observe_interval: float = 5.0):
        if observe_interval <= 0:
            raise ValueError("Observation interval must be positive")
        root = Path(state_dir).resolve()
        definitions = {}
        for definition in agents:
            if definition.id in definitions:
                raise ValueError(f"Duplicate Agent id: {definition.id}")
            if Path(definition.project).is_relative_to(root):
                raise ValueError("The Agent project cannot be inside its runtime state directory")
            definitions[definition.id] = definition
        runtime = cls(TaskStore(root), definitions)
        try:
            runtime._store.recover()
            for definition in definitions.values():
                runtime._schedule_coordinator(definition)
                runtime._memory.maintain(definition.id)
                runtime._schedule_learning(definition)
                if observe and (Path(definition.project) / ".git").exists():
                    runtime._observers.append(asyncio.create_task(runtime._observe(definition, observe_interval)))
                for task_id in runtime._store.queued(definition.id):
                    runtime._schedule(definition, task_id)
            yield runtime
        finally:
            await runtime._close()

    def agent(self, agent_id: str) -> Agent:
        return Agent(self, self._definitions[agent_id])

    def _schedule_coordinator(self, definition: AgentDefinition) -> None:
        job = self._coordinators.get(definition.id)
        if not self._closing and (job is None or job.done()):
            self._coordinators[definition.id] = asyncio.create_task(self._coordinate(definition))

    async def _observe(self, definition: AgentDefinition, interval: float) -> None:
        aid = definition.id
        project = Path(definition.project)
        prefix = "observation:" + hashlib.sha256(str(project).encode()).hexdigest()[:16] + ":"
        previous_error = None
        previous_revision = self._coordination.revision(aid, prefix)
        while not self._closing:
            try:
                current = await observe_project(project, previous_revision)
            except ObservationError as error:
                if str(error) != previous_error:
                    previous_error = str(error)
                    with self._store.db:
                        self._coordination.notice(aid, "blocked", f"项目观察暂不可用：{error}")
            else:
                previous_error = None
                if current:
                    self._coordination.receive(aid, prefix + current.revision, current.evidence,
                                               revision=current.revision,
                                               change_fingerprint=current.change_fingerprint)
                    previous_revision = current.revision
                    self._schedule_coordinator(definition)
            await asyncio.sleep(interval)

    def _schedule_learning(self, definition: AgentDefinition, task_id: str | None = None) -> None:
        job = asyncio.create_task(self._learn(definition, task_id))
        self._learners.add(job)
        job.add_done_callback(self._learners.discard)

    async def _learn(self, definition: AgentDefinition, task_id: str | None) -> None:
        aid = definition.id
        for candidate_id in (task_id,) if task_id else self._store.task_ids(aid):
            await asyncio.sleep(0)
            task = self._store.task(candidate_id)
            if task.project != definition.project:
                continue
            project = str(task.project)
            for source, drafts in learn_from_session(
                self._store.history(task.id), self._store.actions(task.id), time.time(),
            ):
                if not self._memory.processed(aid, project, source):
                    self._memory.record(aid, project, source, drafts)

    def _recall(self, definition: AgentDefinition, query: str) -> list[str]:
        memories = self._memory.recall(definition.id, str(definition.project), query)
        return [f"{memory.text} [来源 {', '.join(memory.sources)}，更新时间 {memory.updated_at:.0f}]"
                for memory in memories]

    async def _coordinate(self, definition: AgentDefinition) -> None:
        aid = definition.id
        while not self._closing and (message := self._coordination.next(aid)) is not None:
            try:
                decision = message.decision
                if decision is None and message.change_fingerprint:
                    task_id = self._store.delivered_task(aid, definition.project, message.change_fingerprint)
                    if task_id:
                        decision = Route("attach", "项目变更与已有交付一致。", task_id=task_id)
                if decision is None:
                    tasks = self.agent(aid).tasks()[:20]
                    decision = await route(
                        definition.complete, definition.model, definition.system_prompt,
                        text=message.text,
                        tasks=[{"id": t.id, "goal": t.goal, "state": t.state} for t in tasks],
                        conversation=self._coordination.conversation(aid),
                        memories=self._recall(definition, message.text), reply_to=message.reply_to,
                        observation=message.revision is not None, cancel=asyncio.Event(),
                    )
                self._apply_route(definition, message, decision)
            except (ValueError, KeyError, TypeError, AttributeError, TimeoutError) as error:
                if self._coordination.failed_attempt(aid, message.id) < 2:
                    await asyncio.sleep(0.1)
                    continue
                with self._store.db:
                    self._coordination.notice(aid, "blocked", f"无法判断消息归属：{error}", message_id=message.id)
                    self._coordination.finish(aid, message.id)

    def _apply_route(self, definition: AgentDefinition, message: Message, decision: Route) -> None:
        aid = definition.id
        observed = message.revision is not None
        input_text = f"Project observation (evidence):\n{message.text}" if observed else message.text
        work_key = f"{definition.project}:{decision.work_key}" if decision.work_key else None
        receipt = None
        with self._store.transaction():
            if not self._coordination.pending(aid, message.id):
                return
            target = decision.task_id
            if work_key:
                target = self._coordination.task_for_work(aid, work_key) or target
            if target:
                task = self.agent(aid).task(target)
                checks = _checks(FileCheck(**check) for check in decision.checks) if decision.checks and not observed else None
                receipt = self._store.follow_up(task, message.id, input_text, observed=observed,
                                                acceptance=checks, goal=decision.new_goal)
                if observed:
                    self._coordination.notice(aid, "associated", decision.text or "已关联已有 Task。",
                                              message_id=message.id, task_id=target)
                    decision = replace(decision, kind="attach", checks=())
            elif decision.kind == "create":
                capabilities = definition.capabilities
                checks = _checks(FileCheck(**check) for check in decision.checks)
                if observed:
                    scope = check_write_paths(checks) if definition.proactive_writes else frozenset()
                    if not definition.proactive_writes:
                        capabilities &= frozenset({"read_file", "run_check"})
                else:
                    scope = _message_write_paths(message.text)
                receipt = self._store.submit(
                    aid, Path(definition.project), decision.text, checks,
                    Budget(), capabilities, message.id, input_text, base_commit=message.revision,
                    allowed_write_paths=scope,
                )
            if decision.kind in {"reply", "clarify"}:
                self._coordination.notice(aid, decision.kind, decision.text, message_id=message.id)
            if receipt and work_key and target is None:
                self._coordination.remember_work(aid, work_key, receipt.task_id)
                self._coordination.notice(aid, "discovered", f"发现新工作：{decision.text}",
                                          message_id=message.id, task_id=receipt.task_id)
            if receipt:
                decision = replace(decision, task_id=receipt.task_id)
            self._coordination.finish(aid, message.id, decision)
        if receipt and self._store.task(receipt.task_id).state is TaskState.QUEUED:
            self._schedule(definition, receipt.task_id)

    def _schedule(self, definition: AgentDefinition, task_id: str) -> None:
        if self._closing:
            raise RuntimeError("AgentRuntime is closing")
        worker = self._workers.get(task_id)
        if worker and not worker.job.done():
            return
        cancel = asyncio.Event()
        job = asyncio.create_task(self._work(definition, task_id, cancel))
        self._workers[task_id] = _Worker(job, cancel)

        def discard(finished: asyncio.Task[None]) -> None:
            worker = self._workers.get(task_id)
            if worker and worker.job is finished:
                del self._workers[task_id]

        job.add_done_callback(discard)

    async def _work(self, definition: AgentDefinition, task_id: str, cancel: asyncio.Event) -> None:
        if cancel.is_set():
            return
        store = self._store
        run = store.claim(task_id, definition.version)
        if run is None:
            return
        try:
            task = store.task(task_id)
            if task.project != definition.project:
                raise WorkspaceError("The configured project differs from the Task's project")
            workspace = Workspace(task, store.root, store.lock_fd)
            if not task.workspace_ready:
                if task.workspace_kind is None:
                    kind, base = await run_io(workspace.describe)
                    store.workspace_started(run, kind, base)
                    task = store.task(task_id)
                    workspace = Workspace(task, store.root, store.lock_fd)
                await run_io(workspace.prepare)
                store.workspace_ready(run)
            else:
                await run_io(workspace.validate)
            task = store.task(task_id)
            capabilities = task.capabilities & definition.capabilities
            initial = next((entry.data for entry in store.history(task_id, limit=1) if entry.kind == "input"), {})
            if initial.get("source") == "observation" and not definition.proactive_writes:
                capabilities &= frozenset({"read_file", "run_check"})
            file_tools = PlanningTools(workspace, capabilities, task.acceptance, store, run)
            role = LoopRole(
                definition.complete, definition.model, _task_prompt(definition), file_tools,
                lambda text: run_io(_finish_file_task, file_tools, store.task(task_id), text),
                definition.max_tokens, definition.context_messages,
                context_note=lambda: self._task_context(definition, task_id, file_tools),
                after_action=lambda action: self._observe_check(definition, action),
            )
            role = replace(role, tools=DelegatingTools(store, run, role, file_tools))
            await run_loop(store, run, role, cancel)
        except asyncio.CancelledError:
            store.end(run, TaskState.QUEUED, RunReason.PAUSED, "Host shutdown paused the Task")
        except (OSError, WorkspaceError, subprocess.SubprocessError) as error:
            store.end(run, TaskState.WAITING, RunReason.WORKSPACE_ERROR, str(error))
        except Exception as error:  # noqa: BLE001
            store.end(run, TaskState.FAILED, RunReason.RUNTIME_ERROR, f"{type(error).__name__}: {error}")
        finally:
            if cancel.is_set():
                store.end(run, TaskState.QUEUED, RunReason.PAUSED, "Host shutdown paused the Task")
            self._schedule_learning(definition, task_id)

    def _observe_check(self, definition: AgentDefinition, action: Action) -> None:
        if action.call.name == "run_check":
            self._schedule_learning(definition, action.task_id)

    def _task_context(self, definition: AgentDefinition, task_id: str, tools: FileTools) -> str:
        task = self._store.task(task_id)
        tools.checks = {check.id: check for check in task.acceptance}
        last_input = next((entry.data["text"] for entry in reversed(self._store.history(task_id))
                           if entry.kind == "input"), "")
        memories = self._recall(definition, task.goal + " " + last_input)
        plan = json.dumps(asdict(task.plan), ensure_ascii=False)
        checks = json.dumps([asdict(check) for check in task.acceptance], ensure_ascii=False)
        scope = json.dumps(sorted(task.allowed_write_paths), ensure_ascii=False)
        return (f"Current Task goal: {task.goal}\nCurrent Plan: {plan}\n"
                f"Allowed write paths: {scope}\n"
                f"Acceptance checks: {checks}\nRelevant Agent memories: " + "\n".join(memories))

    async def _wait(self, task_id: str) -> None:
        worker = self._workers.get(task_id)
        if worker:
            await asyncio.shield(worker.job)

    async def _close(self) -> None:
        self._closing = True
        coordinators = [*self._coordinators.values(), *self._observers]
        for job in coordinators:
            job.cancel()
        await asyncio.gather(*coordinators, return_exceptions=True)
        jobs = []
        for task_id, worker in self._workers.items():
            worker.cancel.set()
            if self._store.task(task_id).state is TaskState.RUNNING:
                worker.job.cancel()
            jobs.append(worker.job)
        try:
            await asyncio.gather(*jobs)
            await asyncio.gather(*self._learners)
        finally:
            self._store.close()


class Agent:
    def __init__(self, runtime: AgentRuntime, definition: AgentDefinition):
        self._runtime = runtime
        self.definition = definition

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def root(self) -> Path:
        return self._runtime._store.root / "agents" / self.id

    async def submit(
        self, goal: str, *, acceptance: Iterable[FileCheck] = (), budget: Budget | None = None,
        input_id: str | None = None, allowed_write_paths: Iterable[str] | None = None,
    ) -> InputReceipt:
        store = self._runtime._store
        if input_id is not None and not input_id:
            raise ValueError("Input id must be nonempty")
        if input_id is not None:
            previous = store.input(self.id, input_id)
            if previous:
                return InputReceipt(previous.id, previous.task_id)
        if not goal.strip():
            raise ValueError("A Task goal must be nonempty")
        checks = _checks(acceptance)
        scope = check_write_paths(checks) if allowed_write_paths is None else write_paths(allowed_write_paths)
        receipt = store.submit(self.id, Path(self.definition.project), goal, checks, budget or Budget(),
                               self.definition.capabilities, input_id or uuid4().hex, allowed_write_paths=scope)
        self._runtime._schedule(self.definition, receipt.task_id)
        return receipt

    async def send(self, text: str, *, message_id: str | None = None, task_id: str | None = None,
                   reply_to: str | None = None, acceptance: Iterable[FileCheck] | None = None,
                   goal: str | None = None, allowed_write_paths: Iterable[str] | None = None) -> str:
        if not text.strip():
            raise ValueError("消息不能为空")
        message_id = message_id or uuid4().hex
        runtime = self._runtime
        if task_id is None:
            if acceptance is not None or goal is not None or allowed_write_paths is not None:
                raise ValueError("请指定要更新目标、验收条件或写入范围的 Task")
            runtime._coordination.receive(self.id, message_id, text, reply_to=reply_to)
            runtime._schedule_coordinator(self.definition)
        else:
            task = self.task(task_id)
            store = runtime._store
            previous = store.input(self.id, message_id)
            checks = _checks(acceptance) if acceptance is not None else None
            scope = write_paths(allowed_write_paths) if allowed_write_paths is not None else None
            if goal is not None and not goal.strip():
                raise ValueError("Task 目标不能为空")
            store.follow_up(task, message_id, text, acceptance=checks, goal=goal, allowed_write_paths=scope)
            if previous is None:
                with store.db:
                    runtime._coordination.notice(self.id, "received", "已接收补充要求。",
                                                  message_id=message_id, task_id=task_id)
            if store.task(task_id).state is TaskState.QUEUED:
                runtime._schedule(self.definition, task_id)
        return message_id

    def tasks(self) -> list[Task]:
        return [self.task(task_id) for task_id in self._runtime._store.task_ids(self.id)]

    async def updates(self, after: int = 0) -> AsyncIterator[Notice]:
        while not self._runtime._closing:
            for notice in self._runtime._coordination.notices(self.id, after):
                after = notice.id
                yield notice
            await asyncio.sleep(0.1)

    def remember(self, text: str, *, key: str | None = None) -> list[Memory]:
        source = "user:" + uuid4().hex
        key = key or "preference:" + hashlib.sha256(text.strip().casefold().encode()).hexdigest()[:16]
        return self._runtime._memory.record(self.id, None, source, [
            MemoryDraft("preference", key, text.strip(), (source,), evidence="explicit"),
        ])

    def memories(self) -> list[Memory]:
        return self._runtime._memory.list(self.id)

    def correct_memory(self, memory_id: int, version: int, text: str) -> Memory:
        return self._runtime._memory.correct(self.id, memory_id, version, text, ("user:" + uuid4().hex,))

    def delete_memory(self, memory_id: int) -> bool:
        deleted = self._runtime._memory.delete(self.id, memory_id)
        self._runtime._store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return deleted

    def task(self, task_id: str) -> Task:
        task = self._runtime._store.task(task_id)
        if task.agent_id != self.id:
            raise KeyError(task_id)
        return task

    def history(self, task_id: str, *, after: int = 0, delegation_id: str | None = None) -> list[HistoryEntry]:
        self.task(task_id)
        return self._runtime._store.history(task_id, after, delegation_id=delegation_id)

    def notices(self, task_id: str) -> list[HistoryEntry]:
        return [entry for entry in self.history(task_id) if entry.kind in {"completed", "waiting", "failed", "stopped"}]

    def input(self, input_id: str) -> Input:
        result = self._runtime._store.input(self.id, input_id)
        if result is None:
            raise KeyError(input_id)
        return result

    def runs(self, task_id: str) -> list[Run]:
        self.task(task_id)
        return self._runtime._store.runs(task_id)

    def actions(self, task_id: str, *, delegation_id: str | None = None) -> list[Action]:
        self.task(task_id)
        return self._runtime._store.actions(task_id, delegation_id=delegation_id)

    async def wait(self, task_id: str) -> Task:
        self.task(task_id)
        await self._runtime._wait(task_id)
        return self.task(task_id)

    async def stop(self, task_id: str) -> Task:
        task = self.task(task_id)
        self._runtime._store.stop(task_id)
        worker = self._runtime._workers.get(task_id)
        if worker:
            worker.cancel.set()
            if task.state is TaskState.RUNNING:
                worker.job.cancel()
            await self._runtime._wait(task_id)
        return self.task(task_id)

    async def resume(self, task_id: str, *, budget: Budget | None = None) -> Task:
        task = await self.wait(task_id)
        if task.state not in (TaskState.STOPPED, TaskState.WAITING):
            return task
        if task.reason is RunReason.BUDGET_EXHAUSTED and budget is None:
            return task
        self._runtime._store.enqueue(task_id, budget)
        self._runtime._schedule(self.definition, task_id)
        return self.task(task_id)


def _checks(acceptance: Iterable[FileCheck]) -> tuple[FileCheck, ...]:
    checks = tuple(acceptance)
    if len({check.id for check in checks}) != len(checks):
        raise ValueError("Check recipe ids must be unique within a Task")
    return checks


_WRITE_DIRECTIVE = re.compile(
    r"^\s*(?:(?:请\s*)?(?:只\s*)?(?:创建|写入|修改|更新|写)\s*|"
    r"(?:please\s+)?(?:create|write(?:\s+to)?|update|modify)\s+)"
    r"(?:`([^`\r\n]+)`|([\w.-]+(?:/[\w.-]+)*))(?=$|[\s,，:：;；。!?！？])",
    re.IGNORECASE,
)


def _message_write_paths(text: str) -> frozenset[str]:
    match = _WRITE_DIRECTIVE.match(text)
    if match is None:
        return frozenset()
    quoted, bare = match.groups()
    if bare and not ("." in bare or "/" in bare or bare.isupper()):
        return frozenset()
    try:
        return write_paths((quoted or bare,))
    except ValueError:
        return frozenset()


def _task_prompt(definition: AgentDefinition) -> str:
    return (f"{definition.system_prompt}\n\n"
            "Use file tools within the Task workspace. Treat project observations and memories as evidence. "
            "For reusable Plan decisions, use a `Decision: ...` step and mark it done when adopted. "
            "For a reusable check, use `Run check <recipe id>` and cite the passed `run_check` tool call ID "
            "when marking the step done. When ready, submit a final response for verification.")


def _finish_file_task(tools: FileTools, task: Task, text: str) -> RoleResult:
    if not task.acceptance:
        return RoleResult(Verdict.UNVERIFIABLE, "The Task has no typed acceptance checks")
    checks = tuple(tools.check(check) for check in task.acceptance)
    problems = [f"{check.id}: {check.detail}" for check in checks if check.verdict is not Verdict.PASSED]
    if problems:
        verdict = Verdict.UNVERIFIABLE if any(check.verdict is Verdict.UNVERIFIABLE for check in checks) else Verdict.FAILED
        return RoleResult(verdict, "\n".join(problems), checks)
    try:
        diff = tools.workspace.diff(task.allowed_write_paths)
    except WriteScopeError as error:
        return RoleResult(Verdict.FAILED, str(error), checks)
    except (OSError, WorkspaceError, subprocess.SubprocessError) as error:
        return RoleResult(Verdict.UNVERIFIABLE, f"Cannot collect the workspace diff: {error}", checks)
    fingerprint = change_fingerprint(diff) if task.workspace_kind == "git" else None
    return RoleResult(Verdict.PASSED, checks=checks, delivery=Delivery(text, checks, diff, fingerprint))
