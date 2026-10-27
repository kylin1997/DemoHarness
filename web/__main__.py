"""Web 启动入口（web/__main__.py）。

一键启动 Web 观察面：读环境变量配置模型 → 建 BotRuntime → serve。

用法：
    export OPENAI_API_KEY=sk-...        # 必需（真实模型）
    export OPENAI_BASE_URL=...          # 可选（OpenAI 兼容端点，默认官方）
    export MODEL=gpt-4o                  # 可选（模型名，默认 gpt-4o）
    uv run python -m web                 # 启动，默认 http://127.0.0.1:8765

无 key 时可用 mock 模型跑通界面（教学演示）：
    uv run python -m web --mock

环境变量名与 RuntimeConfig.api_key_env 对应（默认 OPENAI_API_KEY）。
"""

from __future__ import annotations

import os
import sys

from democlaw.core.loop import ModelResponse, ModelStop
from democlaw.providers.openai import make_openai_stream_fn
from democlaw.runtime import BotRuntime, RuntimeConfig

from .server import WebServer


def _mock_stream_fn():
    """无 key 时的确定性 mock：先读一次文件再回答，用于跑通界面。"""
    state = {"n": 0}

    async def stream_fn(request, cancel):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(ModelStop.OK, "我先读一下文件", [{"call_id": "c1", "name": "read_file", "params": {"path": "README.md"}}])
        return ModelResponse(ModelStop.OK, "已读完：这是一个教学用 agent harness。", [])

    return stream_fn


def main() -> None:
    use_mock = "--mock" in sys.argv
    port = 8765
    for i, arg in enumerate(sys.argv):
        if arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])

    if use_mock:
        stream_fn = _mock_stream_fn()
    else:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            print("缺少 OPENAI_API_KEY。用真实模型请先 export OPENAI_API_KEY=sk-…；", file=sys.stderr)
            print("或教学演示用 mock 模型：uv run python -m web --mock", file=sys.stderr)
            sys.exit(1)
        base_url = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        model = os.environ.get("MODEL") or "gpt-4o"
        stream_fn = make_openai_stream_fn(key, base_url=base_url, model=model)

    runtime = BotRuntime.build(RuntimeConfig(stream_fn=stream_fn))
    WebServer(runtime, port=port).serve()


if __name__ == "__main__":
    main()