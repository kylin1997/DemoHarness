# DemoHarness

DemoHarness 是一个可观察的 Agent 工程教学项目。先在浏览器里和 Agent 对话，右栏会告诉你模型收到了什么、工具何时执行、结果如何推动下一轮。再沿代码地图找到触发这些行为的代码。

## 启动

准备 Python 3.11+ 和 uv。在仓库根目录运行下面两条命令。

```sh
uv sync --locked
uv run demoharness serve
```

打开 [本机 DemoHarness](http://127.0.0.1:8765)。默认是离线演示模型。在聊天框输入「请用命令打印 hello」，审批卡弹出后选择「仅本次允许」，观察右栏的调用、批准、执行和回传。更多练习覆盖 ReAct、计划、SubAgent、工具发现和压缩。 [快速体验](docs/guide.md#chapter-02)

## 阅读路径

1. 跑一遍快速体验，熟悉时序图、上下文构成和压缩面板。 [教学指南](docs/guide.md)
2. 双击下面的 HTML 打开代码地图。十站导览会在每一站打开对应的图、讲解卡和源码入口。 [交互式代码地图](docs/code-map/demoharness-arch/demoharness-arch.html)
3. 从架构总览跳到 `agent_loop()`，读模型回复、工具请求和观察怎样构成最小循环。 [架构总览](docs/architecture.md)

会话和长期记忆保存在 `~/.demoharness-v2/harness.sqlite3`，默认工作目录为同级的 `workspace/`。用 `--state-dir`、`--workspace`、`--skills` 和 `--config` 指定其他位置。模型档案可以通过配置文件接入。 [配置 OpenAI 兼容模型和 MCP](docs/guide.md#chapter-10)

项目用于本机教学。投入生产前需要完善用户与进程隔离、执行沙箱、可靠性、隐私、审计和模型评测。 [生产化边界](docs/architecture.md#生产化边界)
