"""Deterministic teaching checks and session-scoped command approval."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Awaitable, Callable

from .state import Store

INPUT_LIMIT = 8_000
INPUT_MARKER = "TINYHARNESS_BLOCK_INPUT"
TOOL_MARKER = "TINYHARNESS_BLOCK_TOOL"
OUTPUT_MARKER = "TINYHARNESS_BLOCK_OUTPUT"

_READ_ONLY_COMMANDS = frozenset({("pwd",), ("ls",), ("ls", "-la")})
_PROGRAM_NAME = re.compile(r"[a-zA-Z0-9_+][a-zA-Z0-9._+-]{0,127}")


def validate_command_argv(argv: object) -> str | None:
    if not isinstance(argv, list) or not 1 <= len(argv) <= 32:
        return "命令需要 1 至 32 个 argv 参数。"
    if any(
        not isinstance(arg, str) or len(arg) > 2048 or (arg and not arg.isprintable())
        for arg in argv
    ):
        return "命令参数必须为可打印的字符串，每项最多 2048 个字符。"
    if sum(map(len, argv)) > 8192:
        return "命令参数合计最多 8192 个字符。"
    if not _PROGRAM_NAME.fullmatch(argv[0]):
        return "命令需要直接指定可执行程序名称，且名称不能包含路径。"
    return None


class Guardrails:
    # A streaming caller can withhold this many trailing characters per delta.
    output_holdback = len(OUTPUT_MARKER) - 1

    def __init__(self, store: Store, *, approval_timeout: float = 60.0):
        self._store = store
        self._approval_timeout = approval_timeout
        self._pending: dict[tuple[str, str], asyncio.Future[str]] = {}

    def check_input(self, message: str) -> str | None:
        if not message.strip():
            return "请输入消息。"
        if len(message) > INPUT_LIMIT:
            return f"输入最多 {INPUT_LIMIT} 个字符。"
        if INPUT_MARKER.casefold() in message.casefold():
            return "输入命中演示拦截规则。"
        return None

    def check_tool(self, name: str, args: dict) -> str | None:
        if TOOL_MARKER.casefold() in json.dumps(args, ensure_ascii=False).casefold():
            return "工具参数命中演示拦截规则。"
        if name == "run_command":
            return validate_command_argv(args.get("argv"))
        return None

    def check_output(self, text: str) -> str | None:
        if OUTPUT_MARKER.casefold() in text.casefold():
            return "输出命中演示拦截规则。"
        return None

    async def invoke_with_approval(
        self,
        session_id: str,
        name: str,
        args: dict,
        emit: Callable[..., Awaitable[None]],
    ) -> str | None:
        blocked = self.check_tool(name, args)
        if blocked or name != "run_command":
            return blocked
        argv = args["argv"]
        if tuple(argv) in _READ_ONLY_COMMANDS:
            return None
        signature = json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
        if self._store.is_trusted(session_id, signature):
            return None

        approval_id = uuid.uuid4().hex
        pending_key = (session_id, approval_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[pending_key] = future
        try:
            await emit("approval", approval_id=approval_id, name=name, args=args)
            try:
                decision = await asyncio.wait_for(
                    future, timeout=self._approval_timeout
                )
            except TimeoutError:
                decision = "timeout"
        finally:
            self._pending.pop(pending_key, None)
            if not future.done():
                future.cancel()

        if decision == "trust":
            self._store.trust(session_id, signature)
        reason = {"deny": "用户拒绝执行命令。", "timeout": "等待命令批准超时。"}.get(
            decision
        )
        payload = {"approval_id": approval_id, "decision": decision}
        if reason:
            payload["reason"] = reason
        await emit("approval_resolved", **payload)
        return reason

    def resolve(self, session_id: str, approval_id: str, decision: str) -> bool:
        if decision not in {"allow", "trust", "deny"}:
            return False
        future = self._pending.get((session_id, approval_id))
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True
