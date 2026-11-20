"""plan-mode 策略示例（examples/plan_mode.py）。

演示 plan-mode 扩展策略：进入只读探索 → 模型产「Plan:」步骤 → 提取为 todo
→ 标记 [DONE:n] 进度。能力本体 + 接口（UI 层可经 enter/execute 交互）。
真实模型。先 cp examples/.env.example examples/.env。

运行：`uv run python examples/plan_mode.py`
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from _common import demo_runtime

from demoharness.strategies.plan_mode import PlanMode


def main():
    runtime = demo_runtime("plan")
    runtime.bot("architect")

    plan = PlanMode()

    # 1. 进入 plan 模式（只读约束已挂在 runtime.plan_mode.before_hook）
    plan.enter()
    print("进入 plan 模式（只读白名单约束生效）")

    # 2. 让模型产计划（真实模型会带「Plan:」标题列步骤）
    #    注意：这里直接演示「能力本体」——模型产出计划文本后，用接口提取。
    sample = (
        "Plan:\n"
        "1. 分析现有目录结构\n"
        "2. 列出要补的示例文件\n"
        "3. 逐个实现示例\n"
    )
    items = plan.load_plan(sample)
    print(f"提取计划 {len(items)} 步：{[i.text for i in items]}")

    # 3. 标记进度（[DONE:n]）
    n = plan.mark_done("完成了 [DONE:1] 和 [DONE:2]")
    print(f"标记完成 {n} 步，剩余：{[t.text for t in plan.remaining]}")

    # 4. 退出
    plan.reset()
    print("退出 plan 模式")

    runtime.close()


if __name__ == "__main__":
    main()