"""支持 ``python -m origin_fulfillment`` 运行命令行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
