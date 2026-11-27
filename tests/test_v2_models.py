"""Exercise the public model protocol with fixtures that never use the network."""

import asyncio
import json

import httpx
import pytest

from demoharness.models import ModelError, OpenAiChatModel, ScriptedChatModel
from demoharness.protocol import ToolCall


def test_openai_stream_reassembles_interleaved_calls_and_reports_usage():
    async def run():
        deltas = []

        async def on_delta(text):
            deltas.append(text)

        class EventStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                chunks = [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "content": "检查",
                                    "reasoning_content": "private",
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 1,
                                            "id": "read-2",
                                            "function": {
                                                "name": "read",
                                                "arguments": '{"path":"',
                                            },
                                        },
                                        {
                                            "index": 0,
                                            "id": "read-1",
                                            "function": {
                                                "name": "read",
                                                "arguments": '{"path":"',
                                            },
                                        },
                                    ]
                                }
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": 'first.md"}'},
                                        },
                                        {
                                            "index": 1,
                                            "function": {"arguments": 'second.md"}'},
                                        },
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 21, "completion_tokens": 12},
                    },
                ]
                yield b": keepalive\r\n\r\n"
                for chunk in chunks:
                    event = f"event: message\r\ndata: {json.dumps(chunk, ensure_ascii=False)}\r\n\r\n".encode()
                    for start in range(0, len(event), 7):
                        yield event[start : start + 7]
                yield b"data: [DONE]\r\n\r\n"

        schemas = _schemas("read")

        def respond(request):
            assert str(request.url) == "https://fixture.example/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer fixture-key"
            assert json.loads(request.content) == {
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "Read"}],
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": schemas,
                "reasoning_effort": "high",
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream; charset=utf-8"},
                stream=EventStream(),
            )

        model = OpenAiChatModel(
            "https://fixture.example/v1/",
            "fixture-key",
            "fixture-model",
            transport=httpx.MockTransport(respond),
        )
        reply = await model.chat_stream(
            [{"role": "user", "content": "Read"}], schemas, on_delta, reasoning="high"
        )
        assert reply.text == "检查"
        assert deltas == ["检查"]
        assert reply.calls == (
            ToolCall("read-1", "read", {"path": "first.md"}),
            ToolCall("read-2", "read", {"path": "second.md"}),
        )
        assert (reply.input_tokens, reply.output_tokens) == (21, 12)

    asyncio.run(run())


def test_openai_accepts_json_and_omits_default_reasoning_override():
    async def run():
        deltas = []

        async def on_delta(text):
            deltas.append(text)

        def respond(request):
            assert "Authorization" not in request.headers
            assert "reasoning_effort" not in json.loads(request.content)
            assert "tools" not in json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "Hello"}, "finish_reason": "stop"}
                    ]
                },
            )

        model = OpenAiChatModel(
            "https://fixture.example",
            "",
            "fixture-model",
            transport=httpx.MockTransport(respond),
        )
        reply = await model.chat_stream([], [], on_delta)
        assert reply.text == "Hello"
        assert reply.calls == ()
        assert deltas == ["Hello"]
        assert reply.input_tokens is None
        assert reply.output_tokens is None

    asyncio.run(run())


def test_openai_rejects_tool_calls_from_an_interrupted_stream():
    async def run():
        def respond(request):
            chunk = {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "first",
                                    "function": {
                                        "name": "read",
                                        "arguments": '{"path":"note.md"}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                text=f"data: {json.dumps(chunk)}\n\n",
            )

        model = OpenAiChatModel(
            "https://fixture.example",
            "",
            "fixture-model",
            transport=httpx.MockTransport(respond),
        )
        with pytest.raises(ModelError, match="提前结束"):
            await model.chat_stream([], _schemas("read"), _discard)

    asyncio.run(run())


@pytest.mark.parametrize(
    "observation, expected",
    [("hello", "命令输出"), ("工具失败：用户拒绝执行。", "用户拒绝执行")],
)
def test_scripted_command_waits_for_schema_and_observation(observation, expected):
    async def run():
        model = ScriptedChatModel()
        messages = [{"role": "user", "content": "请用命令打印 hello"}]
        schemes = _schemas("tool_search", "read", "todo")
        discovery = await model.chat_stream(messages, schemes, _discard)
        assert discovery.calls[0].name == "tool_search"
        assert discovery.calls[0].arguments == {"query": "run_command"}
        _observe(messages, discovery.calls[0], "tool schema")
        command = await model.chat_stream(
            messages, schemes + _schemas("run_command"), _discard
        )
        assert command.calls[0].arguments == {"argv": ["printf", "hello"]}
        _observe(messages, command.calls[0], observation)
        final = await model.chat_stream(
            messages, schemes + _schemas("run_command"), _discard
        )
        assert final.calls == ()
        assert expected in final.text

    asyncio.run(run())


def test_scripted_child_searches_readonly_tools_and_reads_actual_result():
    async def run():
        model = ScriptedChatModel()
        parent = [{"role": "user", "content": "让子 Agent 读取 notes.md 并总结"}]
        first = await model.chat_stream(
            parent, _schemas("tool_search", "read", "todo"), _discard
        )
        assert first.calls[0].arguments == {"query": "subagent"}
        _observe(parent, first.calls[0], "tool schema")
        delegate = await model.chat_stream(
            parent, _schemas("tool_search", "read", "todo", "subagent"), _discard
        )
        assert delegate.calls[0].name == "subagent"

        messages = [
            {"role": "system", "content": "DEMOHARNESS_CHILD"},
            {"role": "user", "content": delegate.calls[0].arguments["task"]},
        ]
        discovery = await model.chat_stream(
            messages, _schemas("tool_search", "read"), _discard
        )
        assert discovery.calls[0].arguments == {"query": "glob"}
        _observe(messages, discovery.calls[0], "glob schema")
        schemas = _schemas("tool_search", "read", "glob")
        glob = await model.chat_stream(messages, schemas, _discard)
        assert glob.calls[0].name == "glob"
        _observe(messages, glob.calls[0], "notes.md")
        read = await model.chat_stream(messages, schemas, _discard)
        assert read.calls[0].name == "read"
        _observe(messages, read.calls[0], "# Meeting notes\nKeep the lesson short.")
        result = await model.chat_stream(messages, schemas, _discard)
        assert result.calls == ()
        assert "Keep the lesson short." in result.text
        _observe(parent, delegate.calls[0], result.text)
        final = await model.chat_stream(
            parent, _schemas("tool_search", "read", "todo", "subagent"), _discard
        )
        assert final.calls == ()
        assert "Keep the lesson short." in final.text

    asyncio.run(run())


def test_scripted_plan_and_compaction_use_separate_markers():
    async def run():
        model = ScriptedChatModel()
        user = {"role": "user", "content": "请生成一个可执行计划并检查 notes.md"}
        plan = await model.chat_stream(
            [{"role": "system", "content": "DEMOHARNESS_PLAN"}, user], [], _discard
        )
        assert json.loads(plan.text) == {"steps": ["定位 notes.md", "读取并核对内容"]}
        assert plan.calls == ()
        active = await model.chat_stream(
            [{"role": "system", "content": "DEMOHARNESS_ACTIVE_PLAN"}, user],
            _schemas("tool_search", "read", "todo"),
            _discard,
        )
        assert active.calls[0].name == "todo"
        assert active.calls[0].arguments["items"][0]["status"] == "in_progress"
        summary = await model.chat_stream(
            [
                {"role": "system", "content": "DEMOHARNESS_COMPACT"},
                {"role": "user", "content": json.dumps([user])},
            ],
            [],
            _discard,
        )
        assert summary.calls == ()
        assert user["content"] in summary.text

    asyncio.run(run())


@pytest.mark.parametrize(
    "prompt, name, arguments",
    [
        ("请用 MCP 回显 hello", "mcp__demo__echo", {"text": "hello"}),
        (
            "请记住：下次演示先检查 notes.md",
            "memory",
            {"action": "save", "text": "下次演示先检查 notes.md"},
        ),
        ("请搜索记忆：notes.md", "memory", {"action": "search", "text": "notes.md"}),
    ],
)
def test_scripted_optional_tools_wait_for_discovery(prompt, name, arguments):
    async def run():
        model = ScriptedChatModel()
        messages = [{"role": "user", "content": prompt}]
        schemas = _schemas("tool_search", "read", "todo")
        discovery = await model.chat_stream(messages, schemas, _discard)
        assert discovery.calls[0].name == "tool_search"
        assert discovery.calls[0].arguments == {"query": name}
        _observe(messages, discovery.calls[0], "tool schema")
        missing = await model.chat_stream(messages, schemas, _discard)
        assert missing.calls == ()
        call = await model.chat_stream(messages, schemas + _schemas(name), _discard)
        assert call.calls[0].name == name
        assert call.calls[0].arguments == arguments

    asyncio.run(run())


async def _discard(text):
    pass


def _schemas(*names):
    return [
        {
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object"}},
        }
        for name in names
    ]


def _observe(messages, call, content):
    messages.extend(
        [
            {"role": "assistant", "content": "", "tool_calls": [call.as_message()]},
            {"role": "tool", "tool_call_id": call.id, "content": content},
        ]
    )
