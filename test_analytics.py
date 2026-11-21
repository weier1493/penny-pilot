from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from analytics import AnalyticsService
from dbmanager import DBmanager


class AnalyticsServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.db = DBmanager(Path(self._temp_dir.name) / "test.db")
        self.analytics = AnalyticsService(self.db)

        self.food = self.db.add_category("Food", "expense")
        self.salary = self.db.add_category("Salary", "income")

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_summary_and_category_breakdown(self) -> None:
        self.db.add_transaction(
            "expense",
            2500,
            self.food["id"],
            "2026-01-07",
        )
        self.db.add_transaction(
            "expense",
            1000,
            self.food["id"],
            "2026-01-08",
        )
        self.db.add_transaction(
            "income",
            10000,
            self.salary["id"],
            "2026-02-01",
        )

        summary = self.analytics.get_summary(
            "2026-01-01",
            "2026-02-28",
        )
        self.assertEqual(summary["total_income_cents"], 10000)
        self.assertEqual(summary["total_expense_cents"], 3500)
        self.assertEqual(summary["balance_cents"], 6500)
        self.assertEqual(summary["transaction_count"], 3)

        breakdown = self.analytics.get_category_breakdown(
            "2026-01-01",
            "2026-01-31",
        )
        self.assertEqual(len(breakdown), 1)
        self.assertEqual(breakdown[0]["category_name"], "Food")
        self.assertEqual(breakdown[0]["total_cents"], 3500)
        self.assertEqual(breakdown[0]["share_percent"], 100.0)

    def test_trend_periods(self) -> None:
        self.db.add_transaction(
            "expense",
            2500,
            self.food["id"],
            "2026-01-07",
        )

        weekly = self.analytics.get_trend(
            "2026-01-01",
            "2026-02-03",
            "week",
        )
        self.assertEqual(
            [item["period"] for item in weekly],
            [
                "2025-12-29",
                "2026-01-05",
                "2026-01-12",
                "2026-01-19",
                "2026-01-26",
                "2026-02-02",
            ],
        )
        self.assertEqual(weekly[1]["expense_cents"], 2500)

        monthly = self.analytics.get_trend(
            "2026-01-07",
            "2026-03-03",
            "month",
        )
        self.assertEqual(
            [item["period"] for item in monthly],
            ["2026-01", "2026-02", "2026-03"],
        )

    def test_budget_status_and_zero_budget(self) -> None:
        self.db.add_transaction(
            "expense",
            3500,
            self.food["id"],
            "2026-01-07",
        )
        self.db.set_budget(self.food["id"], "2026-01", 5000)

        item = self.analytics.get_budget_status("2026-01")[0]
        self.assertEqual(item["spent_cents"], 3500)
        self.assertEqual(item["remaining_cents"], 1500)
        self.assertEqual(item["usage_ratio"], 0.7)
        self.assertFalse(item["over_budget"])

        zero = self.db.add_category("Zero", "expense")
        self.db.set_budget(zero["id"], "2026-02", 0)
        item = self.analytics.get_budget_status("2026-02")[0]
        self.assertIsNone(item["usage_ratio"])
        self.assertFalse(item["over_budget"])

    def test_diagnosis_snapshot_uses_one_read_transaction(self) -> None:
        self.db.add_transaction(
            "expense",
            100,
            self.food["id"],
            "2026-01-01",
        )
        self.db.set_budget(self.food["id"], "2026-01", 1000)

        original = self.db.fetch_category_totals

        def fetch_after_concurrent_write(
            conn,
            start_date,
            end_date,
            kind,
        ):
            self.db.add_transaction(
                "expense",
                777,
                self.food["id"],
                "2026-01-02",
            )
            return original(conn, start_date, end_date, kind)

        self.db.fetch_category_totals = fetch_after_concurrent_write
        try:
            snapshot = self.analytics.get_diagnosis_snapshot("2026-01")
        finally:
            self.db.fetch_category_totals = original

        self.assertEqual(snapshot["summary"]["total_expense_cents"], 100)
        self.assertEqual(
            snapshot["category_breakdown"][0]["total_cents"],
            100,
        )
        self.assertEqual(snapshot["daily_trend"][1]["expense_cents"], 0)
        self.assertEqual(
            self.analytics.get_summary(
                "2026-01-01",
                "2026-01-31",
            )["total_expense_cents"],
            877,
        )

    def test_empty_statistics(self) -> None:
        summary = self.analytics.get_summary(
            "2026-01-01",
            "2026-01-31",
        )
        self.assertEqual(summary["transaction_count"], 0)
        self.assertEqual(
            self.analytics.get_category_breakdown(
                "2026-01-01",
                "2026-01-31",
            ),
            [],
        )
        self.assertEqual(self.analytics.get_budget_status("2026-01"), [])


if __name__ == "__main__":
    unittest.main()
