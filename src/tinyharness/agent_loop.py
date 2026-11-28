"""最小 Agent 循环：请求模型、执行工具，再将观察结果回填到消息列表。

每轮固定本轮可见的工具集合；新发现的工具到下一轮才可用。
上下文装配、权限检查和事件记录由调用方通过 think 与 act 回调提供。
"""

from collections.abc import Awaitable, Callable

from .protocol import ModelReply, ToolCall


class LoopLimit(RuntimeError):
    """模型用尽允许的调用轮次，仍未返回不含工具调用的最终回复。"""


async def agent_loop(
    messages: list[dict],
    revealed: set[str],
    think: Callable[[int, set[str]], Awaitable[ModelReply]],
    act: Callable[[ToolCall, set[str]], Awaitable[str]],
    *,
    max_rounds: int,
) -> ModelReply:
    for round_number in range(1, max_rounds + 1):
        exposed = set(revealed)
        reply = await think(round_number, exposed)
        if not reply.calls:
            return reply
        if round_number == max_rounds:
            break

        messages.append(
            {
                "role": "assistant",
                "content": reply.text or None,
                "tool_calls": [call.as_message() for call in reply.calls],
            }
        )
        for call in reply.calls:
            observation = await act(call, exposed)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": observation}
            )

    raise LoopLimit("已达到模型轮次上限。")
