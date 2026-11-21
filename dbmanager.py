from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from date_utils import (
    build_periods,
    month_bounds,
    normalize_date,
    normalize_granularity,
    normalize_month,
    period_key,
)

# SQLite 等待其他连接释放锁的最长时间，单位是毫秒。
_BUSY_TIMEOUT_MS = 5000

# 已有数据库文件必须包含这些列，否则在初始化阶段直接报错。
_REQUIRED_COLUMNS = {
    "categories": {
        "id",
        "name",
        "kind",
        "is_active",
        "created_at",
        "updated_at",
    },
    "transactions": {
        "id",
        "kind",
        "amount_cents",
        "category_id",
        "occurred_on",
        "note",
        "created_at",
        "updated_at",
    },
    "budgets": {
        "id",
        "category_id",
        "month",
        "amount_cents",
        "created_at",
        "updated_at",
    },
}


class DBmanager:
    """负责文件型 SQLite 数据库初始化和账目数据访问。"""

    # 初始化与连接管理

    def __init__(self, db_path: str | Path = "pennypilot.db"):
        """解析数据库路径、创建父目录并初始化数据库结构。"""
        if str(db_path).strip() == ":memory:":
            raise ValueError("DBmanager only supports file databases")

        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        """创建数据库表和索引，并检查已有文件的结构是否兼容。"""
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row

        try:
            # 外键约束是按连接启用的，因此每个新连接都要重新设置。
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")

            # WAL 允许读取和写入更好地并发进行。
            conn.execute("PRAGMA journal_mode = WAL").fetchone()

            # 将建表和建索引放在同一事务中，任一步失败都会整体回滚。
            conn.execute("BEGIN IMMEDIATE")

            # 分类名称在同一收支类型内不区分大小写地唯一。
            conn.execute(
                "CREATE TABLE IF NOT EXISTS categories ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL COLLATE NOCASE, "
                "kind TEXT NOT NULL CHECK (kind IN ('expense', 'income')), "
                "is_active INTEGER NOT NULL DEFAULT 1 "
                "CHECK (is_active IN (0, 1)), "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, "
                "UNIQUE (kind, name COLLATE NOCASE))"
            )

            # 金额统一存整数分，避免浮点数产生精度误差。
            conn.execute(
                "CREATE TABLE IF NOT EXISTS transactions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "kind TEXT NOT NULL CHECK (kind IN ('expense', 'income')), "
                "amount_cents INTEGER NOT NULL CHECK (amount_cents > 0), "
                "category_id INTEGER NOT NULL "
                "REFERENCES categories(id) ON DELETE RESTRICT, "
                "occurred_on TEXT NOT NULL, "
                "note TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )

            # 每个分类在每个月份最多有一条预算。
            conn.execute(
                "CREATE TABLE IF NOT EXISTS budgets ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "category_id INTEGER NOT NULL "
                "REFERENCES categories(id) ON DELETE RESTRICT, "
                "month TEXT NOT NULL, "
                "amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0), "
                "created_at TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, "
                "UNIQUE (category_id, month))"
            )

            # 索引分别服务于日期筛选、分类查询、收支类型查询和月度预算。
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_date "
                "ON transactions(occurred_on)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_category_date "
                "ON transactions(category_id, occurred_on)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_kind_date "
                "ON transactions(kind, occurred_on)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_budgets_month "
                "ON budgets(month)"
            )
            self._validate_schema(conn)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """为一次数据库操作提供自动回滚和关闭的连接。"""
        conn = self._open_connection()
        try:
            yield conn
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        """确认已有表包含当前代码依赖的全部列。"""
        for table, required_columns in _REQUIRED_COLUMNS.items():
            rows = conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
            existing_columns = {row["name"] for row in rows}
            missing_columns = required_columns - existing_columns

            if missing_columns:
                missing = ", ".join(sorted(missing_columns))
                raise RuntimeError(
                    "incompatible database schema: "
                    f"{table} is missing column(s): {missing}"
                )

    def _open_connection(self) -> sqlite3.Connection:
        """打开新连接，并设置统一的行类型和外键策略。"""
        conn = sqlite3.connect(
            self.db_path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return conn

    # 增：创建分类和账目

    def add_category(self, name: str, kind: str) -> dict[str, Any]:
        """新增一个启用状态的收入或支出分类。"""
        name = self._normalize_name(name)
        kind = self._validate_kind(kind)
        now = self._now()

        with self._connection() as conn:
            # 内层连接上下文在成功时提交，在异常时回滚。
            with conn:
                cursor = conn.execute(
                    "INSERT INTO categories "
                    "(name, kind, is_active, created_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (name, kind, now, now),
                )
                category_id = int(cursor.lastrowid)

            return self._fetch_category(conn, category_id)

    def add_transaction(
        self,
        kind: str,
        amount_cents: int,
        category_id: int,
        occurred_on: str | date,
        note: str = "",
    ) -> dict[str, Any]:
        """新增账目，并校验金额、日期、分类类型和启用状态。"""
        kind = self._validate_kind(kind)
        amount_cents = self._validate_amount(
            amount_cents,
            allow_zero=False,
        )
        category_id = self._validate_id(category_id, "category_id")
        occurred_on = normalize_date(occurred_on)
        note = self._normalize_note(note)
        now = self._now()

        with self._connection() as conn:
            with conn:
                # 外键只检查分类是否存在，这里额外检查类型和启用状态。
                self._fetch_category(
                    conn,
                    category_id,
                    required_kind=kind,
                    require_active=True,
                )
                cursor = conn.execute(
                    "INSERT INTO transactions "
                    "(kind, amount_cents, category_id, occurred_on, "
                    "note, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        kind,
                        amount_cents,
                        category_id,
                        occurred_on,
                        note,
                        now,
                        now,
                    ),
                )
                transaction_id = int(cursor.lastrowid)

            return self._fetch_transaction(conn, transaction_id)

    # 删：删除账目和预算

    def delete_transaction(self, transaction_id: int) -> None:
        """按 ID 删除账目，记录不存在时抛出异常。"""
        transaction_id = self._validate_id(
            transaction_id,
            "transaction_id",
        )

        with self._connection() as conn:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM transactions WHERE id = ?",
                    (transaction_id,),
                )
                if cursor.rowcount == 0:
                    raise LookupError("transaction not found")

    def delete_budget(
        self,
        category_id: int,
        month: str,
    ) -> None:
        """删除指定分类和月份对应的预算。"""
        category_id = self._validate_id(category_id, "category_id")
        month = normalize_month(month)

        with self._connection() as conn:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM budgets "
                    "WHERE category_id = ? AND month = ?",
                    (category_id, month),
                )
                if cursor.rowcount == 0:
                    raise LookupError("budget not found")

    # 改：修改记录、状态和预算

    def rename_category(
        self,
        category_id: int,
        name: str,
    ) -> dict[str, Any]:
        """修改分类名称，并同步更新时间。"""
        category_id = self._validate_id(category_id, "category_id")
        name = self._normalize_name(name)

        with self._connection() as conn:
            with conn:
                cursor = conn.execute(
                    "UPDATE categories "
                    "SET name = ?, updated_at = ? "
                    "WHERE id = ?",
                    (name, self._now(), category_id),
                )
                if cursor.rowcount == 0:
                    raise LookupError("category not found")

            return self._fetch_category(conn, category_id)

    def set_category_active(
        self,
        category_id: int,
        active: bool,
    ) -> dict[str, Any]:
        """启用或停用分类；停用不会删除分类及其历史数据。"""
        category_id = self._validate_id(category_id, "category_id")

        if not isinstance(active, bool):
            raise ValueError("active must be a bool")

        with self._connection() as conn:
            with conn:
                cursor = conn.execute(
                    "UPDATE categories "
                    "SET is_active = ?, updated_at = ? "
                    "WHERE id = ?",
                    (int(active), self._now(), category_id),
                )
                if cursor.rowcount == 0:
                    raise LookupError("category not found")

            return self._fetch_category(conn, category_id)

    def update_transaction(
        self,
        transaction_id: int,
        *,
        kind: str | None = None,
        amount_cents: int | None = None,
        category_id: int | None = None,
        occurred_on: str | date | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """部分更新账目；类型或分类变化时重新校验二者是否匹配。"""
        transaction_id = self._validate_id(
            transaction_id,
            "transaction_id",
        )

        if all(
            value is None
            for value in (
                kind,
                amount_cents,
                category_id,
                occurred_on,
                note,
            )
        ):
            raise ValueError("at least one field must be provided")

        if kind is not None:
            kind = self._validate_kind(kind)

        if amount_cents is not None:
            amount_cents = self._validate_amount(
                amount_cents,
                allow_zero=False,
            )

        if category_id is not None:
            category_id = self._validate_id(category_id, "category_id")

        if occurred_on is not None:
            occurred_on = normalize_date(occurred_on)

        if note is not None:
            note = self._normalize_note(note)

        with self._connection() as conn:
            with conn:
                current = conn.execute(
                    "SELECT kind, category_id "
                    "FROM transactions WHERE id = ?",
                    (transaction_id,),
                ).fetchone()

                if current is None:
                    raise LookupError("transaction not found")

                final_kind = (
                    kind if kind is not None else current["kind"]
                )
                final_category_id = (
                    category_id
                    if category_id is not None
                    else current["category_id"]
                )

                # 只有类型或分类发生变化时才需要重新检查组合合法性。
                if kind is not None or category_id is not None:
                    self._fetch_category(
                        conn,
                        final_category_id,
                        required_kind=final_kind,
                        require_active=True,
                    )

                changes = []
                values: list[Any] = []

                for column, value in (
                    ("kind", kind),
                    ("amount_cents", amount_cents),
                    ("category_id", category_id),
                    ("occurred_on", occurred_on),
                    ("note", note),
                ):
                    if value is not None:
                        changes.append(f"{column} = ?")
                        values.append(value)

                changes.append("updated_at = ?")
                values.extend([self._now(), transaction_id])

                conn.execute(
                    "UPDATE transactions SET "
                    + ", ".join(changes)
                    + " WHERE id = ?",
                    values,
                )

            return self._fetch_transaction(conn, transaction_id)

    def set_budget(
        self,
        category_id: int,
        month: str,
        amount_cents: int,
    ) -> dict[str, Any]:
        """新增或更新某支出分类的月度预算。"""
        category_id = self._validate_id(category_id, "category_id")
        month = normalize_month(month)
        amount_cents = self._validate_amount(
            amount_cents,
            allow_zero=True,
        )
        now = self._now()

        with self._connection() as conn:
            with conn:
                self._fetch_category(
                    conn,
                    category_id,
                    required_kind="expense",
                    require_active=True,
                )
                conn.execute(
                    # 同一分类和月份只能有一条预算，冲突时更新金额。
                    "INSERT INTO budgets "
                    "(category_id, month, amount_cents, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(category_id, month) DO UPDATE SET "
                    "amount_cents = excluded.amount_cents, "
                    "updated_at = excluded.updated_at",
                    (
                        category_id,
                        month,
                        amount_cents,
                        now,
                        now,
                    ),
                )

            return self._fetch_budget(conn, category_id, month)

    # 查：查询记录、预算和统计数据

    def list_categories(
        self,
        kind: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """按收支类型筛选分类，默认只返回启用分类。"""
        if kind is not None:
            kind = self._validate_kind(kind)

        if not isinstance(active_only, bool):
            raise ValueError("active_only must be a bool")

        conditions = []
        params: list[Any] = []

        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)

        if active_only:
            conditions.append("is_active = 1")

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, name, kind, is_active, created_at, updated_at "
                "FROM categories"
                + where
                + " ORDER BY kind, id",
                params,
            ).fetchall()

            return [self._category_dict(row) for row in rows]

    def get_transaction(
        self,
        transaction_id: int,
    ) -> dict[str, Any]:
        """按 ID 查询一条账目及其分类信息。"""
        transaction_id = self._validate_id(
            transaction_id,
            "transaction_id",
        )

        with self._connection() as conn:
            return self._fetch_transaction(conn, transaction_id)

    def list_transactions(
        self,
        start_date: str | date | None = None,
        end_date: str | date | None = None,
        kind: str | None = None,
        category_id: int | None = None,
        keyword: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按日期、类型、分类和关键词筛选账目并分页返回。"""
        if start_date is not None:
            start_date = normalize_date(start_date)

        if end_date is not None:
            end_date = normalize_date(end_date)

        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date must not be later than end_date")

        if kind is not None:
            kind = self._validate_kind(kind)

        if category_id is not None:
            category_id = self._validate_id(category_id, "category_id")

        if keyword is not None and not isinstance(keyword, str):
            raise ValueError("keyword must be a string")

        limit, offset = self._validate_paging(limit, offset)

        conditions = []
        params: list[Any] = []

        if start_date:
            conditions.append("t.occurred_on >= ?")
            params.append(start_date)

        if end_date:
            conditions.append("t.occurred_on <= ?")
            params.append(end_date)

        if kind:
            conditions.append("t.kind = ?")
            params.append(kind)

        if category_id is not None:
            conditions.append("t.category_id = ?")
            params.append(category_id)

        if keyword and keyword.strip():
            # 转义通配符，避免用户输入的 % 和 _ 改变 LIKE 的含义。
            pattern = f"%{self._escape_like(keyword.strip())}%"
            conditions.append(
                "(t.note LIKE ? ESCAPE '\\' "
                "OR c.name LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern])

        where = (
            f" WHERE {' AND '.join(conditions)}"
            if conditions
            else ""
        )
        base = (
            " FROM transactions AS t "
            "JOIN categories AS c ON c.id = t.category_id"
            + where
        )

        with self._connection() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*)" + base,
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                self._transaction_select()
                + where
                + " ORDER BY t.occurred_on DESC, t.id DESC "
                + "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()

            return {
                "items": [
                    self._transaction_dict(row)
                    for row in rows
                ],
                "total": total,
            }

    def get_budget(
        self,
        category_id: int,
        month: str,
    ) -> dict[str, Any]:
        """按分类 ID 和月份查询一条预算。"""
        category_id = self._validate_id(category_id, "category_id")
        month = normalize_month(month)

        with self._connection() as conn:
            return self._fetch_budget(conn, category_id, month)

    def list_budgets(self, month: str) -> list[dict[str, Any]]:
        """查询指定月份的全部预算并附带分类信息。"""
        month = normalize_month(month)

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT "
                "b.id, b.category_id, "
                "c.name AS category_name, "
                "c.kind AS category_kind, "
                "c.is_active AS category_active, "
                "b.month, b.amount_cents, "
                "b.created_at, b.updated_at "
                "FROM budgets AS b "
                "JOIN categories AS c ON c.id = b.category_id "
                "WHERE b.month = ? "
                "ORDER BY c.name COLLATE NOCASE, b.id",
                (month,),
            ).fetchall()

            return [self._budget_dict(row) for row in rows]

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

        with self._connection() as conn:
            return self._summary_with_connection(
                conn,
                start_date,
                end_date,
            )

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

        kind = self._validate_kind(kind)

        with self._connection() as conn:
            return self._category_breakdown_with_connection(
                conn,
                start_date,
                end_date,
                kind,
            )

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

        with self._connection() as conn:
            return self._trend_with_connection(
                conn,
                start,
                end,
                granularity,
            )

    def get_budget_status(
        self,
        month: str,
    ) -> list[dict[str, Any]]:
        """计算月度预算的执行金额、剩余金额和使用比例。"""
        month = normalize_month(month)

        with self._connection() as conn:
            return self._budget_status_with_connection(conn, month)

    def get_diagnosis_snapshot(
        self,
        month: str,
    ) -> dict[str, Any]:
        """在一个读事务中生成月度诊断所需的完整数据快照。"""
        month = normalize_month(month)
        start_date, end_date = month_bounds(month)

        with self._connection() as conn:
            with conn:
                # 固定读取快照，确保四部分统计来自同一数据时点。
                conn.execute("BEGIN")
                return {
                    "month": month,
                    "period": {
                        "start_date": start_date,
                        "end_date": end_date,
                    },
                    "summary": self._summary_with_connection(
                        conn,
                        start_date,
                        end_date,
                    ),
                    "category_breakdown": (
                        self._category_breakdown_with_connection(
                            conn,
                            start_date,
                            end_date,
                            "expense",
                        )
                    ),
                    "daily_trend": self._trend_with_connection(
                        conn,
                        start_date,
                        end_date,
                        "day",
                    ),
                    "budget_status": (
                        self._budget_status_with_connection(
                            conn,
                            month,
                        )
                    ),
                }

    # 查询内部辅助

    @staticmethod
    def _transaction_select() -> str:
        """返回账目查询公共 SELECT，并连接分类表补充分类信息。"""
        return (
            "SELECT "
            "t.id, t.kind, t.amount_cents, t.category_id, "
            "c.name AS category_name, "
            "c.kind AS category_kind, "
            "c.is_active AS category_active, "
            "t.occurred_on, t.note, "
            "t.created_at, t.updated_at "
            "FROM transactions AS t "
            "JOIN categories AS c ON c.id = t.category_id"
        )

    @staticmethod
    def _escape_like(value: str) -> str:
        """转义 LIKE 查询中的反斜杠、百分号和下划线。"""
        return (
            value.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

    @staticmethod
    def _summary_with_connection(
        conn: sqlite3.Connection,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """在调用方提供的连接上执行汇总查询。"""
        row = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN kind = 'income' "
            "THEN amount_cents ELSE 0 END), 0) "
            "AS total_income_cents, "
            "COALESCE(SUM(CASE WHEN kind = 'expense' "
            "THEN amount_cents ELSE 0 END), 0) "
            "AS total_expense_cents, "
            "COUNT(*) AS transaction_count "
            "FROM transactions "
            "WHERE occurred_on BETWEEN ? AND ?",
            (start_date, end_date),
        ).fetchone()

        income = int(row["total_income_cents"])
        expense = int(row["total_expense_cents"])

        return {
            "start_date": start_date,
            "end_date": end_date,
            "total_income_cents": income,
            "total_expense_cents": expense,
            "balance_cents": income - expense,
            "transaction_count": int(row["transaction_count"]),
        }

    @staticmethod
    def _category_breakdown_with_connection(
        conn: sqlite3.Connection,
        start_date: str,
        end_date: str,
        kind: str,
    ) -> list[dict[str, Any]]:
        """在调用方提供的连接上执行分类汇总。"""
        rows = conn.execute(
            "SELECT "
            "c.id AS category_id, "
            "c.name AS category_name, "
            "c.is_active AS category_active, "
            "SUM(t.amount_cents) AS total_cents, "
            "COUNT(*) AS transaction_count "
            "FROM transactions AS t "
            "JOIN categories AS c ON c.id = t.category_id "
            "WHERE t.kind = ? "
            "AND t.occurred_on BETWEEN ? AND ? "
            "GROUP BY c.id, c.name, c.is_active "
            "ORDER BY total_cents DESC, c.id",
            (kind, start_date, end_date),
        ).fetchall()

        total = sum(int(row["total_cents"]) for row in rows)

        return [
            {
                "category_id": int(row["category_id"]),
                "category_name": row["category_name"],
                "category_active": bool(row["category_active"]),
                "total_cents": int(row["total_cents"]),
                "transaction_count": int(row["transaction_count"]),
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

    @staticmethod
    def _trend_with_connection(
        conn: sqlite3.Connection,
        start: str,
        end: str,
        granularity: str,
    ) -> list[dict[str, Any]]:
        """在调用方提供的连接上汇总趋势并补齐空周期。"""
        periods = build_periods(start, end, granularity)
        totals = {
            period: {
                "income_cents": 0,
                "expense_cents": 0,
            }
            for period in periods
        }

        rows = conn.execute(
            "SELECT "
            "occurred_on, "
            "SUM(CASE WHEN kind = 'income' "
            "THEN amount_cents ELSE 0 END) AS income_cents, "
            "SUM(CASE WHEN kind = 'expense' "
            "THEN amount_cents ELSE 0 END) AS expense_cents "
            "FROM transactions "
            "WHERE occurred_on BETWEEN ? AND ? "
            "GROUP BY occurred_on "
            "ORDER BY occurred_on",
            (start, end),
        ).fetchall()

        for row in rows:
            period = period_key(
                row["occurred_on"],
                granularity,
            )
            totals[period]["income_cents"] += int(
                row["income_cents"]
            )
            totals[period]["expense_cents"] += int(
                row["expense_cents"]
            )

        return [
            {
                "period": period,
                "income_cents": totals[period]["income_cents"],
                "expense_cents": totals[period]["expense_cents"],
            }
            for period in periods
        ]

    @classmethod
    def _budget_status_with_connection(
        cls,
        conn: sqlite3.Connection,
        month: str,
    ) -> list[dict[str, Any]]:
        """在调用方提供的连接上计算月度预算执行情况。"""
        start_date, end_date = month_bounds(month)
        rows = conn.execute(
            "SELECT "
            "b.id, b.category_id, "
            "c.name AS category_name, "
            "c.kind AS category_kind, "
            "c.is_active AS category_active, "
            "b.month, b.amount_cents, "
            "b.created_at, b.updated_at, "
            "COALESCE(SUM(t.amount_cents), 0) AS spent_cents "
            "FROM budgets AS b "
            "JOIN categories AS c ON c.id = b.category_id "
            "LEFT JOIN transactions AS t "
            "ON t.category_id = b.category_id "
            "AND t.kind = 'expense' "
            "AND t.occurred_on BETWEEN ? AND ? "
            "WHERE b.month = ? "
            "GROUP BY "
            "b.id, b.category_id, c.name, c.kind, "
            "c.is_active, b.month, b.amount_cents, "
            "b.created_at, b.updated_at "
            "ORDER BY c.name COLLATE NOCASE, b.id",
            (start_date, end_date, month),
        ).fetchall()

        result = []

        for row in rows:
            item = cls._budget_dict(row)
            spent = int(row["spent_cents"])
            budget = int(row["amount_cents"])

            item["spent_cents"] = spent
            item["remaining_cents"] = budget - spent
            item["usage_ratio"] = (
                round(spent / budget, 4)
                if budget
                else None
            )
            item["over_budget"] = spent > budget
            result.append(item)

        return result

    def _fetch_category(
        self,
        conn: sqlite3.Connection,
        category_id: int,
        *,
        required_kind: str | None = None,
        require_active: bool = False,
    ) -> dict[str, Any]:
        """读取分类，并可检查分类类型和启用状态。"""
        row = conn.execute(
            "SELECT id, name, kind, is_active, "
            "created_at, updated_at "
            "FROM categories WHERE id = ?",
            (category_id,),
        ).fetchone()

        if row is None:
            raise LookupError("category not found")

        if (
            required_kind is not None
            and row["kind"] != required_kind
        ):
            raise ValueError(
                "category kind does not match transaction kind"
            )

        if require_active and not row["is_active"]:
            raise ValueError("category is inactive")

        return self._category_dict(row)

    def _fetch_transaction(
        self,
        conn: sqlite3.Connection,
        transaction_id: int,
    ) -> dict[str, Any]:
        """读取一条完整账目，不存在时抛出 LookupError。"""
        row = conn.execute(
            self._transaction_select()
            + " WHERE t.id = ?",
            (transaction_id,),
        ).fetchone()

        if row is None:
            raise LookupError("transaction not found")

        return self._transaction_dict(row)

    def _fetch_budget(
        self,
        conn: sqlite3.Connection,
        category_id: int,
        month: str,
    ) -> dict[str, Any]:
        """读取一条完整预算，不存在时抛出 LookupError。"""
        row = conn.execute(
            "SELECT "
            "b.id, b.category_id, "
            "c.name AS category_name, "
            "c.kind AS category_kind, "
            "c.is_active AS category_active, "
            "b.month, b.amount_cents, "
            "b.created_at, b.updated_at "
            "FROM budgets AS b "
            "JOIN categories AS c ON c.id = b.category_id "
            "WHERE b.category_id = ? AND b.month = ?",
            (category_id, month),
        ).fetchone()

        if row is None:
            raise LookupError("budget not found")

        return self._budget_dict(row)

    @staticmethod
    def _category_dict(
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        """把分类查询结果转换成普通字典。"""
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "kind": row["kind"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _transaction_dict(
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        """把账目联表查询结果转换成普通字典。"""
        return {
            "id": int(row["id"]),
            "kind": row["kind"],
            "amount_cents": int(row["amount_cents"]),
            "category_id": int(row["category_id"]),
            "category_name": row["category_name"],
            "category_kind": row["category_kind"],
            "category_active": bool(row["category_active"]),
            "occurred_on": row["occurred_on"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _budget_dict(
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        """把预算联表查询结果转换成普通字典。"""
        return {
            "id": int(row["id"]),
            "category_id": int(row["category_id"]),
            "category_name": row["category_name"],
            "category_kind": row["category_kind"],
            "category_active": bool(row["category_active"]),
            "month": row["month"],
            "amount_cents": int(row["amount_cents"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # 输入校验与通用辅助

    @staticmethod
    def _validate_kind(value: str) -> str:
        """只接受 expense 或 income 两个固定英文类型。"""
        if (
            not isinstance(value, str)
            or value not in {"expense", "income"}
        ):
            raise ValueError("kind must be expense or income")

        return value

    @staticmethod
    def _validate_id(value: int, field: str) -> int:
        """要求数据库 ID 是大于零的整数，并排除布尔值。"""
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            raise ValueError(f"{field} must be a positive integer")

        return value

    @staticmethod
    def _validate_amount(
        value: int,
        *,
        allow_zero: bool,
    ) -> int:
        """校验整数金额，并根据业务场景决定是否允许零。"""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("amount_cents must be an integer")

        if value < 0 or (value == 0 and not allow_zero):
            raise ValueError(
                "amount_cents is outside the allowed range"
            )

        return value

    @staticmethod
    def _normalize_name(value: str) -> str:
        """去除分类名称两端空白，并禁止空名称。"""
        if not isinstance(value, str):
            raise ValueError("name must be a string")

        value = value.strip()

        if not value:
            raise ValueError("name must not be empty")

        return value

    @staticmethod
    def _normalize_note(value: str) -> str:
        """校验备注类型并去除两端空白。"""
        if not isinstance(value, str):
            raise ValueError("note must be a string")

        return value.strip()

    @staticmethod
    def _validate_paging(
        limit: int,
        offset: int,
    ) -> tuple[int, int]:
        """限制分页大小并确保偏移量不是负数。"""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("limit must be between 1 and 200")

        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise ValueError(
                "offset must be a non-negative integer"
            )

        return limit, offset

    @staticmethod
    def _now() -> str:
        """生成带 Z 后缀的 UTC 时间字符串。"""
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
