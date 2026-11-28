# TinyHarness

一个可运行、可观察的 Agent Harness 入门项目

模型根据上下文选择工具，宿主检查权限并执行，工具结果回到下一轮模型请求。你可以在界面上跟踪这条链路，再用代码地图找到每一步的实现。

| 区域 | 可以做什么 |
| --- | --- |
| 左栏 · 会话 | 新建和切换会话，重启后重看消息和运行记录。 |
| 中栏 · 对话 | 查看流式回复、工具结果和审批卡。底部可切换 ReAct／计划模式、模型档案和推理强度，设置用于下一条消息。 |
| 右栏 · 运行观测 | 按轮查看时序、计划和子 Agent，核对上下文构成、Token 用量及压缩前后窗口。 |

## 运行

需要 Python 3.11+ 和 uv。文件工具使用 POSIX 接口，在 Linux 或 macOS 上运行。 [安装 uv](https://docs.astral.sh/uv/getting-started/installation/)

在仓库根目录运行下面两条命令。

```sh
uv sync --locked
uv run tinyharness serve
```

打开页面后选择「离线演示」。这个档案无需模型密钥，默认工作区里有一份可供读取的 `notes.md`。 [打开本机 TinyHarness](http://127.0.0.1:8765)

## 从一次审批开始

1. 在聊天框发送「请用命令打印 hello」。观察右栏第一轮的工具清单，以及 `tool_search` 如何让 `run_command` 出现在下一轮。
2. 审批卡弹出后，点击「允许一次」。对照右栏的批准事件、命令结果和下一轮模型回复。
3. 按快速体验继续练习计划、SubAgent、记忆和历史压缩。 [快速体验](docs/guide.md#chapter-02)

## 怎么学习

1. **先看现象。** 跑一遍快速体验，找出模型调用、审批卡和压缩横幅分别在什么时候出现。 [教学指南](docs/guide.md)
2. **沿图追源码。** 双击 `docs/code-map/tinyharness-arch/tinyharness-arch.html` 打开静态代码地图。十站导览会高亮对应节点和讲解卡。时序图可放大。点选一条箭头后，可从图下方打开对应章节。 [代码地图](docs/code-map/tinyharness-arch/tinyharness-arch.html)
3. **读最短的闭环。** 先读 `agent_loop()` 中的模型、工具和观察回填，再从架构总览选择感兴趣的扩展，例如工具搜索、Skills、MCP 或三层护栏。 [最小 Agent Loop](docs/guide.md#chapter-01) · [架构总览](docs/architecture.md)

## 会话、工作区与模型

每个会话保存在 `~/.tinyharness/sessions/<会话 ID>/`。消息、事件和记忆可直接用文本工具查看 `history.jsonl`，会话配置和模型窗口保存在 `state.json`。 [会话与回放](docs/guide.md#chapter-04)

默认工作目录为 `~/.tinyharness/workspace/`。每个状态目录同时只供一个应用进程使用。可用 `--state-dir` 和 `--workspace` 分开不同的练习，也可用 `--port` 调整端口。`uv run tinyharness serve --help` 列出所有启动选项。

真实模型走 OpenAI 兼容接口。可配置 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `MODEL`，或在配置文件里设置多个档案。stdio MCP 服务也从配置文件加载。 [模型档案和 MCP 示例](docs/guide.md#chapter-10)

TinyHarness 面向本机教学。用于生产前需要补齐进程与用户隔离、执行沙箱、可靠性、隐私、审计和模型评测。提交新需求和维护代码时，按开发规约中的 Poteto Mode 流程执行。 [生产化边界](docs/architecture.md#生产化边界) · [开发规约](AGENTS.md)
