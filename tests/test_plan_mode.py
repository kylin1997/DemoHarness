"""strategies/plan_mode.py 的验收测试。"""

from demoharness.strategies.plan_mode import (
    PlanMode,
    extract_todo_items,
    is_safe_command,
)


def test_is_safe_command_readonly():
    assert is_safe_command("ls -la")
    assert is_safe_command("grep foo file.txt")
    assert is_safe_command("git status")
    assert not is_safe_command("rm -rf /tmp/x")
    assert not is_safe_command("sudo rm -rf /")
    assert not is_safe_command("git push origin main")


def test_extract_todo_items_plan_header():
    text = "Plan:\n1. 分析代码的整体结构\n2. 修复登录认证的缺陷\n3. 运行完整测试套件"
    items = extract_todo_items(text)
    assert [i.step for i in items] == [1, 2, 3]
    assert all(not i.completed for i in items)


def test_extract_todo_items_no_plan_header():
    assert extract_todo_items("没有 plan 标题\n1. 某步骤") == []


def test_mark_completed_steps():
    pm = PlanMode()
    pm.enter()
    pm.load_plan("Plan:\n1. 完成第一步的拆解\n2. 完成第二步的实现\n3. 完成第三步的验证")
    n = pm.mark_done("完成了 [DONE:1] 和 [DONE:3]")
    assert n == 2
    assert pm.todos[0].completed is True
    assert pm.todos[1].completed is False
    assert pm.todos[2].completed is True
    assert len(pm.remaining) == 1


def test_before_hook_blocks_dangerous_in_plan():
    pm = PlanMode()
    pm.enter()
    # plan 模式下禁用 write_file
    v1 = pm.before_hook("write_file", {"path": "x"})
    assert v1 is not None and v1.block is True
    # plan 模式下 run_command 危险命令被拦
    v2 = pm.before_hook("run_command", {"command": "rm -rf x"})
    assert v2 is not None and v2.block is True
    # plan 模式下 run_command 只读命令放行
    v3 = pm.before_hook("run_command", {"command": "ls -la"})
    assert v3 is None


def test_before_hook_noop_when_disabled():
    pm = PlanMode()
    assert pm.before_hook("write_file", {}) is None
    assert pm.before_hook("run_command", {"command": "rm -rf x"}) is None


def test_state_machine_flow():
    pm = PlanMode()
    assert pm.enabled is False
    pm.enter()
    assert pm.enabled is True
    pm.load_plan("Plan:\n1. 完成第一项任务")
    assert len(pm.todos) == 1
    pm.execute()
    assert pm.enabled is False
    pm.reset()
    assert pm.todos == []