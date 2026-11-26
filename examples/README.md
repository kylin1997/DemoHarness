# 可运行的教学示例

## 操作教学 CLI

按入门命令启动教学 CLI。这个场景以可控模型演示动态工作流和两种验收路径。 [启动命令](../README.md#开始运行)

1. 输入 `整理上线简报`。收到首份交付后，用 `/tasks` 找到任务 ID，用 `/plan TASK` 查看步骤清单，用 `/show TASK` 查看草稿和 `review_required`。
2. 输入 `/event calendar release-1 上线时间改为周五`。用 `/plan TASK` 查看新增的「核对日历更新」步骤，用 `/history TASK` 对照事件、修订原因和新版交付。再次输入同一事件 ID 可验证去重。
3. 用 `/review TASK approve 已核对上线日期` 验收新版。用 `/show TASK` 确认同一 Task 已进入 `completed`。需要退回时，用 `/review TASK revise 修改要求`。
4. 输入 `计算 12 + 30`。查看新 Task 的 `42` 和通过的自动验收。计算工具与文本验收器都通过 Agent 配置注册。 [教学模式源码](../src/demoharness/demo.py)

## 追踪具体机制

下列脚本使用可控模型和临时项目，无需模型凭据。

| 想观察什么 | 运行命令 | 如何核对 |
| --- | --- | --- |
| 一个 Task 的完整交付 | `uv run python examples/verified_task.py` | 按 Session 记录核对两次模型调用、`write_file` 和验收。交付包含 `passed`、SHA-256 和 Git 差异。 |
| 归属普通消息，同时推进多件事 | `uv run python examples/super_agent_demo.py` | 看协调器创建 A、用户如何明确更新 A 的验收条件，以及 B 如何先交付。随后核对 Plan 版本变化、委派和记忆来源。 |
| 用文件范围约束工具 | `uv run python examples/write_scope.py` | 在一个 Task 里核对获准写入、额外写入被拒、实际文件和交付证据。 |
| 停止、重开与恢复 | `uv run python examples/resume_task.py` | 刚重开时仍是 `stopped`，只有一个 Run。显式恢复后出现第二个 Run，复用已记录的写入结果。 |

从运行结果追到执行模块，可以沿着文档里的源码入口阅读。 [架构总览](../docs/architecture.md)

## 核对崩溃恢复

```sh
uv run pytest -q tests/test_agent_runtime.py -k crash
```

测试在文件写入前后向子进程发送 `SIGKILL`，核对按指纹补记结果、安全重试、等待核对，以及撤销旧写入后的行为。运行全量测试用 `uv run pytest -q`。 [Session、上下文和恢复](../docs/session-and-recovery.md)

## 使用真实模型聊天

设置 `OPENAI_API_KEY` 后运行下方命令。OpenAI 兼容服务可用 `OPENAI_BASE_URL` 和 `MODEL` 指定地址与模型。这些命令会调用配置的服务并可能产生费用。

在仓库根目录用一个空目录试跑，项目和状态分别存放。

```sh
demo_dir="$(mktemp -d)"
mkdir "$demo_dir/project"
uv run demoharness chat local --project "$demo_dir/project" --state-dir "$demo_dir/state"
```

输入 `授权写入：notes.md | 创建 notes.md，内容为 hello，然后核对内容`。用 `/tasks` 查看 Task ID 和状态，等 Task 进入 `completed` 后用 `/show TASK` 查看 `workspace` 路径、差异和验收证据。产物在 `workspace` 里。`/history TASK` 查看 Session 条目，`/exit` 退出。用同一个状态目录重开后仍可查看记录。

用 `/tell TASK MESSAGE` 补充要求，用 `/allow TASK PATH...` 授权已有 Task 写额外文件。其他命令见 `/help`。

用 `/contract TASK {"goal":"新的目标"}` 明确更新已有任务的目标。也可传入 `acceptance` 数组，例如 `[{"path":"notes.md","equals":"hello"}]`。

外部系统可通过 Python 的 `Agent.receive_event(source, event_id, text)` 提交事件。CLI 的 `/event SOURCE ID TEXT` 走同一个入口。通用事件的文件写入范围可在 Agent 配置的 `event_write_paths` 列出。

默认状态目录是 `~/.demoharness`，可用 `--state-dir` 更改。单 Agent 启动时，从该目录的 `agents/<ID>/` 读取可选的 `SOUL.md` 和 `AGENTS.md`。Git 项目默认观察提交，各 Task 的产物留在独立的工作副本。

也可以保存以下内容为 `agents.json`，运行 `uv run demoharness chat local --config agents.json`。配置中的 `project` 和 `root` 可相对于配置文件定位。可选 `model` 覆盖环境中的默认模型，用 `/agent review` 切换当前 Agent。

```json
{
  "agents": [
    {"id": "local", "project": "./repo", "root": "./profiles/local"},
    {"id": "review", "project": "./repo"}
  ]
}
```

用 `uv run python examples/verified_task.py --live` 可以查看第一个示例的真实模型路径。

[返回入门](../README.md)
