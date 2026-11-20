# DemoHarness

## 这是什么？

大模型本身只会对话：给它一段文字，它回一段文字。可要让它完成一个真实任务——读几个文件、执行命令、中途失败后重试、进程被杀后从断点继续——就需要在模型外面搭一层工程。这层工程叫 **harness**（上下文管理 + 工具接口 + 约束 + 验证 + 纠正），**Agent = Model + Harness**（定义自李博杰《深入理解 AI Agent》）。

DemoHarness 是一个**完整的、教学目的的 Agent harness 实现**。它不是玩具 demo（harness 五要素不减配），也不追求生产级完备（多进程、部署、网关这些工程细节刻意不做）——约 3000 行 Python，恰好够读到长程 Agent 的全部必然机制：任务循环、工具执行、执行边界、中断恢复、会话树、预算与验证。

- 整体结构与设计取舍：[docs/architecture.md](docs/architecture.md)（怎么搭的）
- 能力范围与阶段划分：[docs/specs/agent-feature-scope.md](docs/specs/agent-feature-scope.md)（做什么）


## 长什么样？

SDK 形态，类似 pi 的 `createAgentSession`——几个调用就能驱动一个真实任务（摘自 [examples/quickstart.py](examples/quickstart.py)）：

```python
runtime = demo_runtime("quickstart")   # 装配存储、模型接入、bot 配置
arch = runtime.bot("architect")        # 取一个 bot（身份 + 指南 + 工具集）

result = arch.run("读一下 README.md，告诉我它是做什么的")
print(result.stop_reason, result.content)   # stop_reason 是终态权威，不凭模型自称

for s in runtime.sessions():           # 会话树：一切事实都是 entry，可回溯可分叉
    print(s.id, s.bot_id, s.active_entry_id)
```

模型自己决定是否调用工具、循环多少轮；harness 负责落盘每一步、限制边界、验证终态。

## 跑起来

**第一步（无需 API key）**：验证骨架可用，测试用 mock 模型、确定性、不花一分钱：

```sh
uv sync
uv run pytest
```

**第二步（真实模型）**：复制环境模板并填 key，然后逐个跑示例（真实模型调用，产生 API 费用）：

```sh
cp examples/.env.example examples/.env   # 填 OPENAI_API_KEY
uv run python examples/quickstart.py
```

模型配置链路：`OPENAI_API_KEY`（密钥，只经环境变量、不写源码）→ `OPENAI_BASE_URL`（可选，OpenAI 兼容端点）→ `MODEL`（可选，模型名）。11 个示例的清单见 [examples/README.md](examples/README.md)。

## 如何用这个工程学习？

每一步都回答一个具体疑问，按顺序走：

1. **演示什么能力？** 读 [docs/specs/agent-feature-scope.md](docs/specs/agent-feature-scope.md) 的能力域全景表——每个能力域对应长程 Agent 的一个必然问题（进程会中断所以要有恢复，上下文有限所以要有压缩），并标注了三个参考项目里的对应实现。
2. **整体怎么搭的？** 读 [docs/architecture.md](docs/architecture.md)——微内核结构：内核只做任务循环、会话存储、工具调用、状态提交四件事，其余全是注入点上的扩展。
3. **读懂一个循环**：读 [src/demoharness/core/loop.py](src/demoharness/core/loop.py)（唯一的真循环，ReAct 形态）。顺带两个相邻文件：工具三段式协议在 [core/tools.py](src/demoharness/core/tools.py)，Task 状态机与六态 stop_reason 在 [core/task.py](src/demoharness/core/task.py)。
4. **逐能力跑示例、对照源码**：跑一个示例，读它对应的那块源码，看行为与机制对上：
   - `tools_demo.py` ↔ `core/tools.py` + `tools/_builtin.py`（工具三段式）
   - `recovery.py` ↔ `core/task.py`（中断恢复、checkpoint）
   - `session_tree.py` ↔ `core/session.py` + `core/storage.py`（entry 树、fork/reset）
   - `steering.py`（运行中转向）· `events.py` ↔ `core/events.py`（事件流）· `status.py`（状态查询）
   - `bots.py` ↔ `bots/` 目录（多 bot 隔离）· `subagent.py` ↔ `tools/subagent.py`（任务分派）· `plan_mode.py` ↔ `strategies/plan_mode.py`（执行策略）· `todo.py` ↔ `tools/todo.py`
5. **深读单个模块**：`docs/design/` 下每个模块一份设计稿（含与 pi/nanobot 的对比和留下的取舍），读了源码有疑问来这找答案。
6. **进阶：对照参考项目**：`references/` 下有 pi（TypeScript，最小 agent harness）、nanobot（Python agent 框架）、hermes-agent 的完整源码，只读不 import。spec 表中每个能力域都标注了参考文件路径，可对照"成熟实现长什么样"。

测试（[tests/](tests/)）用 mock 模型保持确定性，也是"如何在没有真实模型的条件下测一个 agent harness"的示范。

## 目录

```text
DemoHarness/
├── README.md          # 本文件
├── AGENTS.md          # 开发规约（含新人上手指引）
├── docs/
│   ├── specs/         # 能力范围（做什么，唯一权威）
│   ├── design/        # 各模块设计稿（怎么做，含参考对比）
│   └── architecture.md# 整体结构
├── src/demoharness/
│   ├── runtime.py     # BotRuntime 组合根：build() 装配一切，暴露 SDK API
│   ├── core/          # 内核：loop（循环）· session/storage（会话与存储）·
│   │                  #        task（状态机）· tools（工具协议）· events（事件）
│   ├── tools/         # 工具实现（读写文件、subagent、todo）
│   ├── strategies/    # 执行策略扩展（plan-mode）
│   └── providers/     # 模型接入（OpenAI 兼容最小实现）
├── bots/              # bot 定义（每 bot：SOUL.md + AGENTS.md + spec.json）
├── examples/          # 11 个最小可运行示例（见 examples/README.md）
├── tests/             # 测试（mock 模型，确定性）
└── references/        # 参考项目源码（仅供阅读对照，git 忽略）
```

## 想改代码？

开发规约、设计准则与新人上手引导都在 [AGENTS.md](AGENTS.md)。
