"""ISO 时间文本列的日期 / 日期范围筛选（YYYY-MM-DD）。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str | None) -> str | None:
    text = (value or "").strip()
    if not _DATE_RE.match(text):
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _next_day(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def build_date_range(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """返回 (start_inclusive, end_exclusive)，用于 ISO 文本时间比较。"""
    d_from = _parse_date(date_from)
    d_to = _parse_date(date_to)
    if d_from and d_to:
        if d_from > d_to:
            d_from, d_to = d_to, d_from
        return d_from, _next_day(d_to)
    if d_from:
        return d_from, _next_day(d_from)
    if d_to:
        return d_to, _next_day(d_to)
    return None, None


def append_date_where(
    where: list[str],
    params: list,
    column: str,
    date_from: str | None,
    date_to: str | None,
) -> None:
    start, end_excl = build_date_range(date_from, date_to)
    if not start:
        return
    where.append(f"{column} >= ? AND {column} < ?")
    params.extend([start, end_excl])
