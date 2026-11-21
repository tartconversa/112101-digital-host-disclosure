"""把 ``src/`` 布局加入测试进程的导入路径（测试目录本身不装包）。"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
