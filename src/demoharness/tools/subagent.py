"""sub-agent 工具（strategies/subagent.py 或 tools/subagent.py）——对齐 multi-agent.md 的 delegate_subagent。

一机制两来源（见 docs/design/multi-agent.md）：用「新会话 + Task」执行，身份来源
是工具参数内联（聚焦 prompt + 受限工具集 + 预算切片）。`final_response` 作为工具结果
返回父会话；子会话不进父上下文（结果仅作为工具结果回传）。

本工具是策略扩展提供的工具，默认 ReAct 不含分派。实现依赖 runtime 的「run 子任务」
回调——BotRuntime 装配期注入，把 (prompt, tools, budget) 跑成一个独立 session 的 Task。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from demoharness.core.tools import ExecMode, ReplayPolicy, Tool, ToolResult, ToolSpec


@dataclass(frozen=True)
class SubagentInput:
    prompt: str
    tools: list[str] | None = None
    budget: int | None = None
    wait: bool = True


class DelegateSubagentTool:
    spec = ToolSpec(
        name="delegate_subagent",
        description="把聚焦子任务交给一个临时 subagent 执行（内联 prompt + 受限工具集 + 预算切片），返回其最终回答。",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "string"}},
                "budget": {"type": "integer"},
                "wait": {"type": "boolean"},
            },
            "required": ["prompt"],
        },
        replay=ReplayPolicy.NEVER,   # 分派有副作用（派生新会话），不可重放
        execution=ExecMode.SEQUENTIAL,
    )

    def __init__(self, run_subagent):
        """run_subagent: async (prompt, tools, budget) -> str（子会话 final_response 文本）。"""
        self._run_subagent = run_subagent

    async def execute(self, call_id: str, params: dict, cancel: asyncio.Event) -> ToolResult:
        prompt = params["prompt"]
        tools = params.get("tools")
        budget = params.get("budget")
        try:
            final_text = await self._run_subagent(prompt, tools, budget)
        except Exception as exc:  # noqa: BLE001 —— 子任务失败，编码为工具错误
            return ToolResult(content=f"subagent 执行失败：{exc}", is_error=True)
        return ToolResult(content=final_text, details={"prompt": prompt, "tools": tools, "budget": budget})


def register_delegate_subagent(registry, run_subagent) -> Tool:
    tool = DelegateSubagentTool(run_subagent)
    registry.register(tool)
    return tool