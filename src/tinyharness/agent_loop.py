"""The shared model → tool → observation loop."""

from collections.abc import Awaitable, Callable

from .protocol import ModelReply, ToolCall


class LoopLimit(RuntimeError):
    """The agent ran out of model rounds before reaching an answer."""


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
