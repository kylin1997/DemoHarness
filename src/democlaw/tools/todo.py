"""todo 工具（tools/todo.py）——对齐 pi 的 todo 扩展（references/pi/.../todo.ts）。

让模型管理待办清单（list/add/toggle/clear）。关键设计照 pi：
- todo 状态写进 tool result 的 details（落盘进 tool_result entry），不是外部
  文件——这样 fork 分支时 todo 状态自动随历史正确，可扫描 entry 重建。

本实现：持有进程内状态，每次调用把全量 todo 状态写入 ToolResult.details
（loop 落盘进 tool_result entry）。启动重建（扫描 details）作为后续增强。
"""

from __future__ import annotations

import asyncio

from democlaw.core.tools import ExecMode, ReplayPolicy, ToolResult, ToolSpec


class TodoTool:
    spec = ToolSpec(
        name="todo",
        description="管理待办清单。action: list/add/toggle/clear（add 带 text，toggle 带 id）",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "toggle", "clear"]},
                "text": {"type": "string"},
                "id": {"type": "integer"},
            },
            "required": ["action"],
        },
        replay=ReplayPolicy.SAFE,   # 清单操作无副作用，可安全重放
        execution=ExecMode.SEQUENTIAL,  # 清单是有序状态，串行避免竞态
    )

    def __init__(self):
        self._todos: list[dict] = []
        self._next_id = 1

    async def execute(self, call_id: str, params: dict, cancel: asyncio.Event) -> ToolResult:
        action = params["action"]
        if action == "list":
            text = self._render()
            return ToolResult(content=text, details={"action": "list", "todos": self._snapshot(), "next_id": self._next_id})
        if action == "add":
            text = params.get("text")
            if not text:
                return ToolResult(content="错误：add 需要 text", is_error=True)
            todo = {"id": self._next_id, "text": text, "done": False}
            self._next_id += 1
            self._todos.append(todo)
            return ToolResult(content=f"已添加 #{todo['id']}: {text}", details={"action": "add", "todos": self._snapshot(), "next_id": self._next_id})
        if action == "toggle":
            tid = params.get("id")
            todo = next((t for t in self._todos if t["id"] == tid), None)
            if todo is None:
                return ToolResult(content=f"#{tid} 不存在", is_error=True)
            todo["done"] = not todo["done"]
            state = "已完成" if todo["done"] else "未完成"
            return ToolResult(content=f"#{tid} {state}", details={"action": "toggle", "todos": self._snapshot(), "next_id": self._next_id})
        if action == "clear":
            count = len(self._todos)
            self._todos = []
            self._next_id = 1
            return ToolResult(content=f"已清空 {count} 项", details={"action": "clear", "todos": [], "next_id": 1})
        return ToolResult(content=f"未知 action: {action}", is_error=True)

    def _snapshot(self) -> list[dict]:
        return [dict(t) for t in self._todos]

    def _render(self) -> str:
        if not self._todos:
            return "无待办"
        return "\n".join(
            f"[{'x' if t['done'] else ' '}] #{t['id']}: {t['text']}" for t in self._todos
        )


def register_todo_tool(registry) -> TodoTool:
    tool = TodoTool()
    registry.register(tool)
    return tool