"""两段式压缩（compaction/run.py，第 6 步）。

段 1（事务外，慢）：读活跃分支 → select_cut_point → 调 stream_fn 生成摘要；
段 2（事务内）：经 CompactOp 提交（session._compact 校验 active_leaf 未漂移，
漂移则放弃返回 False）。摘要复用当前模型（compaction.md 分歧 6 默认执行）。

触发阈值与切点选择见 cut.py；`should_compact` 是阈值判定（compaction.md
「触发」节），供 loop 在每轮组装上下文前调用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from demoharness.core.loop import ModelRequest, ModelStop
from demoharness.core.session import CompactOp, SessionManager
from demoharness.core.storage import EntryRow

from .cut import estimate_tokens, select_cut_point

_SUMMARY_SYSTEM = "你是上下文压缩器，把对话与工具工作摘要化，保留关键事实、结论与未完成事项。"
_SUMMARY_TMPL = "请把以下内容压缩成一段简明摘要（保留关键事实、结论、未完成事项）：\n\n{content}"


@dataclass(frozen=True)
class ContextStats:
    tokens: int
    context_window: int
    reserve: int = 16384


def should_compact(stats: ContextStats) -> bool:
    """阈值判定：估算 token > 模型窗口 - 保留量。"""
    return stats.tokens > stats.context_window - stats.reserve


def _parse_content(content: str) -> str:
    """entry.content 是 JSON 时取 text 字段，否则原文（与 core 解析对称）。"""
    import json

    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return content
    if isinstance(parsed, dict):
        return parsed.get("text") or parsed.get("summary") or parsed.get("content") or ""
    return content


def _render_entry(entry: EntryRow) -> str:
    """把一条 entry 渲染成摘要输入的纯文本行（含旧 compaction 摘要，天然迭代）。"""
    label = {
        "user": "用户",
        "assistant": "助手",
        "tool_call": "工具调用",
        "tool_result": "工具结果",
        "compaction": "此前摘要",
        "verification_failed": "验证未通过",
    }.get(entry.kind, entry.kind)
    text = _parse_content(entry.content)
    return f"[{label}] {text}"


class Compactor:
    """压缩扩展：loop 经 `should_compact_for` 判断、`compact` 触发两段式压缩。

    loop 只在循环内部使用它（鸭子类型，不反向 import 本模块，避免环）。
    """

    def __init__(
        self,
        session_mgr: SessionManager,
        stream_fn,
        model: str,
        context_window: int,
        reserve: int = 16384,
        keep_recent_tokens: int = 20000,
    ) -> None:
        self._session_mgr = session_mgr
        self._stream_fn = stream_fn
        self._model = model
        self._context_window = context_window
        self._reserve = reserve
        self._keep_recent_tokens = keep_recent_tokens

    def should_compact_for(self, replayed: list[EntryRow]) -> bool:
        tokens = sum(estimate_tokens(e.content) for e in replayed)
        return should_compact(
            ContextStats(tokens=tokens, context_window=self._context_window, reserve=self._reserve)
        )

    async def compact(self, sid: str, replayed: list[EntryRow]) -> bool:
        """两段式压缩。返回 True = 已落盘 compaction entry；False = 放弃。"""
        cut = select_cut_point(replayed, self._keep_recent_tokens)
        if cut is None:
            return False
        leaf = replayed[-1].id
        summary = await self._summarize(replayed[: cut.index + 1])
        if not summary:
            return False
        # 段 2：CompactOp 经会话 FIFO 提交；session._compact 校验 leaf 漂移。
        # during_task=True 允许在任务运行中压缩（loop 在任务边界调用，FIFO 空闲）。
        try:
            return await self._session_mgr.submit(
                sid,
                CompactOp(sid, summary=summary, leaf=leaf, during_task=True),
            )
        except Exception:
            return False

    async def _summarize(self, entries: list[EntryRow]) -> str:
        content = "\n".join(_render_entry(e) for e in entries)
        request = ModelRequest(
            system_prompt=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": _SUMMARY_TMPL.format(content=content)}],
            tools=[],
            model=self._model,
        )
        resp = await self._stream_fn(request, asyncio.Event())
        if resp.stop_reason == ModelStop.OK and resp.text:
            return resp.text
        return ""


async def compact(sid: str, session_mgr: SessionManager, stream_fn, model: str,
                  context_window: int, replayed: list[EntryRow]) -> bool:
    """模块级便捷入口（compaction.md 的函数签名），等价 Compactor.compact。"""
    return await Compactor(session_mgr, stream_fn, model, context_window).compact(sid, replayed)
