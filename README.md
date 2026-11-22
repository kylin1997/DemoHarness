# DemoHarness

DemoHarness 是一个 Python 教学项目。它展示一个 Agent 怎样同时推进多件事、检查自己的产物，以及在中断后继续工作。每件事是一个 Task，拥有独立的 Session、动态 Plan 和工作副本。Agent 可以委派临时子 Agent，从任务证据中积累长期记忆，也可以自行发现新工作。

## 十分钟上手

准备 Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。当前文件工具使用 POSIX 接口，建议在 Linux 或 macOS 上运行。

```sh
uv sync
uv run python examples/verified_task.py
uv run pytest -q
```

示例使用可控模型和真实 Git 仓库。输入要求创建 `report.md`，你会看到 Task 与 Session ID、生成的正文、验收证据及 Git 差异。程序还会检查原仓库的文件没有被改动。整个示例使用临时目录，无需凭据。

Task 表示做什么，Session 记录发生过什么。示例中的两个 ID 相同，因为每件事只有一个业务 Session。Run 表示一次执行尝试，同一 Task 可以有多次 Run。

用综合示例观察两个 Task 同时推进。A 等待模型期间，B 会交付。A 收到补充后修改 Plan，委派子 Agent 并交付新内容。随后观察长期记忆、新提交触发的任务，以及重启后的状态。

```sh
uv run python examples/super_agent_demo.py
```

## 用真实模型聊天

设置 `OPENAI_API_KEY`。如使用 OpenAI 兼容服务，设置 `OPENAI_BASE_URL` 和 `MODEL`。这些变量由运行进程读取，可以在 shell 中配置。

```sh
uv run demoharness chat local --project /path/to/a/git/repository
```

试着说「创建 `notes.md`，内容为 `hello`，然后核对内容」。用 `/tasks` 查看 Task ID，用 `/show TASK` 查看 Plan、产物差异和验收证据。`/tell TASK MESSAGE` 把补充直接送到该 Task，`/history TASK` 展开记录，`/stop TASK` 停止，`/resume TASK` 继续。`/remember TEXT` 保存偏好，`/memory` 查看记忆。用 `/help` 查看命令。

默认运行目录是 `~/.demoharness`，可以用 `--state-dir` 指定干净的教学目录。Agent 会读 `agents/<ID>/SOUL.md` 和 `AGENTS.md` 作为身份与行为说明。使用 `--config` 可以加载多个 Agent。配置包含 `agents` 列表，每项提供 `id`、`project`，可另设 `root` 与 `model`。运行中用 `/agent ID` 切换。

程序运行期间会观察配置仓库的提交。Agent 可以自行创建发现的 Task。默认观察任务具有读取和内置检查权限。编辑只发生在各 Task 独立副本中，仅在用户或现有配置授权时进行。成果由用户审阅差异后决定如何合入。关闭 CLI 会暂停后台推进，重启时先核对已有记录和产物。

也可以用真实模型执行同一个文件示例，运行 `uv run python examples/verified_task.py --live`。这会调用配置的服务。

## 对照源码学习

1. 从 [examples/verified_task.py](examples/verified_task.py) 或 [cli.py](src/demoharness/cli.py) 找到公开 API，再读 [runtime.py](src/demoharness/runtime.py) 如何接收消息和分派 Task。
2. 读 [core/loop.py](src/demoharness/core/loop.py) 如何组装上下文、记录工具调用、验证和纠正。父 Task 和子 Agent 共用这条执行路径。
3. 选一个能力追踪到结果。读 [files.py](src/demoharness/tools/files.py) 看验收与文件指纹，读 [delegation.py](src/demoharness/tools/delegation.py) 看子上下文，或读 [memory.py](src/demoharness/memory.py) 看证据怎样在下一个 Task 中被召回。

[架构说明](docs/architecture.md)解释状态边界和恢复。[能力规格](docs/specs/agent-feature-scope.md)列出首版范围。[示例索引](examples/README.md)列出各机制对应的可运行检查。
