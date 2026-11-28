"""工具实现与目录：按用户轮次发现工具，校验参数并执行内置或 MCP 工具。

文件工具限制在工作区内，命令和网页工具设置执行边界；会话审批由运行层调用护栏。
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from functools import cache
from pathlib import Path, PurePosixPath

from aiohttp import ClientError, ClientSession, TCPConnector
from aiohttp.abc import ResolveResult
from aiohttp.helpers import is_ip_address
from aiohttp.resolver import ThreadedResolver
from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable
from yarl import URL

from .guardrails import validate_command_argv
from .mcp import McpError, StdioMcpClient, stop_process
from .protocol import ToolContext, ToolSpec


class ToolError(ValueError):
    """可回填给模型的工具失败说明，使循环能基于错误观察继续判断。"""


_MAX_FILE_BYTES = 64 * 1024
_MAX_RESULT_BYTES = 16 * 1024
_MAX_SCAN_ENTRIES = 2000
_MAX_SCAN_DEPTH = 24
_MAX_GREP_BYTES = 4 * 1024 * 1024
_MAX_MATCHES = 100
_COMMAND_TIMEOUT = 5
_FETCH_TIMEOUT = 8
_FETCH_REDIRECTS = 3
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_ONLY = frozenset({"read", "glob", "grep", "tool_search"})


def _bounded_text(text: str) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= _MAX_RESULT_BYTES:
        return data.decode("utf-8")
    marker = "\n[Output truncated.]"
    return data[: _MAX_RESULT_BYTES - len(marker)].decode("utf-8", "ignore") + marker


def _relative(path: str, *, kind: str = "path") -> tuple[str, ...]:
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or ".." in parsed.parts
        or "\0" in path
        or "\\" in path
    ):
        raise ToolError(f"Expected a workspace-relative {kind}")
    if len(parsed.parts) > _MAX_SCAN_DEPTH:
        raise ToolError(f"Workspace {kind} exceeds the depth limit")
    return parsed.parts


@contextmanager
def _directory(
    root: Path, parts: tuple[str, ...] = (), *, create: bool = False
) -> Iterator[int]:
    descriptor = os.open(root, _DIRECTORY)
    try:
        for part in parts:
            try:
                child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _regular_mode(info: os.stat_result) -> int:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ToolError("Expected a regular file without symlinks or hard links")
    return stat.S_IMODE(info.st_mode) & 0o777


def _read_bytes(parent: int, name: str) -> bytes:
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _regular_mode(info)
    if info.st_size > _MAX_FILE_BYTES:
        raise ToolError(f"File exceeds {_MAX_FILE_BYTES} bytes")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        _regular_mode(os.fstat(stream.fileno()))
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise ToolError(f"File exceeds {_MAX_FILE_BYTES} bytes")
    return data


def _replace_file(parent: int, name: str, data: bytes) -> None:
    try:
        mode = _regular_mode(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
        mode = 0o644
    temporary = f".tinyharness-write-{secrets.token_hex(16)}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _file_content(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:
        raise ToolError(f"File exceeds {_MAX_FILE_BYTES} bytes")
    return data


async def _read(context: ToolContext, args: dict) -> str:
    *parents, name = _relative(args["path"])
    with _directory(context.workspace, tuple(parents)) as parent:
        return _read_bytes(parent, name).decode("utf-8")


async def _write(context: ToolContext, args: dict) -> str:
    *parents, name = _relative(args["path"])
    data = _file_content(args["content"])
    with _directory(context.workspace, tuple(parents), create=True) as parent:
        _replace_file(parent, name, data)
    return f"Wrote {args['path']} ({len(data)} bytes)."


async def _edit(context: ToolContext, args: dict) -> str:
    *parents, name = _relative(args["path"])
    old, new = args["old"], args["new"]
    with _directory(context.workspace, tuple(parents)) as parent:
        text = _read_bytes(parent, name).decode("utf-8")
        position = text.find(old)
        if position < 0 or text.find(old, position + 1) >= 0:
            raise ToolError("The old text must appear exactly once")
        data = _file_content(text[:position] + new + text[position + len(old) :])
        _replace_file(parent, name, data)
    return f"Edited {args['path']}."


class _ScanLimit(Exception):
    """文件遍历达到条目上限时中止扫描，防止搜索无界消耗资源。"""


def _walk_files(root: Path) -> Iterator[tuple[str, ...]]:
    remaining = _MAX_SCAN_ENTRIES

    def walk(directory: int, prefix: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
        nonlocal remaining
        with os.scandir(directory) as entries:
            for entry in entries:
                if not remaining:
                    raise _ScanLimit
                remaining -= 1
                path = (*prefix, entry.name)
                try:
                    if (
                        entry.is_dir(follow_symlinks=False)
                        and len(path) < _MAX_SCAN_DEPTH
                    ):
                        child = os.open(entry.name, _DIRECTORY, dir_fd=directory)
                        try:
                            yield from walk(child, path)
                        finally:
                            os.close(child)
                    elif entry.is_file(follow_symlinks=False):
                        yield path
                except (FileNotFoundError, PermissionError, NotADirectoryError):
                    continue

    with _directory(root) as directory:
        yield from walk(directory, ())


def _matches(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    @cache
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern):
            return path_index == len(path)
        segment = pattern[pattern_index]
        if segment == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path)
            and fnmatch.fnmatchcase(path[path_index], segment)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


async def _glob(context: ToolContext, args: dict) -> str:
    pattern = _relative(args["pattern"], kind="pattern")
    matches = []
    try:
        with closing(_walk_files(context.workspace)) as paths:
            for path in paths:
                if _matches(path, pattern):
                    matches.append("/".join(path))
                    if len(matches) >= _MAX_MATCHES:
                        raise _ScanLimit
    except _ScanLimit:
        return "\n".join([*sorted(matches), "[Search limit reached.]"])
    return "\n".join(sorted(matches)) or "No matching files."


async def _grep(context: ToolContext, args: dict) -> str:
    pattern = _relative(args.get("pattern", "**/*"), kind="pattern")
    query = args["query"]
    matches = []
    remaining = _MAX_GREP_BYTES
    try:
        with closing(_walk_files(context.workspace)) as paths:
            for path in paths:
                if not _matches(path, pattern):
                    continue
                *parents, name = path
                try:
                    with _directory(context.workspace, tuple(parents)) as parent:
                        data = _read_bytes(parent, name)
                    remaining -= len(data)
                    if remaining < 0:
                        raise _ScanLimit
                    text = data.decode("utf-8")
                    if "\0" in text:
                        continue
                except (OSError, ToolError, UnicodeError):
                    continue
                for index, line in enumerate(text.splitlines(), 1):
                    position = line.find(query)
                    if position >= 0:
                        start = max(0, position - 40)
                        excerpt = line[start : start + 240]
                        matches.append(f"{'/'.join(path)}:{index}: {excerpt}")
                        if len(matches) >= _MAX_MATCHES:
                            raise _ScanLimit
    except _ScanLimit:
        matches.append("[Search limit reached.]")
    return "\n".join(matches) or "No matching lines."


async def _run_command(context: ToolContext, args: dict) -> str:
    argv = args["argv"]
    reason = validate_command_argv(argv)
    if reason:
        raise ToolError(reason)
    with _directory(context.workspace):
        pass
    output = bytearray()
    process = None
    finished = False
    try:
        async with asyncio.timeout(_COMMAND_TIMEOUT):
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=context.workspace,
                env={
                    "PATH": os.defpath,
                    "LANG": "C.UTF-8",
                },
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name == "posix",
                limit=_MAX_RESULT_BYTES,
            )
            assert process.stdout is not None
            while chunk := await process.stdout.read(
                _MAX_RESULT_BYTES + 1 - len(output)
            ):
                output.extend(chunk)
                if len(output) > _MAX_RESULT_BYTES:
                    text = output[:_MAX_RESULT_BYTES].decode("utf-8", "replace")
                    raise ToolError(
                        _bounded_text(
                            f"Command exceeded the output limit and was stopped.\n{text}"
                        )
                    )
            status = await process.wait()
            finished = True
    except TimeoutError as error:
        raise ToolError(
            f"Command timed out after {_COMMAND_TIMEOUT} seconds"
        ) from error
    finally:
        if process is not None and not finished:
            await stop_process(process)
    text = output.decode("utf-8", "replace")
    if status != 0:
        raise ToolError(
            _bounded_text(f"Command exited with status {status}.\n{text}".rstrip())
        )
    return text


_TRANSITION_NETWORKS = tuple(
    ipaddress.ip_network(prefix)
    for prefix in ("64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16")
)


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.scope_id or any(ip in network for network in _TRANSITION_NETWORKS):
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _check_url(url: URL) -> None:
    host = url.raw_host
    if url.scheme not in {"http", "https"} or not host or url.user is not None:
        raise ToolError("Expected an HTTP or HTTPS URL without credentials")
    host = host.rstrip(".").casefold()
    if (
        not host
        or "%" in host
        or host == "localhost"
        or host.endswith((".localhost", ".local"))
    ):
        raise ToolError("Web fetch requires a public address")
    if is_ip_address(url.raw_host):
        try:
            allowed = _is_public_address(url.raw_host)
        except ValueError:
            allowed = False
        if not allowed:
            raise ToolError("Web fetch requires a public address")


class _PublicResolver(ThreadedResolver):
    """检查 DNS 解析结果，仅将全部通过公网地址检查的记录交给 HTTP 连接器。"""

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_UNSPEC
    ) -> list[ResolveResult]:
        records = await super().resolve(host, port, family)
        if not records or any(
            not _is_public_address(record["host"]) for record in records
        ):
            raise ToolError("Web fetch requires a public address")
        # Pass the vetted numeric addresses to the connector without doing another lookup.
        return records


async def _web_fetch(context: ToolContext, args: dict) -> str:
    try:
        url = URL(args["url"])
        async with asyncio.timeout(_FETCH_TIMEOUT):
            async with ClientSession(
                connector=TCPConnector(resolver=_PublicResolver(), use_dns_cache=False),
                trust_env=False,
                auto_decompress=False,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                for _ in range(_FETCH_REDIRECTS + 1):
                    _check_url(url)
                    async with client.get(url, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                raise ToolError(
                                    "Web redirect did not specify a destination"
                                )
                            url = response.url.join(URL(location))
                            continue
                        if response.status >= 400:
                            raise ToolError(
                                f"Web server returned HTTP {response.status}"
                            )
                        if (
                            response.headers.get("Content-Encoding", "identity").lower()
                            != "identity"
                        ):
                            raise ToolError(
                                "Web server returned unsupported content encoding"
                            )
                        body = await response.content.readexactly(_MAX_RESULT_BYTES + 1)
                        return _bounded_text(body.decode("utf-8", "replace"))
        raise ToolError("Web fetch exceeded the redirect limit")
    except asyncio.IncompleteReadError as error:
        return error.partial.decode("utf-8", "replace")
    except TimeoutError as error:
        raise ToolError(
            f"Web fetch timed out after {_FETCH_TIMEOUT} seconds"
        ) from error
    except (ClientError, ValueError) as error:
        if isinstance(error, ToolError):
            raise
        raise ToolError("Could not fetch the URL") from error


async def _subagent(context: ToolContext, args: dict) -> str:
    if context.delegate is None:
        raise ToolError("Subagent delegation is unavailable")
    return await context.delegate(args["task"])


async def _memory(context: ToolContext, args: dict) -> str:
    text = args.get("text", "").strip()
    if args["action"] == "save":
        if not text:
            raise ToolError("Memory save requires text")
        context.remember(text)
        return "Memory saved."
    matches = [
        entry for entry in context.recall() if text.casefold() in entry.casefold()
    ]
    return "\n".join(matches) or "No matching memory."


async def _todo(context: ToolContext, args: dict) -> str:
    items = [dict(item) for item in args["items"]]
    context.plan[:] = items
    await context.emit("plan", items=items)
    return f"Plan updated with {len(items)} items."


def _object(properties: dict, *required: str) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _string(maximum: int, *, minimum: int = 1) -> dict:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def _array(items: dict, maximum: int, *, minimum: int = 0) -> dict:
    return {"type": "array", "items": items, "maxItems": maximum, "minItems": minimum}


_PATH = _string(1024)
_PATTERN = _string(512)
_CONTENT = _string(_MAX_FILE_BYTES, minimum=0)
_PLAN_ITEM = _object(
    {
        "title": _string(180),
        "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
    },
    "title",
    "status",
)
_BUILTINS = (
    ToolSpec(
        "read",
        "Read a UTF-8 file in the workspace. 读取文件。",
        _object({"path": _PATH}, "path"),
        _read,
    ),
    ToolSpec(
        "write",
        "Create or replace a UTF-8 file in the workspace. 写入文件。",
        _object({"path": _PATH, "content": _CONTENT}, "path", "content"),
        _write,
    ),
    ToolSpec(
        "edit",
        "Replace a unique piece of text in a workspace file. 编辑文件。",
        _object(
            {"path": _PATH, "old": _string(_MAX_FILE_BYTES), "new": _CONTENT},
            "path",
            "old",
            "new",
        ),
        _edit,
    ),
    ToolSpec(
        "glob",
        "Find workspace file paths by glob pattern. 按路径模式查找文件。",
        _object({"pattern": _PATTERN}, "pattern"),
        _glob,
    ),
    ToolSpec(
        "grep",
        "Find literal text in workspace files. 搜索文件内容。",
        _object({"query": _string(300), "pattern": _PATTERN}, "query"),
        _grep,
    ),
    ToolSpec(
        "run_command",
        "Run an argv command in the workspace under the command-approval policy. 执行命令。",
        _object({"argv": _array(_string(2048, minimum=0), 32, minimum=1)}, "argv"),
        _run_command,
    ),
    ToolSpec(
        "web_fetch",
        "Fetch text from a public HTTP(S) URL. 读取网页。",
        _object({"url": _string(2048)}, "url"),
        _web_fetch,
    ),
    ToolSpec(
        "subagent",
        "Delegate a read-only workspace investigation to a child agent. 委派子 Agent。",
        _object({"task": _string(4000)}, "task"),
        _subagent,
    ),
    ToolSpec(
        "memory",
        "Save or search this session's memory. 保存或搜索记忆。",
        _object(
            {
                "action": {"type": "string", "enum": ["save", "search"]},
                "text": _string(4000, minimum=0),
            },
            "action",
        ),
        _memory,
    ),
    ToolSpec(
        "todo",
        "Replace the current plan with its updated items. 更新计划。",
        _object({"items": _array(_PLAN_ITEM, 20)}, "items"),
        _todo,
    ),
)


def _mcp_name(server_name: str, tool_name: str) -> str:
    full = f"mcp__{server_name}__{tool_name}"
    if (
        len(full) <= 64
        and re.fullmatch(r"[A-Za-z0-9_-]+", full)
        and "__" not in server_name
        and "__" not in tool_name
    ):
        return full
    prefix = re.sub(r"[^A-Za-z0-9_-]", "_", full)
    identity = json.dumps([server_name, tool_name], ensure_ascii=True)
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{prefix[:50]}__{suffix}"


def _mcp_spec(client: StdioMcpClient, definition: dict) -> ToolSpec:
    name = definition.get("name")
    parameters = definition.get("inputSchema")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(parameters, dict)
        or parameters.get("type") != "object"
    ):
        raise ToolError(f"MCP server {client.name} returned an invalid tool definition")
    description = (
        definition.get("description") or f"Use {name} on the {client.name} MCP server."
    )
    if not isinstance(description, str):
        raise ToolError(
            f"MCP server {client.name} returned an invalid tool description"
        )
    try:
        Draft202012Validator.check_schema(parameters)
    except SchemaError as error:
        raise ToolError(
            f"MCP server {client.name} returned an invalid input schema"
        ) from error

    async def handler(context: ToolContext, args: dict) -> str:
        return await client.call_tool(name, args)

    return ToolSpec(_mcp_name(client.name, name), description, parameters, handler)


def _mcp_client(settings: dict) -> StdioMcpClient:
    if not isinstance(settings, dict):
        raise ToolError("Each MCP configuration must be an object")
    name, command = settings.get("name"), settings.get("command")
    argv, env = settings.get("args", []), settings.get("env", {})
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(command, str)
        or not command
        or "\0" in command
        or not isinstance(argv, list)
        or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        or not isinstance(env, dict)
        or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
            for key, value in env.items()
        )
    ):
        raise ToolError(
            "MCP configuration requires name and command strings, string args and string env values"
        )
    return StdioMcpClient(name, command, argv, env)


class ToolRegistry:
    """维护内置与 MCP 工具定义，提供 Schema 查询、按需发现、参数校验及执行入口。

    工具发现结果写入当前 ToolContext 的 revealed 集合，不在不同用户轮次间共享。
    MCP 客户端随目录连接和关闭，工具结果统一限制长度并转换常见执行错误。
    """

    def __init__(self):
        specs = (
            ToolSpec(
                "tool_search",
                "Search tool names or descriptions and load matching parameter schemas. 查找并加载工具。",
                _object({"query": _string(200)}, "query"),
                self._search,
            ),
            *_BUILTINS,
        )
        self.specs = {spec.name: spec for spec in specs}
        self._clients: list[StdioMcpClient] = []

    def schemas(self, names: set[str]) -> list[dict]:
        return [spec.schema() for name, spec in self.specs.items() if name in names]

    def validate(self, name: str, args: dict) -> ToolSpec:
        spec = self.specs.get(name)
        if spec is None:
            raise ToolError(f"Unknown tool: {name}")
        if not isinstance(args, dict):
            raise ToolError(f"{name} arguments must be an object")
        try:
            validator = Draft202012Validator(spec.parameters, registry=Registry())
            error = next(validator.iter_errors(args), None)
            if error:
                raise ToolError(
                    f"Invalid {name} arguments at {error.json_path} ({error.validator})"
                )
        except Unresolvable as error:
            raise ToolError(
                f"Unresolved parameter schema reference for {name}"
            ) from error
        return spec

    async def invoke(self, name: str, context: ToolContext, args: dict) -> str:
        return await self.execute(self.validate(name, args), context, args)

    async def execute(self, spec: ToolSpec, context: ToolContext, args: dict) -> str:
        try:
            return _bounded_text(await spec.handler(context, args))
        except McpError as error:
            raise ToolError(_bounded_text(str(error))) from error
        except UnicodeError as error:
            raise ToolError("Expected UTF-8 text") from error
        except OSError as error:
            raise ToolError(error.strerror or "Local operation failed") from error

    async def _search(self, context: ToolContext, args: dict) -> str:
        query = args["query"].strip().casefold()
        allowed = [
            spec
            for name, spec in self.specs.items()
            if name != "tool_search"
            and (context.delegate is not None or name in _READ_ONLY)
        ]
        exact = [spec for spec in allowed if spec.name.casefold() == query]
        terms = re.findall(r"[\w-]+", query)
        candidates = []
        for spec in allowed:
            name, description = spec.name.casefold(), spec.description.casefold()
            weight = sum(
                3 if term in name else int(term in description) for term in terms
            )
            if weight or query == "*":
                candidates.append((weight, spec))
        candidates.sort(key=lambda item: (-item[0], item[1].name))
        results = exact or [spec for _, spec in candidates]
        schemas, names = [], []
        for spec in results[:6]:
            schema = spec.schema()
            candidate = json.dumps([*schemas, schema], ensure_ascii=False)
            if len(candidate.encode("utf-8")) <= _MAX_RESULT_BYTES:
                schemas.append(schema)
                names.append(spec.name)
        new_names = [name for name in names if name not in context.revealed]
        if new_names:
            context.revealed.update(new_names)
            await context.emit("tools_loaded", names=new_names)
        return json.dumps(schemas, ensure_ascii=False)

    async def connect_mcp(self, config: list[dict]) -> None:
        clients = []
        pending_specs: dict[str, ToolSpec] = {}
        server_names = {client.name for client in self._clients}
        try:
            for settings in config:
                client = _mcp_client(settings)
                if client.name in server_names:
                    raise ToolError(f"Duplicate MCP server: {client.name}")
                server_names.add(client.name)
                clients.append(client)
                await client.start()
                for definition in await client.list_tools():
                    spec = _mcp_spec(client, definition)
                    if spec.name in self.specs or spec.name in pending_specs:
                        raise ToolError(f"Duplicate MCP tool name: {spec.name}")
                    if (
                        len(json.dumps(spec.schema(), ensure_ascii=False).encode())
                        > _MAX_RESULT_BYTES - 2
                    ):
                        raise ToolError(f"MCP tool schema is too large: {spec.name}")
                    pending_specs[spec.name] = spec
        except BaseException:
            await asyncio.gather(*(client.close() for client in clients))
            raise
        self._clients.extend(clients)
        self.specs.update(pending_specs)

    async def close(self) -> None:
        clients, self._clients = self._clients, []
        for name in list(self.specs):
            if name.startswith("mcp__"):
                del self.specs[name]
        await asyncio.gather(*(client.close() for client in clients))
