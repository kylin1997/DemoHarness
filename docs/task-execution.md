# 任务如何执行和交付

跟着文件示例走一遍。目标是创建 `report.md`，验收条件是文件内容与指定文本逐字相等，写入范围也只有 `report.md`。这三个值分别回答要做什么、如何判断完成、允许改哪里。 [文件示例](../examples/verified_task.py)

```sh
uv run python examples/verified_task.py
```

## 1. 创建 Task 和工作副本

`Agent.submit()` 接收目标、`FileCheck` 和 `allowed_write_paths`，保存初始输入、Plan 和预算，并返回含 Task ID 的回执。聊天入口还需要判断消息属于哪件事。已知 Task ID 时，可以用 `send(..., task_id=...)` 直接补充要求。 [Agent API](../src/demoharness/runtime.py) · [消息归属](super-agent.md#消息如何找到-task)

Worker 认领 Task 时创建 Run。Git 项目从已记录的提交创建独立 worktree，普通目录先保存基线再复制。宿主在自己的状态目录里管理工作副本，源项目用于取基线。工作副本准备、文件操作和差异收集交给线程池，SQLite 状态仍由事件循环更新，因此慢操作期间其他 Task 可以推进。 [运行时](../src/demoharness/runtime.py) · [Workspace](../src/demoharness/tools/files.py)

## 2. 组装上下文，提出并执行工具操作

`run_loop()` 先处理历史中尚待结算的工具动作，再决定核对完成提议或发起新的模型请求。模型适配器接收 `ModelRequest`，包括提示、消息和当前角色的工具定义。可控模型和 OpenAI 兼容适配器使用同一种接口。 [执行循环](../src/demoharness/core/loop.py) · [OpenAI 适配器](../src/demoharness/providers/openai.py)

模型返回 `write_file` 调用后，宿主核对参数、Agent 的能力和 Task 的写入范围。文件路径须是工作副本内的相对路径。Task 级授权可以通过明确的用户输入修改，验收中提到的文件本身没有写入授权。观察任务默认只读，详细边界见主动发现一节。 [文件与授权](../src/demoharness/tools/files.py) · [主动发现](super-agent.md#git-提交如何变成工作)

| 看到的检查 | 实际检查的对象 |
| --- | --- |
| `allowed_write_paths=["report.md"]` | 允许模型的文件工具修改这个路径。 |
| `FileCheck("report.md", equals=...)` | 读取真实文件，比较精确内容。也支持文件存在、文本包含和静态 Python 语法。 |
| 交付范围检查 | 比较工作副本与基线，拒绝范围之外的变化。Git 副本也核对暂存区。 |

授权示例故意让模型多请求一次写入，读回文件和动作记录就能看到哪一次被拒绝。 [写入授权示例](../examples/write_scope.py)

```sh
uv run python examples/write_scope.py
```

## 3. 修订 Plan，按最新输入推进

Plan 用有序步骤表达当前做法。模型调用 `set_plan` 修订步骤，调用 `mark_step` 更新状态和证据，两者都要提交当前 `expected_revision`。宿主拒绝过期版本。修订时保留仍在计划里的步骤 ID，移除的步骤标为 `retired` 并保留证据。完整修订记录写进 Session。 [Plan 数据结构](../src/demoharness/core/plan.py) · [Plan 工具](../src/demoharness/tools/planning.py)

用户补充要求落盘后，Worker 会在模型返回、动作开始前或验收返回时核对输入时间。已经开始的动作记录结果，尚未执行且已过时的动作跳过。Plan 负责记录做法，实际验收仍取决于文件和范围检查。 [执行循环](../src/demoharness/core/loop.py)

## 4. 验收通过才交付

模型的完成文本进入宿主验收。宿主重新读取产物，执行定义好的 `FileCheck`，检查全部文件变化，并把检查结果、SHA-256 和差异放入 `Delivery`。检查失败且还有预算时，失败原因会进入下一轮上下文。没有可执行的验收条件时，Task 进入 `waiting`。验收通过后，可以查看 `task.delivery`，或者在 CLI 用 `/show TASK`。 [Task、CheckEvidence 和 Delivery](../src/demoharness/core/task.py)

运行文件示例时，可以对照正文、`passed` 和 diff 中的 `+The file check passed.`。三个值分别来自实际文件、宿主核对结果和 Git 差异。示例也断言源项目里没有 `report.md`。 [Session 如何保存这些记录](session-and-recovery.md)
