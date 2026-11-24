# 可运行的教学示例

先在仓库根目录执行 `uv sync --locked`。下列命令使用可控模型和临时项目，无需模型凭据。

| 想观察什么 | 运行命令 | 如何核对 |
| --- | --- | --- |
| 一个 Task 的完整交付 | `uv run python examples/verified_task.py` | `report.md` 内容逐字匹配，验收是 `passed`，交付包含 SHA-256 和 Git 差异。 |
| 归属普通消息，同时推进多件事 | `uv run python examples/super_agent_demo.py` | 看协调器创建 A、把补充关联到同一个 Task 和 Session，以及 B 如何先交付。随后核对 Plan 版本变化、委派和记忆来源。 |
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

```sh
uv run demoharness chat local --project /path/to/a/git/repository
```

可输入 `授权写入：notes.md | 创建 notes.md，内容为 hello，然后核对内容`。用 `/tasks` 查看 Task ID，`/show TASK` 查看 Plan、差异和验收证据，`/history TASK` 查记录，`/tell TASK MESSAGE` 补充要求。用 `/allow TASK PATH...` 授权已有 Task 写额外文件，其他命令见 `/help`。

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
