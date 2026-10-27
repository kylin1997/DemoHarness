"""tools/todo.py 与 tools/subagent.py 的验收测试。"""

from __future__ import annotations

import asyncio

from democlaw.core.tools import ToolRegistry
from democlaw.tools.todo import TodoTool, register_todo_tool


def run(coro):
    return asyncio.run(coro)


def test_todo_tool_list_add_toggle_clear():
    tool = TodoTool()
    # add
    r = run(tool.execute("c1", {"action": "add", "text": "第一件事"}, asyncio.Event()))
    assert not r.is_error
    assert "#1" in r.content
    # 状态进 details
    assert r.details["todos"][0]["text"] == "第一件事"
    # add 第二项
    run(tool.execute("c2", {"action": "add", "text": "第二件任务"}, asyncio.Event()))
    # list
    r = run(tool.execute("c3", {"action": "list"}, asyncio.Event()))
    assert "#1" in r.content and "#2" in r.content
    # toggle #1
    r = run(tool.execute("c4", {"action": "toggle", "id": 1}, asyncio.Event()))
    assert "已完成" in r.content
    # list 应显示 [x]
    r = run(tool.execute("c5", {"action": "list"}, asyncio.Event()))
    assert "[x] #1" in r.content and "[ ] #2" in r.content
    # clear
    r = run(tool.execute("c6", {"action": "clear"}, asyncio.Event()))
    assert "清空 2 项" in r.content
    # 再 list 为空
    r = run(tool.execute("c7", {"action": "list"}, asyncio.Event()))
    assert "无待办" in r.content


def test_todo_tool_errors():
    tool = TodoTool()
    # add 缺 text
    r = run(tool.execute("c1", {"action": "add"}, asyncio.Event()))
    assert r.is_error
    # toggle 不存在的 id
    r = run(tool.execute("c2", {"action": "toggle", "id": 99}, asyncio.Event()))
    assert r.is_error


def test_todo_registered():
    registry = ToolRegistry()
    register_todo_tool(registry)
    names = [s.name for s in registry.definitions()]
    assert "todo" in names


def test_subagent_tool_registers_and_calls_back():
    from democlaw.tools.subagent import register_delegate_subagent

    calls = []

    async def fake_run_subagent(prompt, tools, budget):
        calls.append((prompt, tools, budget))
        return "子任务完成了"

    registry = ToolRegistry()
    register_delegate_subagent(registry, fake_run_subagent)
    assert "delegate_subagent" in [s.name for s in registry.definitions()]

    tool = registry.get("delegate_subagent")
    r = run(tool.execute("c1", {"prompt": "查一下 X", "tools": ["read_file"], "budget": 5}, asyncio.Event()))
    assert not r.is_error
    assert r.content == "子任务完成了"
    assert calls == [("查一下 X", ["read_file"], 5)]


def test_subagent_tool_error():
    from democlaw.tools.subagent import register_delegate_subagent

    async def failing(prompt, tools, budget):
        raise RuntimeError("子任务崩了")

    registry = ToolRegistry()
    register_delegate_subagent(registry, failing)
    tool = registry.get("delegate_subagent")
    r = run(tool.execute("c1", {"prompt": "任务"}, asyncio.Event()))
    assert r.is_error
    assert "子任务崩了" in r.content