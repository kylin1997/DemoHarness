"""DemoHarness 执行策略扩展目录（strategies/）。

执行策略（全部是扩展，不进内核）——architecture.md「执行策略」节。
本目录提供策略实现：

- plan_mode.py：Plan-and-Execute 的可交互只读形态（对齐 pi plan-mode 扩展）
  ——能力本体 + 接口，SDK 调用方经接口交互。

ReAct 是内核 loop.py 的默认形态（不在此目录）；其余策略（Plan-and-Execute /
Reflection / Multi-Agent）按需在此落扩展。
"""
