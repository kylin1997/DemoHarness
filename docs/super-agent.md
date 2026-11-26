# Super Agent 如何组织工作

试想你先让 Agent 写一份报告，期间又交代另一件事，最后补充第一件事的要求。需要区分消息归属、单件事的执行进度和跨任务记忆。综合示例把这些机制放在一次运行里。 [综合示例](../examples/super_agent_demo.py)

```sh
uv run python examples/super_agent_demo.py
```

## 消息如何找到 Task

不带 Task ID 的 `Agent.send()` 与 `Agent.receive_event()` 都先把输入写入协调日志。协调器给模型提供近期对话、候选 Task 和相关记忆，再保存回复、澄清、创建、关联或忽略的决定。指定 `task_id` 的补充直接进该 Task 的 Session。`submit()` 适合程序调用方已经知道目标和验收条件的情况。 [消息路由](../src/demoharness/coordination.py) · [Agent API](../src/demoharness/runtime.py)

示例给同一个 Agent 发送一条 `授权写入：a.txt | ...` 消息，协调器据此创建 A。A 等模型时，B 通过 `submit()` 独立完成。用户随后带 A 的 Task ID 发送新要求，并显式提交新的验收条件。对照输出中的输入 ID，可以看到两次输入进入同一个 Task 和 Session。 [综合示例](../examples/super_agent_demo.py)

Worker 会在动作边界读取补充。示例中，A 的旧模型响应在补充入库后才返回，Session 记录一条 `stale_response`，旧写入没有进入动作队列。新的模型调用将 Plan 从版本 1 修订到 2，输出会显示修订原因、过时响应的条目 ID 和唯一一次实际写入。对已完成的 Task 明确发起跟进时，系统也会保留历史交付并开始新的 Run。 [执行与交付](task-execution.md)

## 通用事件怎样触发工作

调用 `Agent.receive_event(source, event_id, text)`，或在 CLI 输入 `/event SOURCE ID TEXT`。同一个 Agent 按事件来源和 ID 去重。关联到已有 Task 后，事件作为新的 Session 输入，Agent 可以据此调整 Plan 或继续执行。已完成或主动停止的 Task 保留关联记录。明确修改目标或验收条件时，可用 CLI 的 `/contract` 或 Python 的 `Agent.send(..., task_id=..., goal=..., acceptance=...)`。

事件触发的工作使用宿主预先注册的工具。通用事件可写的文件路径来自 `AgentDefinition.event_write_paths`，已关联的 Task 沿用原授权。 [教学 CLI 事件演练](../examples/README.md#操作教学-cli)

## 子 Agent 接手小块调查

模型通过 `delegate` 给临时子 Agent 一段调查指令。子 Agent 有独立的模型上下文，以只读方式访问获准的工作副本文件，也能用 `read_parent_history` 按页查看父 Session。它受主任务预算和停止信号约束，另有单次委派的模型调用上限。结论和证据作为工具结果交回主 Task，主 Task 自己执行修改并承担验收。 [委派工具](../src/demoharness/tools/delegation.py)

子 Agent 的 Run 与动作记录按委派 ID 查询，生命周期随委派结束，长期记忆仍归主 Agent。综合示例会打印委派 ID 和子 Agent 读到的父 Session 补充条目 ID，可与 A 的补充记录对应。

## 有来源的记忆怎样跨 Task 使用

用户可显式保存偏好，也可在 Task 输入开头用 `项目事实：名称 = 内容` 或 `项目约束：名称 = 内容` 提供项目知识。运行时在阶段检查、Run 结束或重启后从 Session 中提炼经验。带通过检查证据的 Plan 步骤可形成有效经验，模型提出的普通决策可以留下为候选。 [记忆提炼](../src/demoharness/memory.py)

召回时按 Agent 和项目筛选，只把少量相关的有效记忆及来源注入模型请求。记忆可查看、更正和删除，过期项退出召回。示例先验收 `a.txt`，再打印记忆 ID 和 `task:<Task ID>:entry:<条目 ID>` 形式的来源。可用它定位原 Task 的 Session 交付记录。

## Git 提交如何变成工作

运行时定时读取本地源项目的 `HEAD`。首次读取时收集已提交快照里的路径和文本证据，之后在 `HEAD` 变化时收集变更证据。工作副本的输出留在副本里。源项目出现提交后，如果变更文件的路径、模式和前后对象 ID 与已有交付匹配，宿主会关联原 Task。其他变更交由协调器决定忽略、关联或建新 Task，问题键用于合并重复工作。决定、Task 变更和通知在同一事务落盘。 [Git 提交读取](../src/demoharness/discovery.py) · [轮询与分派](../src/demoharness/runtime.py)

观察触发的新 Task 默认可以读取和执行内置检查。项目可明确启用 `proactive_writes`，授权这类任务写入验收项指定的文件。关联观察不会自行恢复主动停止的 Task。

综合示例最后提交一条包含 `TODO` 的源项目变更。新的 Task ID 和召回结果会出现在输出中。重开运行时后，再对照 A 的 Task、Session 和记忆 ID。

[返回架构总览](architecture.md)
