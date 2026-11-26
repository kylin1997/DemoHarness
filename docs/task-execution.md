# 任务如何执行和交付

跟着文件示例走一遍。目标是创建 `report.md`，验收条件是文件内容与指定文本逐字相等，写入范围也只有 `report.md`。这三个值分别回答要做什么、如何判断完成、允许改哪里。 [文件示例](../examples/verified_task.py)

```sh
uv run python examples/verified_task.py
```

终端的 Session 条目按记录顺序排列。用下面的事件找源码里的调用点，再对照后面的说明。 [主流程时序图](architecture.md#主流程时序)

| 在终端找什么 | 到源码看什么 |
| --- | --- |
| `input` → `run_started` → `workspace_ready` | `Agent.submit()` 保存输入并调度，`AgentRuntime._work()` 认领 Task、开启 Run，准备工作副本。 [运行时和 Agent API](../src/demoharness/runtime.py) |
| 第一次 `model_started` → `action_pending` | `run_loop()` 组装 `ModelRequest`，调用 `AgentDefinition.complete`。本例的实现是 `scripted_model()`，首轮返回 `write_file`。 [执行循环](../src/demoharness/core/loop.py) · [可控模型](../examples/verified_task.py) |
| `action_started` → `action_result` | `_advance_action()` 先让 `FileTools` 核对权限，再记录动作开始、执行写入并保存结果。 [动作推进](../src/demoharness/core/loop.py) · [文件工具](../src/demoharness/tools/files.py) |
| 第二次 `model_started` → `verification_started` → `completed` | `scripted_model()` 从 `request.messages` 看到 `role="tool"` 的结果后提出完成。宿主调用 `_finish_task()` 运行验收，再把 `Delivery` 存入 `TaskStore`。 [可控模型](../examples/verified_task.py) · [宿主验收](../src/demoharness/runtime.py) · [TaskStore](../src/demoharness/core/storage.py) |

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

## 3. 修订动态工作流，按最新输入推进

Plan 是 Task 当前的步骤清单。Agent 根据清单、工具结果和新输入选择下一步。模型调用 `set_plan` 修订步骤，调用 `mark_step` 更新状态和证据，两者都要提交当前 `expected_revision`。宿主拒绝过期版本。修订时保留仍在计划里的步骤 ID，移除的步骤标为 `retired` 并保留证据。完整修订记录写进 Session。 [Plan 数据结构](../src/demoharness/core/plan.py) · [Plan 工具](../src/demoharness/tools/planning.py)

用户补充要求落盘后，Worker 会在模型返回、动作开始前或验收返回时核对输入时间。已经开始的动作记录结果，尚未执行且已过时的动作跳过。Plan 负责记录做法，实际验收仍取决于文件和范围检查。 [执行循环](../src/demoharness/core/loop.py)

## 4. 验收通过才交付

模型提出完成后，宿主执行 Task 已配置的验收条件，并检查文件变化是否在授权范围内。文件验收把证据、SHA-256 和差异放入 `Delivery`。检查失败且还有预算时，失败原因进入下一轮 Context。

没有配置自动验收条件时，宿主会保存交付物，Task 进入 `waiting`，原因为 `review_required`。可以在 CLI 用 `/show TASK` 查看交付，用 `/review TASK approve` 通过，或用 `/review TASK revise 修改要求` 退回给同一个 Task。自动验收器无法运行时，Task 会记录阻塞原因。 [Task、CheckEvidence 和 Delivery](../src/demoharness/core/task.py)

运行文件示例时，可以对照正文、`passed` 和 diff 中的 `+The file check passed.`。三个值分别来自实际文件、宿主核对结果和 Git 差异。示例也断言源项目里没有 `report.md`。 [Session 如何保存这些记录](session-and-recovery.md)

## 改一次验收条件

在示例的 `FileCheck` 中把 `equals=CONTENT` 改成 `equals=CONTENT + "\n"`，再运行同一命令。模型仍写入原来的内容。终端会先出现 `action_result`，随后出现 `verification_failed`，最后因验收次数用尽以 `failed` 结束。把验收条件恢复后再运行，核对 `completed`。 [修改这里的 FileCheck](../examples/verified_task.py)

想看模型根据验收反馈主动修正，可以运行这条测试。它让模型先写错，再根据反馈重写，并检查最终工作副本和交付证据。 [验收修正测试](../tests/test_agent_runtime.py)

```sh
uv run pytest -q tests/test_agent_runtime.py::test_file_task_corrects_failed_acceptance_and_delivers_worktree
```

## 接入自己的工具和验收器

教学 CLI 的计算任务可以作为模板。在 `AgentDefinition.tools` 注册 `RegisteredTool`，由 `ToolSpec` 描述工具及参数。宿主核对授权、校验参数，再调用实现并把 `ToolResult` 写入该 Task 的历史。工具声明能安全重放时，可使用 `ReplayPolicy.SAFE`。有未知副作用的调用在恢复时会等待核对。

在 `AgentDefinition.verifiers` 注册 `Verifier`，通过 `Check(id, verifier, params)` 选择验收器。调用方可以用 `Agent.submit(..., acceptance=[check])` 明确指定。验收器接收当前 Task、验收条件和交付文本，返回判定和证据说明。工具和验收器是宿主装入的 Python 代码。 [注册与运行示例](../src/demoharness/demo.py)
