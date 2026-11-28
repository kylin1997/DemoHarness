"""共享协议与数据结构，约定模型、Agent 循环和工具之间的交互格式。

本模块定义消息结果、工具调用、事件回调和工具上下文，不承担具体执行逻辑。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

FIXED_TOOLS = frozenset({"tool_search", "read", "todo"})


class ModelError(RuntimeError):
    """可直接向用户展示的模型调用错误，避免暴露底层请求中的私密数据。"""


@dataclass(frozen=True)
class ToolCall:
    """模型提出的工具请求，包含调用 ID、工具名称及已解析的参数对象。"""

    id: str
    name: str
    arguments: dict

    def as_message(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(frozen=True)
class ModelReply:
    """一次模型调用的完整结果，包含文本、工具请求和可选的输入输出 Token 用量。"""

    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatModel(Protocol):
    """模型适配器接口，接收消息与工具定义，通过异步回调发送文本增量并返回结果。"""

    async def chat_stream(
        self,
        messages: list[dict],
        schemas: list[dict],
        on_delta: Callable[[str], Awaitable[None]],
        *,
        reasoning: str = "default",
    ) -> ModelReply: ...


class Emit(Protocol):
    """运行事件发送接口，接收事件类型及数据，由运行层补充身份并保存和转发。"""

    async def __call__(self, kind: str, **data: object) -> None: ...


@dataclass
class ToolContext:
    """传给工具处理函数的执行上下文，包含工作区、会话及受控的宿主能力。

    plan 与 revealed 供本次执行更新计划和工具发现状态；delegate 为 None 时
    表示子 Agent 上下文，工具目录据此限制可发现的工具。
    """

    workspace: Path
    session_id: str
    emit: Emit
    remember: Callable[[str], None]
    recall: Callable[[], list[str]]
    delegate: Callable[[str], Awaitable[str]] | None
    plan: list[dict]
    revealed: set[str]


@dataclass(frozen=True)
class ToolSpec:
    """工具定义，将名称、模型可见说明、参数 Schema 和异步处理函数绑定在一起。"""

    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], Awaitable[str]]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
