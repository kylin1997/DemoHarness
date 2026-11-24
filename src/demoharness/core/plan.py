"""A Task's editable sequence of steps and its current revision."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import uuid4


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    RETIRED = "retired"


@dataclass(frozen=True)
class Step:
    id: str
    action: str
    state: StepState = StepState.PENDING
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    revision: int = 0
    steps: tuple[Step, ...] = ()

    def revise(self, actions: list[str]) -> Plan:
        texts = [text.strip() for text in actions]
        if not 1 <= len(texts) <= 20 or any(not text or len(text) > 500 for text in texts):
            raise ValueError("计划需要 1 至 20 个非空步骤，每步不超过 500 字")
        if len(set(texts)) != len(texts):
            raise ValueError("计划步骤不能重复")
        known = {step.action: step for step in self.steps if step.state is not StepState.RETIRED}
        steps = tuple(known[text] if text in known else Step(uuid4().hex[:12], text)
                      for text in texts)
        steps += tuple(replace(step, state=StepState.RETIRED) for step in self.steps
                       if step.state is StepState.RETIRED or step.action not in texts)
        return Plan(self.revision + 1, steps) if steps != self.steps else self

    def mark(self, step_id: str, state: StepState, evidence: tuple[str, ...]) -> Plan:
        for index, step in enumerate(self.steps):
            if step.id == step_id:
                if step.state is StepState.RETIRED or state is StepState.RETIRED:
                    raise ValueError("退役步骤只能通过修订计划产生，之后不能更新")
                updated = replace(step, state=state, evidence=tuple(dict.fromkeys((*step.evidence, *evidence))))
                if updated == step:
                    return self
                steps = (*self.steps[:index], updated, *self.steps[index + 1:])
                return Plan(self.revision + 1, steps)
        raise ValueError("步骤不在当前计划中")

    @classmethod
    def from_record(cls, data: dict) -> Plan:
        return cls(data["revision"], tuple(
            Step(step["id"], step["action"], StepState(step["state"]), tuple(step["evidence"]))
            for step in data["steps"]
        ))
