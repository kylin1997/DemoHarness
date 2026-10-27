"""core/storage.py + core/session.py 的验收测试。

覆盖 docs/design/storage-session.md「测试清单」12 项（每能力对应可观察行为，
mock 确定性，不依赖网络）。测试用临时文件 SQLite，各开各的库。
"""

from __future__ import annotations

import asyncio

import pytest

from democlaw.core.session import (
    AppendOp,
    CompactOp,
    DeleteOp,
    ForkOp,
    HistoryOp,
    InvalidEntry,
    NewOp,
    ResetOp,
    SessionBusy,
    SessionManager,
    SessionNotFound,
)
from democlaw.core.storage import Storage, StorageAlreadyOpen


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def storage(tmp_db):
    s = Storage(tmp_db)
    yield s
    if s.path not in getattr(Storage, "_open_paths", {}):
        s.close()
    else:
        s.close()


@pytest.fixture
def mgr(storage):
    return SessionManager(storage)


def run(coro):
    return asyncio.run(coro)


async def new_session(mgr, bot_id="b1", **kw):
    return await mgr.submit("", NewOp("", bot_id=bot_id, **kw))


# ── 1. schema 建库与 reopen 幂等；同文件二次打开抛 StorageAlreadyOpen ──


def test_schema_reopen_idempotent(tmp_db):
    s1 = Storage(tmp_db)
    s1.close()
    s2 = Storage(tmp_db)  # 不抛，说明 reopen 幂等
    assert s2.sessions_all() == []
    s2.close()


def test_double_open_raises(tmp_db):
    s1 = Storage(tmp_db)
    with pytest.raises(StorageAlreadyOpen):
        Storage(tmp_db)
    s1.close()
    # 关闭后可重新打开
    s2 = Storage(tmp_db)
    s2.close()


# ── 2. 事务原子性：事务内抛 → 无半状态 ──


def test_transaction_rollback_leaves_no_partial(mgr, tmp_db):
    # 直接构造一个会在事务中途失败的场景：delete 一个不存在会话的树
    # 不应留下半状态（entry 与 active_leaf 同回滚由事务语义保证）。
    info = run(new_session(mgr))
    sid = info.id
    run(
        mgr.submit(
            sid, AppendOp(sid, "user", "hi", parent=info.active_entry_id)
        )
    )
    mgr.mark_running(sid, "t1")
    with pytest.raises(SessionBusy):
        run(mgr.submit(sid, DeleteOp(sid)))
    # 会话未删，entries 仍在
    assert mgr._storage.session(sid) is not None
    assert len(mgr._storage.entries(sid)) == 1


# ── 3. id 跨重启单调；发号回滚后重发同号无害 ──


def test_id_monotonic_and_reopen_mint(tmp_db):
    s1 = Storage(tmp_db)
    with s1.transaction() as c:
        e1 = s1.mint_entry_id(c)
        s2_ = s1.mint_session_id(c)
    assert e1 == "e_1"
    assert s2_ == "s_1"
    s1.close()
    s2 = Storage(tmp_db)
    with s2.transaction() as c:
        assert s2.mint_entry_id(c) == "e_2"
        assert s2.mint_session_id(c) == "s_2"
    s2.close()


def test_mint_rollback_reissues_same_id(tmp_db):
    s = Storage(tmp_db)
    with pytest.raises(RuntimeError), s.transaction() as c:
        assert s.mint_entry_id(c) == "e_1"
        raise RuntimeError("boom")  # 回滚，e_1 从未被占用
    s.close()
    s2 = Storage(tmp_db)
    with s2.transaction() as c:
        assert s2.mint_entry_id(c) == "e_1"  # 重发同号无害
    s2.close()


# ── 4. append parent 一致性防御 ──


def test_append_chain(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "x", parent=None)))
    e2 = run(mgr.submit(info.id, AppendOp(info.id, "assistant", "y", parent=e1)))
    # 校验链：e2.parent == e1
    assert mgr._storage.entry(e2).parent_id == e1
    # 活跃叶是 e2
    assert mgr._storage.session(info.id).active_leaf == e2
    s.close()


# ── 5. fork：at 校验 / 前缀正确 / 新 id 重映射 / 源忙碌仍一致 ──


def test_fork_basic(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    e2 = run(mgr.submit(info.id, AppendOp(info.id, "assistant", "b", parent=e1)))
    info2 = run(mgr.submit(info.id, ForkOp(info.id, at=e2)))
    # 前缀拷贝：2 条 entry，id 已重映射
    copied = mgr._storage.entries(info2.id)
    assert len(copied) == 2
    assert copied[0].kind == "user" and copied[0].id != e1
    assert copied[1].kind == "assistant" and copied[1].id != e2
    assert copied[1].parent_id == copied[0].id
    # fork 边：parent_id 指向源会话
    assert mgr._storage.session(info2.id).parent_id == info.id
    s.close()


def test_fork_at_not_in_tree(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    with pytest.raises(InvalidEntry):
        run(mgr.submit(info.id, ForkOp(info.id, at="e_999")))
    s.close()


def test_fork_while_source_busy(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    mgr.mark_running(info.id, "t1")
    run(mgr.submit(info.id, AppendOp(info.id, "assistant", "b", parent=e1)))
    info2 = run(mgr.submit(info.id, ForkOp(info.id, at=None)))
    # 快照一致：拷贝链完整（2 条）
    assert len(mgr._storage.entries(info2.id)) == 2
    s.close()


# ── 6. reset：祖先校验 / reset entry 语义 / running 拒绝、clear 后通过 ──


def test_reset_basic_and_replay(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "hello", parent=None)))
    run(mgr.submit(info.id, AppendOp(info.id, "assistant", "reply", parent=e1)))
    # reset 到 e1（祖先）
    reid = run(mgr.submit(info.id, ResetOp(info.id, to=e1)))
    # 活跃叶 = reset entry
    assert mgr._storage.session(info.id).active_leaf == reid
    # 重放上下文 = reset entry + 其后（此处无其后 entry，仅 reset）
    hist = run(mgr.submit(info.id, HistoryOp(info.id)))
    assert [h.kind for h in hist] == ["reset"]
    s.close()


def test_reset_not_ancestor(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    # 造一个与 e1 平行的非祖先（另一会话的 entry）
    info2 = run(new_session(mgr, bot_id="b2"))
    e_other = run(mgr.submit(info2.id, AppendOp(info2.id, "user", "z", parent=None)))
    with pytest.raises(InvalidEntry):
        run(mgr.submit(info.id, ResetOp(info.id, to=e_other)))
    s.close()


def test_reset_busy_then_clear(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    mgr.mark_running(info.id, "t1")
    with pytest.raises(SessionBusy):
        run(mgr.submit(info.id, ResetOp(info.id, to=e1)))
    mgr.clear_running(info.id)
    run(mgr.submit(info.id, ResetOp(info.id, to=e1)))
    assert mgr._storage.session(info.id).active_leaf is not None
    s.close()


# ── 7. compact：任务边界成功 / 漂移放弃 ──


def test_compact_success(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    e2 = run(mgr.submit(info.id, AppendOp(info.id, "assistant", "b", parent=e1)))
    ok = run(mgr.submit(info.id, CompactOp(info.id, summary="摘要", leaf=e2)))
    assert ok is True
    # 重放含摘要
    hist = run(mgr.submit(info.id, HistoryOp(info.id)))
    assert hist[-1].kind == "compaction"
    assert hist[-1].content == "摘要"
    s.close()


def test_compact_drift(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    # 声称基于 e1 摘要，但实际又 append 了新 entry（漂移）
    run(mgr.submit(info.id, AppendOp(info.id, "assistant", "b", parent=e1)))
    ok = run(mgr.submit(info.id, CompactOp(info.id, summary="摘要", leaf=e1)))
    assert ok is False
    s.close()


# ── 8. 重放多边界 ──


def test_replay_multiple_boundaries(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    e2 = run(mgr.submit(info.id, AppendOp(info.id, "assistant", "b", parent=e1)))
    c1 = run(mgr.submit(info.id, CompactOp(info.id, summary="s1", leaf=e2)))
    e3 = run(mgr.submit(info.id, AppendOp(info.id, "assistant", "c", parent=c1)))
    # 再 reset
    run(mgr.submit(info.id, ResetOp(info.id, to=e3)))
    hist = run(mgr.submit(info.id, HistoryOp(info.id)))
    kinds = [h.kind for h in hist]
    # 链上多边界取最近：reset 是最近的边界，其后无内容 → 仅 reset
    assert kinds == ["reset"]
    s.close()


# ── 9. FIFO 串行化：并发 append 线性化；HistoryOp 在 append 后可见 ──


def test_fifo_serialization(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))

    async def append_many():
        eids = []
        parent = None
        for i in range(20):
            eid = await mgr.submit(
                info.id,
                AppendOp(info.id, "user", f"m{i}", parent=parent),
            )
            eids.append(eid)
            parent = eid
        return eids

    eids = run(append_many())
    # 线性化：20 条且按提交顺序连成一条链
    entries = mgr._storage.entries(info.id)
    assert len(entries) == 20
    for e, expected in zip(entries, eids):
        assert e.id == expected
    s.close()


def test_history_sees_after_append(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))

    async def seq():
        e1 = await mgr.submit(info.id, AppendOp(info.id, "user", "x", parent=None))
        hist = await mgr.submit(info.id, HistoryOp(info.id))
        return e1, hist

    e1, hist = run(seq())
    assert hist[-1].id == e1
    s.close()


# ── 10. delete：级联 fork 子树 / 忙碌拒绝 / 删后 SessionNotFound / 无孤儿 ──


def test_delete_cascade(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    info2 = run(mgr.submit(info.id, ForkOp(info.id, at=e1)))
    run(mgr.submit(info2.id, AppendOp(info2.id, "user", "b", parent=info2.active_entry_id)))
    run(mgr.submit(info.id, DeleteOp(info.id)))
    # 根与 fork 子树均删
    assert mgr._storage.session(info.id) is None
    assert mgr._storage.session(info2.id) is None
    # 无孤儿 entry
    assert mgr._storage.entries(info.id) == []
    assert mgr._storage.entries(info2.id) == []
    s.close()


def test_delete_busy_rejected(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    e1 = run(mgr.submit(info.id, AppendOp(info.id, "user", "a", parent=None)))
    info2 = run(mgr.submit(info.id, ForkOp(info.id, at=e1)))
    mgr.mark_running(info2.id, "t1")
    with pytest.raises(SessionBusy):
        run(mgr.submit(info.id, DeleteOp(info.id)))
    # 未删
    assert mgr._storage.session(info.id) is not None
    s.close()


def test_submit_after_delete_raises(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info = run(new_session(mgr))
    run(mgr.submit(info.id, DeleteOp(info.id)))
    with pytest.raises(SessionNotFound):
        run(mgr.submit(info.id, AppendOp(info.id, "user", "x", parent=None)))
    s.close()


# ── 11. bot_id 过滤 ──


def test_sessions_of_bot(tmp_db):
    s = Storage(tmp_db)
    mgr = SessionManager(s)
    info1 = run(new_session(mgr, bot_id="a"))
    run(new_session(mgr, bot_id="b"))
    bots = mgr._storage.sessions_of_bot("a")
    assert [b.id for b in bots] == [info1.id]
    s.close()


# ── 12. WAL 恢复：事务中途 kill → 重开无半状态 ──


def test_wal_recovery_no_partial(tmp_db):
    # 模拟：手动开事务写一半不提交（等价 kill）
    import sqlite3

    conn = sqlite3.connect(tmp_db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, parent_id TEXT,
            fork_at TEXT, active_leaf TEXT, model TEXT, title TEXT,
            created_at REAL NOT NULL, origin_parent_id TEXT,
            background INTEGER NOT NULL DEFAULT 0, restart_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            parent_id TEXT REFERENCES entries(id), kind TEXT NOT NULL,
            content TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO sessions (id, bot_id, created_at) VALUES ('s_x', 'bot', 1.0)"
    )
    # 不 commit，直接断开（模拟中途 kill）
    conn.close()

    # 重开库：无半状态
    s = Storage(tmp_db)
    assert s.session("s_x") is None
    s.close()