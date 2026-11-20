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

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
