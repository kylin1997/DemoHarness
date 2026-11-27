"""Exercise the v2 runtime through its session and chat entry point."""

import asyncio

import pytest

from demoharness.guardrails import INPUT_MARKER, OUTPUT_MARKER, Guardrails
from demoharness.harness import DemoHarness
from demoharness.protocol import ModelReply, ToolCall
from demoharness.skills import Skills
from demoharness.state import Store
from demoharness.toolbox import ToolRegistry


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "sessions.db")
    session_id = store.create_session()["id"]
    guardrails = Guardrails(store)
    registry = ToolRegistry()

    def make(model):
        return DemoHarness(
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
        async def chat_stream(self, messages, schemas, on_delta, *, reasoning):
            if "DEMOHARNESS_COMPACT" in messages[0]["content"]:
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
