# 设计稿：web + scenarios（第 7 步）

状态：草案 v2（已按独立评审修订：场景复现性与会话树覆盖、Web 后端形态简化、POST 返回定义；run 复现锚点等留用户决策）。
依据：[architecture.md](../architecture.md)「Web 界面」「场景模拟」；[events-bots.md](./events-bots.md) EventBus；[botruntime-api.md](./botruntime-api.md)。

## Web 界面（web/，旁路体验工程）

**定位重申**：SDK 之上的薄壳，零业务逻辑。**后端用 BotRuntime 同步形态**（评审 C-5 采纳：http.server 线程 + 同步 run/result，不搞 async 双线程桥——SSE 是唯一需要跨线程的部分）。

### 后端（web/server.py，标准库 + SSE）

```python
class WebServer:
    """~200 行，运行在独立线程（持有自己简单的调度：同步调用 + SSE 轮询桥）：
    - GET /                       → index.html
    - GET /settings               → settings.html
    - GET /api/sessions           → runtime 会话列表
    - GET /api/sessions/<sid>/history | tasks → history() / session_tasks()
    - POST /api/bots/<name>/run   → bot.start(task) → 返回 {"task_id", "session_id"}（JSON，
                                     TaskHandle 不可序列化，投影为 id 对——评审 C-2 闭合）
    - POST /api/tasks/<tid>/steer | cancel → handle.steer/cancel
    - PUT  /api/sessions/<sid>/model      → 写会话层模型（sessions.model，events-bots 稿已补列）
    - GET /api/events?session=<sid>       → SSE：subscribe(sid) → chunked 推送
    SSE 断连处理：ThreadingHTTPServer（每请求一线程）；写失败（对端已死）→
    退订 Subscription → 线程结束。浏览器关 tab 由写超时自然清理。
    """
```

### 前端（web/static/，原生 HTML/CSS/JS）

```text
index.html      # 观察面：会话列表（running 标记）| 任务流（SSE）| 历史视图
settings.html   # 设置面：模型选择（PUT sessions/<sid>/model）+ bot spec 只读展示
style.css / app.js
```

**历史视图的诚实预算**（评审 C-4）：entry 树的完整树形渲染（分叉布局、连接线）在原生 JS 里是 400+ 行的真工程——**第一阶段做线性列表视图**（parent_id 数据在但渲染线性），树形可视化留给后续（API 已支持，纯前端增强）。phase 类事件按 events-bots 稿建议用覆盖式渲染。

**不做**：框架、构建、鉴权（127.0.0.1）、会话编辑。

## 场景模拟（scenarios/）

```text
scenarios/
├── long-research/         # 场景 1：长程调研（循环+工具+压缩；任务设计够长触发压缩）
├── interrupt-resume/      # 场景 2：中断恢复 + 会话树（kill→恢复→fork 对比续跑）
├── bot-tuning/            # 场景 3：bot 优化循环（任务→观察→改 AGENTS.md→再任务→对比）
└── multi-bot/             # 场景 4：多 bot 协作（架构师 bot 拆任务派给写手 bot / subagent，结果回传汇聚）
```

**场景 2 的可复现设计**（评审 D-1 修正——kill 时机必须确定性）：

- README 指令：**"看到第一个 tool_result 事件出现在终端输出后，Ctrl-C 中断"**（awaiting_tools 已落盘是合法中断窗口的确定性锚点，不依赖轮数猜测）
- 场景脚本打显式标记（"--- 现在可以中断 ---"）+ 对照说明（kill 早了：输入未落盘无 interrupted；kill 晚了：已终态无中断）
- 恢复后**补会话树演示**：fork 中断前的分支重跑一个修正版，对比两条分支（评审 D-3 的会话树覆盖缺口闭合——spec"综合运用特性"里的 fork/reset 至少落地一个场景）

**场景 3 的对比可判定设计**（评审 D-2 修正——真实模型噪声下"变好"不可判）：

- AGENTS.md 的改动是**可机械验证的约束**（如"回答必须以'【验证清单】'小节结尾，列出检查过的文件"）
- 两轮任务的对比证据 = 工具调用序列 + 产出文件 diff + 格式断言（regex 验证约束是否被遵守）——不靠"质量更好"的主观判断

**通用原则**：README 四件套（输入/预期过程/完成证据/复现步骤）；`export OPENAI_API_KEY` 前置声明；evidence/ 运行生成（gitignore，README 说明再生成方法）；场景脚本直接用 SDK（证明 SDK 自足）。

## 测试清单（验收标准）

1. Web：run/steer/cancel/SSE/历史各冒烟；POST /run 返回 id 对
2. Web 127.0.0.1 绑定
3. 场景 1：跑通 + 压缩触发证据（compaction_started 事件或 entry）
4. 场景 2：按 README 锚点中断 → 重启发现 → 续跑完成 → fork 对比可复现
5. 场景 3：两轮对比 + 格式断言通过
6. 换机复现（README 照做）

**场景 4 的可判定设计（多 bot 协作，随「多 Agent」进第一阶段）**：架构师 bot 接到一个可机械拆分的任务（如“写一份包含 N 个章节的文档 + 校验”），经分派工具拆出子任务派给写手 bot（派给另一 bot）与 subagent（临时会话）。对比证据走机械断言：最终产物包含 N 个章节且各章节由对应 bot 产出、子会话回传结果在父会话历史可追溯、分派/回传构成一条可读的溯源链——不靠“协作得好不好”的主观判断。此场景综合演示多 Agent 与两类后台 subagent（前台等待 / 后台不等待）。

## 留用户决策（已定案）

**分歧 7：场景数量与覆盖——已定四场景**（用户拍板：多 Agent 进第一阶段，故加「多 bot 协作」场景）。原“三场景”方案的超纲理由（多 Agent 未排期）不再成立。

## 评审要点（给用户）

1. 分歧 7；场景 2 的确定性锚点方案；历史视图线性起步的取舍
