# 对比：todo / sub-agent / plan-mode（pi vs Claude Code）

状态：调研记录，供「三机制」实现取型参考。DemoClaw 的三机制（todo / delegate_subagent / plan-mode）已按 pi 语义落为扩展（见 strategies/、tools/），本文记录 pi 与 Claude Code 的实现差异及取型理由，便于后续调整。

## 三机制的对应关系

| 机制 | pi 实现 | Claude Code 实现 | 本质 |
|---|---|---|---|
| todo | 工具 `todo`（扩展 examples/extensions/todo.ts） | 工具 `TodoWrite`（内置，默认禁用，由 Task 工具四件套取代） | 一个让模型管理待办清单的工具 |
| sub-agent | 工具 `subagent`（扩展 examples/extensions/subagent） | 工具 `Agent`（内置，v2.1.63 由 Task 更名） | 派任务给隔离上下文子 agent |
| plan-mode | 扩展 `plan-mode`（examples/extensions/plan-mode），交互式开关 | 权限模式 `plan`（permission mode，非工具） | 只读探索 + 计划提取 + 进度 |

## 逐项差异

### 1. todo

| 维度 | pi | Claude Code |
|---|---|---|
| 载体 | 普通工具（模型经参数 action=list/add/toggle/clear 调用） | `TodoWrite` 工具，一次性写入整个 `todos` 数组 |
| 状态存放 | 写进 **tool result 的 details**，扫描会话 entry 重建——fork 分支时状态自动随历史正确 | 独立于对话的 harness 会话状态（`~/.claude/tasks/` 文件 + 转录里的普通 tool 调用镜像），跨 compaction 存活 |
| 交互面 | `/todos` 命令 + TUI 组件渲染 | `Ctrl+T` 任务列表视图 + `/tasks`（后者是后台 shell/subagent，非 todo 清单） |
| 取型 | **采纳 details 落盘**（DemoClaw 的 `ToolResult.details` 已落盘进 entry，天然支持） | 不采纳独立文件（教学规模进程内 + entry 重建足够） |

关键差异：pi 用「状态落 details + 扫描重建」达到**分支正确性**；Claude 用「harness 会话状态」达到**跨 compaction 存活**。DemoClaw 两者都要「分支正确」，故取 pi 的 details 落盘；跨重启重建留作增强（plan_mode.py 现为进程内状态，注释已声明）。

### 2. sub-agent

| 维度 | pi | Claude Code |
|---|---|---|
| 隔离模型 | **起独立 pi 进程**（spawn 子进程，完全隔离） | **独立上下文窗口**（同进程 agent loop，见 docs 对比：普通 subagent 在父进程内、context 隔离） |
| 结果回传 | 子进程 stdout 的 JSON 流解析，`final_response` 文本作为工具结果 | 父只收到最终 report（中间 tool 调用不可见），经 Agent 工具输出返回 |
| 模式 | single / parallel / chain（`{previous}` 占位链式） | 单一 agent 定义 + 前台/后台；fork 变体继承父会话 |
| 派生元数据 | 无（一次性 spawn） | agent 定义 frontmatter（tools/model/permissionMode/maxTurns/isolation） |
| 取型 | 采纳「新会话 + Task」一机制两来源（DemoClaw 单进程 asyncio，不 spawn 进程，见 multi-agent.md）| 采纳「父只见 final 报告」+「隔离上下文」语义 |

关键差异：pi 用**进程隔离**换绝对隔离；Claude 用**上下文窗口隔离**换轻量。DemoClaw 的技术立场是单进程 asyncio，故不做进程 spawn，取 Claude 的「上下文隔离 + final 报告」语义 + pi/multi-agent 的「新会话 + Task」机制。

### 3. plan-mode

| 维度 | pi | Claude Code |
|---|---|---|
| 本质 | 扩展（可开关的只读模式 + 计划提取 + 进度） | 权限模式（`plan`），在**权限层**拦截写入，不进系统提示 |
| 进入/退出 | `/plan` 命令 + `Ctrl+Alt+P` 快捷键 toggle | `Shift+Tab` / `/plan` 前缀 / 模型调 `EnterPlanMode`/`ExitPlanMode` |
| 工具约束 | disable write/edit 工具 + bash 只读白名单（before hook block）| `before_tool_call` 权限层拦截（写工具被 block），读工具放行 |
| 计划产物 | 模型产「Plan: 1. 2.」→ 提取 todo → 用户三选一（执行/留/改）→ `[DONE:n]` 标记 | 模型产计划 → `ExitPlanMode` 呈现给用户批准 → 批准即切权限模式执行 |
| 状态持久化 | `plan-mode` entry 落盘，session_start 恢复 | 权限模式本身是 session 状态（不落独立 entry） |
| 交互面 | TUI（`ctx.ui.select`/status/进度 widget） | 权限提示 UI + 编辑器预览计划 |
| 取型 | 采纳「能力本体」（只读白名单 + 计划提取 + `[DONE:n]`）；**交互面做成接口**（enter/execute/reset 方法，UI 层调用）| 采纳「批准这一步是接口而非 TUI」的边界（DemoClaw 无交互式 CLI）|

关键差异：pi 的 plan-mode 是**扩展 + 交互式开关**；Claude 的 plan 是**权限模式 + 批准流**。DemoClaw 取 pi 的「能力本体」（纯函数白名单/提取/进度），把「用户确认执行」这一个交互点做成 SDK 接口 `execute()`，交给 CLI/Web 各自触发——这正是本项目「执行策略/多 Agent 全部是扩展、交互界面是旁路」的定位。

## 取型总原则

1. **状态用 entry/details 落盘**（pi）而非独立文件（Claude）——与 DemoClaw「历史事实只来自 entry 流水（不变量 6）」一致。
2. **隔离用上下文窗口**（Claude）而非进程 spawn（pi）——与「单进程 asyncio」技术立场一致。
3. **交互面做成接口**（DemoClaw 定位），谁的 TUI/命令/快捷键（pi 的 `/plan`、Claude 的 `Shift+Tab`）都不照搬——第一版无交互式 CLI。

## 关联设计稿

- sub-agent → [multi-agent.md](./multi-agent.md)（delegate_subagent）
- plan-mode → [architecture.md](../architecture.md)「执行策略」节
- todo → tools.md 的「ToolResult.details 落盘」决策