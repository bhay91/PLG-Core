from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any


def get_recent_jobs(
    connection: sqlite3.Connection,
    *,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Return the most recent jobs with their current quote and invoice state."""

    safe_limit = max(1, min(int(limit), 100))

    return connection.execute(
        """
        SELECT
            jobs.*,

            (
                SELECT COUNT(*)
                FROM basket_items
                JOIN baskets
                  ON baskets.id = basket_items.basket_id
                WHERE baskets.job_id = jobs.id
                  AND basket_items.selected = 1
            ) AS selected_items,

            (
                SELECT GROUP_CONCAT(
                    basket_items.requested_description,
                    ', '
                )
                FROM basket_items
                JOIN baskets
                  ON baskets.id = basket_items.basket_id
                WHERE baskets.job_id = jobs.id
                  AND basket_items.selected = 1
            ) AS selected_descriptions,

            (
                SELECT quotes.id
                FROM quotes
                WHERE quotes.job_id = jobs.id
                  AND COALESCE(quotes.is_archived, 0) = 0
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_id,

            (
                SELECT quotes.quote_number
                FROM quotes
                WHERE quotes.job_id = jobs.id
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_number,

            (
                SELECT invoices.id
                FROM invoices
                WHERE invoices.job_id = jobs.id
                ORDER BY invoices.id DESC
                LIMIT 1
            ) AS invoice_id,

            (
                SELECT invoices.status
                FROM invoices
                WHERE invoices.job_id = jobs.id
                ORDER BY invoices.id DESC
                LIMIT 1
            ) AS invoice_status,

            (
                SELECT invoices.balance_due
                FROM invoices
                WHERE invoices.job_id = jobs.id
                ORDER BY invoices.id DESC
                LIMIT 1
            ) AS balance_due

        FROM jobs
        ORDER BY jobs.id DESC
        LIMIT ?
        """,
        (safe_limit,),
    ).fetchall()


def get_dashboard_stats(
    connection: sqlite3.Connection,
) -> sqlite3.Row:
    """Return the current high-level dashboard counts."""

    return connection.execute(
        """
        SELECT
            (
                SELECT COUNT(*)
                FROM jobs
                WHERE status IN (
                    'REQUESTED',
                    'RESEARCHING',
                    'VERIFIED'
                )
            ) AS needs_attention,

            (
                SELECT COUNT(*)
                FROM quotes
                WHERE COALESCE(is_archived, 0) = 0
            ) AS active_quotes,

            (
                SELECT COUNT(*)
                FROM invoices
                WHERE status IN ('UNPAID', 'PARTIAL')
            ) AS waiting_payment,

            (
                SELECT COUNT(*)
                FROM invoices
                WHERE status = 'PAID'
            ) AS ready_to_order
        """
    ).fetchone()


def get_financial_snapshot(
    connection: sqlite3.Connection,
    *,
    today: date | None = None,
) -> sqlite3.Row:
    """Return cash received and outstanding invoice metrics."""

    report_date = today or date.today()
    week_start = report_date - timedelta(
        days=report_date.weekday()
    )

    return connection.execute(
        """
        SELECT
            (
                SELECT COALESCE(SUM(amount), 0)
                FROM customer_transactions
                WHERE transaction_type = 'PAYMENT'
                  AND transaction_date = ?
            ) AS payments_today,

            (
                SELECT COUNT(*)
                FROM customer_transactions
                WHERE transaction_type = 'PAYMENT'
                  AND transaction_date = ?
            ) AS payment_transactions_today,

            (
                SELECT COALESCE(SUM(amount), 0)
                FROM customer_transactions
                WHERE transaction_type = 'PAYMENT'
                  AND transaction_date BETWEEN ? AND ?
            ) AS payments_this_week,

            (
                SELECT COALESCE(SUM(balance_due), 0)
                FROM invoices
                WHERE status IN ('UNPAID', 'PARTIAL')
            ) AS outstanding_balance,

            (
                SELECT COUNT(DISTINCT transactions.invoice_id)
                FROM customer_transactions AS transactions
                JOIN invoices
                  ON invoices.id = transactions.invoice_id
                WHERE transactions.transaction_type = 'PAYMENT'
                  AND transactions.transaction_date = ?
                  AND transactions.invoice_id IS NOT NULL
                  AND invoices.status = 'PAID'
            ) AS invoices_paid_today
        """,
        (
            report_date.isoformat(),
            report_date.isoformat(),
            week_start.isoformat(),
            report_date.isoformat(),
            report_date.isoformat(),
        ),
    ).fetchone()


def get_dashboard_data(
    connection: sqlite3.Connection,
    *,
    recent_job_limit: int = 10,
) -> dict[str, Any]:
    """Collect all data required by the current Dashboard page."""

    return {
        "recent_jobs": get_recent_jobs(
            connection,
            limit=recent_job_limit,
        ),
        "stats": get_dashboard_stats(connection),
        "financial": get_financial_snapshot(connection),
    }
