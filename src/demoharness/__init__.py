"""DemoHarness: a minimal teaching skeleton for long-running agent tasks."""

from .core.task import (
    Action,
    ActionState,
    Budget,
    CheckEvidence,
    Delivery,
    FileCheck,
    Input,
    InputReceipt,
    Run,
    RunReason,
    Task,
    TaskState,
    Verdict,
)
from .runtime import Agent, AgentDefinition, AgentRuntime

__all__ = [
    "Action", "ActionState", "Agent", "AgentDefinition", "AgentRuntime", "Budget", "CheckEvidence",
    "Delivery", "FileCheck", "Input", "InputReceipt", "Run", "RunReason", "Task", "TaskState", "Verdict",
]

__version__ = "0.1.0"
