from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path

from fastapi import HTTPException

from legacy_app import get_connection, next_job_number
from plg_core.basket.models import BasketItemCreate
from plg_core.basket.service import add_item_with_connection


def _job_operator_state(job, invoice, orders, movement, financial, fallback):
    """Derive the one visible operator state from durable lifecycle evidence."""
    job_status = str(job["status"] or "").upper()
    invoice_status = str(invoice["status"] or "").upper() if invoice else ""
    paid = invoice_status == "PAID" or (invoice and float(invoice["balance_due"] or 0) <= 0)
    statuses = [str(order["status"] or "DRAFT").upper() for order in orders]
    draft_count = statuses.count("DRAFT")
    placed_count = sum(status in {"ORDERED", "PARTIAL", "RECEIVED"} for status in statuses)
    partial_count = statuses.count("PARTIAL")

    if job_status in {"DELIVERED", "COMPLETED", "COMPLETE", "CLOSED"}:
        if financial and financial["actual_cost_state"] != "CONFIRMED":
            return "Completed · Costs Pending", "Confirm actual supplier costs", "/purchasing", "GET"
        return "Completed", "Review completed Job", f"/jobs/{int(job['id'])}/delivery", "GET"
    if not invoice:
        return fallback[0], fallback[1], fallback[2], fallback[3]
    if not paid:
        return "Waiting for Payment", "Collect payment", f"/invoices/{int(invoice['id'])}/documents", "GET"
    if not orders:
        return (
            "Ready to Order",
            "Create Supplier Order",
            f"/jobs/{int(job['id'])}/fulfillment/order",
            "POST",
        )
    if draft_count and placed_count:
        return "Ordering", f"Place {draft_count} remaining supplier order{'s' if draft_count != 1 else ''}", "/purchasing", "GET"
    if draft_count:
        return "Ready to Order", f"Place {draft_count} supplier order{'s' if draft_count != 1 else ''}", "/purchasing", "GET"
    if partial_count or (movement["received_units"] and movement["remaining_units"]):
        return "Partial Receiving", "Receive incoming parts", "/purchasing", "GET"
    if movement["remaining_units"] > 0:
        return "Waiting for Supplier", "Receive incoming items", "/purchasing", "GET"
    if movement["ordered_units"] and movement["delivered_units"] < movement["ordered_units"]:
        return "Ready to Deliver", "Complete remaining delivery", f"/jobs/{int(job['id'])}/delivery", "GET"
    if financial and financial["actual_cost_state"] != "CONFIRMED":
        return "Costs Pending", "Confirm actual supplier costs", "/purchasing", "GET"
    return "Completed", "Review completed Job", f"/jobs/{int(job['id'])}/delivery", "GET"


def _pending_revision_action(connection, job_id: int, quote):
    """Return a committed revision waiting for its derived quote, if any.

    A committed revision is finished work. It remains the active source only
    until its idempotent quote projection is generated; it must not be shown as
    editable or as an unfinished change.
    """
    if quote is None:
        return None
    return connection.execute(
        """
        SELECT wr.id, wr.revision_number, wr.lock_version, wr.based_on_quote_id,
               wr.state, q.quote_number AS source_quote_number
        FROM work_revisions wr
        JOIN quotes q ON q.id = wr.based_on_quote_id
        WHERE wr.job_id=? AND wr.state='COMMITTED'
          AND wr.based_on_quote_id=?
          AND NOT EXISTS (
              SELECT 1 FROM quotes generated
              WHERE generated.work_revision_id=wr.id
          )
        ORDER BY wr.id DESC LIMIT 1
        """,
        (job_id, quote["id"]),
    ).fetchone()


def get_job_operational_snapshot(
    job_id: int,
    *,
    connection=None,
    document_root: Path | None = None,
) -> dict:
    """Return a read-only aggregate of authoritative Job lifecycle state."""
    owned = connection is None
    connection = connection or get_connection()
    try:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        customer = connection.execute(
            "SELECT id,customer_number,name,company,phone,email,address FROM customers WHERE id=?",
            (job["customer_id"],),
        ).fetchone() if job["customer_id"] else None
        assets = [dict(row) for row in connection.execute(
            "SELECT * FROM job_assets WHERE job_id=? AND state='ACTIVE' ORDER BY is_primary DESC,id",
            (job_id,),
        ).fetchall()]
        needs = [dict(row) for row in connection.execute(
            "SELECT * FROM requested_needs WHERE job_id=? AND state!='ARCHIVED' ORDER BY id",
            (job_id,),
        ).fetchall()]
        quote = connection.execute(
            "SELECT * FROM quotes WHERE job_id=? AND COALESCE(is_current,1)=1 ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        pending_revision = _pending_revision_action(connection, job_id, quote)
        invoice = connection.execute(
            "SELECT * FROM invoices WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
        order_rows = connection.execute(
            "SELECT * FROM supplier_orders WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()
        movement_rows = connection.execute(
            """
            SELECT oi.*,po.status,
              COALESCE((SELECT SUM(CASE WHEN d.status='DELIVERED' THEN di.quantity_delivered ELSE 0 END)
                FROM delivery_items di JOIN deliveries d ON d.id=di.delivery_id
                WHERE di.order_item_id=oi.id),0) delivered,
              COALESCE((SELECT SUM(CASE WHEN d.status='READY' THEN di.quantity_delivered ELSE 0 END)
                FROM delivery_items di JOIN deliveries d ON d.id=di.delivery_id
                WHERE di.order_item_id=oi.id),0) reserved
            FROM supplier_order_items oi JOIN supplier_orders po ON po.id=oi.order_id
            WHERE po.job_id=? ORDER BY po.id,oi.id
            """,
            (job_id,),
        ).fetchall()
        movement = {
            "ordered_units": sum(int(row["quantity_ordered"] or 0) for row in movement_rows),
            "received_units": sum(int(row["quantity_received"] or 0) for row in movement_rows),
            "delivered_units": sum(int(row["delivered"] or 0) for row in movement_rows),
        }
        movement["remaining_units"] = max(movement["ordered_units"] - movement["received_units"], 0)
        movement["available_to_deliver_units"] = sum(max(
            int(row["quantity_received"] or 0) - int(row["delivered"] or 0) - int(row["reserved"] or 0), 0
        ) for row in movement_rows)
        exception_rows = connection.execute(
            """
            SELECT rei.id,rei.disposition,rei.quantity,rei.reason,po.id AS order_id,
                   COALESCE((SELECT SUM(rr.quantity)
                     FROM receiving_exception_resolutions rr
                     WHERE rr.exception_item_id=rei.id
                       AND rr.resolution NOT IN ('REPLACEMENT_EXPECTED','BACKORDER_CONFIRMED')),0)
                     AS resolved_quantity
            FROM receiving_exception_items rei
            JOIN receiving_events r ON r.id=rei.receipt_id
            JOIN supplier_orders po ON po.id=r.order_id
            WHERE po.job_id=? ORDER BY rei.id DESC
            """,
            (job_id,),
        ).fetchall()
        unresolved_exceptions = []
        for source in exception_rows:
            row = dict(source)
            row["unresolved_quantity"] = max(
                int(row["quantity"] or 0) - int(row["resolved_quantity"] or 0), 0
            )
            if row["unresolved_quantity"]:
                unresolved_exceptions.append(row)
        backorder_rows = connection.execute(
            """
            SELECT be.*,oi.quantity_ordered,oi.quantity_received,po.id AS order_id
            FROM supplier_order_item_backorder_events be
            JOIN supplier_order_items oi ON oi.id=be.order_item_id
            JOIN supplier_orders po ON po.id=oi.order_id
            WHERE po.job_id=? AND be.id=(
              SELECT MAX(latest.id) FROM supplier_order_item_backorder_events latest
              WHERE latest.order_item_id=be.order_item_id)
            """,
            (job_id,),
        ).fetchall()
        active_backorders = []
        for source in backorder_rows:
            row = dict(source)
            row["effective_quantity"] = min(
                int(row["backordered_quantity"] or 0),
                max(int(row["quantity_ordered"] or 0) - int(row["quantity_received"] or 0), 0),
            )
            if row["event_kind"] != "RESOLVED" and row["effective_quantity"]:
                active_backorders.append(row)

        financial = None
        if invoice is not None:
            from plg_core.admin.service import invoice_financial_state
            financial = invoice_financial_state(connection, int(invoice["id"]))

        orders = []
        rows_by_order = {}
        for row in movement_rows:
            rows_by_order.setdefault(int(row["order_id"]), []).append(row)
        for source in order_rows:
            order_id = int(source["id"])
            items = rows_by_order.get(order_id, [])
            booked = float(connection.execute(
                """SELECT COALESCE(SUM(ii.supplier_line_total),0)
                   FROM supplier_order_items oi JOIN invoice_items ii ON ii.id=oi.invoice_item_id
                   WHERE oi.order_id=?""", (order_id,),
            ).fetchone()[0] or 0)
            placed = float(source["order_total"] or 0) if str(source["status"] or "").upper() in {"ORDERED", "PARTIAL", "RECEIVED"} else 0.0
            confirmed = sum(float(row["actual_unit_cost"] or 0) * int(row["quantity_ordered"] or 0) for row in items if row["actual_unit_cost"] is not None)
            item_components = len(items)
            confirmed_components = sum(row["actual_unit_cost"] is not None for row in items)
            if source["actual_shipping_total"] is not None:
                confirmed += float(source["actual_shipping_total"] or 0)
                confirmed_components += 1
            component_count = item_components + 1
            actual_state = "NOT_CONFIRMED" if not confirmed_components else "CONFIRMED" if confirmed_components == component_count else "PARTIALLY_CONFIRMED"
            ordered = sum(int(row["quantity_ordered"] or 0) for row in items)
            received = sum(int(row["quantity_received"] or 0) for row in items)
            delivered = sum(int(row["delivered"] or 0) for row in items)
            available = sum(max(int(row["quantity_received"] or 0) - int(row["delivered"] or 0) - int(row["reserved"] or 0), 0) for row in items)
            orders.append({
                "id": order_id, "supplier": source["supplier_name"], "po_number": source["po_number"],
                "status": str(source["status"] or "DRAFT").upper(), "booked_cost": round(booked, 2),
                "placed_cost": round(placed, 2), "actual_cost": round(confirmed, 2),
                "actual_cost_state": actual_state, "cost_variance": round(confirmed - booked, 2),
                "ordered_units": ordered, "received_units": received,
                "remaining_units": max(ordered - received, 0), "delivered_units": delivered,
                "available_to_deliver_units": available,
                "order_url": f"/purchasing/orders/{order_id}",
                "receive_url": f"/purchasing/orders/{order_id}/receive",
                "delivery_url": f"/jobs/{job_id}/delivery",
            })

        basket = connection.execute("SELECT id,status FROM baskets WHERE job_id=?", (job_id,)).fetchone()
        selected = research = quoted = 0
        if basket:
            counts = connection.execute(
                """SELECT COUNT(CASE WHEN selected=1 THEN 1 END),
                          COUNT(CASE WHEN selected=1 AND UPPER(COALESCE(part_status,'RESEARCH'))='RESEARCH' THEN 1 END),
                          COUNT(CASE WHEN selected=1 AND UPPER(COALESCE(part_status,''))='QUOTED' THEN 1 END)
                   FROM basket_items WHERE basket_id=?""", (basket["id"],),
            ).fetchone()
            selected, research, quoted = map(int, counts)
        from plg_core.jobs.engine import JobEngine
        base = JobEngine.evaluate(
            job, selected_items=selected, research_items=research, quoted_items=quoted,
            ordered_items=movement["ordered_units"], received_items=movement["received_units"],
            open_requested_needs=sum(
                str(need.get("state") or "").upper() == "OPEN" for need in needs
            ),
            basket_status=basket["status"] if basket else "OPEN",
            customer_request=connection.execute("SELECT id FROM customer_requests WHERE job_id=? LIMIT 1", (job_id,)).fetchone(),
            quote=quote, invoice=invoice,
        )
        if pending_revision is not None and invoice is None and not order_rows:
            stage, next_action, next_url, action_method = (
                "Revision Ready", "Generate Revised Quote",
                f"/work-revisions/{int(pending_revision['id'])}/generate-quote", "POST",
            )
        else:
            stage, next_action, next_url, action_method = _job_operator_state(
                job, invoice, order_rows, movement, financial,
                (
                    base.workflow_label,
                    base.next_action,
                    base.action_url,
                    base.action_method,
                ),
            )
        overlay_stages = {
            "Partial Receiving", "Waiting for Supplier", "Ready to Deliver",
            "Costs Pending", "Completed", "Completed · Costs Pending",
        }
        if unresolved_exceptions and stage in overlay_stages:
            dispositions = {row["disposition"] for row in unresolved_exceptions}
            stage = "Receiving Exception"
            if "QUARANTINED" in dispositions:
                next_action = "Review quarantined units"
            elif "SHORT" in dispositions or active_backorders:
                next_action = "Follow up with supplier"
            else:
                next_action = "Resolve supplier exception"
            next_url = f"/purchasing/orders/{unresolved_exceptions[0]['order_id']}#receiving-exceptions"
            action_method = "GET"
        elif active_backorders and stage in overlay_stages:
            stage = "Supplier Backorder"
            next_action = "Track supplier backorder"
            next_url = f"/purchasing/orders/{active_backorders[0]['order_id']}#backorders"
            action_method = "GET"
        from plg_core.jobs.fulfillment import fulfillment_snapshot
        fulfillment = fulfillment_snapshot(job_id, connection=connection)

        from legacy_app import DOCUMENTS_DIR
        from plg_core.documents.library import query_authoritative_documents
        root = Path(document_root or DOCUMENTS_DIR)
        document_result = query_authoritative_documents(
            connection, root, q=str(job["job_number"]), version_scope="current"
        )
        documents = [item for item in document_result["items"] if int(item.get("job_id") or 0) == job_id]
        activity = [dict(row) for row in connection.execute(
            "SELECT id,event_type,icon,message,created_at FROM job_timeline WHERE job_id=? ORDER BY datetime(created_at) DESC,id DESC LIMIT 12",
            (job_id,),
        ).fetchall()]
        payment_state = "PAID" if invoice and str(invoice["status"] or "").upper() == "PAID" else "BALANCE DUE" if invoice else "NOT INVOICED"
        return {
            "job_id": job_id, "job_number": job["job_number"], "job_status": job["status"],
            "customer": dict(customer) if customer else {"name": job["customer"], "company": job["company"]},
            "assets": assets, "needs": needs,
            "needs_summary": {"total": len(needs), "open": sum(str(n["state"]).upper() == "OPEN" for n in needs), "covered": sum(str(n["state"]).upper() == "SATISFIED" for n in needs)},
            "workflow": {
                "stage": stage,
                "next_action": next_action,
                "next_url": next_url,
                "action_method": action_method,
            },
            "pending_revision": dict(pending_revision) if pending_revision else None,
            "quote": ({"id": int(quote["id"]), "quote_number": quote["quote_number"], "status": quote["status"]} if quote else None),
            "invoice": ({"id": int(invoice["id"]), "invoice_number": invoice["invoice_number"], "status": invoice["status"], "customer_total": float(invoice["customer_total"] or 0), "balance_due": float(invoice["balance_due"] or 0), "amount_paid": round(max(float(invoice["customer_total"] or 0) - float(invoice["balance_due"] or 0), 0), 2), "payment_state": payment_state, "url": f"/invoices/{int(invoice['id'])}/documents"} if invoice else None),
            "financial": financial, "supplier_orders": orders, "movement": movement,
            "receiving_exceptions": unresolved_exceptions,
            "active_backorders": active_backorders,
            "fulfillment": fulfillment,
            "documents": documents, "documents_url": f"/documents?q={job['job_number']}",
            "accounting_url": f"/accounting?invoice={invoice['invoice_number']}" if invoice else "/accounting",
            "delivery_url": f"/jobs/{job_id}/delivery", "activity": activity,
        }
    finally:
        if owned:
            connection.close()


def resolve_job_id_by_number(job_number: str, *, connection=None) -> int:
    """Resolve one exact PPS Job business number through the Job service."""
    owned = connection is None
    connection = connection or get_connection()
    try:
        row = connection.execute(
            "SELECT id FROM jobs WHERE job_number=? LIMIT 1",
            (str(job_number or "").strip().upper(),),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return int(row["id"])
    finally:
        if owned:
            connection.close()


def create_job_from_customer_request(request_id: int) -> dict:
    """Create one PPS Job from an existing Customer Request."""

    with closing(get_connection()) as connection:
        record = connection.execute(
            """
            SELECT *
            FROM customer_requests
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()

        if record is None:
            raise HTTPException(
                status_code=404,
                detail="Customer request not found.",
            )

        if record["job_id"]:
            existing_job = connection.execute(
                """
                SELECT id, job_number
                FROM jobs
                WHERE id = ?
                """,
                (record["job_id"],),
            ).fetchone()

            return {
                "created": False,
                "job_id": int(record["job_id"]),
                "job_number": (
                    existing_job["job_number"]
                    if existing_job is not None
                    else ""
                ),
                "request_id": request_id,
                "request_number": record["request_number"],
            }

        if not record["customer_id"]:
            raise HTTPException(
                status_code=400,
                detail="Create or link a customer first.",
            )

        customer = connection.execute(
            """
            SELECT *
            FROM customers
            WHERE id = ?
              AND active = 1
            """,
            (record["customer_id"],),
        ).fetchone()

        if customer is None:
            raise HTTPException(
                status_code=400,
                detail="Linked customer is unavailable.",
            )

        machine = None

        if record["machine_id"]:
            machine = connection.execute(
                """
                SELECT *
                FROM machines
                WHERE id = ?
                  AND customer_id = ?
                  AND active = 1
                """,
                (
                    record["machine_id"],
                    customer["id"],
                ),
            ).fetchone()

        manufacturer = (
            machine["manufacturer"]
            if machine
            else record["manufacturer"]
        ) or ""

        model = (
            (
                machine["model"]
                or machine["name"]
            )
            if machine
            else record["model"]
        ) or ""

        identifier = (
            machine["vin_pin_serial"]
            if machine
            else record["identifier"]
        ) or ""

        job_number = next_job_number(connection)

        note_parts = [
            f"Created from {record['request_number']}"
        ]

        if (record["request_text"] or "").strip():
            note_parts.append(
                (record["request_text"] or "").strip()
            )

        cursor = connection.execute(
            """
            INSERT INTO jobs (
                job_number,
                created_date,
                customer_id,
                machine_id,
                customer,
                company,
                phone,
                email,
                address,
                manufacturer,
                machine,
                pin_serial,
                status,
                notes
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'REQUESTED', ?
            )
            """,
            (
                job_number,
                date.today().isoformat(),
                customer["id"],
                record["machine_id"],
                customer["name"],
                customer["company"] or "",
                customer["phone"] or "",
                customer["email"] or "",
                customer["address"] or "",
                manufacturer,
                model,
                identifier,
                "\n\n".join(note_parts),
            ),
        )

        job_id = int(cursor.lastrowid)

        parts = [
            line.strip(" -•\t")
            for line in (
                record["requested_parts"] or ""
            ).splitlines()
        ]

        for part in (
            part
            for part in parts
            if part
        ):
            add_item_with_connection(
                connection,
                job_id,
                BasketItemCreate(
                    requested_description=part,
                ),
            )

        connection.execute(
            """
            UPDATE customer_requests
            SET job_id = ?,
                status = 'COMPLETED',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                job_id,
                request_id,
            ),
        )

        connection.commit()

        return {
            "created": True,
            "job_id": job_id,
            "job_number": job_number,
            "request_id": request_id,
            "request_number": record["request_number"],
        }
