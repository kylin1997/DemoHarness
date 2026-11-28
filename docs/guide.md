# TinyHarness 教学指南

先做第 02 章，再从第 01 章回读源码。界面中的模型调用、工具结果、审批和压缩都能找到对应的处理函数。 [架构总览](architecture.md) · [代码地图](code-map/tinyharness-arch/tinyharness-arch.html)

<a id="chapter-01"></a>

## 01 · 最小 Agent Loop

核心循环反复做三件事。把消息和可用工具定义交给模型，执行它请求的工具，把每个结果按调用 ID 放回历史。模型收到环境反馈后决定下一步。它返回文本且没有工具请求时，循环收敛。轮数超出上限时，Harness 把结束原因交给页面。 [最小循环](../src/tinyharness/agent_loop.py)

<a id="chapter-02"></a>

## 02 · 快速体验

在仓库根目录运行以下命令，打开浏览器后选「离线演示」档案。

```sh
uv sync --locked
uv run tinyharness serve
```

[打开本机界面](http://127.0.0.1:8765)

| 操作 | 观察什么 |
| --- | --- |
| 新建会话，发送「核对 workspace 里的 notes.md」。 | 模型先发现 `glob`，找到文件后调用固定工具 `read`。右侧显示工具清单、结果回填和本轮上下文。 |
| 发送「请用命令打印 hello」，先拒绝，再重新发送并批准。 | 第一轮工具清单不含 `run_command`，模型通过 `tool_search` 找到它。右侧出现审批事件，拒绝成为工具观察，批准后结果出现 `hello`。 |
| 切到计划模式，发送「请生成一个可执行计划并检查 notes.md」。 | 右栏显示计划步骤和状态变化，执行会产生逐轮时序。 |
| 切回 ReAct，发送「让子 Agent 读取 notes.md 并总结」。 | 展开委派卡，查看子 Agent 的模型调用、工具结果和回给父 Agent 的结论。 |
| 发送「请记住：下次演示先检查 notes.md」，再发送「请搜索记忆：notes.md」。 | 查看记忆工具如何保存并取回会话中的文字。 |
| 新建会话，连续发九条普通短消息。 | 第九次发送时，历史窗口达到 17 条，超过 16 条的演示阈值。点击「历史已压缩」，查看旧窗口、保留条数和压缩后的估算 Token。 |
| 关闭服务，以相同状态目录重新启动。 | 左栏历史保留，打开会话可重看对话和执行记录。 |

界面中的模式与模型设置在下一条消息使用。当前执行按发起时的配置完成。离线模型用预先定义的回复稳定演示这些交互。 [模型实现](../src/tinyharness/models.py)

<a id="chapter-03"></a>

## 03 · 上下文构成

每轮发送系统提示、工具 Schema、技能索引和命中的技能全文、历史窗口及本轮输入。工具返回值带调用 ID，下一轮可以关联到具体请求。右栏按组成拆开 `context` 事件。估算 Token 用于观察变化，提供商返回的用量对应单次模型调用。 [上下文与时序事件](../src/tinyharness/harness.py)

<a id="chapter-04"></a>

## 04 · 时间线与 Session

`POST /api/chat` 流式返回事件。聊天区用文本增量更新，观测区显示模型调用、工具调用、守卫、审批和回填等公开事件。重开会话时，页面从 `GET /api/sessions/{session_id}` 读取消息和回放事件。 [HTTP/SSE 与会话接口](../src/tinyharness/application.py)

用默认配置运行服务，在会话里发一条消息。先用下面的命令查到对应标题的 `id`，它由 32 个十六进制字符组成。

```sh
curl -sS http://127.0.0.1:8765/api/sessions
```

把 `这里填会话ID` 换成刚查到的值，再查看两个文件。使用 `--state-dir` 时，把 `$HOME/.tinyharness` 换成实际状态目录。

```sh
session_dir="$HOME/.tinyharness/sessions/这里填会话ID"
cat "$session_dir/history.jsonl"
cat "$session_dir/state.json"
```

`history.jsonl` 的每行都有类型 `kind`、Unix 时间戳 `at` 和内容 `value`。找一行 `kind` 为 `message` 的记录，与聊天里的原始消息对照。再找一行 `event`，把其中 `value.type` 与右栏时序对照。使用记忆工具后还会有 `memory` 记录。`state.json` 记录会话信息、选项、模型窗口和信任的命令。重启服务后，可以打开同一个会话核对回放。 [Store](../src/tinyharness/state.py)

<a id="chapter-05"></a>

## 05 · 工具策略与搜索

固定清单包含 `tool_search`、`read` 和 `todo`。其他工具先通过关键词发现。对照时序中的首轮清单、搜索结果和下一轮清单。新发现的 Schema 在当前用户轮次内有效。策略目录提供文件五件套、命令、网页获取、记忆、任务列表、委派和已配置的 MCP 工具。 [ToolRegistry](../src/tinyharness/toolbox.py) · [工具协议](../src/tinyharness/protocol.py)

<a id="chapter-06"></a>

## 06 · 计划模式

ReAct 直接进入模型、工具和观察的循环。计划模式先请求结构化步骤，再运行同一个循环。执行中模型可以用 `todo` 更新步骤，右栏随 `plan` 事件变化。计划状态记录做法，工具结果提供执行证据。 [计划与执行](../src/tinyharness/harness.py)

<a id="chapter-07"></a>

## 07 · SubAgent 与记忆

主 Agent 发现并调用 `subagent`。子 Agent 从独立上下文出发，在只读工具范围内复用最小循环。它的事件在右栏按委派分组，结论以工具结果回填父 Agent。 `memory` 工具读写当前会话的持久记忆，父 Agent 可在后续轮次复用。 [委派边界](../src/tinyharness/harness.py) · [记忆存储](../src/tinyharness/state.py)

<a id="chapter-08"></a>

## 08 · 历史压缩

**教学规则。** 历史窗口超过 16 条时，TinyHarness 尝试把较早记录压成一条摘要，保留至少最近 6 条。这两个数让你在几轮短对话内就能观察压缩。窗口按记录计数，包括对话消息和工具往返。切分点落在一组工具请求与结果之间时，整组保留。当前用户请求及其工具往返也会完整保留。 [压缩实现](../src/tinyharness/harness.py)

**生产环境的常见规则。** 按模型的上下文窗口设置 Token 预算。统计系统提示词、工具定义、历史和当前输入的 Token，并给下一次输出留足空间。接近预算时，摘要较早的内容或裁剪过长的工具结果，同时保留当前任务、关键约束和最近交互。一条很长的工具结果也可能触发压缩。 [Claude · 按 Token 阈值压缩](https://platform.claude.com/docs/en/build-with-claude/compaction-threshold) · [OpenAI · Compaction](https://developers.openai.com/api/docs/guides/compaction)

TinyHarness 把压缩后的模型窗口写入 `state.json` 的 `context_window`，完整对话和压缩事件继续保存在 `history.jsonl`。面板对照压缩前后的条数和估算 Token。摘要调用本身也会产生模型用量。 [查看会话文件](#chapter-04)

<a id="chapter-09"></a>

## 09 · Skills

在 `skills/<技能名>/SKILL.md` 编辑名称、描述和触发词，重启后加载。平时注入简要索引。当 `match_input()` 命中本轮输入时，再加入对应的 L2 全文。运行快速体验的 notes 练习可以观察命中。 [Skills 加载](../src/tinyharness/skills.py) · [示例技能](../skills/demo/SKILL.md)

<a id="chapter-10"></a>

## 10 · MCP 与模型档案

应用从状态目录的 `config.json` 或 `--config` 指定的 JSON 文件装配 stdio 服务和模型档案。下面示范注册回显工具。启动命令用你当前环境里的 Python，脚本路径换成实际路径。

```json
{
  "mcp": [
    {"name": "demo", "command": "python3", "args": ["/absolute/path/to/examples/mcp_echo.py"]}
  ],
  "profiles": [
    {
      "id": "local-model",
      "label": "本机兼容接口",
      "base_url": "http://127.0.0.1:11434/v1",
      "model": "your-model",
      "api_key_env": "LOCAL_MODEL_KEY"
    }
  ]
}
```

只配置 MCP 时可删掉 `profiles`。用「请用 MCP 回显 hello」观察 `mcp__demo__echo` 如何经工具搜索出现在下一轮。stdio 服务以宿主权限启动，只配置你信任的程序。设置 `OPENAI_API_KEY` 可启用内置 OpenAI 兼容档案，`OPENAI_BASE_URL` 和 `MODEL` 可覆盖默认端点和模型。密钥由服务进程读取，配置面板只列档案名称。 [MCP 客户端](../src/tinyharness/mcp.py) · [档案加载](../src/tinyharness/application.py)

<a id="chapter-11"></a>

## 11 · 三层护栏与审批

`check_input()` 在模型调用前处理拦截，页面直接收到原因，当前请求没有模型用量。 `check_tool()` 在策略执行前检查工具和参数，拒绝原因作为观察回填。 `check_output()` 筛查对页面公开的输出。 [Guardrails](../src/tinyharness/guardrails.py)

白名单外的有效命令通过 `invoke_with_approval()` 发送 SSE `approval` 事件。用户经 `POST /api/approve` 返回 `allow`、`trust` 或 `deny`。宿主用空结果表示允许，用拒绝文字表示阻止。信任按会话和完整命令参数记录。命令在指定工作目录内启动，拥有启动进程所具备的系统权限。 [审批入口](../src/tinyharness/application.py)
