# 跑一个教学场景

从仓库根目录运行。下表使用可控模型，无需模型凭据。

同目录里的 `quickstart.py`、`recovery.py` 等早期示例使用 `legacy_runtime.py`，供对照原 SDK。

| 场景 | 命令 | 应看到的结果 |
|---|---|---|
| 验证文件任务 | `uv run python examples/verified_task.py` | 临时 Git 副本生成 `report.md`，打印检查与差异。 |
| 综合场景 | `uv run python examples/super_agent_demo.py` | 先交付 B，再修订 A 并委派子 Agent，随后查看记忆与主动创建的 Task。正常关闭再打开后，原 Task 仍保有同一个 Session ID。 |
| 同时处理两件事 | `uv run pytest -q tests/test_agent_coordination.py` | A 等待时 B 完成，A 的补充改动生效。同一 Task 可在补齐验收后继续。 |
| 临时子 Agent | `uv run pytest -q tests/test_agent_delegation.py` | 父子拥有独立上下文，预算和中断记录可核对。 |
| 记忆与主动发现 | `uv run pytest -q tests/test_super_agent.py tests/test_agent_memory.py` | 验收证据被提炼和召回，本地新提交可触发新工作，重复提交不重复建 Task。 |

运行全部测试用 `uv run pytest -q`。想按现象追到源码，读[系统设计](../docs/architecture.md)。

## 观察崩溃恢复

```sh
uv run pytest -q tests/test_agent_runtime.py -k crash
```

测试在文件写入前后向运行中的子进程发送 `SIGKILL`。它检查重启后三种处理：核对已写出的结果并补记、安全重试、等待人工核对。它也验证用户撤销的旧写入不会重放。

## 进阶：用真实模型聊天

设置 `OPENAI_API_KEY` 后运行下方命令。OpenAI 兼容服务可用 `OPENAI_BASE_URL` 和 `MODEL` 指定地址与模型。也可以运行 `uv run python examples/verified_task.py --live` 查看同一示例的真实模型路径。这些命令会调用配置的服务。

```sh
uv run demoharness chat local --project /path/to/a/git/repository
```

可以输入 `授权写入：notes.md | 创建 notes.md，内容为 hello，然后核对内容`。用 `/tasks` 取得 Task ID，`/show TASK` 查看 Plan、差异与验收证据，`/history TASK` 看输入和工具记录，`/tell TASK MESSAGE` 补充要求。要授权额外文件，使用 `/allow TASK PATH...`。其余命令见 `/help`。

默认运行目录是 `~/.demoharness`，可用 `--state-dir` 更改。单 Agent 启动时，从该目录的 `agents/<ID>/` 读取可选的 `SOUL.md` 和 `AGENTS.md`。Git 项目默认观察提交，观察发起的 Task 默认可以读取并运行内置检查。各 Task 的产物留在独立副本里。

用 `--config` 加载多个 Agent，例如保存以下内容为 `agents.json`，然后运行 `uv run demoharness chat local --config agents.json`。配置中的 `project` 和提示文件目录 `root` 可相对于配置文件定位，`root` 省略时用 Agent ID 作为目录名。可选的 `model` 覆盖环境中的默认模型。用 `/agent review` 切换身份。

```json
{
  "agents": [
    {"id": "local", "project": "./repo", "root": "./profiles/local"},
    {"id": "review", "project": "./repo"}
  ]
}
```

[返回入门](../README.md)
