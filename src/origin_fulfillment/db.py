"""SQLite 连接管理。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import SCHEMA_STATEMENTS


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）离线仓库并确保表结构存在。

    开启外键约束与 ``WITHOUT ROWID`` 友好的截断提交；调用方负责关闭连接。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()
    return conn
