import asyncio
import json
from functools import partial

import httpx
import pytest

from demoharness.core.loop import ModelRequest, ModelStop
from demoharness.providers.openai import make_openai_stream_fn


REQUEST = ModelRequest("Review only.", [{"role": "user", "content": "Hello"}], [], "test-model", 64)


def model_with_transport(monkeypatch, handle):
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return make_openai_stream_fn("test-key")


def test_provider_sends_the_callers_model_prompt_and_limit(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
        })

    complete = model_with_transport(monkeypatch, handle)
    response = asyncio.run(complete(REQUEST, asyncio.Event()))

    assert requests[0].url.path == "/v1/chat/completions"
    assert json.loads(requests[0].content) == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Review only."},
            {"role": "user", "content": "Hello"},
        ],
        "max_tokens": 64,
    }
    assert response.stop_reason is ModelStop.LENGTH


@pytest.mark.parametrize("body", [
    b"invalid json",
    b'{"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "call_1", '
    b'"function": {"name": "write_file", "arguments": "[]"}}]}}]}',
])
def test_provider_rejects_malformed_results_before_tools_can_run(monkeypatch, body):
    complete = model_with_transport(monkeypatch, lambda request: httpx.Response(200, content=body))

    response = asyncio.run(complete(REQUEST, asyncio.Event()))

    assert response.stop_reason is ModelStop.ERROR
    assert response.tool_calls == []


def test_provider_cancels_an_inflight_request(monkeypatch):
    async def exercise():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handle(request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        complete = model_with_transport(monkeypatch, handle)
        cancel = asyncio.Event()
        response = asyncio.create_task(complete(REQUEST, cancel))
        await asyncio.wait_for(entered.wait(), timeout=2)
        cancel.set()
        assert (await asyncio.wait_for(response, timeout=2)).stop_reason is ModelStop.ABORTED
        assert cancelled.is_set()

    asyncio.run(exercise())
