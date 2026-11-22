"""Agent 长期记忆及来源去重。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .core.plan import Plan, StepState
from .core.task import Verdict

if TYPE_CHECKING:
    from .core.storage import HistoryEntry
    from .core.task import Action

MemoryKind = Literal["preference", "fact", "constraint", "experience"]
MemoryEvidence = Literal["explicit", "verified", "inferred"]
MemoryState = Literal["active", "candidate", "archived"]

_KINDS = {"preference", "fact", "constraint", "experience"}
_EVIDENCE = {"explicit", "verified", "inferred"}
_ARCHIVE_RETENTION = 30 * 86400
_EXPERIENCE_LIFETIME = 30 * 86400
_PROJECT_DECLARATION = re.compile(
    r"(project fact|项目事实|project constraint|项目约束)[:：]"
    r"\s*([^:=：＝]{1,80})[=＝]\s*(.{1,500})",
    re.IGNORECASE,
)
_DECLARATION_KIND: dict[str, MemoryKind] = {
    "project fact": "fact", "项目事实": "fact",
    "project constraint": "constraint", "项目约束": "constraint",
}
_PLAN_DECISION = re.compile(r"(?:decision|决策)[:：]\s*(.+)", re.IGNORECASE)
_PLAN_CHECK = re.compile(r"(?:run check |run_check[:：]\s*|运行检查[:：]\s*)(.+)", re.IGNORECASE)
_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS agent_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        project TEXT NOT NULL,
        kind TEXT NOT NULL,
        key TEXT NOT NULL,
        text TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active', 'candidate', 'archived')),
        sources TEXT NOT NULL,
        updated_at REAL NOT NULL,
        valid_until REAL,
        supersedes INTEGER,
        version INTEGER NOT NULL,
        archived_at REAL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS agent_memory_active_key
    ON agent_memories (agent_id, project, kind, key) WHERE state = 'active'
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_memory_receipts (
        fingerprint BLOB PRIMARY KEY
    ) WITHOUT ROWID
    """,
)
_COLUMNS = (
    "id, agent_id, project, kind, key, text, state, sources, "
    "updated_at, valid_until, supersedes, version"
)
_CREDENTIAL = re.compile(
    r"""(?ix)
    (?:
        (?<![a-z0-9])(?:password|passwd|pwd|api[\s_-]*key|secret(?:[\s_-]*key)?
        |credential|(?:access[\s_-]*|auth[\s_-]*|refresh[\s_-]*)?token)\b
        |密码|口令|密钥|令牌
    )["']?\s*(?:[:=：]|\bis\b|是|为)\s*\S
    |\bbearer\s+[a-z0-9._~+/-]{8,}
    |\bauthorization["']?\s*:\s*["']?basic\s+[a-z0-9+/]{8,}
    |://[^\s/@]+:[^\s/@]+@
    |-----BEGIN\s+(?:[a-z]+\s+)*PRIVATE\s+KEY-----
    |\b(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{16,}|(?:AKIA|ASIA)[a-z0-9]{16})
    |\beyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+\b
    """
)
_WORDS = re.compile(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+")
_HAN = re.compile(r"[\u3400-\u9fff]")
_STOP_WORDS = {
    "an", "and", "are", "for", "from", "how", "in", "is", "of", "on",
    "or", "please", "run", "the", "this", "to", "use", "with", "you",
}
_FAMILY = "agent_id = ? AND project = ? AND kind = ? AND key = ?"


@dataclass(frozen=True)
class MemoryDraft:
    """Supply explicit or verified evidence only after checking its source."""

    kind: MemoryKind
    key: str
    text: str
    sources: tuple[str, ...]
    evidence: MemoryEvidence = "inferred"
    valid_until: float | None = None


@dataclass(frozen=True)
class Memory:
    id: int
    agent_id: str
    project: str | None
    kind: MemoryKind
    key: str
    text: str
    state: MemoryState
    sources: tuple[str, ...]
    updated_at: float
    valid_until: float | None
    supersedes: int | None
    version: int


class MemoryConflict(ValueError):
    """The requested revision is no longer current."""


def _fingerprint(*parts: str) -> bytes:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).digest()


def _draft_fields(draft: MemoryDraft) -> tuple[str, str, tuple[str, ...]]:
    key, text = draft.key.strip().casefold(), draft.text.strip()
    sources = tuple(dict.fromkeys(source.strip() for source in draft.sources))
    if not key or not text or not sources or not all(sources):
        raise ValueError("memory requires a key, text, and source references")
    if draft.kind not in _KINDS or draft.evidence not in _EVIDENCE:
        raise ValueError("unknown memory kind or evidence")
    if draft.valid_until is not None and not math.isfinite(draft.valid_until):
        raise ValueError("memory expiration must be a finite timestamp")
    return key, text, sources


def _memory(row: tuple | sqlite3.Row, now: float) -> Memory:
    memory_id, agent_id, project, kind, key, text, state, sources, updated, until, old, version = row
    if until is not None and until <= now:
        state = "archived"
    return Memory(memory_id, agent_id, project or None, kind, key, text, state,
                  tuple(json.loads(sources)), updated, until, old, version)


def _terms(text: str) -> set[str]:
    terms = set()
    for word in _WORDS.findall(text.casefold()):
        if _HAN.match(word):
            terms.update(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        elif len(word) > 1 and word not in _STOP_WORDS:
            terms.add(word)
    return terms


def _user_memories(entry: HistoryEntry, source: str) -> Iterator[MemoryDraft]:
    text = entry.data.get("text", "")
    if (entry.run_id is not None or not entry.data.get("input_id")
            or entry.data.get("source", "user") != "user"
            or text.startswith("Project observation (evidence):\n")):
        return
    for line in text.strip().splitlines():
        declaration = _PROJECT_DECLARATION.fullmatch(line)
        if declaration is None:
            break
        label, key, value = declaration.groups()
        key, value = " ".join(key.casefold().split()), value.strip()
        if key and value:
            yield MemoryDraft(_DECLARATION_KIND[label.casefold()], key, f"{key}: {value}",
                              (source,), evidence="explicit")


def learn_from_session(
    history: Iterable[HistoryEntry], actions: Iterable[Action], now: float,
) -> Iterator[tuple[str, list[MemoryDraft]]]:
    """Collect memories from trusted inputs, Plan changes, and host checks."""
    by_id = {action.id: action for action in actions}
    passed_checks: dict[tuple[str, str], tuple[str, str] | None] = {}
    plan = Plan()
    valid_until = now + _EXPERIENCE_LIFETIME
    for entry in history:
        source = f"task:{entry.task_id}:entry:{entry.id}"
        drafts = []
        if entry.kind == "input":
            drafts = list(_user_memories(entry, source))
        elif entry.kind in {"action_result", "completed"}:
            checks = entry.data.get("checks", ()) if entry.kind == "completed" else ()
            action = by_id.get(entry.data.get("action_id"))
            if action and action.call.name == "run_check" and action.result:
                check = action.result.details or {}
                passed = not action.result.is_error and check.get("verdict") == Verdict.PASSED
                checks = (check,) if passed else ()
                references = {action.id, action.call.call_id, source}
                for reference in references:
                    key = (action.run_id, reference)
                    passed_checks[key] = (check["id"], source) if passed and key not in passed_checks else None
            drafts = [MemoryDraft(
                "experience", f"check:{check['id']}",
                f"项目文件 {check['path']} 可以用验收项 {check['id']} 校验。此次检查通过。",
                (source,), evidence="verified", valid_until=valid_until,
            ) for check in checks if check.get("verdict") == Verdict.PASSED]
        elif entry.kind == "plan_revised" and entry.run_id is not None:
            old = {step.id: step for step in plan.steps}
            plan = Plan.from_record(entry.data["plan"])
            for step in plan.steps:
                if step.state != StepState.DONE or step == old.get(step.id):
                    continue
                decision = _PLAN_DECISION.fullmatch(step.action)
                text = decision[1].strip() if decision else step.action
                recipe = _PLAN_CHECK.fullmatch(text)
                if not text or (decision is None and recipe is None):
                    continue
                proofs = (passed_checks.get((entry.run_id, ref)) for ref in step.evidence)
                proof = next((check for check in proofs if check and recipe and recipe[1] == check[0]), None)
                drafts.append(MemoryDraft(
                    "experience", f"plan:{text}", text,
                    (source, proof[1]) if proof else (source,),
                    evidence="verified" if proof else "inferred", valid_until=valid_until,
                ))
        if drafts:
            yield source, drafts


class MemoryStore:
    """Reuse the host connection and serialize calls on its thread.

    Writes use a savepoint. Releasing it commits a standalone call or leaves
    the commit to an enclosing host transaction. Sources must be durable
    reference IDs, such as an input ID or a Task entry ID.
    """

    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], float] = time.time):
        self._conn = conn
        self._clock = clock
        with self._write():
            for statement in _SCHEMA:
                conn.execute(statement)

    @contextmanager
    def _write(self) -> Iterator[None]:
        self._conn.execute("SAVEPOINT memory_write")
        try:
            yield
            self._conn.execute("RELEASE memory_write")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK TO memory_write")
                self._conn.execute("RELEASE memory_write")
            raise

    def _claim(self, fingerprint: bytes) -> bool:
        return bool(self._conn.execute(
            "INSERT INTO agent_memory_receipts VALUES (?) ON CONFLICT DO NOTHING",
            (fingerprint,),
        ).rowcount)

    def processed(self, agent_id: str, project: str | None, checkpoint: str) -> bool:
        """Return whether the checkpoint batch was already committed or staged."""
        return self._conn.execute(
            "SELECT 1 FROM agent_memory_receipts WHERE fingerprint = ?",
            (_fingerprint("checkpoint", agent_id, project or "", checkpoint),),
        ).fetchone() is not None

    def record(
        self, agent_id: str, project: str | None, checkpoint: str, drafts: Iterable[MemoryDraft],
    ) -> list[Memory]:
        """Record a batch once and return the newly created memories."""
        if not checkpoint.strip():
            raise ValueError("memory checkpoint is required")
        scope = (agent_id, project or "")
        now = self._clock()
        saved = []
        with self._write():
            if not self._claim(_fingerprint("checkpoint", *scope, checkpoint)):
                return saved
            self._archive_expired(agent_id, now)
            for draft in drafts:
                key, text, sources = _draft_fields(draft)
                if any(_CREDENTIAL.search(value) for value in (key, text, *sources)):
                    continue
                family = (*scope, draft.kind, key)
                new_sources = [
                    source for source in sources
                    if self._claim(_fingerprint("source", *family, source))
                ]
                if not new_sources:
                    continue
                active = self._conn.execute(
                    f"SELECT text FROM agent_memories WHERE {_FAMILY} AND state = 'active'",
                    family,
                ).fetchone()
                if active and active[0] == text:
                    continue
                state: MemoryState = "candidate"
                if draft.valid_until is not None and draft.valid_until <= now:
                    state = "archived"
                elif not active and draft.evidence in {"explicit", "verified"}:
                    state = "active"
                if state == "candidate" and self._conn.execute(
                    f"SELECT 1 FROM agent_memories WHERE {_FAMILY} AND state = ? AND text = ?",
                    (*family, state, text),
                ).fetchone():
                    continue
                if state == "active":
                    self._conn.execute(
                        "UPDATE agent_memories SET state = 'archived', archived_at = ? "
                        f"WHERE {_FAMILY} AND state = 'candidate' AND text = ?",
                        (now, *family, text),
                    )
                saved.append(self._insert(family, text, state, sources, now, draft.valid_until))
        return saved

    def _insert(
        self, family: tuple[str, str, MemoryKind, str], text: str, state: MemoryState,
        sources: tuple[str, ...], now: float, valid_until: float | None,
        previous: Memory | None = None,
    ) -> Memory:
        supersedes = previous.id if previous else None
        version = previous.version + 1 if previous else 1
        cursor = self._conn.execute(
            """
            INSERT INTO agent_memories
            (agent_id, project, kind, key, text, state, sources, updated_at,
             valid_until, supersedes, version, archived_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*family, text, state, json.dumps(sources, ensure_ascii=False), now,
             valid_until, supersedes, version, now if state == "archived" else None),
        )
        agent_id, project, kind, key = family
        return Memory(cursor.lastrowid, agent_id, project or None, kind, key, text, state,
                      sources, now, valid_until, supersedes, version)

    def get(self, agent_id: str, memory_id: int) -> Memory | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM agent_memories WHERE agent_id = ? AND id = ?",
            (agent_id, memory_id),
        ).fetchone()
        return _memory(row, self._clock()) if row else None

    def list(
        self, agent_id: str, project: str | None = None, *, state: MemoryState | None = None,
    ) -> list[Memory]:
        """List all Agent memories or the records for one exact project."""
        scope = "" if project is None else " AND project = ?"
        params = (agent_id,) if project is None else (agent_id, project)
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM agent_memories WHERE agent_id = ?{scope} ORDER BY id DESC",
            params,
        )
        now = self._clock()
        return [memory for memory in (_memory(row, now) for row in rows)
                if state is None or memory.state == state]

    def recall(
        self, agent_id: str, project: str | None, query: str, *, limit: int = 5,
    ) -> list[Memory]:
        if limit < 0:
            raise ValueError("memory recall limit must be nonnegative")
        terms = _terms(query)
        if not terms or limit == 0:
            return []
        now = self._clock()
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM agent_memories "
            "WHERE agent_id = ? AND project IN ('', ?) AND state = 'active' "
            "AND (valid_until IS NULL OR valid_until > ?) ORDER BY updated_at DESC, id DESC",
            (agent_id, project or "", now),
        )
        ranked = []
        for row in rows:
            memory = _memory(row, now)
            score = len(terms & _terms(memory.key + " " + memory.text))
            if score:
                ranked.append((score, memory))
        ranked.sort(key=lambda match: match[0], reverse=True)
        return [memory for _, memory in ranked[:limit]]

    def correct(
        self, agent_id: str, memory_id: int, expected_version: int,
        text: str, sources: tuple[str, ...], *, valid_until: float | None = None,
    ) -> Memory:
        """Archive the current values for this key and write a user revision."""
        now = self._clock()
        with self._write():
            self._archive_expired(agent_id, now)
            current = self.get(agent_id, memory_id)
            if not current or current.version != expected_version or current.state == "archived":
                raise MemoryConflict("memory revision is no longer current")
            draft = MemoryDraft(current.kind, current.key, text, sources, "explicit", valid_until)
            key, text, sources = _draft_fields(draft)
            if any(_CREDENTIAL.search(value) for value in (text, *sources)):
                raise ValueError("memory contains a credential")
            if valid_until is not None and valid_until <= now:
                raise ValueError("memory correction must expire in the future")
            family = (agent_id, current.project or "", current.kind, key)
            for source in sources:
                self._claim(_fingerprint("source", *family, source))
            self._conn.execute(
                "UPDATE agent_memories SET state = 'archived', archived_at = ? "
                f"WHERE {_FAMILY} AND state != 'archived'",
                (now, *family),
            )
            return self._insert(family, text, "active", sources, now, valid_until, current)

    def delete(self, agent_id: str, memory_id: int) -> bool:
        """Delete a revision's text and provenance while retaining replay receipts."""
        with self._write():
            return bool(self._conn.execute(
                "DELETE FROM agent_memories WHERE agent_id = ? AND id = ?",
                (agent_id, memory_id),
            ).rowcount)

    def _archive_expired(self, agent_id: str, now: float) -> None:
        self._conn.execute(
            "UPDATE agent_memories SET state = 'archived', archived_at = ? "
            "WHERE agent_id = ? AND state != 'archived' AND valid_until <= ?",
            (now, agent_id, now),
        )

    def maintain(self, agent_id: str, *, retention: float = _ARCHIVE_RETENTION) -> int:
        """Archive expired records and remove archives beyond their retention."""
        if not math.isfinite(retention) or retention < 0:
            raise ValueError("memory archive retention must be finite and nonnegative")
        now = self._clock()
        with self._write():
            self._archive_expired(agent_id, now)
            return self._conn.execute(
                "DELETE FROM agent_memories WHERE agent_id = ? AND archived_at <= ?",
                (agent_id, now - retention),
            ).rowcount
