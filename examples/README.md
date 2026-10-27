# DemoClaw 使用示例（examples/）

本目录存放**如何使用 DemoClaw SDK** 的最小可运行示例——面向刚上手的开发者，
每个示例演示一个能力，真实模型驱动（证明「功能不减配」）。

## 准备

先复制环境模板并填 API key：

```sh
cp examples/.env.example examples/.env   # 填 OPENAI_API_KEY
```

运行任意示例：

```sh
uv run python examples/quickstart.py
```

## 与 scenarios/ 的区别

- `examples/`：**API 上手**。短、单个能力、真实模型单任务。
- `scenarios/`（第 9 步）：**教学验证**。长、综合多特性、附四件套（输入/预期过程/完成证据/复现步骤）。

两者互补：examples 教你「怎么调用」，scenarios 证明「综合起来为什么需要它」。

## 示例清单

| 示例 | 演示的能力 |
|---|---|
| `quickstart.py` | 最小闭环：build → run → 读结果 → 会话树 |
| `steering.py` | follow-up（同一会话再 run，上下文接续） |
| `recovery.py` | 中断恢复（未终态 → 物化 interrupted → 发现 → 续跑） |
| `session_tree.py` | fork / reset / delete 会话树动词 |
| `tools_demo.py` | 工具三段式（read_file / write_file / run_command） |
| `todo.py` | todo 工具（计划清单，状态落 details） |
| `subagent.py` | delegate_subagent 多 Agent 分派 |
| `plan_mode.py` | plan-mode 策略（计划提取 + 进度标记） |
| `events.py` | task.events() 实时事件流 |
| `status.py` | 状态查询（sessions / session_tasks / task / sessions_with） |
| `bots.py` | 多 bot 隔离（各自世界 + bot_id 命名空间） |

## 运行时数据

示例产生的 db 存在 `examples/_data/<示例名>.db`（`_data/` 目录 git 忽略）。
真实模型配置经 `examples/.env`（不提交），模板见 `.env.example`。