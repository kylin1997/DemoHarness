"""DemoClaw 内核（core/）。

内核只做四件事，每件事对应一个必然问题（架构文档「模块地图」）：

- ``loop.py``   任务循环骨架——Agent 必须行动；ReAct 循环 + stream_fn 注入点。
- ``session.py`` 会话拥有者——进程会中断；每个会话一个 FIFO 串行化一切写入。
- ``storage.py`` 存储层——SQLite 唯一实现；entry 树、事务、id 发号。
- ``task.py``   Task 状态机（纯函数库）——phase 推导、stop_reason、恢复物化。
- ``tools.py``  工具三段式协议 + 注册表——模型必须借助工具。
- ``events.py`` 事件流——界面的活体通道。

重要边界（架构「内核与扩展的边界」）：

- 依赖方向只有一个：**能力扩展 → core/，core/ 不 import 任何扩展**。
  所以本包不会 import ``tools/``、``compaction/``、``providers/`` 等扩展目录。
- 外部（Web、测试、教学示例）一律经 ``BotRuntime``（runtime.py 组合根）
  使用 DemoClaw，不直接 import 本包细节。

阅读动线：先 ``loop.py``（循环）→ 再 ``session.py`` + ``storage.py``（数据）
→ 任一扩展目录看注入点怎么被使用。
"""