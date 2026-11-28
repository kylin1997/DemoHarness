import asyncio
import json
import re
import sys

import pytest

from demoharness import toolbox
from demoharness.mcp import McpError, StdioMcpClient
from demoharness.protocol import FIXED_TOOLS, ToolContext
from demoharness.toolbox import ToolError, ToolRegistry


def context(workspace, *, child=False):
    events, memory = [], []

    async def emit(kind, **payload):
        events.append((kind, payload))

    async def delegate(task):
        return task

    return (
        ToolContext(
            workspace,
            "session",
            emit,
            memory.append,
            lambda: memory[:],
            None if child else delegate,
            [],
            set(FIXED_TOOLS),
        ),
        events,
    )


def test_discovery_is_scoped_to_context_and_emits_names(tmp_path):
    async def run():
        registry = ToolRegistry()
        parent, events = context(tmp_path)
        child, child_events = context(tmp_path, child=True)
        assert "write" not in parent.revealed
        result = json.loads(
            await registry.invoke("tool_search", parent, {"query": "write"})
        )
        assert result[0]["function"]["name"] == "write"
        assert result[0]["function"]["parameters"]["required"] == ["path", "content"]
        assert events == [("tools_loaded", {"names": ["write"]})]
        assert "write" in parent.revealed and "write" not in child.revealed
        assert await registry.invoke("tool_search", child, {"query": "write"}) == "[]"
        await registry.invoke("tool_search", child, {"query": "glob"})
        assert child_events == [("tools_loaded", {"names": ["glob"]})]

    asyncio.run(run())


def test_file_tools_scope_symlinks_and_type_validation(tmp_path):
    async def run():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        private = outside / "secret.txt"
        private.write_text("outside")
        (workspace / "dir_link").symlink_to(outside, target_is_directory=True)
        (workspace / "file_link").symlink_to(private)
        (workspace / "hard_link").hardlink_to(private)
        registry = ToolRegistry()
        session, _ = context(workspace)

        with pytest.raises(ToolError, match="Invalid write arguments"):
            await registry.invoke("write", session, {"path": "bad.txt", "content": 42})
        assert not (workspace / "bad.txt").exists()
        await registry.invoke(
            "write", session, {"path": "nested/notes.md", "content": "hello\nworld\n"}
        )
        await registry.invoke(
            "write", session, {"path": "notes.md", "content": "hello\n"}
        )
        await registry.invoke(
            "edit", session, {"path": "nested/notes.md", "old": "world", "new": "Earth"}
        )
        assert (
            await registry.invoke("read", session, {"path": "nested/notes.md"})
            == "hello\nEarth\n"
        )
        assert (
            await registry.invoke("glob", session, {"pattern": "**/*.md"})
            == "nested/notes.md\nnotes.md"
        )
        assert (
            await registry.invoke("grep", session, {"query": "Earth"})
            == "nested/notes.md:2: Earth"
        )

        for name in (
            "../outside/secret.txt",
            str(private),
            "dir_link/secret.txt",
            "file_link",
            "hard_link",
        ):
            with pytest.raises(ToolError):
                await registry.invoke("read", session, {"path": name})
            with pytest.raises(ToolError):
                await registry.invoke(
                    "write", session, {"path": name, "content": "changed"}
                )
        assert private.read_text() == "outside"
        assert (
            await registry.invoke("glob", session, {"pattern": "**/secret.txt"})
            == "No matching files."
        )

    asyncio.run(run())


def test_command_output_failure_and_deadline(tmp_path, monkeypatch):
    async def run():
        registry = ToolRegistry()
        session, _ = context(tmp_path)
        assert (
            await registry.invoke("run_command", session, {"argv": ["printf", "hello"]})
            == "hello"
        )
        assert (
            await registry.invoke("run_command", session, {"argv": ["pwd"]})
        ).strip() == str(tmp_path)
        with pytest.raises(ToolError, match="status 1"):
            await registry.invoke("run_command", session, {"argv": ["false"]})
        with pytest.raises(ToolError, match="output limit"):
            await registry.invoke(
                "run_command", session, {"argv": ["printf", "%20000s", "x"]}
            )
        monkeypatch.setattr(toolbox, "_COMMAND_TIMEOUT", 0.05)
        with pytest.raises(ToolError, match="timed out"):
            await registry.invoke("run_command", session, {"argv": ["sleep", "10"]})

    asyncio.run(run())


def test_web_fetch_checks_literals_and_dns_results(tmp_path, monkeypatch):
    async def local_dns(self, host, port, family):
        return [{"host": "127.0.0.1"}]

    async def run():
        registry = ToolRegistry()
        session, _ = context(tmp_path)
        for url in (
            "http://127.0.0.1",
            "http://127.1",
            "http://[::ffff:127.0.0.1]",
            "http://169.254.169.254",
            "http://[::1]",
            "http://localhost.",
        ):
            with pytest.raises(ToolError, match="public address"):
                await registry.invoke("web_fetch", session, {"url": url})
        monkeypatch.setattr(toolbox.ThreadedResolver, "resolve", local_dns)
        with pytest.raises(ToolError, match="public address"):
            await registry.invoke(
                "web_fetch", session, {"url": "http://public.example/"}
            )

    asyncio.run(run())


_SERVER = r"""
import json
import sys

initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request.get("params", {})
    if method == "notifications/initialized":
        initialized = True
        continue
    if method == "initialize":
        result = {"protocolVersion": params["protocolVersion"]}
    elif method == "tools/list" and initialized:
        name = "a." * 40 if not params.get("cursor") else "echo"
        result = {"tools": [{
            "name": name, "description": "Return text.",
            "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                            "required": ["text"], "additionalProperties": False},
        }]}
        if not params.get("cursor"):
            result["nextCursor"] = "page-2"
    elif method == "tools/call" and initialized:
        result = {"content": [{"type": "text", "text": params["arguments"]["text"]}]}
    else:
        raise RuntimeError("unexpected request")
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


def test_mcp_discovers_paginated_tools_and_validates_calls(tmp_path):
    async def run():
        script = tmp_path / "server.py"
        script.write_text(_SERVER)
        registry = ToolRegistry()
        session, _ = context(tmp_path)
        try:
            await registry.connect_mcp(
                [{"name": "demo", "command": sys.executable, "args": [str(script)]}]
            )
            names = [name for name in registry.specs if name.startswith("mcp__")]
            assert len(names) == 2 and "mcp__demo__echo" in names
            assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in names)
            await registry.invoke("tool_search", session, {"query": "mcp__demo__echo"})
            assert "mcp__demo__echo" in session.revealed
            with pytest.raises(ToolError, match="Invalid mcp__demo__echo arguments"):
                await registry.invoke("mcp__demo__echo", session, {"text": 5})
            assert (
                await registry.invoke("mcp__demo__echo", session, {"text": "hi"})
                == "hi"
            )
        finally:
            await registry.close()
        assert "mcp__demo__echo" not in registry.specs

    asyncio.run(run())


def test_unresponsive_mcp_server_times_out():
    async def run():
        client = StdioMcpClient(
            "sleeping",
            sys.executable,
            ["-c", "import time; time.sleep(10)"],
            timeout=0.1,
        )
        with pytest.raises(McpError, match="timed out"):
            await client.start()
        await client.close()

    asyncio.run(run())


def test_cancelling_one_mcp_call_keeps_the_shared_server(tmp_path):
    script = tmp_path / "server.py"
    marker = tmp_path / "request-arrived"
    script.write_text(
        _SERVER.replace(
            "import sys", "import sys\nimport time\nfrom pathlib import Path"
        ).replace(
            '    elif method == "tools/call" and initialized:\n',
            '    elif method == "tools/call" and initialized:\n'
            '        if params["arguments"]["text"] == "slow":\n'
            "            Path(sys.argv[1]).touch()\n"
            "            time.sleep(0.15)\n",
        )
    )

    async def verify():
        client = StdioMcpClient(
            "fixture", sys.executable, [str(script), str(marker)], timeout=2
        )
        await client.start()
        try:
            pending = asyncio.create_task(client.call_tool("echo", {"text": "slow"}))
            async with asyncio.timeout(1):
                while not marker.exists():
                    await asyncio.sleep(0.005)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert (
                await client.call_tool("echo", {"text": "next request"})
                == "next request"
            )
        finally:
            await client.close()

    asyncio.run(verify())
