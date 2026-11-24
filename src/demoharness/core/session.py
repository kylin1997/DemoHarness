"""从 Session 历史和工具动作投影模型上下文，并摘要较早的消息组。"""

from __future__ import annotations

from dataclasses import dataclass

from .storage import HistoryEntry
from .task import Action
from .tools import model_tool_call


@dataclass(frozen=True)
class MessageGroup:
    through: int
    messages: list[dict]


def message_groups(entries: list[HistoryEntry], actions: list[Action]) -> list[MessageGroup]:
    responses: dict[int, list[Action]] = {}
    for action in actions:
        responses.setdefault(action.response_id, []).append(action)
    groups = []
    for entry in entries:
        data = entry.data
        if entry.kind == "input":
            groups.append(MessageGroup(entry.id, [{"role": "user", "content": data["text"]}]))
        elif entry.kind == "assistant":
            calls = responses.get(entry.id, [])
            if any(action.result is None for action in calls):
                continue
            assistant = {"role": "assistant", "content": data["text"]}
            messages = [assistant]
            if calls:
                assistant["tool_calls"] = [model_tool_call(a.call.call_id, a.call.name, a.call.params) for a in calls]
                messages.extend({"role": "tool", "tool_call_id": a.call.call_id, "content": a.result.content} for a in calls)
            groups.append(MessageGroup(entry.id, messages))
        elif entry.kind == "verification_failed":
            messages = [{"role": "user", "content": f"Verification failed. {data['detail']}"}]
            groups.append(MessageGroup(entry.id, messages))
    return groups


def compact_groups(groups: list[MessageGroup], limit: int, force: bool = False) -> tuple[int, str] | None:
    if len(groups) < 3:
        return None
    total = sum(len(group.messages) for group in groups)
    if not force and total <= limit:
        return None
    cut = 0
    while cut < len(groups) - 2 and (total > limit or force and cut == 0):
        total -= len(groups[cut].messages)
        cut += 1
    excerpts = []
    for group in groups[:cut]:
        for message in group.messages:
            content = message["content"].strip().replace("\n", " ")[:180]
            names = ", ".join(call["function"]["name"] for call in message.get("tool_calls", []))
            excerpts.append(f"{message['role']}: {content} {names}".rstrip())
    return groups[cut - 1].through, "\n".join(excerpts)[-2000:]
