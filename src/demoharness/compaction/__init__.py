"""上下文压缩扩展（第 6 步）。

能力本体：阈值触发 + 切点选择 + 两段式提交。挂进 LoopRuntime.compaction，
由 loop 在每轮组装前与 LENGTH 响应时触发。公开符号：
- estimate_tokens / select_cut_point（纯函数，可确定性单测）
- should_compact / ContextStats（阈值判定）
- Compactor（装配进 BotRuntime 的可注入对象）
"""

from .cut import CutPoint, entry_tokens, estimate_tokens, select_cut_point
from .run import Compactor, ContextStats, compact, should_compact

__all__ = [
    "CutPoint",
    "entry_tokens",
    "estimate_tokens",
    "select_cut_point",
    "Compactor",
    "ContextStats",
    "compact",
    "should_compact",
]
