from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any


def get_recent_jobs(
    connection: sqlite3.Connection,
    *,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Return recent jobs with their current sales and supply-chain state."""

    safe_limit = max(1, min(int(limit), 100))

    return connection.execute(
        """
        SELECT
            jobs.*,

            (
                SELECT COUNT(*)
                FROM basket_items
                JOIN baskets
                  ON baskets.id=basket_items.basket_id
                WHERE baskets.job_id=jobs.id
                  AND basket_items.selected=1
            ) AS selected_items,

            (
                SELECT GROUP_CONCAT(
                    basket_items.requested_description,
                    ', '
                )
                FROM basket_items
                JOIN baskets
                  ON baskets.id=basket_items.basket_id
                WHERE baskets.job_id=jobs.id
                  AND basket_items.selected=1
            ) AS selected_descriptions,

            (
                SELECT quotes.id
                FROM quotes
                WHERE quotes.job_id=jobs.id
                  AND COALESCE(quotes.is_archived,0)=0
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_id,

            (
                SELECT quotes.quote_number
                FROM quotes
                WHERE quotes.job_id=jobs.id
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_number,

            (
                SELECT quotes.status
                FROM quotes
                WHERE quotes.job_id=jobs.id
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_status,

            (
                SELECT invoices.id
                FROM invoices
                WHERE invoices.job_id=jobs.id
                  AND UPPER(
                        COALESCE(invoices.status,'')
                      ) != 'VOID'
                ORDER BY invoices.id DESC
                LIMIT 1
            ) AS invoice_id,

            (
                SELECT invoices.status
                FROM invoices
                WHERE invoices.job_id=jobs.id
                  AND UPPER(
                        COALESCE(invoices.status,'')
                      ) != 'VOID'
                ORDER BY invoices.id DESC
                LIMIT 1
            ) AS invoice_status,

            (
                SELECT invoices.balance_due
                FROM invoices
                WHERE invoices.job_id=jobs.id
                  AND UPPER(
                        COALESCE(invoices.status,'')
                      ) != 'VOID'
                ORDER BY invoices.id DESC
                LIMIT 1
            ) AS balance_due,

            (
                SELECT COUNT(*)
                FROM supplier_orders
                WHERE supplier_orders.job_id=jobs.id
            ) AS supplier_order_count,

            (
                SELECT COUNT(*)
                FROM supplier_orders
                WHERE supplier_orders.job_id=jobs.id
                  AND UPPER(
                        COALESCE(supplier_orders.status,'')
                      )='DRAFT'
            ) AS draft_order_count,

            (
                SELECT COUNT(*)
                FROM supplier_orders
                WHERE supplier_orders.job_id=jobs.id
                  AND UPPER(
                        COALESCE(supplier_orders.status,'')
                      ) IN ('ORDERED','PARTIAL')
            ) AS open_order_count,

            (
                SELECT supplier_orders.id
                FROM supplier_orders
                WHERE supplier_orders.job_id=jobs.id
                  AND UPPER(
                        COALESCE(supplier_orders.status,'')
                      )='DRAFT'
                ORDER BY supplier_orders.id
                LIMIT 1
            ) AS draft_order_id,

            (
                SELECT supplier_orders.id
                FROM supplier_orders
                WHERE supplier_orders.job_id=jobs.id
                  AND UPPER(
                        COALESCE(supplier_orders.status,'')
                      ) IN ('ORDERED','PARTIAL')
                ORDER BY
                    CASE
                        WHEN UPPER(
                            COALESCE(supplier_orders.status,'')
                        )='PARTIAL'
                        THEN 0
                        ELSE 1
                    END,
                    supplier_orders.id
                LIMIT 1
            ) AS receiving_order_id,

            (
                SELECT deliveries.id
                FROM deliveries
                WHERE deliveries.job_id=jobs.id
                  AND UPPER(
                        COALESCE(deliveries.status,'')
                      )='READY'
                ORDER BY deliveries.id DESC
                LIMIT 1
            ) AS ready_delivery_id

        FROM jobs
        ORDER BY jobs.id DESC
        LIMIT ?
        """,
        (safe_limit,),
    ).fetchall()


def get_dashboard_stats(
    connection: sqlite3.Connection,
) -> sqlite3.Row:
    """Return current operational queue counts."""

    return connection.execute(
        """
        SELECT
            (
                SELECT COUNT(*)
                FROM jobs
                WHERE UPPER(
                    COALESCE(status,'')
                ) IN (
                    'REQUESTED',
                    'RESEARCHING',
                    'VERIFIED'
                )
            ) AS needs_attention,

            (
                SELECT COUNT(*)
                FROM quotes
                WHERE COALESCE(is_archived,0)=0
                  AND UPPER(
                        COALESCE(status,'DRAFT')
                      ) NOT IN (
                        'REJECTED',
                        'INVOICE'
                      )
            ) AS active_quotes,

            (
                SELECT COUNT(*)
                FROM invoices
                WHERE UPPER(
                    COALESCE(status,'')
                ) IN ('UNPAID','PARTIAL')
            ) AS waiting_payment,

            (
                SELECT COUNT(*)
                FROM invoices i
                WHERE UPPER(
                        COALESCE(i.status,'')
                      )='PAID'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM supplier_orders po
                      WHERE po.invoice_id=i.id
                  )
            ) AS ready_to_order,

            (
                SELECT COUNT(DISTINCT job_id)
                FROM supplier_orders
                WHERE UPPER(
                    COALESCE(status,'')
                ) IN ('ORDERED','PARTIAL')
            ) AS waiting_parts,

            (
                SELECT COUNT(*)
                FROM jobs
                WHERE UPPER(
                    COALESCE(status,'')
                )='RECEIVED'
            ) AS ready_delivery
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



def get_follow_up_data(
    connection: sqlite3.Connection,
    *,
    today: date | None = None,
    limit: int = 150,
) -> dict[str, Any]:
    """Return records currently waiting on a customer, supplier, payment, or delivery."""

    report_date = today or date.today()
    safe_limit = max(1, min(int(limit), 500))

    rows = connection.execute(
        """
        SELECT *
        FROM (
            SELECT
                'CUSTOMER_DECISION' AS category,
                q.id AS record_id,
                q.quote_number AS record_number,
                j.customer AS title,
                TRIM(
                    COALESCE(j.job_number,'')
                    || CASE
                        WHEN TRIM(COALESCE(j.pin_serial,'')) != ''
                        THEN ' · ' || j.pin_serial
                        ELSE ''
                    END
                ) AS subtitle,
                COALESCE(
                    NULLIF(q.quote_date,''),
                    q.created_at
                ) AS waiting_since,
                NULL AS due_date,
                '/quotes/' || q.id || '/documents' AS url,
                'Await customer decision' AS action_label,
                COALESCE(q.customer_total,0) AS amount
            FROM quotes q
            JOIN jobs j
              ON j.id=q.job_id
            WHERE UPPER(
                    COALESCE(q.status,'')
                  )='SENT'
              AND COALESCE(q.is_archived,0)=0

            UNION ALL

            SELECT
                'PAYMENT' AS category,
                i.id AS record_id,
                i.invoice_number AS record_number,
                j.customer AS title,
                TRIM(
                    COALESCE(j.job_number,'')
                    || ' · '
                    || UPPER(COALESCE(i.status,'UNPAID'))
                ) AS subtitle,
                COALESCE(
                    NULLIF(i.invoice_date,''),
                    i.created_at
                ) AS waiting_since,
                NULL AS due_date,
                '/invoices/' || i.id || '/documents' AS url,
                'Follow up on payment' AS action_label,
                COALESCE(i.balance_due,0) AS amount
            FROM invoices i
            JOIN jobs j
              ON j.id=i.job_id
            WHERE UPPER(
                    COALESCE(i.status,'')
                  ) IN ('UNPAID','PARTIAL')

            UNION ALL

            SELECT
                'SUPPLIER' AS category,
                po.id AS record_id,
                po.po_number AS record_number,
                po.supplier_name AS title,
                TRIM(
                    COALESCE(j.job_number,'')
                    || CASE
                        WHEN TRIM(COALESCE(j.customer,'')) != ''
                        THEN ' · ' || j.customer
                        ELSE ''
                    END
                ) AS subtitle,
                COALESCE(
                    NULLIF(po.ordered_at,''),
                    po.created_at
                ) AS waiting_since,
                NULLIF(TRIM(COALESCE(po.expected_at,'')),'') AS due_date,
                '/purchasing/orders/' || po.id AS url,
                CASE
                    WHEN UPPER(COALESCE(po.status,''))='PARTIAL'
                    THEN 'Follow up on remaining parts'
                    ELSE 'Check supplier order'
                END AS action_label,
                COALESCE(po.order_total,0) AS amount
            FROM supplier_orders po
            JOIN jobs j
              ON j.id=po.job_id
            WHERE UPPER(
                    COALESCE(po.status,'')
                  ) IN ('ORDERED','PARTIAL')

            UNION ALL

            SELECT
                'PARTS_SHIPPING' AS category,
                j.id AS record_id,
                j.job_number AS record_number,
                j.customer AS title,
                TRIM(
                    COALESCE(j.manufacturer,'')
                    || ' '
                    || COALESCE(j.machine,'')
                ) AS subtitle,
                COALESCE(
                    (
                        SELECT MAX(po.received_at)
                        FROM supplier_orders po
                        WHERE po.job_id=j.id
                          AND UPPER(
                                COALESCE(po.status,'')
                              )='RECEIVED'
                    ),
                    j.created_date
                ) AS waiting_since,
                NULL AS due_date,
                '/jobs/' || j.id || '/delivery' AS url,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM deliveries d
                        WHERE d.job_id=j.id
                          AND UPPER(
                                COALESCE(d.status,'')
                              )='READY'
                    )
                    THEN 'Confirm customer delivery'
                    ELSE 'Prepare customer delivery'
                END AS action_label,
                0 AS amount
            FROM jobs j
            WHERE UPPER(
                    COALESCE(j.status,'')
                  )='RECEIVED'
        )
        ORDER BY waiting_since ASC
        LIMIT ?
        """,
        (safe_limit,),
    ).fetchall()

    items = []

    for row in rows:
        item = dict(row)

        waiting_text = str(
            item.get("waiting_since") or ""
        ).strip()

        waiting_date = None

        if waiting_text:
            try:
                waiting_date = datetime.fromisoformat(
                    waiting_text.replace("Z", "+00:00")
                ).date()
            except ValueError:
                try:
                    waiting_date = date.fromisoformat(
                        waiting_text[:10]
                    )
                except ValueError:
                    waiting_date = None

        item["age_days"] = (
            max(
                (report_date - waiting_date).days,
                0,
            )
            if waiting_date
            else 0
        )

        due_text = str(
            item.get("due_date") or ""
        ).strip()

        due = None

        if due_text:
            try:
                due = date.fromisoformat(
                    due_text[:10]
                )
            except ValueError:
                due = None

        item["is_overdue"] = bool(
            due
            and due < report_date
            and item["category"] == "SUPPLIER"
        )

        if item["is_overdue"]:
            item["priority"] = "OVERDUE"
            item["priority_rank"] = 0

        elif item["category"] == "PARTS_SHIPPING":
            item["priority"] = "ACTION"
            item["priority_rank"] = 1

        elif item["category"] == "PAYMENT":
            item["priority"] = "PAYMENT"
            item["priority_rank"] = 2

        elif item["category"] == "CUSTOMER_DECISION":
            item["priority"] = "CUSTOMER"
            item["priority_rank"] = 3

        else:
            item["priority"] = "SUPPLIER"
            item["priority_rank"] = 4

        items.append(item)

    manual_follow_ups = connection.execute(
        """
        SELECT f.*,j.job_number,j.customer,j.manufacturer,j.machine,
               a.manufacturer AS asset_manufacturer,a.model AS asset_model,
               a.vin_pin_serial AS asset_serial
        FROM job_follow_ups f
        JOIN jobs j ON j.id=f.job_id
        LEFT JOIN job_assets a ON a.id=f.job_asset_id
        WHERE f.status IN ('OPEN','RECEIVED')
        ORDER BY f.requested_at,f.id
        """
    ).fetchall()
    for row in manual_follow_ups:
        item = dict(row)
        received = item["status"] == "RECEIVED"
        category = (
            "NEEDS_ATTENTION"
            if received or item["category"] == "OPERATOR_ATTENTION"
            else "CUSTOMER_INFORMATION"
        )
        waiting_text = str(
            item["received_at"] if received else item["requested_at"]
        )[:10]
        try:
            waiting_date = date.fromisoformat(waiting_text)
        except ValueError:
            waiting_date = report_date
        items.append({
            "category": category,
            "record_id": item["id"],
            "record_number": item["job_number"],
            "title": item["customer"],
            "subtitle": " · ".join(filter(None, [
                " ".join(filter(None, [item["asset_manufacturer"], item["asset_model"]])).strip(),
                item["summary"],
            ])),
            "detail": item["resolution"] if received else item["reason"],
            "waiting_since": item["received_at"] if received else item["requested_at"],
            "due_date": None,
            "url": f"/jobs/{item['job_id']}/basket#follow-ups",
            "action_label": "Review now" if category == "NEEDS_ATTENTION" else "Open request",
            "amount": 0,
            "age_days": max((report_date - waiting_date).days, 0),
            "is_overdue": False,
            "priority": "ACTION" if category == "NEEDS_ATTENTION" else "CUSTOMER",
            "priority_rank": 0 if category == "NEEDS_ATTENTION" else 3,
        })

    items.sort(
        key=lambda item: (
            int(item["priority_rank"]),
            -int(item["age_days"]),
            str(item["record_number"] or ""),
        )
    )

    summary = {
        "total": len(items),
        "customer_decisions": sum(
            1
            for item in items
            if item["category"] == "CUSTOMER_DECISION"
        ),
        "payments": sum(
            1
            for item in items
            if item["category"] == "PAYMENT"
        ),
        "suppliers": sum(
            1
            for item in items
            if item["category"] == "SUPPLIER"
        ),
        "parts_shipping": sum(
            1
            for item in items
            if item["category"] == "PARTS_SHIPPING"
        ),
        "customer_information": sum(
            1 for item in items if item["category"] == "CUSTOMER_INFORMATION"
        ),
        "needs_attention": sum(
            1 for item in items if item["category"] == "NEEDS_ATTENTION"
        ),
        "overdue": sum(
            1
            for item in items
            if item["is_overdue"]
        ),
    }

    return {
        "items": items,
        "summary": summary,
        "report_date": report_date.isoformat(),
    }



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
