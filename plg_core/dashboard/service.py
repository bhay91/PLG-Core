from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from plg_core.jobs.engine import JobEngine
from plg_core.jobs.workflow import derive_machine_work_status


WORK_QUEUE_CATEGORIES = (
    ("NEEDS_RESEARCH", "Needs Research"),
    ("WAITING_SUPPLIER_PRICING", "Waiting for Supplier Pricing"),
    ("READY_TO_QUOTE", "Ready to Quote"),
    ("CUSTOMER_DECISION_FOLLOW_UP", "Customer Decision / Follow-Up"),
    ("READY_TO_INVOICE", "Ready to Invoice"),
    ("WAITING_FOR_PAYMENT", "Waiting for Payment"),
    ("READY_TO_ORDER", "Ready to Order"),
    ("WAITING_FOR_PARTS", "Waiting for Parts"),
    ("READY_FOR_DELIVERY", "Ready for Delivery"),
    ("COMPLETE", "Complete"),
)


def _age_days(value: Any, report_date: date) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        waiting_date = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            waiting_date = date.fromisoformat(text[:10])
        except ValueError:
            return None
    return max((report_date - waiting_date).days, 0)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read optional projection fields from sqlite rows and test dictionaries."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _money(value: Any) -> str:
    return f"${float(value or 0):,.2f}"


def _short_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.strftime("%b %-d")


def get_work_queue_data(
    connection: sqlite3.Connection,
    *,
    category: str = "ALL",
    today: date | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Build the operator queue from authoritative PPS records without writes."""
    report_date = today or date.today()
    valid_keys = {key for key, _label in WORK_QUEUE_CATEGORIES}
    selected_category = str(category or "ALL").strip().upper()
    if selected_category not in valid_keys | {"ALL", "FOLLOW_UP"}:
        selected_category = "ALL"
    safe_limit = max(1, min(int(limit), 1000))

    jobs = get_recent_jobs(connection, limit=safe_limit)
    jobs_by_id = {int(row["id"]): row for row in jobs}
    # A current quote is the authoritative boundary between sourcing work and
    # commercial work. Historical open Needs remain stored, but must not put a
    # quoted Job back into the sourcing queue.
    jobs_in_commercial_work = {
        int(row["id"])
        for row in jobs
        if _row_value(row, "quote_id")
        and str(_row_value(row, "quote_status", "") or "").strip().upper()
    }
    rows: list[dict[str, Any]] = []

    need_rows = connection.execute(
        """
        SELECT n.id AS need_id,n.job_id,n.job_asset_id,n.wording,n.created_at,
               a.manufacturer AS manufacturer,
               COALESCE(NULLIF(TRIM(a.manufacturer || ' ' || a.model),''),
                        NULLIF(TRIM(a.name),''),'No machine linked') AS machine,
               COUNT(DISTINCT bi.id) AS result_count,
               COUNT(DISTINCT CASE WHEN bi.selected=1 THEN bi.id END) AS candidate_count,
               COUNT(DISTINCT CASE
                   WHEN bi.supplier_unit_cost IS NOT NULL
                    AND (bi.supplier_unit_cost > 0
                         OR UPPER(COALESCE(bi.pricing_mode,'')) IN
                            ('MANUAL','MANUAL_OVERRIDE','LEGACY_FIXED'))
                   THEN bi.id END) AS priced_count
               ,CASE WHEN COUNT(DISTINCT NULLIF(TRIM(bi.supplier_name),''))=1
                     THEN MAX(NULLIF(TRIM(bi.supplier_name),'')) ELSE '' END
                    AS supplier_name
        FROM requested_needs n
        JOIN jobs j ON j.id=n.job_id
        LEFT JOIN job_assets a ON a.id=n.job_asset_id
        LEFT JOIN baskets b ON b.job_id=n.job_id
        LEFT JOIN basket_items bi
          ON bi.basket_id=b.id
         AND (bi.primary_requested_need_id=n.id OR EXISTS (
             SELECT 1 FROM basket_item_need_links link
             WHERE link.basket_item_id=bi.id AND link.requested_need_id=n.id
         ))
        WHERE n.state='OPEN'
          AND COALESCE(j.is_archived,0)=0
          AND UPPER(COALESCE(j.status,'')) NOT IN
              ('CANCELLED','DELIVERED','COMPLETED','COMPLETE','CLOSED')
        GROUP BY n.id
        ORDER BY n.created_at,n.id
        """
    ).fetchall()

    jobs_with_needs: set[int] = set()
    for need in need_rows:
        job_id = int(need["job_id"])
        job = jobs_by_id.get(job_id)
        if job is None:
            continue
        if job_id in jobs_in_commercial_work:
            continue
        jobs_with_needs.add(job_id)
        quote = {"id": job["quote_id"], "status": job["quote_status"]} if job["quote_id"] else None
        invoice = {"id": job["invoice_id"], "status": job["invoice_status"]} if job["invoice_id"] else None
        intelligence = JobEngine.evaluate(
            job,
            selected_items=int(job["selected_items"] or 0),
            open_requested_needs=1,
            quote=quote,
            invoice=invoice,
        )
        if intelligence.workflow_stage not in {"RESEARCH", "READY_TO_QUOTE"}:
            continue

        result_count = int(need["result_count"] or 0)
        candidate_count = int(need["candidate_count"] or 0)
        priced_count = int(need["priced_count"] or 0)
        work_key, _work_label = derive_machine_work_status(
            need_count=1,
            parts_found_count=result_count,
            quote_candidate_count=candidate_count,
            covered_need_count=1 if candidate_count else 0,
            active_research=False,
        )
        if candidate_count or (result_count and priced_count):
            queue_key = "READY_TO_QUOTE"
            action_label = "Confirm for Quote"
            anchor = "parts-ready" if candidate_count else "research-results"
        elif work_key == "PARTS_FOUND":
            queue_key = "WAITING_SUPPLIER_PRICING"
            action_label = "Add Supplier Price"
            anchor = "research-results"
        else:
            queue_key = "NEEDS_RESEARCH"
            action_label = "Research Need"
            anchor = "research-results"
        rows.append({
            "key": f"need:{need['need_id']}",
            "category": queue_key,
            "customer": job["customer"] or job["company"] or "Customer needed",
            "job_id": job_id,
            "job_number": job["job_number"],
            "machine": need["machine"],
            "manufacturer": need["manufacturer"] or "",
            "need_action": need["wording"],
            "need_label": need["wording"],
            "action_detail": (
                "Research Parts"
                if queue_key == "NEEDS_RESEARCH"
                else action_label
            ),
            "waiting_since": need["created_at"],
            "age_days": _age_days(need["created_at"], report_date),
            "next_action": action_label,
            "url": (
                f"/jobs/{job_id}/basket?asset_id={need['job_asset_id']}"
                f"&need_id={need['need_id']}#{anchor}"
            ),
            "context_detail": (
                str(need["supplier_name"] or "").strip()
                if queue_key == "WAITING_SUPPLIER_PRICING"
                else ""
            ),
            "is_follow_up": False,
        })

    for job in jobs:
        job_id = int(job["id"])
        if int(job["is_archived"] or 0) or str(job["status"] or "").upper() == "CANCELLED":
            continue
        quote = {"id": job["quote_id"], "status": job["quote_status"]} if job["quote_id"] else None
        invoice = {"id": job["invoice_id"], "status": job["invoice_status"]} if job["invoice_id"] else None
        intelligence = JobEngine.evaluate(
            job,
            selected_items=int(job["selected_items"] or 0),
            quote=quote,
            invoice=invoice,
        )
        stage = intelligence.workflow_stage
        quote_status = str(job["quote_status"] or "").strip().upper()
        outstanding_order_quantity = int(
            job["outstanding_order_quantity"] or 0
        )
        if quote_status == "DRAFT":
            queue_key, action_label, url = (
                "READY_TO_QUOTE", "Open Quote",
                f"/quotes/{job['quote_id']}/documents",
            )
            need_action = "Review Draft Quote"
        elif quote_status == "SENT":
            queue_key, action_label, url = (
                "CUSTOMER_DECISION_FOLLOW_UP", "Open Quote",
                f"/quotes/{job['quote_id']}/documents",
            )
            need_action = "Waiting for Customer"
        elif stage in {"REQUEST", "CUSTOMER", "REGISTRY", "RESEARCH"}:
            if job_id in jobs_with_needs:
                continue
            queue_key, action_label, url = (
                "NEEDS_RESEARCH", "Open Job", f"/jobs/{job_id}/basket"
            )
            need_action = intelligence.next_action
        elif stage == "READY_TO_QUOTE":
            if any(row["job_id"] == job_id and row["category"] == "READY_TO_QUOTE" for row in rows):
                continue
            queue_key, action_label, url = (
                "READY_TO_QUOTE", "Continue to Quote", f"/jobs/{job_id}/basket#parts-ready"
            )
            need_action = intelligence.next_action
        elif stage == "WAITING_CUSTOMER":
            queue_key, action_label, url = (
                "CUSTOMER_DECISION_FOLLOW_UP", "Open Quote",
                f"/quotes/{job['quote_id']}/documents",
            )
            need_action = intelligence.next_action
        elif stage == "WAITING_PAYMENT" and not job["invoice_id"]:
            queue_key, action_label, url = (
                "READY_TO_INVOICE", "Open Quote",
                f"/quotes/{job['quote_id']}/documents",
            )
            need_action = intelligence.next_action
        elif stage == "WAITING_PAYMENT":
            queue_key, action_label, url = (
                "WAITING_FOR_PAYMENT", "Open Invoice",
                f"/invoices/{job['invoice_id']}/documents",
            )
            need_action = intelligence.next_action
        elif stage == "READY_TO_ORDER":
            queue_key, action_label, url = (
                "READY_TO_ORDER", "Open Paid Invoice",
                f"/invoices/{job['invoice_id']}/documents",
            )
            need_action = intelligence.next_action
        elif stage == "WAITING_PARTS":
            if outstanding_order_quantity <= 0:
                queue_key, action_label, url = (
                    "READY_FOR_DELIVERY", "Prepare Delivery",
                    f"/jobs/{job_id}/delivery",
                )
                need_action = "Prepare Delivery"
            else:
                queue_key, action_label, url = (
                    "WAITING_FOR_PARTS", "Open Supplier Order",
                    (
                        f"/purchasing/orders/{job['receiving_order_id'] or job['draft_order_id']}"
                        if job["receiving_order_id"] or job["draft_order_id"]
                        else "/purchasing"
                    ),
                )
                need_action = (
                    f"Receive {outstanding_order_quantity} remaining "
                    f"part{'s' if outstanding_order_quantity != 1 else ''}"
                )
        elif stage == "READY_TO_COMPLETE":
            queue_key, action_label, url = (
                "READY_FOR_DELIVERY", "Prepare Delivery", f"/jobs/{job_id}/delivery"
            )
            need_action = intelligence.next_action
        elif stage == "COMPLETE":
            queue_key, action_label, url = (
                "COMPLETE", "Open Job", f"/jobs/{job_id}/basket"
            )
            need_action = intelligence.next_action
        else:
            continue
        if queue_key in {"NEEDS_RESEARCH", "READY_TO_QUOTE"}:
            waiting_since = job["created_date"]
        elif queue_key in {"CUSTOMER_DECISION_FOLLOW_UP", "READY_TO_INVOICE"}:
            waiting_since = job["quote_waiting_since"]
        elif queue_key in {"WAITING_FOR_PAYMENT", "READY_TO_ORDER"}:
            waiting_since = job["invoice_waiting_since"]
        elif queue_key in {"WAITING_FOR_PARTS", "READY_FOR_DELIVERY"}:
            waiting_since = job["order_waiting_since"]
        else:
            waiting_since = None
        machine = " ".join(filter(None, [job["manufacturer"], job["machine"]])).strip()
        commercial_labels = {
            "CUSTOMER_DECISION_FOLLOW_UP": "CUSTOMER DECISION",
            "READY_TO_INVOICE": "INVOICE",
            "WAITING_FOR_PAYMENT": "INVOICE",
            "READY_TO_ORDER": "SUPPLIER PARTS",
            "WAITING_FOR_PARTS": "SUPPLIER PARTS",
            "READY_FOR_DELIVERY": "DELIVERY",
            "COMPLETE": "COMPLETED",
        }
        need_label = (
            "DRAFT QUOTE"
            if quote_status == "DRAFT"
            else job["requested_need_wording"]
            if queue_key == "READY_TO_QUOTE"
            and str(job["requested_need_wording"] or "").strip()
            else commercial_labels.get(queue_key, "REQUESTED PARTS")
        )
        context_detail = ""
        quote_number = str(_row_value(job, "quote_number", "") or "").strip()
        invoice_number = str(_row_value(job, "invoice_number", "") or "").strip()
        if quote_status == "DRAFT":
            context_detail = " · ".join(filter(None, (
                quote_number,
                _money(_row_value(job, "quote_total")),
            )))
        elif queue_key == "CUSTOMER_DECISION_FOLLOW_UP":
            sent_age = _age_days(job["quote_waiting_since"], report_date)
            context_detail = " · ".join(filter(None, (
                quote_number,
                _money(_row_value(job, "quote_total")),
                f"Sent {sent_age} day{'s' if sent_age != 1 else ''} ago"
                if sent_age is not None else "",
            )))
        elif queue_key == "READY_TO_INVOICE":
            context_detail = " · ".join(filter(None, (
                quote_number,
                _money(_row_value(job, "quote_total")),
            )))
        elif queue_key == "WAITING_FOR_PAYMENT":
            context_detail = " · ".join(filter(None, (
                invoice_number,
                f"Balance {_money(job['balance_due'])}",
            )))
        elif queue_key == "READY_TO_ORDER":
            context_detail = invoice_number
        elif queue_key == "WAITING_FOR_PARTS":
            supplier = str(_row_value(job, "open_supplier_name", "") or "").strip()
            supplier_count = int(_row_value(job, "open_supplier_count", 0) or 0)
            if supplier and supplier_count > 1:
                supplier = f"{supplier} + {supplier_count - 1} other"
            remaining = (
                f"{outstanding_order_quantity} remaining"
                if outstanding_order_quantity > 0 else ""
            )
            expected = _short_date(_row_value(job, "open_order_expected_at"))
            context_detail = " · ".join(filter(None, (
                supplier,
                str(_row_value(job, "open_order_number", "") or "").strip(),
                remaining,
                f"Expected {expected}" if expected else "",
            )))
        elif queue_key == "READY_FOR_DELIVERY":
            context_detail = "All supplier parts received."
        rows.append({
            "key": f"job:{job_id}:{queue_key}",
            "category": queue_key,
            "customer": job["customer"] or job["company"] or "Customer needed",
            "job_id": job_id,
            "job_number": job["job_number"],
            "machine": machine or "No machine linked",
            "manufacturer": job["manufacturer"] or "",
            "need_action": need_action,
            "need_label": need_label,
            "action_detail": need_action,
            "context_detail": context_detail,
            "waiting_since": waiting_since,
            "age_days": _age_days(waiting_since, report_date),
            "next_action": action_label,
            "url": url,
            "is_follow_up": False,
        })

    follow_ups = connection.execute(
        """
        SELECT f.*,j.job_number,j.customer,j.company,
               COALESCE(NULLIF(TRIM(a.manufacturer),''),NULLIF(TRIM(j.manufacturer),''),'') AS manufacturer,
               COALESCE(NULLIF(TRIM(a.manufacturer || ' ' || a.model),''),
                        NULLIF(TRIM(a.name),''),
                        NULLIF(TRIM(j.manufacturer || ' ' || j.machine),''),
                        'No machine linked') AS machine,
               n.wording AS need_wording
        FROM job_follow_ups f
        JOIN jobs j ON j.id=f.job_id
        LEFT JOIN job_assets a ON a.id=f.job_asset_id
        LEFT JOIN requested_needs n ON n.id=f.requested_need_id
        WHERE f.status IN ('OPEN','RECEIVED')
          AND COALESCE(j.is_archived,0)=0
        ORDER BY f.requested_at,f.id
        """
    ).fetchall()
    for follow_up in follow_ups:
        waiting_since = follow_up["received_at"] if follow_up["status"] == "RECEIVED" else follow_up["requested_at"]
        rows.append({
            "key": f"follow-up:{follow_up['id']}",
            "category": "CUSTOMER_DECISION_FOLLOW_UP",
            "customer": follow_up["customer"] or follow_up["company"] or "Customer needed",
            "job_id": int(follow_up["job_id"]),
            "job_number": follow_up["job_number"],
            "machine": follow_up["machine"],
            "manufacturer": follow_up["manufacturer"] or "",
            "need_action": follow_up["need_wording"] or follow_up["summary"],
            "need_label": follow_up["need_wording"] or "FOLLOW-UP",
            "action_detail": follow_up["summary"],
            "context_detail": "",
            "waiting_since": waiting_since,
            "age_days": _age_days(waiting_since, report_date),
            "next_action": "Review Follow-Up",
            "url": "/follow-up",
            "is_follow_up": True,
        })

    counts = {key: 0 for key, _label in WORK_QUEUE_CATEGORIES}
    for row in rows:
        counts[row["category"]] += 1
    rows.sort(key=lambda item: (item["age_days"] is None, -(item["age_days"] or 0), item["job_number"] or "", item["key"]))
    if selected_category == "FOLLOW_UP":
        visible = [row for row in rows if row["is_follow_up"]]
    elif selected_category == "ALL":
        visible = rows
    else:
        visible = [row for row in rows if row["category"] == selected_category]
    return {
        "items": visible[:safe_limit],
        "counts": counts,
        "total": len(rows),
        "selected_category": selected_category,
        "categories": WORK_QUEUE_CATEGORIES,
        "report_date": report_date.isoformat(),
    }


def get_recent_jobs(
    connection: sqlite3.Connection,
    *,
    limit: int = 10,
) -> list[sqlite3.Row]:
    """Return recent jobs with their current sales and supply-chain state."""

    safe_limit = max(1, min(int(limit), 500))

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
                  AND COALESCE(quotes.is_current,1)=1
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_id,

            (
                SELECT quotes.quote_number
                FROM quotes
                WHERE quotes.job_id=jobs.id
                  AND COALESCE(quotes.is_archived,0)=0
                  AND COALESCE(quotes.is_current,1)=1
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_number,

            (
                SELECT quotes.status
                FROM quotes
                WHERE quotes.job_id=jobs.id
                  AND COALESCE(quotes.is_archived,0)=0
                  AND COALESCE(quotes.is_current,1)=1
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_status,

            (
                SELECT quotes.customer_total
                FROM quotes
                WHERE quotes.job_id=jobs.id
                  AND COALESCE(quotes.is_archived,0)=0
                  AND COALESCE(quotes.is_current,1)=1
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_total,

            (
                SELECT COALESCE(quotes.issued_at,quotes.quote_date,quotes.created_at)
                FROM quotes
                WHERE quotes.job_id=jobs.id
                  AND COALESCE(quotes.is_archived,0)=0
                  AND COALESCE(quotes.is_current,1)=1
                ORDER BY quotes.id DESC
                LIMIT 1
            ) AS quote_waiting_since,

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
                SELECT invoices.invoice_number
                FROM invoices
                WHERE invoices.job_id=jobs.id
                  AND UPPER(COALESCE(invoices.status,'')) != 'VOID'
                ORDER BY invoices.id DESC
                LIMIT 1
            ) AS invoice_number,

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
                SELECT COALESCE(invoices.updated_at,invoices.invoice_date,invoices.created_at)
                FROM invoices
                WHERE invoices.job_id=jobs.id
                  AND UPPER(COALESCE(invoices.status,'')) != 'VOID'
                ORDER BY invoices.id DESC
                LIMIT 1
            ) AS invoice_waiting_since,

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
                SELECT COALESCE(SUM(
                    MAX(
                        COALESCE(items.quantity_ordered,0)
                        - COALESCE(items.quantity_received,0),
                        0
                    )
                ),0)
                FROM supplier_orders orders_for_job
                JOIN supplier_order_items items
                  ON items.order_id=orders_for_job.id
                WHERE orders_for_job.job_id=jobs.id
                  AND UPPER(COALESCE(orders_for_job.status,''))
                      IN ('ORDERED','PARTIAL')
            ) AS outstanding_order_quantity,

            (
                SELECT COUNT(DISTINCT NULLIF(TRIM(open_orders.supplier_name),''))
                FROM supplier_orders open_orders
                WHERE open_orders.job_id=jobs.id
                  AND UPPER(COALESCE(open_orders.status,'')) IN ('ORDERED','PARTIAL')
            ) AS open_supplier_count,

            (
                SELECT open_orders.supplier_name
                FROM supplier_orders open_orders
                WHERE open_orders.job_id=jobs.id
                  AND UPPER(COALESCE(open_orders.status,'')) IN ('ORDERED','PARTIAL')
                ORDER BY CASE WHEN UPPER(COALESCE(open_orders.status,''))='PARTIAL' THEN 0 ELSE 1 END,
                         open_orders.id
                LIMIT 1
            ) AS open_supplier_name,

            (
                SELECT open_orders.po_number
                FROM supplier_orders open_orders
                WHERE open_orders.job_id=jobs.id
                  AND UPPER(COALESCE(open_orders.status,'')) IN ('ORDERED','PARTIAL')
                ORDER BY CASE WHEN UPPER(COALESCE(open_orders.status,''))='PARTIAL' THEN 0 ELSE 1 END,
                         open_orders.id
                LIMIT 1
            ) AS open_order_number,

            (
                SELECT open_orders.expected_at
                FROM supplier_orders open_orders
                WHERE open_orders.job_id=jobs.id
                  AND UPPER(COALESCE(open_orders.status,'')) IN ('ORDERED','PARTIAL')
                ORDER BY CASE WHEN UPPER(COALESCE(open_orders.status,''))='PARTIAL' THEN 0 ELSE 1 END,
                         open_orders.id
                LIMIT 1
            ) AS open_order_expected_at,

            (
                SELECT requested_needs.wording
                FROM requested_needs
                WHERE requested_needs.job_id=jobs.id
                  AND requested_needs.state!='ARCHIVED'
                ORDER BY
                    CASE requested_needs.state WHEN 'OPEN' THEN 0 ELSE 1 END,
                    requested_needs.id
                LIMIT 1
            ) AS requested_need_wording,

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
                SELECT COALESCE(supplier_orders.ordered_at,supplier_orders.updated_at,supplier_orders.created_at)
                FROM supplier_orders
                WHERE supplier_orders.job_id=jobs.id
                ORDER BY supplier_orders.id DESC
                LIMIT 1
            ) AS order_waiting_since,

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
    view: str = "ALL",
) -> dict[str, Any]:
    """Return records currently waiting on a customer, supplier, payment, or delivery."""

    report_date = today or date.today()
    safe_limit = max(1, min(int(limit), 500))

    selected_view = str(view or "ALL").strip().upper()
    valid_views = {
        "ALL", "MY_FOLLOW_UPS", "CUSTOMER_DECISIONS", "PAYMENTS",
        "SUPPLIERS_LOGISTICS", "HISTORY",
    }
    if selected_view not in valid_views:
        selected_view = "MY_FOLLOW_UPS"

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
                COALESCE(q.customer_total,0) AS amount,
                0 AS remaining_quantity
            FROM quotes q
            JOIN jobs j
              ON j.id=q.job_id
            WHERE UPPER(
                    COALESCE(q.status,'')
                  )='SENT'
              AND COALESCE(q.is_archived,0)=0
              AND COALESCE(j.is_archived,0)=0

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
                COALESCE(i.balance_due,0) AS amount,
                0 AS remaining_quantity
            FROM invoices i
            JOIN jobs j
              ON j.id=i.job_id
            WHERE UPPER(
                    COALESCE(i.status,'')
                  ) IN ('UNPAID','PARTIAL')
              AND COALESCE(j.is_archived,0)=0

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
                COALESCE(po.order_total,0) AS amount,
                COALESCE((
                    SELECT SUM(MAX(COALESCE(poi.quantity_ordered,0)-COALESCE(poi.quantity_received,0),0))
                    FROM supplier_order_items poi WHERE poi.order_id=po.id
                ),0) AS remaining_quantity
            FROM supplier_orders po
            JOIN jobs j
              ON j.id=po.job_id
            WHERE UPPER(
                    COALESCE(po.status,'')
                  ) IN ('ORDERED','PARTIAL')
              AND COALESCE(j.is_archived,0)=0

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
                0 AS amount,
                0 AS remaining_quantity
            FROM jobs j
            WHERE UPPER(
                    COALESCE(j.status,'')
                  )='RECEIVED'
              AND COALESCE(j.is_archived,0)=0
        )
        ORDER BY waiting_since ASC
        LIMIT ?
        """,
        (safe_limit,),
    ).fetchall()

    items = []

    for row in rows:
        item = dict(row)
        item["row_kind"] = "AUTOMATIC"
        item["detail"] = ""
        item["context_detail"] = ""

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

        if item["category"] == "CUSTOMER_DECISION":
            item["context_detail"] = (
                f"{item['record_number']} · {_money(item['amount'])} · "
                f"Sent {item['age_days']} day{'s' if item['age_days'] != 1 else ''} ago"
            )
            item["action_label"] = "Open Quote"
        elif item["category"] == "PAYMENT":
            item["context_detail"] = (
                f"{item['record_number']} · Balance {_money(item['amount'])}"
            )
            item["action_label"] = "Open Invoice"
        elif item["category"] == "SUPPLIER":
            remaining = int(item.get("remaining_quantity") or 0)
            expected = _short_date(item.get("due_date"))
            item["context_detail"] = " · ".join(filter(None, (
                item["title"], item["record_number"],
                f"{remaining} remaining",
                f"Expected {expected}" if expected else "",
            )))
            item["action_label"] = "Open Supplier Order"
        elif item["category"] == "PARTS_SHIPPING":
            item["context_detail"] = "All supplier parts received."
            item["action_label"] = "Open Delivery"

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
               a.vin_pin_serial AS asset_serial,a.machine_id,
               n.wording AS need_wording,
               bi.requested_description AS basket_description,
               jp.requested_description AS job_part_description,
               (SELECT q.id FROM quotes q WHERE q.job_id=j.id AND COALESCE(q.is_archived,0)=0 ORDER BY q.id DESC LIMIT 1) AS quote_id,
               (SELECT q.quote_number FROM quotes q WHERE q.job_id=j.id AND COALESCE(q.is_archived,0)=0 ORDER BY q.id DESC LIMIT 1) AS quote_number,
               (SELECT i.id FROM invoices i WHERE i.job_id=j.id ORDER BY i.id DESC LIMIT 1) AS invoice_id,
               (SELECT i.invoice_number FROM invoices i WHERE i.job_id=j.id ORDER BY i.id DESC LIMIT 1) AS invoice_number,
               (SELECT po.id FROM supplier_orders po WHERE po.job_id=j.id ORDER BY po.id DESC LIMIT 1) AS supplier_order_id,
               (SELECT po.po_number FROM supplier_orders po WHERE po.job_id=j.id ORDER BY po.id DESC LIMIT 1) AS supplier_order_number,
               (SELECT cr.id FROM customer_requests cr WHERE cr.job_id=j.id ORDER BY cr.id DESC LIMIT 1) AS request_id,
               (SELECT cr.request_number FROM customer_requests cr WHERE cr.job_id=j.id ORDER BY cr.id DESC LIMIT 1) AS request_number
        FROM job_follow_ups f
        JOIN jobs j ON j.id=f.job_id
        LEFT JOIN job_assets a ON a.id=f.job_asset_id
        LEFT JOIN requested_needs n ON n.id=f.requested_need_id
        LEFT JOIN basket_items bi ON bi.id=f.basket_item_id
        LEFT JOIN job_parts jp ON jp.id=f.job_part_id
        WHERE f.status IN ('OPEN','RECEIVED')
          AND COALESCE(j.is_archived,0)=0
          AND UPPER(COALESCE(j.status,'')) NOT IN ('CANCELLED','DELIVERED','COMPLETED','COMPLETE','CLOSED')
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
                item["asset_serial"], item["need_wording"],
            ])),
            "detail": item["resolution"] if received else item["reason"],
            "summary_text": item["summary"],
            "reason_text": item["reason"],
            "need_wording": item["need_wording"],
            "linked_part": item["basket_description"] or item["job_part_description"] or "",
            "waiting_since": item["received_at"] if received else item["requested_at"],
            "due_date": None,
            "url": f"/jobs/{item['job_id']}/basket",
            "action_label": "Open Linked Context",
            "amount": 0,
            "age_days": max((report_date - waiting_date).days, 0),
            "is_overdue": False,
            "priority": "ACTION" if category == "NEEDS_ATTENTION" else "CUSTOMER",
            "priority_rank": 0 if category == "NEEDS_ATTENTION" else 3,
            "row_kind": "MANUAL",
            "stored_status": item["status"],
            "job_id": item["job_id"],
            "machine_id": item["machine_id"],
            "links": [
                link for link in (
                    {"label": "Job", "url": f"/jobs/{item['job_id']}/basket"},
                    {"label": "Machine", "url": f"/machines/{item['machine_id']}"} if item["machine_id"] else None,
                    {"label": item["quote_number"], "url": f"/quotes/{item['quote_id']}/documents"} if item["quote_id"] else None,
                    {"label": item["invoice_number"], "url": f"/invoices/{item['invoice_id']}/documents"} if item["invoice_id"] else None,
                    {"label": item["supplier_order_number"], "url": f"/purchasing/orders/{item['supplier_order_id']}"} if item["supplier_order_id"] else None,
                    {"label": item["request_number"], "url": f"/requests/{item['request_id']}"} if item["request_id"] else None,
                ) if link
            ],
        })

    history_rows = connection.execute(
        """
        SELECT f.*,j.job_number,j.customer,
               COALESCE(NULLIF(TRIM(a.manufacturer || ' ' || a.model),''),NULLIF(TRIM(a.name),''),'') AS machine,
               a.vin_pin_serial AS asset_serial,n.wording AS need_wording
        FROM job_follow_ups f
        JOIN jobs j ON j.id=f.job_id
        LEFT JOIN job_assets a ON a.id=f.job_asset_id
        LEFT JOIN requested_needs n ON n.id=f.requested_need_id
        WHERE f.status IN ('RESOLVED','CANCELLED')
           OR COALESCE(j.is_archived,0)=1
           OR UPPER(COALESCE(j.status,'')) IN ('CANCELLED','DELIVERED','COMPLETED','COMPLETE','CLOSED')
        ORDER BY COALESCE(f.resolved_at,f.updated_at,f.requested_at) DESC,f.id DESC
        LIMIT ?
        """,
        (safe_limit,),
    ).fetchall()
    history_items = []
    for row in history_rows:
        item = dict(row)
        history_items.append({
            "category": "HISTORY", "record_id": item["id"],
            "record_number": item["job_number"], "title": item["customer"],
            "subtitle": " · ".join(filter(None, (item["machine"], item["asset_serial"], item["need_wording"]))),
            "detail": item["resolution"] or item["reason"], "summary_text": item["summary"],
            "waiting_since": item["requested_at"], "due_date": None,
            "url": f"/jobs/{item['job_id']}/basket", "action_label": "Open Job",
            "amount": 0, "age_days": _age_days(item["requested_at"], report_date) or 0,
            "is_overdue": False, "priority": item["status"], "priority_rank": 9,
            "row_kind": "MANUAL", "stored_status": item["status"], "job_id": item["job_id"],
            "links": [{"label": "Job", "url": f"/jobs/{item['job_id']}/basket"}],
            "context_detail": "",
        })

    items.sort(
        key=lambda item: (
            int(item["priority_rank"]),
            -int(item["age_days"]),
            str(item["record_number"] or ""),
        )
    )

    active_items = items
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
        "my_follow_ups": sum(1 for item in active_items if item.get("row_kind") == "MANUAL"),
        "history": len(history_items),
    }

    if selected_view == "MY_FOLLOW_UPS":
        visible_items = [item for item in active_items if item.get("row_kind") == "MANUAL"]
    elif selected_view == "CUSTOMER_DECISIONS":
        visible_items = [item for item in active_items if item["category"] == "CUSTOMER_DECISION"]
    elif selected_view == "PAYMENTS":
        visible_items = [item for item in active_items if item["category"] == "PAYMENT"]
    elif selected_view == "SUPPLIERS_LOGISTICS":
        visible_items = [item for item in active_items if item["category"] in {"SUPPLIER", "PARTS_SHIPPING"}]
    elif selected_view == "HISTORY":
        visible_items = history_items
    else:
        visible_items = active_items

    return {
        "items": visible_items[:safe_limit],
        "summary": summary,
        "report_date": report_date.isoformat(),
        "selected_view": selected_view,
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


def _dashboard_source_label(payload_json: Any) -> str:
    """Return a human label for a proposal's authenticated transport origin."""
    try:
        origin = json.loads(str(payload_json or "{}")).get("origin", "")
    except (json.JSONDecodeError, TypeError, ValueError):
        origin = ""
    return {
        "CHATGPT_FIREFOX": "ChatGPT / Firefox",
        "CHATGPT_MOBILE": "ChatGPT / Mobile",
        "CHATGPT_MCP": "ChatGPT / MCP",
    }.get(str(origin).upper(), "Smart Intake")


def _dashboard_age_label(age_days: int | None) -> str:
    if age_days is None or age_days <= 0:
        return "New"
    if age_days == 1:
        return "Waiting 1 day"
    if age_days >= 3:
        return "Waiting 3+ days"
    return f"Waiting {age_days} days"


def get_operator_dashboard_data(
    connection: sqlite3.Connection,
    *,
    accounting_rows: list[dict[str, Any]] | None = None,
    today: date | None = None,
    section_limit: int = 5,
) -> dict[str, Any]:
    """Build the read-only daily operator dashboard from existing PPS state."""
    report_date = today or date.today()
    safe_limit = max(1, min(int(section_limit), 10))

    proposal_rows = connection.execute(
        """
        SELECT p.id,p.contact_name,p.company_name,p.raw_input,p.review_state,
               p.created_at,p.updated_at,
               (SELECT GROUP_CONCAT(TRIM(a.manufacturer || ' ' || a.model), ', ')
                  FROM intake_proposal_assets a
                 WHERE a.proposal_id=p.id AND a.included=1) AS machine_summary,
               (SELECT GROUP_CONCAT(n.wording, ', ')
                  FROM intake_proposal_needs n
                 WHERE n.proposal_id=p.id AND n.included=1) AS need_summary,
               (SELECT c.payload_json
                  FROM intake_proposal_contributions c
                 WHERE c.proposal_id=p.id AND c.contributor_type='AI'
                 ORDER BY c.id DESC LIMIT 1) AS transport_payload
          FROM intake_proposals p
         WHERE UPPER(COALESCE(p.status,''))='DRAFT'
         ORDER BY COALESCE(p.created_at,p.updated_at),p.id
        """
    ).fetchall()
    inbox = []
    for row in proposal_rows[:safe_limit]:
        age_days = _age_days(row["created_at"] or row["updated_at"], report_date)
        raw_request = " ".join(str(row["raw_input"] or "").split())
        inbox.append({
            "proposal_id": int(row["id"]),
            "identity": row["company_name"] or row["contact_name"] or "Sender needs review",
            "request": raw_request[:157] + "…" if len(raw_request) > 160 else raw_request or "Smart Intake proposal",
            "context": " · ".join(filter(None, (row["machine_summary"], row["need_summary"]))),
            "source": _dashboard_source_label(row["transport_payload"]),
            "age_days": age_days,
            "age_label": _dashboard_age_label(age_days),
            "status": "Ready for Review" if str(row["review_state"] or "").upper() == "CONFIDENT" else "Needs Review",
            "url": f"/requests/smart-intake/proposals/{int(row['id'])}",
        })

    queue = get_work_queue_data(connection, today=report_date, limit=500)
    queue_items = queue["items"]

    def select(categories: set[str], *, unique_jobs: bool = True) -> list[dict[str, Any]]:
        selected = []
        seen_jobs: set[int] = set()
        for item in queue_items:
            if item["category"] not in categories:
                continue
            job_id = int(item["job_id"])
            if unique_jobs and job_id in seen_jobs:
                continue
            seen_jobs.add(job_id)
            selected.append(item)
            if len(selected) >= safe_limit:
                break
        return selected

    operational_job_categories = {
        "NEEDS_RESEARCH", "WAITING_SUPPLIER_PRICING", "READY_TO_QUOTE",
        "CUSTOMER_DECISION_FOLLOW_UP", "READY_TO_INVOICE",
    }
    sections = {
        "jobs": select(operational_job_categories),
        "payments": select({"WAITING_FOR_PAYMENT"}),
        "ordering": select({"READY_TO_ORDER"}),
        "receiving": select({"WAITING_FOR_PARTS"}),
        "delivery": select({"READY_FOR_DELIVERY"}),
    }
    for item in sections["jobs"]:
        item["dashboard_url"] = f"/jobs/{int(item['job_id'])}/basket"
        item["dashboard_action"] = "Open Job"

    exceptions = []
    for row in accounting_rows or []:
        actual_state = str(row.get("actual_cost_state") or "")
        cost_variance = float(row.get("cost_variance") or 0)
        profit_variance = float(row.get("profit_variance") or 0)
        has_variance = actual_state != "NOT_CONFIRMED" and (
            abs(cost_variance) > 0.005 or abs(profit_variance) > 0.005
        )
        if actual_state == "NOT_CONFIRMED":
            state = "Awaiting Final Cost"
        elif has_variance:
            state = "Variance Detected"
        else:
            continue
        exceptions.append({
            "invoice_number": row.get("invoice_number") or "Invoice",
            "job_number": row.get("job_number") or "",
            "customer": row.get("customer") or "Customer",
            "state": state,
            "url": row.get("invoice_url") or "/accounting#invoice-reconciliation",
        })
        if len(exceptions) >= safe_limit:
            break

    return {
        "inbox": inbox,
        "inbox_total": len(proposal_rows),
        **sections,
        "accounting_exceptions": exceptions,
        "section_limit": safe_limit,
        "report_date": report_date.isoformat(),
    }
