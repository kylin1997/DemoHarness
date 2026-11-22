"""首批教学工具（tools/ 扩展目录，第 3 步）。

三个工具覆盖 replay（safe vs never）与 execution（parallel vs sequential）的真实用途：
- read_file  —— 读文件（SAFE / PARALLEL）：读路径，无副作用，可安全重放。
- write_file —— 写文件（NEVER / SEQUENTIAL）：有副作用，中断后绝不重放。
- run_command —— 执行命令（NEVER / SEQUENTIAL）：副作用最强，timeout 兜底不 kill。

这组工具沿用早期 BotRuntime 的工具协议。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from demoharness.core.tools import (
    ExecMode,
    ReplayPolicy,
    Tool,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


def register_builtin_tools(registry: ToolRegistry) -> list[Tool]:
    tools: list[Tool] = [_ReadFileTool(), _WriteFileTool(), _RunCommandTool()]
    for t in tools:
        registry.register(t)
    return tools


class _ReadFileTool:
    spec = ToolSpec(
        name="read_file",
        description="读取文本文件（可按行偏移/上限截取）。只读，无副作用。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 2000},
            },
            "required": ["path"],
        },
        replay=ReplayPolicy.SAFE,
        execution=ExecMode.PARALLEL,
    )

    async def execute(self, call_id: str, params: dict, cancel: asyncio.Event) -> ToolResult:
        p = Path(params["path"])
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return ToolResult(content=f"读取失败：{e}", is_error=True)
        lines = text.splitlines()
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 2000))
        return ToolResult(content="\n".join(lines[offset:offset + limit]))


class _WriteFileTool:
    spec = ToolSpec(
        name="write_file",
        description="写文本文件（覆盖）。有副作用，中断后不重放。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        replay=ReplayPolicy.NEVER,
        execution=ExecMode.SEQUENTIAL,
    )

    async def execute(self, call_id: str, params: dict, cancel: asyncio.Event) -> ToolResult:
        p = Path(params["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        content = params["content"]
        # 原子写（temp + rename）：避免中断留下半写文件。
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
        return ToolResult(content=f"已写入 {params['path']}（{len(content)} 字符）")


class _RunCommandTool:
    spec = ToolSpec(
        name="run_command",
        description="执行 shell 命令。有副作用，timeout 兜底。",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number", "default": 30.0},
            },
            "required": ["command"],
        },
        replay=ReplayPolicy.NEVER,
        execution=ExecMode.SEQUENTIAL,
    )

    async def execute(self, call_id: str, params: dict, cancel: asyncio.Event) -> ToolResult:
        timeout = float(params.get("timeout", 30.0))
        # shell=True 是教学演示的已知信任面；run_command 是文档声明的「最大信任面」工具。
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                params["command"], shell=True, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(content=f"命令超时（>{timeout}s）", is_error=True)
        return ToolResult(content=(proc.stdout or "") + (proc.stderr or ""))