"""core/task.py 的验收测试。

覆盖 docs/design/task.md「测试清单」8 项（mock 确定性，临时文件 SQLite）。
task.py 是纯函数库：phase 推导 / stop_reason 落点 / 恢复物化与扫描。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from demoharness.core.session import AppendOp, SessionManager
from demoharness.core.storage import EntryRow, Storage
from demoharness.core.task import (
    NextStep,
    StopReason,
    decide,
    is_terminal_entry,
    materialize_interrupted,
    scan_and_recover,
    stop_reason_of,
    terminal_content,
    terminal_text,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "t.db")


@pytest.fixture
def storage(tmp_db):
    s = Storage(tmp_db)
    yield s
    s.close()


@pytest.fixture
def mgr(storage):
    return SessionManager(storage)


def _mk(kind: str, content: str, parent: str | None = None, sid: str = "s", i: int = 0) -> EntryRow:
    return EntryRow(
        id=f"e_{i}",
        session_id=sid,
        parent_id=parent,
        kind=kind,
        content=content,
        created_at=float(i),
    )


async def _new_session(mgr, bot="b1"):
    from demoharness.core.session import NewOp

    return await mgr.submit("", NewOp("", bot_id=bot))


async def _append(mgr, sid, kind, content, parent):
    return await mgr.submit(sid, AppendOp(sid, kind, content, parent=parent))


# ── 1. phase 推导纯函数 ──


def test_decide_pure():
    assert decide([]) == NextStep.CALL_MODEL
    assert decide([_mk("user", "hi")]) == NextStep.CALL_MODEL
    assert decide([_mk("assistant", "text", i=1)]) == NextStep.CALL_MODEL
    # tool_call 叶子 → 执行工具
    assert decide([_mk("tool_call", json.dumps({"call_id": "c1"}), i=1)]) == NextStep.EXECUTE_TOOLS
    # assistant 带 tool_calls（过渡态，content JSON 含 tool_calls 键）→ 执行工具
    tc = json.dumps({"tool_calls": [{"call_id": "c1", "name": "f"}]})
    assert decide([_mk("assistant", tc, i=1)]) == NextStep.EXECUTE_TOOLS
    assert decide([_mk("tool_result", json.dumps({"call_id": "c1"}), i=2)]) == NextStep.CALL_MODEL
    # 终态
    term = terminal_content("done", StopReason.COMPLETED)
    assert decide([_mk("assistant", term, i=3)]) == NextStep.DONE
    assert decide([_mk("interrupted", json.dumps({"phase": "x"}), i=4)]) == NextStep.DONE


# ── 2 & 3. 三相推进落盘顺序 + 预算收尾（预算逻辑归 loop，本模块只测落点） ──


def test_terminal_content_scheme_a():
    content = terminal_content("回答", StopReason.COMPLETED)
    parsed = json.loads(content)
    assert parsed["text"] == "回答"
    assert parsed["stop_reason"] == "completed"
    # 六态均可写入读出
    for reason in StopReason:
        c = terminal_content("x", reason)
        assert StopReason(json.loads(c)["stop_reason"]) == reason


# ── 5. stop_reason 元数据落点（方案 A） ──


def test_stop_reason_metadata_roundtrip():
    c = terminal_content("final", StopReason.ERROR)
    e = _mk("assistant", c, i=1)
    assert is_terminal_entry(e)
    assert stop_reason_of(e) == StopReason.ERROR
    assert terminal_text(e) == "final"
    # 非终态非 assistant 不算
    assert not is_terminal_entry(_mk("assistant", "plain text", i=2))
    assert not is_terminal_entry(_mk("user", "hi", i=3))


# ── 6. 恢复扫描：tool_call→物化 result+interrupted；模型等待→仅 interrupted 等 ──


def _build_session(mgr, sid, entries):
    """按 parent 链顺序 append 一串 entry，返回终态前置状态。"""
    last = None
    for kind, content in entries:
        last = run(_append(mgr, sid, kind, content, parent=last))
    return last


def test_materialize_on_tool_call(tmp_db):
    s = Storage(tmp_db)
    m = SessionManager(s)
    info = run(_new_session(m))
    # user + assistant + tool_call 已落盘，无 tool_result（中断于执行前）
    _build_session(
        m,
        info.id,
        [
            ("user", "go"),
            ("assistant", "I'll use a tool"),
            ("tool_call", json.dumps({"call_id": "c1", "name": "f", "params": {}})),
        ],
    )
    sid = info.id
    eid = run(materialize_interrupted(m, sid))
    entries = s.entries(sid)
    result_kinds = [e.kind for e in entries]
    assert "tool_result" in result_kinds
    assert "interrupted" in result_kinds
    # tool_result content 是 error 标记
    tr = next(e for e in entries if e.kind == "tool_result")
    assert json.loads(tr.content)["error"] == "Task interrupted"
    # interrupted 是活跃叶
    assert s.session(sid).active_leaf == eid
    # 幂等：再物化返回 None
    assert run(materialize_interrupted(m, sid)) is None
    s.close()


def test_materialize_model_wait_only_interrupted(tmp_db):
    s = Storage(tmp_db)
    m = SessionManager(s)
    info = run(_new_session(m))
    # 模型等待中断：叶子是 tool_result（结果已落盘，等待下一次模型调用）
    _build_session(
        m,
        info.id,
        [
            ("user", "go"),
            ("assistant", "I'll use a tool"),
            ("tool_call", json.dumps({"call_id": "c1", "name": "f", "params": {}})),
            ("tool_result", json.dumps({"call_id": "c1", "content": "ok"})),
        ],
    )
    sid = info.id
    run(materialize_interrupted(m, sid))
    entries = s.entries(sid)
    # 不应再补 tool_result（c1 已配对），只落 interrupted
    kinds = [e.kind for e in entries]
    assert kinds.count("tool_result") == 1
    assert "interrupted" in kinds
    s.close()


def test_materialize_already_terminal_noop(tmp_db):
    s = Storage(tmp_db)
    m = SessionManager(s)
    info = run(_new_session(m))
    _build_session(
        m,
        info.id,
        [("user", "go"), ("assistant", terminal_content("done", StopReason.COMPLETED))],
    )
    sid = info.id
    assert run(materialize_interrupted(m, sid)) is None
    kinds = [e.kind for e in s.entries(sid)]
    assert "interrupted" not in kinds
    s.close()


def test_scan_and_recover_reports(tmp_db):
    s = Storage(tmp_db)
    m = SessionManager(s)
    info = run(_new_session(m))
    _build_session(
        m,
        info.id,
        [
            ("user", "go"),
            ("assistant", "I'll use a tool"),
            ("tool_call", json.dumps({"call_id": "c1", "name": "f", "params": {}})),
        ],
    )
    reports = scan_and_recover(s, m)
    assert len(reports) == 1
    r = reports[0]
    assert r.session_id == info.id
    assert r.is_background is False
    assert r.recovered is True
    # 重扫幂等：本次扫描后已是终态，再扫无报告
    assert scan_and_recover(s, m) == []
    s.close()


def test_scan_and_recover_background_flag(tmp_db):
    s = Storage(tmp_db)
    m = SessionManager(s)
    info = run(_new_session(m))
    _build_session(m, info.id, [("user", "go")])
    # 手动把会话标记为 background（第 7、8 步由扩展层写列；这里直接改列验证带出）
    with s.transaction() as conn:
        conn.execute("UPDATE sessions SET background = 1 WHERE id = ?", (info.id,))
    reports = scan_and_recover(s, m)
    assert reports[0].is_background is True
    s.close()


# ── 7. mark/clear 时点 ──


def test_mark_clear_propagates_to_info(tmp_db):
    s = Storage(tmp_db)
    m = SessionManager(s)
    info = run(_new_session(m))
    m.mark_running(info.id, "t1")
    assert m.running_task(info.id) == "t1"
    m.clear_running(info.id)
    assert m.running_task(info.id) is None
    s.close()


# ── 8. interrupted 投影（扫报告即可作为 sessions_with(INTERRUPTED) 的事实源） ──


def test_interrupted_projection_via_scan(tmp_db):
    s = Storage(tmp_db)
    m = SessionManager(s)
    info = run(_new_session(m))
    _build_session(m, info.id, [("user", "go")])
    reports = scan_and_recover(s, m)
    assert reports[0].interrupted_entry_id is not None
    # 活跃叶已有 interrupted 终态标记
    leaf = s.session(info.id).active_leaf
    assert leaf == reports[0].interrupted_entry_id
    # 从 entry 投影：interrupted 是终态，decide 该返回 DONE
    assert decide(s.entries(info.id)) == NextStep.DONE
    s.close()