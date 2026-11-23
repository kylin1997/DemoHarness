# Super Agent 如何组织工作

试想你先让 Agent 写一份报告，期间又交代另一件事，最后补充第一件事的要求。需要区分消息归属、单件事的执行进度和跨任务记忆。综合示例把这些机制放在一次运行里。 [综合示例](../examples/super_agent_demo.py)

```sh
uv run python examples/super_agent_demo.py
```

## 消息如何找到 Task

不带 Task ID 的 `Agent.send()` 先把输入写入协调日志。协调器给模型提供最近的对话、候选 Task 和相关记忆，检查返回的决定，再执行回复、澄清、创建或关联。指定 `task_id` 的补充直接进该 Task 的 Session。`submit()` 适合程序调用方已经知道目标和验收条件的情况。 [消息路由](../src/demoharness/coordination.py) · [Agent API](../src/demoharness/runtime.py)

A 等模型或文件操作时，B 的 Worker 可以继续。A 的补充仍进原 Session，Worker 在动作边界读取。对一个已经完成的 Task 明确发起跟进，也会保留它的历史交付并开始新的 Run。示例先让 B 交付，再给 A 补充要求，从输出可看到 A 修订了 Plan。 [执行与交付](task-execution.md)

## 子 Agent 接手小块调查

模型通过 `delegate` 给临时子 Agent 一段调查指令。子 Agent 有独立的模型上下文，以只读方式访问获准的工作副本文件，也能用 `read_parent_history` 按页查看父 Session。它受主任务预算和停止信号约束，另有单次委派的模型调用上限。结论和证据作为工具结果交回主 Task，主 Task 自己执行修改并承担验收。 [委派工具](../src/demoharness/tools/delegation.py)

子 Agent 的 Run 与动作记录按委派 ID 查询，生命周期随委派结束，长期记忆仍归主 Agent。综合示例里的子 Agent 会从父 Session 核对用户追加的要求。

## 有来源的记忆怎样跨 Task 使用

用户可显式保存偏好，也可在 Task 输入开头用 `项目事实：名称 = 内容` 或 `项目约束：名称 = 内容` 提供项目知识。运行时在阶段检查、Run 结束或重启后从 Session 中提炼经验。带通过检查证据的 Plan 步骤可形成有效经验，模型提出的普通决策可以留下为候选。 [记忆提炼](../src/demoharness/memory.py)

召回时按 Agent 和项目筛选，只把少量相关的有效记忆及来源注入模型请求。记忆可查看、更正和删除，过期项退出召回。原始 Session 仍用于回查当时的证据。示例先验收 `a.txt`，再观察新任务如何召回这条检查经验。

## Git 提交如何变成工作

Git 观察器记录源项目的提交，从有限的路径和文本证据开始，之后按 HEAD 变化处理。工作副本的输出留在副本里。源项目出现提交后，如果变更文件的路径、模式和前后对象 ID 与已有交付匹配，宿主会关联原 Task。其他变更交由协调器决定忽略、关联或建新 Task，问题键用于合并重复工作。决定、Task 变更和通知在同一事务落盘。 [Git 观察](../src/demoharness/discovery.py) · [观察与分派](../src/demoharness/runtime.py)

观察触发的新 Task 默认可以读取和执行内置检查。项目可明确启用 `proactive_writes`，授权这类任务写入验收项指定的文件。关联观察不会自行恢复主动停止的 Task。

综合示例最后创建一个新的源提交，确认它触发了新 Task，随后重开运行时查看之前的 Task、Session 和记忆。

[返回架构总览](architecture.md)
