"""当前工具协议的 JSON Schema 参数校验。"""

from demoharness.core.tools import ToolSpec, validate_params


def _spec(props, required=None):
    return ToolSpec(
        name="t",
        description="d",
        parameters={"type": "object", "properties": props, "required": required or []},
    )


def test_validate_scalar():
    spec = _spec(
        {
            "x": {"type": "integer", "minimum": 0, "maximum": 10},
            "s": {"type": "string"},
            "mode": {"type": "string", "enum": ["a", "b"]},
        },
        required=["x"],
    )
    ok, _ = validate_params(spec, {"x": 5, "s": "hi", "mode": "a"})
    assert ok
    # 缺必需
    ok, reason = validate_params(spec, {})
    assert not ok and "x" in reason
    # 类型错
    ok, reason = validate_params(spec, {"x": "5"})
    assert not ok and "integer" in reason
    # 多余字段
    ok, reason = validate_params(spec, {"x": 1, "yyy": 2})
    assert not ok and "yyy" in reason
    # enum 越界
    ok, reason = validate_params(spec, {"x": 1, "mode": "z"})
    assert not ok
    # 超出范围
    ok, reason = validate_params(spec, {"x": 11})
    assert not ok


def test_validate_complex_rejected():
    spec = _spec({"arr": {"type": "array"}})
    ok, reason = validate_params(spec, {"arr": []})
    assert not ok and ("不支持" in reason or "复合" in reason)


def test_validate_bool_not_integer():
    spec = _spec({"flag": {"type": "integer"}})
    ok, _ = validate_params(spec, {"flag": True})
    assert not ok
