"""examples/ 公共设施（_common.py）。

提供示例共用的两件事：
1. make_stream_fn()：从 examples/.env 读 OPENAI_API_KEY 等配置，构造真实模型
   stream_fn（OpenAI 兼容端点）。缺 key 时报错并给操作指引。
2. demo_runtime(name)：用 _data/<name>.db 作存储，bots/ 作 bot 配置，构造
   BotRuntime。

examples 全用真实模型（不用 mock）——这是「功能不减配」的落地：每个能力的
价值要有一个真实模型可跑的示例作证据。
"""

from __future__ import annotations

import os
from pathlib import Path

from demoharness.providers.openai import make_openai_stream_fn
from demoharness.legacy_runtime import BotRuntime, RuntimeConfig

_ENV_FILE = Path(__file__).parent / ".env"
_DATA_DIR = Path(__file__).parent / "_data"
_BOTS_DIR = Path(__file__).parent.parent / "bots"


def load_env() -> dict[str, str]:
    """从 examples/.env 读 KEY=VALUE 行（轻量解析，标准库，不引 python-dotenv）。"""
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def make_stream_fn():
    """真实模型 stream_fn；缺 key 报错并给指引。"""
    env = load_env()
    key = env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key or key == "sk-...":
        raise RuntimeError(
            "缺少 API key：请复制 examples/.env.example 为 examples/.env 并填入 OPENAI_API_KEY"
        )
    base_url = env.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = env.get("MODEL") or os.environ.get("MODEL") or "gpt-4o"
    return make_openai_stream_fn(key, base_url=base_url, model=model)


def demo_runtime(name: str) -> BotRuntime:
    """构造 BotRuntime：db 落在 examples/_data/<name>.db，bot 配置用项目 bots/。"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    model = load_env().get("MODEL") or os.environ.get("MODEL") or "gpt-4o"
    return BotRuntime.build(
        RuntimeConfig(
            db_path=str(_DATA_DIR / f"{name}.db"),
            bots_dir=str(_BOTS_DIR),
            model=model,
            stream_fn=make_stream_fn(),
        )
    )
