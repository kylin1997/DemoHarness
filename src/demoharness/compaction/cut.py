"""切点选择与 token 估算（compaction/cut.py，第 6 步）。

纯函数库，不依赖模型与会话——便于确定性单测。核心两条规则（compaction.md）：
- 铁律：绝不把切点落在 tool_call 与其 tool_result 之间。
- 孤儿防御：保留段（keep）的首个 entry 不得是 tool_result / tool_call（其
  配对已被摘要段吃掉，投影成孤儿消息会让 OpenAI 400）。
"""

from __future__ import annotations

from dataclasses import dataclass

from demoharness.core.storage import EntryRow

# 不可作为保留段首的 kind（孤儿：缺少其配对的 assistant / tool_call）。
_UNSAFE_KEEP_START = ("tool_call", "tool_result")


def _is_cjk(ch: str) -> bool:
    """粗略判定 CJK 字符（中文为主），用于中文感知的 token 估算。"""
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF      # CJK 统一表意文字
        or 0x3400 <= cp <= 0x4DBF   # 扩展 A
        or 0x3000 <= cp <= 0x303F   # CJK 标点
        or 0xFF00 <= cp <= 0xFFEF   # 全角
    )


def estimate_tokens(text: str) -> int:
    """中文感知的 token 估算：CJK 字计 1，非 CJK 字符计 1/4。

    修正 pi 的「字符/4」英文启发式——中文 1 字 ≈ 1 token，纯英文启发式会
    系统性低估 4 倍、延迟压缩触发（compaction.md 评审 B-3）。
    """
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return cjk + other // 4


def entry_tokens(entry: EntryRow) -> int:
    """单条 entry 的估算 token 数。"""
    return estimate_tokens(entry.content)


@dataclass(frozen=True)
class CutPoint:
    """切点：`index` 为摘要段的最后一条下标（保留段从 index+1 起）。"""

    index: int
    summary_tokens: int
    keep_tokens: int


def select_cut_point(
    replayed: list[EntryRow], keep_recent_tokens: int = 20000
) -> CutPoint | None:
    """从活跃叶向老回溯，累积 token 到 keep_recent 得保留段；切点候选 =
    user entry 或「tool_calls 已全部有结果」的 assistant entry。

    返回 None 表示无需/无法压缩（内容不足或没有安全切点）。保留段首必为
    安全 kind（非 tool_call/tool_result）。
    """
    n = len(replayed)
    if n < 2:
        return None

    keep_tokens = 0
    for i in range(n - 1, -1, -1):
        keep_tokens += entry_tokens(replayed[i])
        # 保留段从 i 起是否安全：i 自身不能是孤儿 tool_call/tool_result。
        if replayed[i].kind in _UNSAFE_KEEP_START:
            continue
        if keep_tokens < keep_recent_tokens:
            continue
        # i == 0 意味着没有可摘要的历史（保留段 = 全量），无可压内容。
        if i == 0:
            return None
        summary_tokens = sum(entry_tokens(e) for e in replayed[:i])
        return CutPoint(index=i - 1, summary_tokens=summary_tokens, keep_tokens=keep_tokens)

    return None
