"""包的命令行入口，使 ``python -m tinyharness`` 使用与控制台命令相同的启动逻辑。"""

from .cli import main

raise SystemExit(main())
