from __future__ import annotations

from datetime import date
from typing import Any

from date_utils import (
    build_periods,
    month_bounds,
    normalize_date,
    normalize_granularity,
    normalize_month,
    period_key,
)
from dbmanager import DBmanager

_VALID_KINDS = {"expense", "income"}


class AnalyticsService:
    """负责统计口径、金额计算和诊断数据组装。"""

    def __init__(self, db: DBmanager):
        self._db = db

    def get_summary(
        self,
        start_date: str | date,
        end_date: str | date,
    ) -> dict[str, Any]:
        """统计日期范围内的收入、支出、结余和账目数量。"""
        start_date = normalize_date(start_date)
        end_date = normalize_date(end_date)

        if start_date > end_date:
            raise ValueError("start_date must not be later than end_date")

        with self._db.read_transaction() as conn:
            totals = self._db.fetch_summary_totals(
                conn,
                start_date,
                end_date,
            )

        return _build_summary(start_date, end_date, totals)

    def get_category_breakdown(
        self,
        start_date: str | date,
        end_date: str | date,
        kind: str = "expense",
    ) -> list[dict[str, Any]]:
        """按分类汇总指定收支类型的金额、笔数和占比。"""
        start_date = normalize_date(start_date)
        end_date = normalize_date(end_date)

        if start_date > end_date:
            raise ValueError("start_date must not be later than end_date")

        kind = _normalize_kind(kind)

        with self._db.read_transaction() as conn:
            rows = self._db.fetch_category_totals(
                conn,
                start_date,
                end_date,
                kind,
            )

        return _build_category_breakdown(rows)

    def get_trend(
        self,
        start_date: str | date,
        end_date: str | date,
        granularity: str = "day",
    ) -> list[dict[str, Any]]:
        """按日、周或月汇总收入与支出，并补齐空时间段。"""
        start = normalize_date(start_date)
        end = normalize_date(end_date)

        if start > end:
            raise ValueError("start_date must not be later than end_date")

        granularity = normalize_granularity(granularity)

        with self._db.read_transaction() as conn:
            daily_totals = self._db.fetch_daily_totals(conn, start, end)

        return _build_trend(start, end, granularity, daily_totals)

    def get_budget_status(
        self,
        month: str,
    ) -> list[dict[str, Any]]:
        """计算月度预算的执行金额、剩余金额和使用比例。"""
        month = normalize_month(month)
        start_date, end_date = month_bounds(month)

        with self._db.read_transaction() as conn:
            rows = self._db.fetch_budget_spending(
                conn,
                month,
                start_date,
                end_date,
            )

        return _build_budget_status(rows)

    def get_diagnosis_snapshot(
        self,
        month: str,
    ) -> dict[str, Any]:
        """在一个读事务中生成月度诊断所需的完整数据快照。"""
        month = normalize_month(month)
        start_date, end_date = month_bounds(month)

        with self._db.read_transaction() as conn:
            summary_totals = self._db.fetch_summary_totals(
                conn,
                start_date,
                end_date,
            )
            category_totals = self._db.fetch_category_totals(
                conn,
                start_date,
                end_date,
                "expense",
            )
            daily_totals = self._db.fetch_daily_totals(
                conn,
                start_date,
                end_date,
            )
            budget_spending = self._db.fetch_budget_spending(
                conn,
                month,
                start_date,
                end_date,
            )

        return {
            "month": month,
            "period": {
                "start_date": start_date,
                "end_date": end_date,
            },
            "summary": _build_summary(
                start_date,
                end_date,
                summary_totals,
            ),
            "category_breakdown": _build_category_breakdown(
                category_totals,
            ),
            "daily_trend": _build_trend(
                start_date,
                end_date,
                "day",
                daily_totals,
            ),
            "budget_status": _build_budget_status(budget_spending),
        }


def _normalize_kind(value: str) -> str:
    """校验统计使用的收入或支出类型。"""
    if not isinstance(value, str) or value not in _VALID_KINDS:
        raise ValueError("kind must be expense or income")

    return value


def _build_summary(
    start_date: str,
    end_date: str,
    totals: dict[str, int],
) -> dict[str, Any]:
    """用原始收支合计生成汇总结果。"""
    income = totals["total_income_cents"]
    expense = totals["total_expense_cents"]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_income_cents": income,
        "total_expense_cents": expense,
        "balance_cents": income - expense,
        "transaction_count": totals["transaction_count"],
    }


def _build_category_breakdown(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为分类金额计算占总金额的比例。"""
    total = sum(int(row["total_cents"]) for row in rows)

    return [
        {
            **row,
            "share_percent": (
                round(
                    int(row["total_cents"]) * 100 / total,
                    2,
                )
                if total
                else 0.0
            ),
        }
        for row in rows
    ]


def _build_trend(
    start: str,
    end: str,
    granularity: str,
    daily_totals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把每日原始金额归档到完整日、周或月周期。"""
    periods = build_periods(start, end, granularity)
    totals = {
        period: {
            "income_cents": 0,
            "expense_cents": 0,
        }
        for period in periods
    }

    for row in daily_totals:
        period = period_key(row["occurred_on"], granularity)
        totals[period]["income_cents"] += int(row["income_cents"])
        totals[period]["expense_cents"] += int(row["expense_cents"])

    return [
        {
            "period": period,
            "income_cents": totals[period]["income_cents"],
            "expense_cents": totals[period]["expense_cents"],
        }
        for period in periods
    ]


def _build_budget_status(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """根据预算和实际支出计算余额、使用率和超支状态。"""
    result = []

    for source in rows:
        item = dict(source)
        spent = int(item["spent_cents"])
        budget = int(item["amount_cents"])

        item["remaining_cents"] = budget - spent
        item["usage_ratio"] = (
            round(spent / budget, 4)
            if budget
            else None
        )
        item["over_budget"] = spent > budget
        result.append(item)

    return result
