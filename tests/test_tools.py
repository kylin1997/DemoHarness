"""core/tools.py 的验收测试（协议层，不含具体工具——tool 扩展在 step 3 后续）。

验证早期 BotRuntime 工具调用协议和参数校验。
"""

from __future__ import annotations

import asyncio

import pytest

from demoharness.core.tools import (
    BeforeVerdict,
    ExecMode,
    ReplayPolicy,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    execute_tool_calls,
    validate_params,
)


def run(coro):
    return asyncio.run(coro)


class _FakeTool:
    def __init__(self, name, replay=ReplayPolicy.NEVER, execution=ExecMode.PARALLEL,
                 params=None, result=None, delay=0.0):
        self.spec = ToolSpec(
            name=name,
            description=f"desc {name}",
            parameters=params or {"type": "object", "properties": {}, "required": []},
            replay=replay,
            execution=execution,
        )
        self._result = result or ToolResult(content=f"ok {name}")
        self._delay = delay
        self.calls = []

    async def execute(self, call_id, params, cancel):
        self.calls.append((call_id, params))
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result


# ── 1. 注册表：重名抛错；definitions 稳定排序 ──


def test_registry_duplicate_and_sort():
    r = ToolRegistry()
    r.register(_FakeTool("b"))
    r.register(_FakeTool("a"))
    with pytest.raises(ValueError):
        r.register(_FakeTool("b"))
    names = [s.name for s in r.definitions()]
    assert names == ["a", "b"]  # 稳定排序


# ── 2. 参数校验 ──


def _spec(props, required=None):
    return ToolSpec(
        name="t",
        description="d",
        parameters={"type": "object", "properties": props, "required": required or []},
    )


def test_validate_scalar():
    spec = _spec(
        {
            "x": {"type": "integer", "minimum": 0, "maximum": 10},
            "s": {"type": "string"},
            "mode": {"type": "string", "enum": ["a", "b"]},
        },
        required=["x"],
    )
    ok, _ = validate_params(spec, {"x": 5, "s": "hi", "mode": "a"})
    assert ok
    # 缺必需
    ok, reason = validate_params(spec, {})
    assert not ok and "x" in reason
    # 类型错
    ok, reason = validate_params(spec, {"x": "5"})
    assert not ok and "integer" in reason
    # 多余字段
    ok, reason = validate_params(spec, {"x": 1, "yyy": 2})
    assert not ok and "yyy" in reason
    # enum 越界
    ok, reason = validate_params(spec, {"x": 1, "mode": "z"})
    assert not ok
    # 超出范围
    ok, reason = validate_params(spec, {"x": 11})
    assert not ok


def test_validate_complex_rejected():
    spec = _spec({"arr": {"type": "array"}})
    ok, reason = validate_params(spec, {"arr": []})
    assert not ok and ("不支持" in reason or "复合" in reason)


def test_validate_bool_not_integer():
    spec = _spec({"flag": {"type": "integer"}})
    ok, _ = validate_params(spec, {"flag": True})
    assert not ok


# ── 3. 三段式：before 钩子 block、after 钩子替换 ──


def test_before_hook_block():
    tool = _FakeTool("f")
    r = ToolRegistry()
    r.register(tool)

    def before(name, params):
        if name == "f":
            return BeforeVerdict(block=True, reason="被拦截")

    results = run(execute_tool_calls([ToolCall("c1", "f", {})], r, before_hooks=[before]))
    assert results[0].is_error
    assert "被拦截" in results[0].content
    assert tool.calls == []  # 未执行


def test_after_hook_replace():
    tool = _FakeTool("f")
    r = ToolRegistry()
    r.register(tool)

    def after(result):
        result.content = "被 after 替换"
        return result

    results = run(execute_tool_calls([ToolCall("c1", "f", {})], r, after_hooks=[after]))
    assert results[0].content == "被 after 替换"


# ── 4. 并发规则 ──


def test_all_parallel_uses_gather():
    t1 = _FakeTool("a", execution=ExecMode.PARALLEL, delay=0.02)
    t2 = _FakeTool("b", execution=ExecMode.PARALLEL, delay=0.02)
    r = ToolRegistry()
    r.register(t1)
    r.register(t2)
    import time

    start = time.monotonic()
    results = run(
        execute_tool_calls([ToolCall("c1", "a", {}), ToolCall("c2", "b", {})], r)
    )
    elapsed = time.monotonic() - start
    assert len(results) == 2
    # 并发：总时长 < 两次串行延迟之和
    assert elapsed < 0.04


def test_sequential_batch_runs_serial():
    t1 = _FakeTool("a", execution=ExecMode.SEQUENTIAL, delay=0.02)
    t2 = _FakeTool("b", execution=ExecMode.PARALLEL, delay=0.02)
    r = ToolRegistry()
    r.register(t1)
    r.register(t2)
    import time

    start = time.monotonic()
    results = run(execute_tool_calls([ToolCall("c1", "a", {}), ToolCall("c2", "b", {})], r))
    elapsed = time.monotonic() - start
    # 含 SEQUENTIAL → 整批串行：总时长 >= 两次延迟之和
    assert elapsed >= 0.04
    # 结果按原序
    assert [x.content for x in results] == ["ok a", "ok b"]


# ── 5. execute 抛异常 → is_error 不向上抛 ──


def test_tool_raises_is_error():
    class BoomTool:
        spec = ToolSpec("boom", "d", {"type": "object", "properties": {}, "required": []})

        async def execute(self, call_id, params, cancel):
            raise RuntimeError("formers")

    r = ToolRegistry()
    r.register(BoomTool())
    results = run(execute_tool_calls([ToolCall("c1", "boom", {})], r))
    assert results[0].is_error
    assert "formers" in results[0].content


# ── 6. cancel 执行前 → 不执行 ──


def test_cancel_before_execute():
    tool = _FakeTool("f")
    r = ToolRegistry()
    r.register(tool)
    cancel = asyncio.Event()
    cancel.set()
    results = run(execute_tool_calls([ToolCall("c1", "f", {})], r, cancel=cancel))
    assert results[0].is_error
    assert tool.calls == []


# ── 9. duration 记录进 details ──


def test_duration_recorded():
    tool = _FakeTool("f", delay=0.01)
    r = ToolRegistry()
    r.register(tool)
    results = run(execute_tool_calls([ToolCall("c1", "f", {})], r))
    assert "duration" in results[0].details
    assert results[0].details["duration"] >= 0


# ── 未注册工具 → is_error ──


def test_unregistered_tool():
    r = ToolRegistry()
    results = run(execute_tool_calls([ToolCall("c1", "missing", {})], r))
    assert results[0].is_error
    assert "不存在" in results[0].content