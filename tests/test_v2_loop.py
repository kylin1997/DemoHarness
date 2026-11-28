"""通过会话和对话入口验证运行阶段、流式护栏、工具发现、压缩及错误恢复。"""

import asyncio

import pytest

from tinyharness.guardrails import INPUT_MARKER, OUTPUT_MARKER, Guardrails
from tinyharness.harness import TinyHarness
from tinyharness.protocol import ModelReply, ToolCall
from tinyharness.skills import Skills
from tinyharness.state import Store
from tinyharness.toolbox import ToolRegistry


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "sessions")
    session_id = store.create_session()["id"]
    guardrails = Guardrails(store)
    registry = ToolRegistry()

    def make(model):
        return TinyHarness(
            store,
            registry,
            guardrails,
            Skills(tmp_path / "skills"),
            {"demo": model},
            tmp_path,
        )

    yield make, store, session_id
    store.close()


def test_streaming_guard_with_cross_chunk_marker(runtime):
    make, store, session_id = runtime

    async def verify():
        provider_paused = asyncio.Event()
        continue_provider = asyncio.Event()
        events = []
        prefix = "Safe public text while the model is still running. "

        class Model:
            """分两次输出拦截标记，模拟提供商暂停，验证跨分块的流式护栏。"""

            async def chat_stream(self, messages, schemas, on_delta, *, reasoning):
                await on_delta(prefix + OUTPUT_MARKER[:12])
                provider_paused.set()
                await continue_provider.wait()
                await on_delta(OUTPUT_MARKER[12:])
                return ModelReply(prefix + OUTPUT_MARKER)

        async def send(event):
            events.append(event)

        harness = make(Model())
        await harness.chat(session_id, INPUT_MARKER, {}, send)
        assert events[-1]["type"] == "done" and events[-1]["blocked"]
        assert not any(event["type"] == "model_call" for event in events)

        events.clear()
        chat = asyncio.create_task(
            harness.chat(session_id, "stream an answer", {}, send)
        )
        await asyncio.wait_for(provider_paused.wait(), timeout=1)
        assert any(event["type"] == "delta" for event in events)
        assert not chat.done()
        continue_provider.set()
        await chat

        deltas = "".join(event["text"] for event in events if event["type"] == "delta")
        assert deltas and prefix.startswith(deltas)
        assert events[-1]["type"] == "done" and events[-1]["blocked"]
        assert OUTPUT_MARKER not in str(store.events(session_id))
        assert all(event["type"] != "delta" for event in store.events(session_id))
        assert [event["stage"] for event in events if event["type"] == "stage"] == [
            "input",
            "context",
            "loop",
            "output",
            "finish",
        ]
        assert [message["role"] for message in store.messages(session_id)] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]

    asyncio.run(verify())


def test_tool_search_exposes_schemas_on_the_next_round_only(runtime, tmp_path):
    make, _, session_id = runtime
    (tmp_path / "notes.md").write_text("a field note", encoding="utf-8")
    events = []
    shown = []

    class Model:
        """先搜索再调用 glob，验证新发现工具的 Schema 从下一轮开始可用。"""

        async def chat_stream(self, messages, schemas, on_delta, *, reasoning):
            shown.append({schema["function"]["name"] for schema in schemas})
            if len(shown) == 1:
                return ModelReply(
                    calls=(
                        ToolCall("search", "tool_search", {"query": "glob"}),
                        ToolCall("premature", "glob", {"pattern": "*.md"}),
                    )
                )
            if len(shown) == 2:
                assert messages[-1]["tool_call_id"] == "premature"
                assert messages[-1]["content"].startswith("工具失败：")
                return ModelReply(
                    calls=(ToolCall("revealed", "glob", {"pattern": "*.md"}),)
                )
            return ModelReply("查找完成。")

    async def send(event):
        events.append(event)

    async def verify():
        harness = make(Model())
        await harness.chat(session_id, "查找 Markdown", {}, send)
        await harness.chat(session_id, "开始下一轮", {}, send)

    asyncio.run(verify())
    fixed = {"tool_search", "read", "todo"}
    assert shown == [fixed, fixed | {"glob"}, fixed | {"glob"}, fixed]
    results = {
        event["call_id"]: event for event in events if event["type"] == "tool_result"
    }
    assert results["premature"]["is_error"]
    assert not results["revealed"]["is_error"]
    assert "notes.md" in results["revealed"]["result"]


def test_compaction_retains_the_complete_tool_exchange_and_raw_history(runtime):
    make, store, session_id = runtime
    old = [
        {"role": role, "content": f"{role} {index}"}
        for index in range(5)
        for role in ("user", "assistant")
    ]
    calls = [ToolCall(str(index), "read", {"path": "notes.md"}) for index in range(4)]
    request = {
        "role": "assistant",
        "content": None,
        "tool_calls": [call.as_message() for call in calls],
    }
    observations = [
        {"role": "tool", "tool_call_id": call.id, "content": "observed"}
        for call in calls
    ]
    recent = [
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent reply"},
    ]
    store.save_window(session_id, [*old, request, *observations, *recent])
    for message in [*old, *recent]:
        store.add_message(session_id, message)
    events = []

    class Model:
        """返回历史摘要并检查后续上下文，验证压缩保留完整的工具往返。"""

        async def chat_stream(self, messages, schemas, on_delta, *, reasoning):
            if "TINYHARNESS_COMPACT" in messages[0]["content"]:
                assert not schemas
                return ModelReply("Earlier conversation summary.")
            assert messages[-8:-3] == [request, *observations]
            return ModelReply("Latest answer.")

    async def send(event):
        events.append(event)

    asyncio.run(make(Model()).chat(session_id, "Latest question.", {}, send))
    event = next(event for event in events if event["type"] == "compaction")
    assert (event["before"], event["after"], event["kept"], event["threshold"]) == (
        18,
        9,
        8,
        16,
    )
    assert events[-1]["type"] == "done"
    assert len(store.messages(session_id)) == 14
    assert store.messages(session_id)[0] == old[0]
    assert store.window(session_id)[1:6] == [request, *observations]


def test_malformed_command_never_requests_approval(runtime):
    make, _, session_id = runtime
    events = []

    class Model:
        """发现命令工具后提交含多余字段的参数，验证无效请求不会进入审批。"""

        async def chat_stream(self, messages, schemas, on_delta, *, reasoning):
            if not any(message["role"] == "tool" for message in messages):
                return ModelReply(
                    calls=(ToolCall("search", "tool_search", {"query": "run_command"}),)
                )
            if messages[-1]["tool_call_id"] == "search":
                return ModelReply(
                    calls=(
                        ToolCall(
                            "invalid",
                            "run_command",
                            {
                                "argv": ["printf", "hello"],
                                "unexpected": True,
                            },
                        ),
                    )
                )
            return ModelReply("命令参数无效。")

    async def send(event):
        events.append(event)

    asyncio.run(make(Model()).chat(session_id, "请求有误的命令", {}, send))
    assert not any(event["type"] == "approval" for event in events)
    result = next(
        event
        for event in events
        if event.get("call_id") == "invalid" and event["type"] == "tool_result"
    )
    assert result["is_error"] and "additionalProperties" in result["result"]


def test_transcript_keeps_run_identity_after_disconnect(runtime):
    make, store, session_id = runtime

    class Model:
        """返回固定回复，供断线后继续对话并核对运行 ID 的测试使用。"""

        async def chat_stream(self, messages, schemas, on_delta, *, reasoning):
            return ModelReply("第二条已完成。")

    async def disconnect(event):
        raise ConnectionResetError

    async def send(event):
        pass

    async def verify():
        harness = make(Model())
        with pytest.raises(ConnectionResetError):
            await harness.chat(session_id, "第一条在 SSE 开头断开", {}, disconnect)
        await harness.chat(session_id, "第二条继续处理", {}, send)

    asyncio.run(verify())
    first, second, reply = store.messages(session_id)
    runs = [
        event["run_id"]
        for event in store.events(session_id)
        if event["type"] == "run_started"
    ]
    assert [first["run_id"], second["run_id"], reply["run_id"]] == [
        runs[0],
        runs[1],
        runs[1],
    ]


def test_tool_exchange_remains_available_after_model_failure(runtime, tmp_path):
    from tinyharness.protocol import ModelError

    make, store, session_id = runtime
    (tmp_path / "notes.md").write_text("remember this observation")
    events = []

    class Model:
        """读取文件后模拟模型失败，再检查下一次对话能否使用此前的工具观察。"""

        async def chat_stream(self, messages, schemas, on_delta, *, reasoning):
            if messages[-1]["content"] == "read a file":
                return ModelReply(
                    calls=(ToolCall("read", "read", {"path": "notes.md"}),)
                )
            if messages[-1]["role"] == "tool":
                raise ModelError("模型暂时不可用。")
            assert any(
                message.get("tool_call_id") == "read"
                and "remember this observation" in message["content"]
                for message in messages
            )
            return ModelReply("读到了上一轮的观察。")

    async def send(event):
        events.append(event)

    async def verify():
        harness = make(Model())
        await harness.chat(session_id, "read a file", {}, send)
        assert (
            events[-1]["type"] == "error"
            and events[-1]["message"] == "模型暂时不可用。"
        )
        await harness.chat(session_id, "use the prior observation", {}, send)
        assert events[-1]["type"] == "done"

    asyncio.run(verify())
    assert any(
        message.get("tool_call_id") == "read" for message in store.window(session_id)
    )
