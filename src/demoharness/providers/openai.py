"""Translate one DemoHarness model request into OpenAI Chat Completions."""

from __future__ import annotations

import asyncio
import json

import httpx

from demoharness.core.loop import ModelRequest, ModelResponse, ModelStop, StreamFn


def make_openai_stream_fn(
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o",
) -> StreamFn:
    async def stream_fn(request: ModelRequest, cancel: asyncio.Event) -> ModelResponse:
        if cancel.is_set():
            return ModelResponse(ModelStop.ABORTED, "")

        selected_model = request.model or model
        if httpx.URL(base_url).host == "api.openai.com":
            selected_model = selected_model.removeprefix("openai/")
        messages = list(request.messages)
        if request.system_prompt:
            messages.insert(0, {"role": "system", "content": request.system_prompt})
        payload = {"model": selected_model, "messages": messages}
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as client:
                response = await _post(client, api_key, payload, cancel)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            return ModelResponse(ModelStop.ERROR, "", error=f"模型请求失败：{type(exc).__name__}")

        if response is None or cancel.is_set():
            return ModelResponse(ModelStop.ABORTED, "")
        if response.status_code != 200:
            return ModelResponse(ModelStop.ERROR, "", error=f"HTTP {response.status_code}")
        try:
            return _decode(response.json())
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return ModelResponse(ModelStop.ERROR, "", error="模型返回格式不符合 Chat Completions 协议")

    return stream_fn


async def _post(client: httpx.AsyncClient, key: str, payload: dict,
                cancel: asyncio.Event) -> httpx.Response | None:
    pending = asyncio.create_task(client.post(
        "/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"},
    ))
    stopped = asyncio.create_task(cancel.wait())
    try:
        await asyncio.wait((pending, stopped), return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set():
            return None
        return pending.result()
    finally:
        for task in (pending, stopped):
            task.cancel()
        await asyncio.gather(pending, stopped, return_exceptions=True)


def _decode(data: dict) -> ModelResponse:
    choice = data["choices"][0]
    reason = choice["finish_reason"]
    message = choice["message"]
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("invalid message content")
    text = content or ""
    usage = data.get("usage")
    if reason == "length":
        return ModelResponse(ModelStop.LENGTH, text, usage=usage)
    if reason == "content_filter" or message.get("refusal"):
        return ModelResponse(ModelStop.ERROR, "", usage=usage, error="模型拒绝或过滤了请求")
    if reason not in {"stop", "tool_calls"}:
        raise ValueError("unexpected finish reason")

    calls = []
    ids = set()
    for call in message.get("tool_calls") or []:
        call_id = call["id"]
        name = call["function"]["name"]
        params = json.loads(call["function"]["arguments"])
        if (not isinstance(call_id, str) or not call_id or call_id in ids
                or not isinstance(name, str) or not name or not isinstance(params, dict)):
            raise ValueError("invalid tool call")
        ids.add(call_id)
        calls.append({"call_id": call_id, "name": name, "params": params})
    if reason == "tool_calls" and not calls:
        raise ValueError("missing tool calls")
    return ModelResponse(ModelStop.OK, text, calls, usage)
