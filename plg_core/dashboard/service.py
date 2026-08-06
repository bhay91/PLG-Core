from __future__ import annotations

import sqlite3
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
    }
