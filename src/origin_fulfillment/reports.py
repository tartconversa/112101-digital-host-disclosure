"""把查询结果渲染为纯文本表格与 JSON，供离线命令行使用。"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from typing import Sequence


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render_table(rows: Sequence[sqlite3.Row | dict[str, object]]) -> str:
    """渲染等宽文本表格；无行时返回提示文本。"""
    if not rows:
        return "（无记录）"
    columns = list(rows[0].keys())
    matrix = [[("" if row[column] is None else str(row[column])) for column in columns] for row in rows]
    widths = [
        max(_display_width(column), *( _display_width(row[i]) for row in matrix))
        for i, column in enumerate(columns)
    ]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [border, "| " + " | ".join(_pad(column, widths[i]) for i, column in enumerate(columns)) + " |", border]
    for row in matrix:
        lines.append("| " + " | ".join(_pad(value, widths[i]) for i, value in enumerate(row)) + " |")
    lines.append(border)
    return "\n".join(lines)


def render_json(rows: Sequence[sqlite3.Row | dict[str, object]]) -> str:
    payload = [dict(row) for row in rows]
    return json.dumps(payload, ensure_ascii=False, indent=2)
