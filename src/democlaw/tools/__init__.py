"""DemoClaw 工具扩展目录（tools/）。

具体工具实现（读写文件、执行命令等）。内核 core/tools.py 只定协议；
本目录的工具是挂在内核注册表上的扩展。

内置教学工具见 _builtin.py：register_builtin_tools(registry) 装配期调用。
"""
