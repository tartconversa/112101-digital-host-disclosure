"""时间与日期解析工具。

来源资料中的时间统一为带时区的 ISO 8601 文本（例如 ``2026-07-05T10:00:00Z``），
采收窗口采用 ISO 8601 时间区间写法 ``起始日期/结束日期``。
仓库内部一律以 UTC 文本存储，保持字典序可比较。
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def parse_instant(text: str) -> datetime:
    """解析带时区的 ISO 8601 时间字符串，``Z`` 后缀按 UTC 处理。"""
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # 来源规范要求带时区；缺失时按 UTC 处理而不是静默使用本地时区。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def instant_text(text: str) -> str:
    """把来源时间规范化为 UTC ISO 8601 文本（秒精度，``Z`` 后缀）。"""
    parsed = parse_instant(text)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_day(text: str) -> date:
    """解析 ``YYYY-MM-DD`` 日期。"""
    return date.fromisoformat(text.strip())


def parse_harvest_window(text: str) -> tuple[date, date]:
    """解析 ``起始日期/结束日期`` 形式的采收承诺窗口，两端均含当天。"""
    start_text, sep, end_text = text.strip().partition("/")
    if not sep:
        raise ValueError(f"采收窗口缺少 '/' 分隔符: {text!r}")
    start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    if start > end:
        raise ValueError(f"采收窗口起始日晚于结束日: {text!r}")
    return start, end
