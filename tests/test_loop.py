"""core/loop.py 的验收测试（覆盖 docs/design/loop-providers.md 1-8 项）。

用 mock stream_fn 驱动完整 ReAct 循环——第 4 步「最小闭环」里程碑。
mock 模型确定性，不依赖网络。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from democlaw.core.events import EventBus
from democlaw.core.loop import (
    LoopRuntime,
    ModelResponse,
    ModelStop,
    project_context,
    run_task_loop,
)
from democlaw.core.session import AppendOp, NewOp, SessionManager
from democlaw.core.storage import EntryRow, Storage
from democlaw.core.task import StopReason
from democlaw.core.tools import (
    ExecMode,
    ReplayPolicy,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


def run(coro):
    return asyncio.run(coro)


class _WriteTool:
    spec = ToolSpec("write", "写文件", {"type": "object", "properties": {}, "required": []},
                    replay=ReplayPolicy.NEVER, execution=ExecMode.SEQUENTIAL)

    async def execute(self, call_id, params, cancel):
        return ToolResult(content="已写入")


class _ReadTool:
    spec = ToolSpec("read", "读文件", {"type": "object", "properties": {}, "required": []},
                    replay=ReplayPolicy.SAFE, execution=ExecMode.PARALLEL)

    async def execute(self, call_id, params, cancel):
        return ToolResult(content="内容")


def _mk_runtime(tmp_db, responses, tools=None, max_iterations=10):
    """构造一个 LoopRuntime，stream_fn 按顺序吐 responses。"""
    storage = Storage(tmp_db)
    session_mgr = SessionManager(storage)
    bus = EventBus()
    registry = ToolRegistry()
    for t in tools or []:
        registry.register(t)

    responses_iter = iter(responses)

    async def stream_fn(request, cancel):
        try:
            return next(responses_iter)
        except StopIteration:
            return ModelResponse(ModelStop.OK, "默认完成", [])

    runtime = LoopRuntime(
        session_mgr=session_mgr,
        stream_fn=stream_fn,
        registry=registry,
        event_bus=bus,
        system_prompt="sys",
        model="mock",
        max_iterations=max_iterations,
        context_window=100,
        finish_turn=lambda ctx: False,
    )
    return storage, session_mgr, bus, runtime


async def _new(session_mgr):
    return await session_mgr.submit("", NewOp("", bot_id="b1"))


async def _append_input(session_mgr, sid, text, task_id):
    await session_mgr.submit(sid, AppendOp(sid, "user", text, parent=None, task_id=task_id))
    session_mgr.mark_running(sid, task_id)


# ── 1. mock 跑通：输入 → 2 轮工具 → 终答 completed ──


def test_full_loop_completed(tmp_path):
    tmp_db = str(tmp_path / "a.db")
    # 第一轮：返回工具调用(read)；第二轮：纯文本终答
    tool_call = {"call_id": "c1", "name": "read", "params": {}}
    responses = [
        ModelResponse(ModelStop.OK, "", [tool_call]),
        ModelResponse(ModelStop.OK, "答案是 42", []),
    ]
    storage, mgr, _bus, runtime = _mk_runtime(tmp_db, responses, tools=[_ReadTool()])

    async def go():
        info = await _new(mgr)
        await _append_input(mgr, info.id, "任务", "task1")
        return await run_task_loop(info.id, "task1", "任务", runtime)

    result = run(go())
    assert result.stop_reason == StopReason.COMPLETED
    assert result.content == "答案是 42"
    assert result.iterations == 1
    assert result.tools_used == 1
    # 树上有 user / assistant(tool_calls) / tool_call / tool_result / assistant(终态)
    kinds = [e.kind for e in storage.entries(result.session_id)]
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert kinds[-1] == "assistant"
    # running 已清
    assert mgr.running_task(result.session_id) is None
    storage.close()


# ── 2. 投影规格：单批多工具乱序落盘 → 投影按 assistant.tool_calls 顺序 ──


def test_project_context_reorder():
    # 构造重放：一条 assistant 带两个 tool_calls（顺序 c1,c2），但 tool_result 落盘乱序（c2 先）
    assistant = json.dumps({"text": "", "tool_calls": [
        {"call_id": "c1", "name": "a", "params": {}},
        {"call_id": "c2", "name": "b", "params": {}},
    ]})
    entries = [
        EntryRow("e1", "s", None, "user", "go", 0.0),
        EntryRow("e2", "s", "e1", "assistant", assistant, 1.0),
        EntryRow("e4", "s", "e2", "tool_result", json.dumps({"call_id": "c2", "content": "r2"}), 3.0),
        EntryRow("e3", "s", "e2", "tool_result", json.dumps({"call_id": "c1", "content": "r1"}), 2.0),
    ]
    msgs = project_context(entries)
    # tool_call_id 配对，不靠相邻顺序
    tool_ids = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
    assert tool_ids == ["c1", "c2"]


# ── 3. call_id 透传：模型 id 落盘一致 ──


def test_call_id_passthrough(tmp_path):
    tool_call = {"call_id": "model_given_id_99", "name": "read", "params": {}}
    responses = [ModelResponse(ModelStop.OK, "", [tool_call]), ModelResponse(ModelStop.OK, "ok", [])]
    storage, mgr, _bus, runtime = _mk_runtime(str(tmp_path / "b.db"), responses, tools=[_ReadTool()])

    async def go():
        info = await _new(mgr)
        await _append_input(mgr, info.id, "t", "task1")
        return await run_task_loop(info.id, "task1", "t", runtime)

    run(go())
    tc = next(e for e in storage.entries("s_1") if e.kind == "tool_call")
    assert json.loads(tc.content)["call_id"] == "model_given_id_99"
    storage.close()


# ── 4. stop_reason 映射：LENGTH/ERROR/ABORTED/空 ──


@pytest.mark.parametrize(
    "resp,expected",
    [
        (ModelResponse(ModelStop.LENGTH, "", []), StopReason.ERROR),
        (ModelResponse(ModelStop.ERROR, "", [], error="boom"), StopReason.ERROR),
        (ModelResponse(ModelStop.ABORTED, "", []), StopReason.CANCELLED),
        (ModelResponse(ModelStop.OK, "", []), StopReason.EMPTY_RESPONSE),
    ],
)
def test_stop_reason_mapping(tmp_path, resp, expected):
    storage, mgr, _bus, runtime = _mk_runtime(str(tmp_path / "c.db"), [resp])

    async def go():
        info = await _new(mgr)
        await _append_input(mgr, info.id, "t", "task1")
        return await run_task_loop(info.id, "task1", "t", runtime)

    result = run(go())
    assert result.stop_reason == expected
    storage.close()


# ── 5. 预算：超限 → max_iterations（收尾文本空） ──


def test_budget_max_iterations(tmp_path):
    tool_call = {"call_id": "c1", "name": "read", "params": {}}
    # 每次都返回工具调用，直到预算耗尽；收尾请求(tools=[])返回空文本
    responses = [ModelResponse(ModelStop.OK, "", [tool_call]) for _ in range(3)]
    responses.append(ModelResponse(ModelStop.OK, "", []))
    storage, mgr, _bus, runtime = _mk_runtime(str(tmp_path / "d.db"), responses, tools=[_ReadTool()], max_iterations=3)

    async def go():
        info = await _new(mgr)
        await _append_input(mgr, info.id, "t", "task1")
        return await run_task_loop(info.id, "task1", "t", runtime)

    result = run(go())
    assert result.stop_reason == StopReason.MAX_ITERATIONS
    assert result.iterations == 3
    storage.close()


# ── 8. 异常路径：loop 中途抛 → interrupted 物化 → running 清空 ──


def test_exception_materializes_interrupted(tmp_path):
    storage = Storage(str(tmp_path / "e.db"))
    mgr = SessionManager(storage)
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(_ReadTool())

    async def throwing_stream(request, cancel):
        raise RuntimeError("loop 层异常来测试物化")

    rt = LoopRuntime(
        session_mgr=mgr, stream_fn=throwing_stream, registry=registry, event_bus=bus,
        system_prompt="s", model="m", max_iterations=10, context_window=100,
        finish_turn=lambda c: False,
    )

    async def go():
        info = await _new(mgr)
        await _append_input(mgr, info.id, "t", "task1")
        with pytest.raises(RuntimeError):
            await run_task_loop(info.id, "task1", "t", rt)
        kinds = [e.kind for e in storage.entries(info.id)]
        assert "interrupted" in kinds
        assert mgr.running_task(info.id) is None

    run(go())
    storage.close()


# ── 9. 事件序列：task_started 在 task_finished 之前 ──


def test_event_sequence(tmp_path):
    tool_call = {"call_id": "c1", "name": "read", "params": {}}
    responses = [ModelResponse(ModelStop.OK, "", [tool_call]), ModelResponse(ModelStop.OK, "ok", [])]

    async def go():
        storage = Storage(str(tmp_path / "f.db"))
        mgr = SessionManager(storage)
        bus = EventBus()
        registry = ToolRegistry()
        registry.register(_ReadTool())
        responses_iter = iter(responses)

        async def stream_fn(request, cancel):
            try:
                return next(responses_iter)
            except StopIteration:
                return ModelResponse(ModelStop.OK, "done", [])

        rt = LoopRuntime(
            session_mgr=mgr, stream_fn=stream_fn, registry=registry, event_bus=bus,
            system_prompt="s", model="m", max_iterations=10, context_window=100,
            finish_turn=lambda c: False,
        )
        sub = bus.subscribe(session_id="s_1")
        info = await _new(mgr)
        await _append_input(mgr, info.id, "t", "task1")
        await run_task_loop(info.id, "task1", "t", rt)
        # 排空队列收集事件
        events = []
        while True:
            try:
                events.append(sub._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        types = [e.type for e in events]
        assert types.index("task_started") < types.index("task_finished")
        storage.close()
        return events

    events = run(go())
    types = [e.type for e in events]
    assert "tool_call_started" in types and "tool_result" in types