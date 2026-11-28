"""The messages shared by the model, loop, and tool strategies."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

FIXED_TOOLS = frozenset({"tool_search", "read", "todo"})


class ModelError(RuntimeError):
    """A model failure suitable for displaying to the user."""


@dataclass(frozen=True)
class ToolCall:
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
    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatModel(Protocol):
    async def chat_stream(
        self,
        messages: list[dict],
        schemas: list[dict],
        on_delta: Callable[[str], Awaitable[None]],
        *,
        reasoning: str = "default",
    ) -> ModelReply: ...


class Emit(Protocol):
    async def __call__(self, kind: str, **data: object) -> None: ...


@dataclass
class ToolContext:
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
