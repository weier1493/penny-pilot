from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

# 趋势统计支持的时间粒度。
_GRANULARITIES = {"day", "week", "month"}


def normalize_date(value: str | date) -> str:
    """把日期统一为 SQLite 可比较的 YYYY-MM-DD 字符串。"""
    if isinstance(value, datetime):
        value = value.date()

    if isinstance(value, date):
        return value.isoformat()

    if not isinstance(value, str):
        raise ValueError("date must be YYYY-MM-DD")

    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def normalize_month(value: str) -> str:
    """校验并返回 YYYY-MM 格式的月份。"""
    if (
        not isinstance(value, str)
        or len(value) != 7
        or value[4] != "-"
    ):
        raise ValueError("month must be YYYY-MM")

    try:
        date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise ValueError("month must be YYYY-MM") from exc

    return value


def normalize_granularity(value: str) -> str:
    """校验趋势统计的日、周、月粒度。"""
    if not isinstance(value, str) or value not in _GRANULARITIES:
        raise ValueError("granularity must be day, week, or month")

    return value


def month_bounds(month: str) -> tuple[str, str]:
    """返回某个月第一天和最后一天的日期字符串。"""
    month = normalize_month(month)
    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]

    return (
        f"{month}-01",
        f"{month}-{last_day:02d}",
    )


def build_periods(
    start: str,
    end: str,
    granularity: str,
) -> list[str]:
    """生成查询范围的日、周或月周期键。"""
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    periods = []

    if granularity == "day":
        current = start_date
        while current <= end_date:
            periods.append(current.isoformat())
            current += timedelta(days=1)
        return periods

    if granularity == "week":
        current = start_date - timedelta(days=start_date.weekday())
        while current <= end_date:
            periods.append(current.isoformat())
            current += timedelta(days=7)
        return periods

    current = start_date.replace(day=1)
    while current <= end_date:
        periods.append(current.strftime("%Y-%m"))

        if current.month == 12:
            current = current.replace(
                year=current.year + 1,
                month=1,
            )
        else:
            current = current.replace(month=current.month + 1)

    return periods


def period_key(value: str, granularity: str) -> str:
    """把具体日期映射到对应的日、周起始日或月份键。"""
    current = date.fromisoformat(value)

    if granularity == "day":
        return current.isoformat()

    if granularity == "week":
        return (current - timedelta(days=current.weekday())).isoformat()

    return current.strftime("%Y-%m")
