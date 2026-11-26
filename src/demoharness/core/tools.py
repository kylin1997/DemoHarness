"""共享的工具规格、入参校验和工具结果。

当前执行循环通过 RecoverableTools 控制准备、执行和恢复时的核对。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .task import Action


class ReplayPolicy(Enum):
    """中断后无法核对结果时，该工具动作是否可以安全重放。"""

    NEVER = "never"
    SAFE = "safe"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict                 # JSON Schema（手写 dict，零依赖）
    replay: ReplayPolicy = ReplayPolicy.NEVER


@dataclass(frozen=True)
class ToolCall:
    """工具调用的标识、名称和参数。"""

    call_id: str
    name: str
    params: dict


@dataclass
class ToolResult:
    content: str                  # 模型看到的结果文本
    details: dict | None = None   # 结构化详情，保存在 Session 历史中
    is_error: bool = False


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    execute: Callable[[dict], ToolResult]


_SCALAR_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool}


def validate_params(spec: ToolSpec, params: dict) -> tuple[bool, str]:
    """按 ToolSpec.parameters 校验**实际传入**的入参，返回 (ok, reason)。

    支持：标量 (string/number/integer/boolean)、顶层 required、enum、
    number/integer 的 minimum/maximum、string 的 minLength/maxLength，以及
    `{"type":"array","items":{"type":<scalar>}}` 的最小子集（含元素类型、
    minItems/maxItems）。object/null/anyOf/oneOf/allOf/$ref 等复合 schema
    仍在显式债务内：遇到即给出可读的「不支持」错误，不静默放行。

    可选的复合参数只在实际传入时校验。
    """
    schema = spec.parameters or {}
    props = schema.get("properties", {})
    top_type = schema.get("type")

    # 参数本身声明为 object 是合法的（工具入参本就是个对象）。
    if top_type not in (None, "object"):
        return False, f"工具 {spec.name} 的参数 schema 顶层类型不支持：{top_type}"

    required = set(schema.get("required", []))
    missing = required - set(params.keys())
    if missing:
        return False, f"缺少必需参数：{', '.join(sorted(missing))}"

    for name, value in params.items():
        prop = props.get(name)
        if prop is None:
            return False, f"参数 {name} 不在 schema 内（多余字段）"
        ptype = prop.get("type")
        if ptype in _SCALAR_TYPES:
            ok, reason = _validate_scalar(name, ptype, value, prop)
            if not ok:
                return False, reason
        elif ptype == "array":
            ok, reason = _validate_array(name, value, prop)
            if not ok:
                return False, reason
        else:
            return False, f"参数 {name} 类型暂不支持（需复合 schema）：{ptype}"

    return True, ""


def _validate_scalar(name: str, ptype: str, value: object, prop: dict) -> tuple[bool, str]:
    """单个标量参数的校验（含 bool 不是 integer 的显式排除）。"""
    expected = _SCALAR_TYPES[ptype]
    # bool 是 int 的子类：integer 必须显式排除 bool，放 isinstance 之前判断。
    if ptype == "integer" and isinstance(value, bool):
        return False, f"参数 {name} 期望 integer，实际是 boolean"
    if not isinstance(value, expected):
        return False, f"参数 {name} 期望 {ptype}，实际是 {type(value).__name__}"
    if "enum" in prop and value not in prop["enum"]:
        return False, f"参数 {name} 值 {value!r} 不在允许集合 {prop['enum']!r}"
    if isinstance(value, int) and not isinstance(value, bool):
        lo, hi = prop.get("minimum"), prop.get("maximum")
        if lo is not None and value < lo:
            return False, f"参数 {name} 值 {value} 小于下限 {lo}"
        if hi is not None and value > hi:
            return False, f"参数 {name} 值 {value} 大于上限 {hi}"
    if isinstance(value, str):
        lo, hi = prop.get("minLength"), prop.get("maxLength")
        if lo is not None and len(value) < lo:
            return False, f"参数 {name} 长度 {len(value)} 小于下限 {lo}"
        if hi is not None and len(value) > hi:
            return False, f"参数 {name} 长度 {len(value)} 大于上限 {hi}"
    return True, ""


def _validate_array(name: str, value: object, prop: dict) -> tuple[bool, str]:
    """复合 schema 的最小子集：`{"type":"array","items":{"type":<scalar>}}`。

    只接受标量 items.type；items 缺失、items 为复合声明一律明确拒绝，
    避免「声明看起来支持、实际没校验」的假通过。
    """
    if not isinstance(value, list):
        return False, f"参数 {name} 期望 array，实际是 {type(value).__name__}"
    items = prop.get("items")
    if not isinstance(items, dict) or items.get("type") not in _SCALAR_TYPES:
        return False, f"参数 {name} 的 array 元素声明不支持（需 items.type 为标量类型）"
    item_type = items["type"]
    for idx, item in enumerate(value):
        if item_type == "integer" and isinstance(item, bool):
            return False, f"参数 {name}[{idx}] 期望 integer，实际是 boolean"
        if not isinstance(item, _SCALAR_TYPES[item_type]):
            return False, (
                f"参数 {name}[{idx}] 期望 {item_type}，实际是 {type(item).__name__}"
            )
    lo, hi = prop.get("minItems"), prop.get("maxItems")
    if lo is not None and len(value) < lo:
        return False, f"参数 {name} 元素数 {len(value)} 小于下限 {lo}"
    if hi is not None and len(value) > hi:
        return False, f"参数 {name} 元素数 {len(value)} 大于上限 {hi}"
    return True, ""


class ToolRejected(ValueError):
    pass


class UnknownEffect(Exception):
    pass


class RecoverableTools(Protocol):
    def definitions(self) -> list[ToolSpec]: ...

    def authorize(self, action: Action) -> None: ...

    async def prepare(self, action: Action) -> dict | None: ...

    async def reconcile(self, action: Action) -> ToolResult | None: ...

    async def execute(self, action: Action, intent: dict | None, cancel: asyncio.Event) -> ToolResult: ...


def model_tool_call(call_id: str, name: str, params: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(params)}}
