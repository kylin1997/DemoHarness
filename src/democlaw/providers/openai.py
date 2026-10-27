"""OpenAI 兼容 provider（providers/ 扩展目录，第 4 步）。

把 ModelRequest/工具清单翻译成 OpenAI /chat/completions 请求，解析回
ModelResponse。非流式第一版（架构「不做流式」是逐步升级）。

stream_fn 契约：不得抛异常，一切失败编码进 ModelResponse.stop_reason。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from democlaw.core.loop import ModelRequest, ModelResponse, ModelStop


def make_openai_stream_fn(
    api_key: str,
    base_url: str = "https://api.openai.com/v1",
    model: str = "gpt-4o",
) -> callable:
    client = httpx.AsyncClient(base_url=base_url, timeout=120.0)

    def _to_openai_tools(tools):
        out = []
        for t in tools:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
            )
        return out

    async def stream_fn(request: ModelRequest, cancel: asyncio.Event) -> ModelResponse:
        if cancel.is_set():
            return ModelResponse(ModelStop.ABORTED, "", [], None)
        payload = {
            "model": model,
            "messages": request.messages,
            "tools": _to_openai_tools(request.tools) if request.tools else None,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            resp = await client.post("/chat/completions", json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 —— 契约：不抛异常
            return ModelResponse(ModelStop.ERROR, "", [], None, error=str(exc))
        if resp.status_code != 200:
            return ModelResponse(ModelStop.ERROR, "", [], None, error=f"HTTP {resp.status_code}")
        data = resp.json()
        msg = data["choices"][0]["message"]
        text = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        calls = [
            {
                "call_id": tc["id"],
                "name": tc["function"]["name"],
                "params": _parse_args(tc["function"]["arguments"]),
            }
            for tc in tool_calls
        ]
        return ModelResponse(ModelStop.OK, text, calls, data.get("usage"))

    return stream_fn


def _parse_args(arguments: str) -> dict:
    try:
        return json.loads(arguments) if arguments else {}
    except (ValueError, TypeError):
        return {}