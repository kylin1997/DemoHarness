# Super Agent 如何组织工作

试想你先让 Agent 写一份报告，期间又交代另一件事，最后补充第一件事的要求。需要区分消息归属、单件事的执行进度和跨任务记忆。综合示例把这些机制放在一次运行里。 [综合示例](../examples/super_agent_demo.py)

```sh
uv run python examples/super_agent_demo.py
```

## 消息如何找到 Task

不带 Task ID 的 `Agent.send()` 先把输入写入协调日志。协调器给模型提供最近的对话、候选 Task 和相关记忆，检查返回的决定，再执行回复、澄清、创建或关联。指定 `task_id` 的补充直接进该 Task 的 Session。`submit()` 适合程序调用方已经知道目标和验收条件的情况。 [消息路由](../src/demoharness/coordination.py) · [Agent API](../src/demoharness/runtime.py)

示例给同一个 Agent 发两条不带 Task ID 的消息。第一条用 `授权写入：a.txt | ...` 请求创建 A。A 等模型时，B 通过 `submit()` 独立完成。第二条消息说「把工作 A 的 a.txt 改成一行『新要求』」，协调器将它关联到 A，并更新验收。对照输出中的两条消息 ID，可以看到它们进入同一个 Task 和 Session。 [综合示例](../examples/super_agent_demo.py)

Worker 会在动作边界读取补充。示例中，A 的旧模型响应在补充入库后才返回，Session 记录一条 `stale_response`，旧写入没有进入动作队列。新的模型调用将 Plan 从版本 1 修订到 2，输出会显示修订原因、过时响应的条目 ID 和唯一一次实际写入。对已完成的 Task 明确发起跟进时，系统也会保留历史交付并开始新的 Run。 [执行与交付](task-execution.md)

## 子 Agent 接手小块调查

模型通过 `delegate` 给临时子 Agent 一段调查指令。子 Agent 有独立的模型上下文，以只读方式访问获准的工作副本文件，也能用 `read_parent_history` 按页查看父 Session。它受主任务预算和停止信号约束，另有单次委派的模型调用上限。结论和证据作为工具结果交回主 Task，主 Task 自己执行修改并承担验收。 [委派工具](../src/demoharness/tools/delegation.py)

子 Agent 的 Run 与动作记录按委派 ID 查询，生命周期随委派结束，长期记忆仍归主 Agent。综合示例会打印委派 ID 和子 Agent 读到的父 Session 补充条目 ID，可与 A 的补充记录对应。

## 有来源的记忆怎样跨 Task 使用

用户可显式保存偏好，也可在 Task 输入开头用 `项目事实：名称 = 内容` 或 `项目约束：名称 = 内容` 提供项目知识。运行时在阶段检查、Run 结束或重启后从 Session 中提炼经验。带通过检查证据的 Plan 步骤可形成有效经验，模型提出的普通决策可以留下为候选。 [记忆提炼](../src/demoharness/memory.py)

召回时按 Agent 和项目筛选，只把少量相关的有效记忆及来源注入模型请求。记忆可查看、更正和删除，过期项退出召回。示例先验收 `a.txt`，再打印记忆 ID 和 `task:<Task ID>:entry:<条目 ID>` 形式的来源。可用它定位原 Task 的 Session 交付记录。

## Git 提交如何变成工作

Git 观察器记录源项目的提交，从有限的路径和文本证据开始，之后按 HEAD 变化处理。工作副本的输出留在副本里。源项目出现提交后，如果变更文件的路径、模式和前后对象 ID 与已有交付匹配，宿主会关联原 Task。其他变更交由协调器决定忽略、关联或建新 Task，问题键用于合并重复工作。决定、Task 变更和通知在同一事务落盘。 [Git 观察](../src/demoharness/discovery.py) · [观察与分派](../src/demoharness/runtime.py)

观察触发的新 Task 默认可以读取和执行内置检查。项目可明确启用 `proactive_writes`，授权这类任务写入验收项指定的文件。关联观察不会自行恢复主动停止的 Task。

综合示例最后提交一条包含 `TODO` 的源项目变更。新的 Task ID 和召回结果会出现在输出中。重开运行时后，再对照 A 的 Task、Session 和记忆 ID。

[返回架构总览](architecture.md)
