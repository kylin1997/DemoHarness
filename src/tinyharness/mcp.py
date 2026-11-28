"""A small stdio MCP client for locally configured servers."""

from __future__ import annotations

import asyncio
import json
import os
import signal


class McpError(RuntimeError):
    """An MCP request could not be completed."""


_MAX_FRAME_BYTES = 256 * 1024
_MAX_LIST_PAGES = 16
_PROTOCOL_VERSION = "2025-03-26"
_SUPPORTED_VERSIONS = {"2024-11-05", _PROTOCOL_VERSION, "2025-06-18", "2025-11-25"}


async def stop_process(process: asyncio.subprocess.Process) -> None:
    """Stop the subprocess and close its pipes, including on timeout."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    # communicate drains bounded pipes so the subprocess transport can finish.
    try:
        async with asyncio.timeout(1):
            await process.communicate()
    except (OSError, RuntimeError, TimeoutError):
        pass


class StdioMcpClient:
    """Exchange bounded, newline-delimited JSON-RPC messages over stdio."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        *,
        timeout: float = 10,
    ):
        self.name = name
        self._argv = [command, *(args or [])]
        self._env = {"PATH": os.defpath, "LANG": "C.UTF-8", **(env or {})}
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._process is not None:
            raise McpError(f"MCP server {self.name} has already started")
        try:
            async with asyncio.timeout(self._timeout):
                self._process = await asyncio.create_subprocess_exec(
                    *self._argv,
                    env=self._env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=os.name == "posix",
                    limit=_MAX_FRAME_BYTES,
                )
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "TinyHarness", "version": "2"},
                },
            )
            version = result.get("protocolVersion")
            if not isinstance(version, str) or version not in _SUPPORTED_VERSIONS:
                raise McpError(
                    f"MCP server {self.name} negotiated an unsupported protocol"
                )
            async with asyncio.timeout(self._timeout):
                await self._send({"method": "notifications/initialized"})
        except (OSError, TimeoutError) as error:
            await self.close()
            raise McpError(f"Could not start MCP server {self.name}") from error
        except BaseException:
            await self.close()
            raise

    async def list_tools(self) -> list[dict]:
        tools = []
        cursor = None
        seen_cursors = set()
        try:
            async with asyncio.timeout(self._timeout):
                for _ in range(_MAX_LIST_PAGES):
                    result = await self._request(
                        "tools/list", {"cursor": cursor} if cursor else {}
                    )
                    batch = result.get("tools")
                    if not isinstance(batch, list) or any(
                        not isinstance(tool, dict) for tool in batch
                    ):
                        raise McpError(
                            f"MCP server {self.name} returned an invalid tool list"
                        )
                    tools.extend(batch)
                    cursor = result.get("nextCursor")
                    if cursor is None:
                        return tools
                    if (
                        not isinstance(cursor, str)
                        or not cursor
                        or cursor in seen_cursors
                    ):
                        raise McpError(
                            f"MCP server {self.name} returned an invalid tool cursor"
                        )
                    seen_cursors.add(cursor)
        except TimeoutError as error:
            raise McpError(f"MCP tool discovery timed out on {self.name}") from error
        raise McpError(f"MCP server {self.name} exceeded the tool page limit")

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        blocks = result.get("content", [])
        if not isinstance(blocks, list) or any(
            not isinstance(block, dict) for block in blocks
        ):
            raise McpError(f"MCP tool {name} returned invalid content")
        text = "\n".join(
            block["text"]
            for block in blocks
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        if result.get("isError"):
            raise McpError(text or f"MCP tool {name} reported an error")
        if text:
            return text
        if "structuredContent" in result:
            return json.dumps(result["structuredContent"], ensure_ascii=False)
        return "MCP tool completed without text content."

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            stdin = process.stdin
            if stdin is not None:
                stdin.close()
            await stop_process(process)

    async def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpError(f"MCP server {self.name} is disconnected")
        frame = json.dumps(
            {"jsonrpc": "2.0", **message}, ensure_ascii=True, allow_nan=False
        ).encode()
        if len(frame) > _MAX_FRAME_BYTES:
            raise McpError("MCP request exceeds the message size limit")
        process.stdin.write(frame + b"\n")
        await process.stdin.drain()

    async def _request(self, method: str, params: dict) -> dict:
        try:
            async with asyncio.timeout(self._timeout):
                # Taking the lock under the deadline bounds the wait behind other sessions.
                async with self._lock:
                    try:
                        return await self._exchange(method, params)
                    except (OSError, ValueError) as error:
                        await self.close()
                        raise McpError(
                            f"MCP request {method} failed on {self.name}"
                        ) from error
        except TimeoutError as error:
            raise McpError(f"MCP request {method} timed out on {self.name}") from error

    async def _exchange(self, method: str, params: dict) -> dict:
        process = self._process
        if process is None or process.stdout is None:
            raise McpError(f"MCP server {self.name} is disconnected")
        self._next_id += 1
        request_id = self._next_id
        await self._send({"id": request_id, "method": method, "params": params})
        while line := await process.stdout.readline():
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("Invalid MCP frame")
            if "method" in message:
                # Servers may send notifications or ping while a request is pending.
                if "id" in message:
                    reply = {"id": message["id"]}
                    if message["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": "Method not found"}
                    await self._send(reply)
                continue
            if type(message.get("id")) is not int or message["id"] != request_id:
                continue
            if "error" in message:
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else None
                raise McpError(
                    detail
                    if isinstance(detail, str)
                    else f"MCP request {method} failed"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpError(f"MCP request {method} returned an invalid result")
            return result
        raise McpError(f"MCP server {self.name} closed its output")
