"""模型适配层：将 OpenAI 兼容接口和离线演示统一为 ChatModel 调用方式。

真实模型响应经 JSON 或 SSE 解析为 ModelReply，文本增量通过回调交给运行层。
离线模型按固定场景选择工具，并依据实际工具观察返回可复现的回复。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from .protocol import ModelError, ModelReply, ToolCall

OnDelta = Callable[[str], Awaitable[None]]


class OpenAiChatModel:
    """调用 OpenAI 兼容的 Chat Completions 接口，输出文本增量和完整工具请求。

    支持 SSE 与普通 JSON 响应，将连接失败及无效响应转换为 ModelError。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self._transport = transport

    async def chat_stream(
        self,
        messages: list[dict],
        schemas: list[dict],
        on_delta: OnDelta,
        *,
        reasoning: str = "default",
    ) -> ModelReply:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if schemas:
            payload["tools"] = schemas
        if reasoning in {"low", "medium", "high"}:
            payload["reasoning_effort"] = reasoning
        elif reasoning != "default":
            raise ValueError("未知的推理强度")

        headers = {"Accept": "text/event-stream, application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=10.0), transport=self._transport
                ) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response,
            ):
                if not response.is_success:
                    raise ModelError(f"模型请求失败（HTTP {response.status_code}）")
                return await _read_response(response, on_delta)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ModelError(f"模型连接失败（{type(exc).__name__}）") from None


async def _read_response(response: httpx.Response, on_delta: OnDelta) -> ModelReply:
    reply = _ReplyParts()
    if (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        == "text/event-stream"
    ):
        completed = False
        async for data in _sse_data(response):
            if data.strip() == "[DONE]":
                completed = True
                break
            try:
                delta = reply.consume(json.loads(data), streaming=True)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ModelError("模型返回的 SSE 数据格式无效") from exc
            if delta:
                await on_delta(delta)
        if not completed and reply.finish_reason is None:
            raise ModelError("模型响应提前结束")
    else:
        await response.aread()
        try:
            delta = reply.consume(response.json(), streaming=False)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ModelError("模型返回的 JSON 数据格式无效") from exc
        if delta:
            await on_delta(delta)
    return reply.finish()


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield one data field at a time, joining multiline SSE events."""
    lines: list[str] = []
    async for line in response.aiter_lines():
        if not line and lines:
            yield "\n".join(lines)
            lines.clear()
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
    if lines:
        yield "\n".join(lines)


@dataclass
class _ToolParts:
    """累积单个工具调用的 ID、名称和参数片段，结束时解析为 ToolCall。"""

    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)

    def consume(self, fragment: dict) -> None:
        function = fragment.get("function") or {}
        self.id += fragment.get("id") or ""
        self.name += function.get("name") or ""
        self.arguments.append(function.get("arguments") or "")

    def finish(self) -> ToolCall:
        try:
            arguments = json.loads("".join(self.arguments))
        except (TypeError, ValueError) as exc:
            raise ModelError("模型返回的工具参数格式无效") from exc
        if not self.id or not self.name or not isinstance(arguments, dict):
            raise ModelError("模型返回的工具调用不完整")
        return ToolCall(self.id, self.name, arguments)


@dataclass
class _ReplyParts:
    """累积回复文本、按索引交错到达的工具片段，以及提供商返回的 Token 用量。

    finish() 检查消息和工具调用是否完整，再组装统一的 ModelReply。
    """

    text: list[str] = field(default_factory=list)
    tools: dict[int, _ToolParts] = field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    saw_choice: bool = False
    finish_reason: str | None = None

    def consume(self, data: dict, *, streaming: bool) -> str:
        if data.get("error") is not None:
            raise ModelError("模型服务返回了错误")
        usage = data.get("usage") or {}
        self.input_tokens = usage.get("prompt_tokens", self.input_tokens)
        self.output_tokens = usage.get("completion_tokens", self.output_tokens)
        choices = data.get("choices") or []
        if not choices:
            return ""
        self.saw_choice = True
        choice = choices[0]
        message = choice["delta" if streaming else "message"]
        reason = choice.get("finish_reason")
        if reason == "content_filter" or message.get("refusal"):
            raise ModelError("模型拒绝或过滤了本次请求")
        if reason is not None:
            self.finish_reason = reason
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("invalid message content")
        for position, fragment in enumerate(message.get("tool_calls") or []):
            index = fragment.get("index", position) if streaming else position
            if type(index) is not int or index < 0:
                raise ValueError("invalid tool call index")
            self.tools.setdefault(index, _ToolParts()).consume(fragment)
        if content:
            self.text.append(content)
        return content or ""

    def finish(self) -> ModelReply:
        if not self.saw_choice:
            raise ModelError("模型响应缺少消息")
        if self.finish_reason == "length" and self.tools:
            raise ModelError("模型因输出长度限制而中断工具调用")
        calls = tuple(self.tools[index].finish() for index in sorted(self.tools))
        if (self.finish_reason == "tool_calls" and not calls) or len(
            {call.id for call in calls}
        ) != len(calls):
            raise ModelError("模型返回的工具调用格式无效")
        return ModelReply(
            "".join(self.text),
            calls,
            _token_count(self.input_tokens),
            _token_count(self.output_tokens),
        )


def _token_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


_NOTES_PLAN = ("定位 notes.md", "读取并核对内容")


class ScriptedChatModel:
    """无需网络的演示模型，按固定场景请求工具并读取实际观察结果。

    文本按固定长度分块发送，Token 用量依据输入和输出的字节数估算。
    """

    async def chat_stream(
        self,
        messages: list[dict],
        schemas: list[dict],
        on_delta: OnDelta,
        *,
        reasoning: str = "default",
    ) -> ModelReply:
        turn = _ScriptedTurn.from_messages(messages, schemas)
        text, calls = turn.reply()
        for start in range(0, len(text), 24):
            await on_delta(text[start : start + 24])
        output = text + json.dumps(
            [call.as_message() for call in calls], ensure_ascii=False
        )
        model_input = json.dumps([messages, schemas], ensure_ascii=False)
        return ModelReply(
            text,
            calls,
            (len(model_input.encode()) + 3) // 4,
            (len(output.encode()) + 3) // 4,
        )


@dataclass
class _ScriptedTurn:
    """提取当前演示轮次的请求、系统标记、可见工具与观察结果，并决定下一步回复。"""

    request: str
    system_headers: list[str]
    available: set[str]
    observations: dict[str, list[str]]
    message_count: int

    @classmethod
    def from_messages(cls, messages: list[dict], schemas: list[dict]) -> _ScriptedTurn:
        system_headers = [
            message["content"].partition("\n")[0]
            for message in messages
            if message.get("role") == "system"
            and isinstance(message.get("content"), str)
        ]
        available = {schema["function"]["name"] for schema in schemas}
        last_user = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if messages[i].get("role") == "user"
            ),
            -1,
        )
        request = str(messages[last_user].get("content", "")) if last_user >= 0 else ""
        calls: dict[str, str] = {}
        observations: dict[str, list[str]] = {}
        for message in messages[last_user + 1 :]:
            for call in message.get("tool_calls") or []:
                calls[call["id"]] = call["function"]["name"]
            if message.get("role") == "tool" and message.get("tool_call_id") in calls:
                name = calls[message["tool_call_id"]]
                observations.setdefault(name, []).append(
                    str(message.get("content", ""))
                )
        return cls(request, system_headers, available, observations, len(messages))

    def reply(self) -> tuple[str, tuple[ToolCall, ...]]:
        request = self.request.casefold()
        phase = self.system_headers[0] if self.system_headers else ""
        if phase == "TINYHARNESS_COMPACT":
            return self.compact(), ()
        if phase == "TINYHARNESS_PLAN":
            steps = (
                _NOTES_PLAN if "notes.md" in request else ("明确请求", "完成并核对结果")
            )
            return json.dumps({"steps": steps}, ensure_ascii=False), ()
        if phase == "TINYHARNESS_CHILD":
            return self.inspect_notes()
        if request == "请用 mcp 回显 hello":
            return self.use_deferred("mcp__demo__echo", {"text": "hello"}, "MCP 回显")
        memory_prompt, _, memory_text = self.request.partition("：")
        if memory_text.strip() and memory_prompt in {"请记住", "请搜索记忆"}:
            action = "save" if memory_prompt == "请记住" else "search"
            return self.use_deferred(
                "memory",
                {"action": action, "text": memory_text.strip()},
                "记忆工具结果",
            )
        if "notes.md" in request and any(
            term in request for term in ("子 agent", "子agent", "subagent")
        ):
            return self.use_deferred(
                "subagent",
                {"task": "请在工作区定位并读取 notes.md，简要总结其中的内容。"},
                "子 Agent 的调查结果",
            )
        if "hello" in request and ("命令" in request or "command" in request):
            return self.use_deferred(
                "run_command", {"argv": ["printf", "hello"]}, "命令输出"
            )
        if "notes.md" in request:
            return self.inspect_notes(
                planned="TINYHARNESS_ACTIVE_PLAN" in self.system_headers
            )
        if "工具" in request or "tool" in request:
            return (
                "每轮先提供工具搜索、文件读取和计划更新。模型可通过工具搜索获取其余工具的参数与用法。",
                (),
            )
        return (
            "已收到你的消息。可以让我核对 workspace 里的 notes.md、用命令打印 hello，或委派子 Agent 读取文件。",
            (),
        )

    def compact(self) -> str:
        try:
            history = json.loads(self.request)
        except (TypeError, ValueError):
            return "此前会话已压缩。"
        if not isinstance(history, list):
            return "此前会话已压缩。"
        excerpts = [
            message["content"].strip()[:120]
            for message in history
            if isinstance(message, dict)
            and message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
            and message["content"].strip()
        ]
        return "此前会话要点：\n" + "\n".join(
            f"- {excerpt}" for excerpt in excerpts[-4:]
        )

    def result(self, name: str) -> str | None:
        results = self.observations.get(name)
        return results[-1] if results else None

    def call(self, name: str, arguments: dict) -> tuple[str, tuple[ToolCall, ...]]:
        return "", (ToolCall(f"demo_{self.message_count}_{name}", name, arguments),)

    def discover(self, name: str) -> tuple[str, tuple[ToolCall, ...]]:
        if "tool_search" in self.available and self.result("tool_search") is None:
            return self.call("tool_search", {"query": name})
        return f"当前回合未能加载 {name} 工具。", ()

    def use_deferred(
        self, name: str, arguments: dict, label: str
    ) -> tuple[str, tuple[ToolCall, ...]]:
        result = self.result(name)
        if result is not None:
            if _tool_failed(result):
                return result, ()
            return f"{label}：\n{result or '（空）'}", ()
        if name not in self.available:
            return self.discover(name)
        return self.call(name, arguments)

    def inspect_notes(
        self, *, planned: bool = False
    ) -> tuple[str, tuple[ToolCall, ...]]:
        glob_result = self.result("glob")
        read_result = self.result("read")
        todo_count = len(self.observations.get("todo", []))
        if planned and "todo" in self.available and todo_count == 0:
            return self.update_todo("in_progress", "pending")
        if glob_result is None:
            if "glob" not in self.available:
                return self.discover("glob")
            return self.call("glob", {"pattern": "notes.md"})
        if _tool_failed(glob_result):
            return glob_result, ()
        if "notes.md" not in glob_result.splitlines():
            return "工作区中没有找到 notes.md。", ()
        if planned and "todo" in self.available and todo_count == 1:
            return self.update_todo("done", "in_progress")
        if read_result is None:
            if "read" not in self.available:
                return "当前回合无法读取 notes.md。", ()
            return self.call("read", {"path": "notes.md"})
        if _tool_failed(read_result):
            return read_result, ()
        if planned and "todo" in self.available and todo_count == 2:
            return self.update_todo("done", "done")
        lines = [line.strip() for line in read_result.splitlines() if line.strip()]
        excerpt = "\n".join(lines[:3])[:320] or "（空文件）"
        return f"已读取 notes.md。文件开头：\n{excerpt}", ()

    def update_todo(self, first: str, second: str) -> tuple[str, tuple[ToolCall, ...]]:
        items = [
            {"title": title, "status": status}
            for title, status in zip(_NOTES_PLAN, (first, second))
        ]
        return self.call("todo", {"items": items})


def _tool_failed(result: str) -> bool:
    return result.startswith(("工具失败：", "Tool error:"))
