# Session、上下文和恢复

以一个写入任务为例。第一次 Run 写出文件，随后宿主停止或崩溃。重开后，Task ID 和 Session ID 仍是原来的，Worker 从已保存的动作和文件状态判断下一步。恢复执行会使用新的 Run ID。 [恢复示例](../examples/resume_task.py)

## 什么会留下

状态目录中的 `agent.sqlite3` 保存 Task 合同、Run、工具动作和 Session 条目。一个宿主持有目录锁。可通过 `Agent.history(task_id)`、`Agent.runs(task_id)`、`Agent.actions(task_id)` 查看对应记录，CLI 用 `/history TASK` 查看 Session。会话里的工具调用 ID 来自模型，动作还有宿主分配的稳定 ID，后者可用于跨 Run 核对证据。 [TaskStore](../src/demoharness/core/storage.py)

| 记录 | 要回答的问题 |
| --- | --- |
| Task 与 Plan | 当前目标、授权、验收和步骤是什么？ |
| Run | 哪次尝试在推进，使用哪版配置，为什么结束？ |
| Action | 这个操作是否已经开始，是否已留下结果？ |
| Session 条目 | 当时收到了什么，发生了什么，最终交付了什么？ |
| 工作副本 | 实际写出了哪些文件，内容能否验证？ |

## Session 怎样变成模型上下文

`message_groups()` 从历史投影用户输入、助手输出、工具结果和验收失败反馈。一次包含工具调用的助手回复和对应的工具结果按组保留。结果还未齐全的组会先由执行循环处理，不会拼成残缺的模型消息。 [Session 投影](../src/demoharness/core/session.py)

当消息超过配置的窗口，或者模型报告长度限制时，循环可以把较早的完整组压成短摘要，并保留最近的组。原始记录仍可查。模型每轮同时收到当前目标、Plan、验收、写入范围和少量相关记忆，因而不会只依赖旧摘要来理解最新要求。 [上下文组装](../src/demoharness/core/loop.py)

## 两种恢复场景

主动停止会把 Task 保留为 `stopped`，重开宿主后仍需明确调用 `resume()`。正常关闭宿主会暂停忙碌的 Worker，正在执行的阻塞 I/O 退出后才释放宿主锁。进程意外退出时，下次打开把未结束的 Run 标记为中断，再使 Task 回到队列。 [停止与恢复入口](../src/demoharness/runtime.py) · [中断记录](../src/demoharness/core/storage.py)

```sh
uv run python examples/resume_task.py
```

从输出中核对主动停止、重开和恢复三个阶段。结束后应看到两次 Run、同一个 Task 和 Session，以及之前写入的记录和通过的验收。

进程突然退出时，写入可能已经落盘，结果还没来得及写进 SQLite。宿主先持久记录 `pending`、`started`，再执行文件写入。写入意图包含原始和目标内容的指纹，用来补足这段时间的不确定性。 [动作推进](../src/demoharness/core/loop.py) · [文件指纹](../src/demoharness/tools/files.py)

| 重开时看到什么 | 下一步 |
| --- | --- |
| 动作未开始 | 核对最新输入、权限和预算，决定执行或跳过。 |
| 已有结果 | 复用历史中的结果。 |
| 动作已开始，文件匹配目标指纹 | 按原动作 ID 补记结果。 |
| 动作已开始，文件仍匹配原指纹 | 权限、预算和最新输入允许时重试。 |
| 两个指纹都对不上 | 进入等待，交给使用者核对。 |

恢复测试会在写入前后向示例子进程发送 `SIGKILL`。它也验证新输入撤销旧要求后，不会重试旧写入。 [崩溃恢复测试](../tests/test_agent_runtime.py)

```sh
uv run pytest -q tests/test_agent_runtime.py -k crash
```

[返回架构总览](architecture.md)
