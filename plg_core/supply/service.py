from contextlib import closing
import hashlib
import json
from pathlib import Path
from fastapi import HTTPException
from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.timeline import log_job_event
from plg_core.supply.models import (
    BackorderEventCreate,
    DeliveryCreate,
    ReceiptCreate,
    ReceivingExceptionResolutionCreate,
)


ACTUAL_COST_STATUSES = {"ORDERED", "PARTIAL", "RECEIVED"}
INTERIM_EXCEPTION_RESOLUTIONS = {
    "REPLACEMENT_EXPECTED",
    "BACKORDER_CONFIRMED",
}
EXCEPTION_RESOLUTION_MATRIX = {
    "DAMAGED": {
        "RETURNED", "DISPOSED", "REPLACEMENT_EXPECTED",
        "REPLACED_BY_RECEIPT", "CLOSED",
    },
    "WRONG_ITEM": {
        "RETURNED", "REPLACEMENT_EXPECTED", "REPLACED_BY_RECEIPT",
        "RECLASSIFIED_REJECTED", "CLOSED",
    },
    "QUARANTINED": {"RECLASSIFIED_REJECTED", "CLOSED"},
    "REJECTED": {
        "RETURNED", "DISPOSED", "REPLACEMENT_EXPECTED",
        "REPLACED_BY_RECEIPT", "CLOSED",
    },
    "SHORT": {"BACKORDER_CONFIRMED", "REPLACED_BY_RECEIPT", "CLOSED"},
}


def _receipt_request_fingerprint(order_id: int, payload: ReceiptCreate) -> str:
    content = {
        "order_id": int(order_id),
        "items": sorted(
            (
                {
                    "order_item_id": int(item.order_item_id),
                    "quantity_received": int(item.quantity_received),
                }
                for item in payload.items
            ),
            key=lambda row: row["order_item_id"],
        ),
        "exceptions": sorted(
            (
                {
                    "order_item_id": int(item.order_item_id),
                    "disposition": str(item.disposition).upper(),
                    "quantity": int(item.quantity),
                    "reason": item.reason.strip(),
                    "notes": item.notes.strip(),
                    "supplier_reference": item.supplier_reference.strip(),
                    "evidence_reference": item.evidence_reference.strip(),
                }
                for item in payload.exceptions
            ),
            key=lambda row: (row["order_item_id"], row["disposition"]),
        ),
        "notes": payload.notes.strip(),
        "receiver": payload.receiver.strip(),
    }
    encoded = json.dumps(content, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _accepted_order_status(connection, order_id: int) -> str:
    quantities = connection.execute(
        """
        SELECT COALESCE(SUM(quantity_received),0) AS accepted,
               COALESCE(SUM(quantity_ordered),0) AS ordered,
               SUM(CASE WHEN quantity_received < quantity_ordered THEN 1 ELSE 0 END)
                   AS remaining_lines
        FROM supplier_order_items
        WHERE order_id=?
        """,
        (order_id,),
    ).fetchone()
    if int(quantities["remaining_lines"] or 0) == 0:
        return "RECEIVED"
    if int(quantities["accepted"] or 0) == 0:
        return "ORDERED"
    return "PARTIAL"


def _apply_receiving_completion(connection, order, new_status: str) -> None:
    connection.execute(
        """
        UPDATE supplier_orders
        SET status=?,
            received_at=CASE WHEN ?='RECEIVED'
                THEN COALESCE(received_at,CURRENT_TIMESTAMP) ELSE received_at END,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (new_status, new_status, int(order["id"])),
    )
    outstanding_orders = connection.execute(
        """
        SELECT COUNT(*) FROM supplier_orders
        WHERE job_id=? AND UPPER(COALESCE(status,'DRAFT'))!='RECEIVED'
        """,
        (int(order["job_id"]),),
    ).fetchone()[0]
    if int(outstanding_orders or 0) != 0:
        return
    job = connection.execute(
        "SELECT status FROM jobs WHERE id=?",
        (int(order["job_id"]),),
    ).fetchone()
    if job is not None and str(job["status"] or "").upper() == "RECEIVED":
        return
    connection.execute(
        "UPDATE jobs SET status='RECEIVED' WHERE id=?",
        (int(order["job_id"]),),
    )
    log_job_event(
        connection,
        job_id=int(order["job_id"]),
        event_type="RECEIVING_COMPLETE",
        icon="✅",
        message="All supplier purchases have been received.",
    )


def _clearance_request_fingerprint(
    exception_item_id: int,
    *,
    quantity: int,
    reason: str,
    receiver: str,
    notes: str,
) -> str:
    encoded = json.dumps(
        {
            "exception_item_id": int(exception_item_id),
            "quantity": int(quantity),
            "reason": str(reason or "").strip(),
            "receiver": str(receiver or "").strip(),
            "notes": str(notes or "").strip(),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ensure_receiving_document(receipt_id: int) -> None:
    from plg_core.documents.integrity import (
        current_receiving_document,
        issue_receiving_document,
        verified_receiving_document,
    )
    with closing(get_connection()) as connection:
        if current_receiving_document(connection, receipt_id) is not None:
            verified_receiving_document(connection, receipt_id)
            return
        issue_receiving_document(connection, receipt_id)
        connection.commit()


def _accepted_receipt_capacity(
    connection,
    *,
    related_receipt_id: int,
    order_item_id: int,
    order_id: int,
):
    related = connection.execute(
        """
        SELECT r.id,r.order_id,COALESCE(SUM(ri.quantity_received),0) AS accepted
        FROM receiving_events r
        LEFT JOIN receiving_event_items ri
          ON ri.receipt_id=r.id AND ri.order_item_id=?
        WHERE r.id=?
        GROUP BY r.id,r.order_id
        """,
        (int(order_item_id), int(related_receipt_id)),
    ).fetchone()
    if related is None or int(related["order_id"]) != int(order_id):
        raise HTTPException(status_code=409, detail="Related receipt belongs to another order.")
    replacement_allocated = connection.execute(
        """
        SELECT COALESCE(SUM(rr.quantity),0)
        FROM receiving_exception_resolutions rr
        JOIN receiving_exception_items rei ON rei.id=rr.exception_item_id
        WHERE rr.related_receipt_id=? AND rei.order_item_id=?
          AND rr.resolution='REPLACED_BY_RECEIPT'
        """,
        (int(related_receipt_id), int(order_item_id)),
    ).fetchone()[0]
    backorder_allocated = connection.execute(
        """
        SELECT COALESCE(SUM(fulfilled_quantity),0)
        FROM supplier_order_item_backorder_events
        WHERE related_receipt_id=? AND order_item_id=? AND event_kind='RESOLVED'
        """,
        (int(related_receipt_id), int(order_item_id)),
    ).fetchone()[0]
    accepted = int(related["accepted"] or 0)
    allocated = int(replacement_allocated or 0) + int(backorder_allocated or 0)
    return {
        "receipt_id": int(related["id"]),
        "accepted": accepted,
        "allocated": allocated,
        "remaining": max(accepted - allocated, 0),
    }


def _validate_related_accepted_receipt(
    connection,
    *,
    exception,
    related_receipt_id: int | None,
    quantity: int,
) -> None:
    if related_receipt_id is None:
        raise HTTPException(
            status_code=400,
            detail="This resolution requires a related accepted receipt.",
        )
    capacity = _accepted_receipt_capacity(
        connection,
        related_receipt_id=int(related_receipt_id),
        order_item_id=int(exception["order_item_id"]),
        order_id=int(exception["order_id"]),
    )
    if int(capacity["receipt_id"]) <= int(exception["receipt_id"]):
        raise HTTPException(status_code=409, detail="Replacement receipt must postdate the exception.")
    if int(capacity["remaining"]) < int(quantity):
        raise HTTPException(
            status_code=409,
            detail="Related receipt has insufficient unallocated accepted quantity for this item.",
        )

def create_orders_from_paid_invoice(invoice_id: int):
    with closing(get_connection()) as connection:
        invoice = connection.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        from plg_core.lifecycle import ensure_job_allows_new_business
        ensure_job_allows_new_business(connection, int(invoice["job_id"]), "create supplier purchases")
        if str(invoice["status"] or "").upper() != "PAID" or float(invoice["balance_due"] or 0) > 0:
            raise HTTPException(status_code=409, detail="Supplier purchases require a fully paid invoice.")
        existing = connection.execute(
            "SELECT * FROM supplier_orders WHERE invoice_id=? ORDER BY id",
            (invoice_id,),
        ).fetchall()
        if existing:
            return [dict(row) for row in existing]
        items = connection.execute(
            "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY supplier_name,id",
            (invoice_id,),
        ).fetchall()
        if not items:
            raise HTTPException(status_code=409, detail="Invoice has no items to purchase.")
        grouped = {}
        for item in items:
            name = str(item["supplier_name"] or "").strip() or "Unassigned Supplier"
            grouped.setdefault(name, []).append(item)
        ids = []
        for supplier_name, supplier_items in grouped.items():
            supplier = connection.execute(
                "SELECT id FROM suppliers WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) LIMIT 1",
                (supplier_name,),
            ).fetchone()
            total = round(sum(float(item["supplier_line_total"] or 0) for item in supplier_items), 2)
            cur = connection.execute("""
                INSERT INTO supplier_orders (
                    job_id,invoice_id,supplier_id,supplier_name,status,
                    parts_total,shipping_total,order_total,notes
                ) VALUES (?, ?, ?, ?, 'DRAFT', ?, 0, ?, ?)
            """, (
                invoice["job_id"], invoice_id,
                supplier["id"] if supplier else None,
                supplier_name, total, total,
                "Generated from paid invoice; invoice-level shipping is not allocated to supplier purchases.",
            ))
            order_id = int(cur.lastrowid)
            ids.append(order_id)
            po_number = f"PPS-PO-{order_id:04d}"
            connection.execute("UPDATE supplier_orders SET po_number=? WHERE id=?", (po_number, order_id))
            for item in supplier_items:
                qty = max(1, int(item["quantity"] or 1))
                unit = float(item["supplier_unit_cost"] or 0)
                connection.execute("""
                    INSERT INTO supplier_order_items (
                        order_id,invoice_item_id,description,supplier_part_number,
                        quantity_ordered,unit_cost,line_cost,job_asset_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id, item["id"], item["description"],
                    item["supplier_part_number"] or "", qty, unit,
                    round(unit * qty, 2),
                    item["job_asset_id"],
                ))
            write_audit(
                connection,
                action="SUPPLIER_ORDER_CREATED",
                entity_type="SUPPLIER_ORDER",
                entity_id=order_id,
                summary=f"{po_number} created for {supplier_name}",
                metadata={"invoice_id": invoice_id},
            )
            log_job_event(
                connection,
                job_id=int(invoice["job_id"]),
                event_type="SUPPLIER_ORDER_CREATED",
                icon="📋",
                message=(
                    f"{po_number} created for {supplier_name}"
                ),
            )
        connection.commit()
        rows = connection.execute(
            f"SELECT * FROM supplier_orders WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id",
            ids,
        ).fetchall()
    return [dict(row) for row in rows]

def list_orders(limit: int = 100):
    limit = max(1, min(int(limit), 500))
    with closing(get_connection()) as connection:
        rows = connection.execute("""
            SELECT po.*, j.job_number, i.invoice_number,
                   (SELECT COUNT(*) FROM supplier_order_items oi WHERE oi.order_id=po.id) AS item_count
            FROM supplier_orders po
            JOIN jobs j ON j.id=po.job_id
            LEFT JOIN invoices i ON i.id=po.invoice_id
            ORDER BY po.id DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(row) for row in rows]


def _purchasing_snapshot(connection, order_id: int, *, document_root=None, include_documents=True):
    order = connection.execute(
        """
        SELECT po.*,j.job_number,j.customer,j.company,i.invoice_number,
               COALESCE(NULLIF(i.bill_to_name_snapshot,''),NULLIF(j.customer,''),'—') customer_name
        FROM supplier_orders po
        JOIN jobs j ON j.id=po.job_id
        LEFT JOIN invoices i ON i.id=po.invoice_id
        WHERE po.id=?
        """,
        (order_id,),
    ).fetchone()
    if order is None:
        raise HTTPException(status_code=404, detail="Supplier purchase not found.")
    order = dict(order)
    items = [dict(row) for row in connection.execute(
        """
        SELECT oi.*,COALESCE(ii.supplier_line_total,0) booked_line_cost
        FROM supplier_order_items oi
        LEFT JOIN invoice_items ii ON ii.id=oi.invoice_item_id
        WHERE oi.order_id=? ORDER BY oi.id
        """,
        (order_id,),
    ).fetchall()]
    availability = {
        int(row["id"]): row for row in _delivery_availability_rows(connection, int(order["job_id"]))
        if int(row["order_id"]) == order_id
    }
    ordered = sum(int(item["quantity_ordered"] or 0) for item in items)
    received = sum(int(item["quantity_received"] or 0) for item in items)
    delivered = sum(int(availability.get(int(item["id"]), {}).get("quantity_delivered") or 0) for item in items)
    available = sum(int(availability.get(int(item["id"]), {}).get("available_to_deliver") or 0) for item in items)

    booked = round(sum(float(item["booked_line_cost"] or 0) for item in items), 2)
    placed_parts = round(float(order["parts_total"] or 0), 2)
    placed_shipping = round(float(order["shipping_total"] or 0), 2)
    placed_total = round(float(order["order_total"] or 0), 2)
    confirmed_items = sum(item["actual_unit_cost"] is not None for item in items)
    confirmed_components = confirmed_items + (order["actual_shipping_total"] is not None)
    component_count = len(items) + 1
    actual_state = (
        "NOT CONFIRMED" if confirmed_components == 0 else
        "CONFIRMED" if confirmed_components == component_count else
        "PARTIALLY CONFIRMED"
    )
    actual = round(
        sum(float(item["actual_unit_cost"] or 0) * int(item["quantity_ordered"] or 0)
            for item in items if item["actual_unit_cost"] is not None)
        + (float(order["actual_shipping_total"] or 0) if order["actual_shipping_total"] is not None else 0),
        2,
    )
    final_actual = actual if actual_state == "CONFIRMED" else None
    status = str(order["status"] or "DRAFT").upper()
    remaining = max(ordered - received, 0)
    if status == "DRAFT":
        next_action, next_url = "Review supplier costs before placing PO", f"/purchasing/orders/{order_id}#purchase-details"
    elif status == "PARTIAL" or (status == "ORDERED" and received > 0):
        next_action, next_url = "Receive remaining supplier items", f"/purchasing/orders/{order_id}#receive-parts"
    elif status == "ORDERED":
        next_action, next_url = "Receive incoming supplier items", f"/purchasing/orders/{order_id}#receive-parts"
    elif status == "RECEIVED" and available > 0:
        next_action, next_url = "Prepare delivery", f"/jobs/{int(order['job_id'])}/delivery"
    elif status == "RECEIVED":
        next_action, next_url = "Review completed supplier order", f"/purchasing/orders/{order_id}#operational-history"
    else:
        next_action, next_url = "Review supplier order", f"/purchasing/orders/{order_id}"

    receipts = [dict(row) for row in connection.execute(
        "SELECT * FROM receiving_events WHERE order_id=? ORDER BY id DESC", (order_id,)
    ).fetchall()]
    adjustments = [dict(row) for row in connection.execute(
        "SELECT * FROM supplier_cost_adjustments WHERE supplier_order_id=? ORDER BY id DESC", (order_id,)
    ).fetchall()]
    audit = [dict(row) for row in connection.execute(
        "SELECT id,action,summary,actor,created_at FROM audit_logs WHERE entity_type='SUPPLIER_ORDER' AND entity_id=? ORDER BY id DESC LIMIT 30",
        (str(order_id),),
    ).fetchall()]
    timeline = [dict(row) for row in connection.execute(
        """SELECT id,event_type action,message summary,'timeline' actor,created_at
           FROM job_timeline WHERE job_id=? AND (message LIKE ? OR event_type LIKE 'SUPPLIER_%' OR event_type LIKE 'PARTS_%')
           ORDER BY id DESC LIMIT 30""",
        (order["job_id"], f"%{order['po_number']}%"),
    ).fetchall()]
    history = []
    seen = set()
    for event in audit + timeline:
        key = (str(event.get("created_at") or ""), str(event.get("summary") or ""))
        if key in seen:
            continue
        seen.add(key)
        history.append(event)
    history.sort(key=lambda event: (str(event.get("created_at") or ""), int(event.get("id") or 0)), reverse=True)

    documents = {"supplier_po": None, "receiving_summaries": [], "delivery_documents": [], "history_available": False}
    if include_documents:
        from legacy_app import DOCUMENTS_DIR
        from plg_core.documents.library import query_authoritative_documents
        root = Path(document_root or DOCUMENTS_DIR)
        result = query_authoritative_documents(connection, root, version_scope="all")
        delivery_ids = {int(row[0]) for row in connection.execute(
            """SELECT DISTINCT d.id FROM deliveries d JOIN delivery_items di ON di.delivery_id=d.id
               JOIN supplier_order_items oi ON oi.id=di.order_item_id WHERE oi.order_id=?""", (order_id,)
        ).fetchall()}
        relevant = []
        for document in result["items"]:
            family = document.get("family")
            belongs = (
                (family == "supplier-po" and int(document.get("parent_id") or 0) == order_id)
                or (family == "receiving-summary" and str(document.get("related_po_number") or "") == str(order["po_number"]))
                or (family == "delivery-note" and int(document.get("parent_id") or 0) in delivery_ids)
            )
            if belongs:
                relevant.append(document)
        current_po = next((doc for doc in relevant if doc["family"] == "supplier-po" and doc["is_current"]), None)
        documents = {
            "supplier_po": current_po,
            "receiving_summaries": [doc for doc in relevant if doc["family"] == "receiving-summary" and doc["is_current"]],
            "delivery_documents": [doc for doc in relevant if doc["family"] == "delivery-note" and doc["is_current"]],
            "history_available": any(not doc["is_current"] for doc in relevant),
        }
    if status == "DRAFT" and documents["supplier_po"] is None:
        documents["supplier_po"] = {
            "document_number": order["po_number"], "version": "PREVIEW", "integrity": "DRAFT PREVIEW",
            "is_valid": False, "open_url": f"/purchasing/orders/{order_id}/purchase-order/pdf",
            "download_url": f"/purchasing/orders/{order_id}/purchase-order/pdf?download=1",
        }

    return {
        "id": order_id,
        "identity": {
            "supplier": order["supplier_name"], "po_number": order["po_number"],
            "job_id": int(order["job_id"]), "job_number": order["job_number"],
            "customer": order["customer_name"], "company": order["company"],
            "invoice_id": int(order["invoice_id"]) if order["invoice_id"] else None,
            "invoice_number": order["invoice_number"], "created_at": order["created_at"],
            "ordered_at": order["ordered_at"], "expected_at": order["expected_at"], "received_at": order["received_at"],
        },
        "status": {"current": status, "next_action": next_action, "next_action_url": next_url},
        "costs": {
            "booked_cost": booked, "placed_parts_cost": placed_parts, "placed_shipping": placed_shipping,
            "placed_total": placed_total, "draft_total": placed_total if status == "DRAFT" else None,
            "confirmed_actual_cost": actual, "final_actual_cost": final_actual,
            "actual_cost_state": actual_state,
            "variance_vs_booked": round(final_actual - booked, 2) if final_actual is not None else None,
            "variance_vs_placed": round(final_actual - placed_total, 2) if final_actual is not None else None,
            "placed_vs_booked": round(placed_total - booked, 2),
        },
        "movement": {"ordered": ordered, "received": received, "remaining": remaining, "delivered": delivered, "available_to_deliver": available},
        "documents": documents,
        "receipts": receipts, "actual_cost_adjustments": adjustments, "history": history,
        "links": {
            "open": f"/purchasing/orders/{order_id}", "job": f"/jobs/{int(order['job_id'])}/basket",
            "invoice": f"/invoices/{int(order['invoice_id'])}/documents" if order["invoice_id"] else None,
            "accounting": f"/accounting?invoice={order['invoice_number']}" if order["invoice_number"] else "/accounting",
            "receiving": f"/purchasing/orders/{order_id}#receive-parts" if status in {"ORDERED", "PARTIAL"} else None,
            "delivery": f"/jobs/{int(order['job_id'])}/delivery" if available > 0 or status == "RECEIVED" else None,
            "documents": f"/documents?q={order['po_number']}",
        },
    }


def get_purchasing_operational_snapshot(order_id: int, *, connection=None, document_root=None):
    """Return authoritative, read-only supplier-order operational state."""
    owned = connection is None
    connection = connection or get_connection()
    try:
        return _purchasing_snapshot(connection, int(order_id), document_root=document_root)
    finally:
        if owned:
            connection.close()


def list_purchasing_operational_snapshots(*, supplier="", status="", customer="", job="", invoice_id=None, limit=500):
    """Return filterable read-only summaries without document filesystem scans."""
    with closing(get_connection()) as connection:
        ids = [int(row[0]) for row in connection.execute(
            "SELECT id FROM supplier_orders ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)
        ).fetchall()]
        all_orders = [_purchasing_snapshot(connection, order_id, include_documents=False) for order_id in ids]
    supplier_needle = str(supplier or "").strip().lower()
    customer_needle = str(customer or "").strip().lower()
    job_needle = str(job or "").strip().lower()
    status_needle = str(status or "").strip().upper()
    filtered = [item for item in all_orders if
        (not supplier_needle or supplier_needle in str(item["identity"]["supplier"] or "").lower()) and
        (not customer_needle or customer_needle in str(item["identity"]["customer"] or "").lower()) and
        (not job_needle or job_needle in str(item["identity"]["job_number"] or "").lower()) and
        (not status_needle or item["status"]["current"] == status_needle) and
        (invoice_id is None or item["identity"]["invoice_id"] == int(invoice_id))]
    return {
        "orders": filtered,
        "options": {
            "suppliers": sorted({str(item["identity"]["supplier"]) for item in all_orders}),
            "statuses": sorted({item["status"]["current"] for item in all_orders}),
        },
    }

def get_order(order_id: int):
    with closing(get_connection()) as connection:
        order = connection.execute(
            """
            SELECT
                po.*,
                j.job_number,
                j.customer,
                j.company,
                i.invoice_number
            FROM supplier_orders po
            JOIN jobs j
              ON j.id=po.job_id
            LEFT JOIN invoices i
              ON i.id=po.invoice_id
            WHERE po.id=?
            """,
            (order_id,),
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Supplier purchase not found.")
        items = connection.execute(
            """
            SELECT
                oi.*,
                ii.supplier_unit_cost AS quoted_unit_cost
            FROM supplier_order_items oi
            LEFT JOIN invoice_items ii
              ON ii.id=oi.invoice_item_id
            WHERE oi.order_id=?
            ORDER BY oi.id
            """,
            (order_id,),
        ).fetchall()
        receipt_ids = [
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM receiving_events WHERE order_id=? ORDER BY id DESC",
                (order_id,),
            ).fetchall()
        ]
        adjustments = connection.execute(
            """
            SELECT a.*,oi.description,oi.supplier_part_number
            FROM supplier_cost_adjustments a
            LEFT JOIN supplier_order_items oi
              ON oi.id=a.supplier_order_item_id
            WHERE a.supplier_order_id=?
            ORDER BY a.id DESC
            """,
            (order_id,),
        ).fetchall()
        exception_rows = connection.execute(
            """
            SELECT rei.*,oi.description,oi.supplier_part_number,
                   r.receipt_number,r.received_at,
                   COALESCE((SELECT SUM(rr.quantity)
                     FROM receiving_exception_resolutions rr
                     WHERE rr.exception_item_id=rei.id
                       AND rr.resolution NOT IN ('REPLACEMENT_EXPECTED','BACKORDER_CONFIRMED')),0)
                     AS resolved_quantity
            FROM receiving_exception_items rei
            JOIN receiving_events r ON r.id=rei.receipt_id
            JOIN supplier_order_items oi ON oi.id=rei.order_item_id
            WHERE r.order_id=? ORDER BY rei.id DESC
            """,
            (order_id,),
        ).fetchall()
        resolution_rows = connection.execute(
            """
            SELECT rr.* FROM receiving_exception_resolutions rr
            JOIN receiving_exception_items rei ON rei.id=rr.exception_item_id
            JOIN receiving_events r ON r.id=rei.receipt_id
            WHERE r.order_id=? ORDER BY rr.id DESC
            """,
            (order_id,),
        ).fetchall()
        backorder_rows = connection.execute(
            """
            SELECT be.*,oi.quantity_ordered,oi.quantity_received
            FROM supplier_order_item_backorder_events be
            JOIN supplier_order_items oi ON oi.id=be.order_item_id
            WHERE oi.order_id=? ORDER BY be.id DESC
            """,
            (order_id,),
        ).fetchall()
    result = dict(order)
    result_items = []
    for source in items:
        item = dict(source)
        confirmed = item["actual_unit_cost"] is not None
        actual_unit = (
            float(item["actual_unit_cost"])
            if confirmed else float(item["unit_cost"] or 0)
        )
        item["actual_confirmed"] = confirmed
        item["actual_display_unit_cost"] = round(actual_unit, 2)
        item["actual_line_cost"] = round(
            actual_unit * int(item["quantity_ordered"] or 0), 2
        )
        item["actual_unit_variance"] = (
            round(actual_unit - float(item["unit_cost"] or 0), 2)
            if confirmed else None
        )
        result_items.append(item)
    result["items"] = result_items
    result["receipts"] = [get_receipt(receipt_id) for receipt_id in receipt_ids]
    resolutions_by_exception = {}
    for source in resolution_rows:
        resolutions_by_exception.setdefault(int(source["exception_item_id"]), []).append(dict(source))
    result["receiving_exceptions"] = []
    for source in exception_rows:
        exception = dict(source)
        exception["unresolved_quantity"] = max(
            int(exception["quantity"] or 0) - int(exception["resolved_quantity"] or 0), 0
        )
        exception["resolutions"] = resolutions_by_exception.get(int(exception["id"]), [])
        result["receiving_exceptions"].append(exception)
    latest_backorders = {}
    for source in backorder_rows:
        item_id = int(source["order_item_id"])
        if item_id in latest_backorders:
            continue
        event = dict(source)
        remaining = max(int(event["quantity_ordered"] or 0) - int(event["quantity_received"] or 0), 0)
        declared = int(event["backordered_quantity"] or 0)
        event["declared_quantity"] = declared
        event["effective_quantity"] = min(declared, remaining)
        event["quantity_satisfied_by_receiving"] = max(declared - event["effective_quantity"], 0)
        event["active"] = event["event_kind"] != "RESOLVED" and event["effective_quantity"] > 0
        latest_backorders[item_id] = event
    result["backorders"] = latest_backorders
    result["actual_adjustments"] = [dict(row) for row in adjustments]
    result["actual_shipping_confirmed"] = result["actual_shipping_total"] is not None
    result["actual_shipping_display"] = round(
        float(
            result["actual_shipping_total"]
            if result["actual_shipping_total"] is not None
            else result["shipping_total"] or 0
        ),
        2,
    )
    result["actual_shipping_variance"] = (
        round(
            float(result["actual_shipping_total"])
            - float(result["shipping_total"] or 0),
            2,
        )
        if result["actual_shipping_total"] is not None else None
    )
    result["actual_confirmation_complete"] = (
        result["actual_shipping_confirmed"]
        and all(item["actual_confirmed"] for item in result_items)
    )
    return result


def record_actual_cost_adjustment(
    order_id: int,
    *,
    cost_kind: str,
    new_amount: float,
    reason: str,
    actor: str,
    request_id: str,
    supplier_order_item_id: int | None = None,
    supplier_reference: str = "",
):
    """Record an attributed, idempotent actual-cost confirmation."""
    kind = str(cost_kind or "").strip().upper()
    amount = round(float(new_amount), 2)
    reason = str(reason or "").strip()
    actor = str(actor or "").strip()
    request_id = str(request_id or "").strip()
    supplier_reference = str(supplier_reference or "").strip()
    if kind not in {"ITEM", "SHIPPING"}:
        raise HTTPException(status_code=400, detail="Actual cost kind must be ITEM or SHIPPING.")
    if amount < 0:
        raise HTTPException(status_code=400, detail="Actual cost cannot be negative.")
    if not reason:
        raise HTTPException(status_code=400, detail="Actual cost adjustment reason is required.")
    if not actor or actor.lower() == "system":
        raise HTTPException(status_code=400, detail="Actual cost adjustment actor is required.")
    if not request_id:
        raise HTTPException(status_code=400, detail="Actual cost adjustment request ID is required.")
    if kind == "ITEM" and supplier_order_item_id is None:
        raise HTTPException(status_code=400, detail="Select a supplier-order item.")
    if kind == "SHIPPING" and supplier_order_item_id is not None:
        raise HTTPException(status_code=400, detail="Shipping adjustments cannot reference an item.")

    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id FROM supplier_cost_adjustments WHERE supplier_order_id=? AND request_id=?",
            (order_id, request_id),
        ).fetchone()
        if existing is not None:
            connection.rollback()
            return get_order(order_id)
        order = connection.execute(
            "SELECT * FROM supplier_orders WHERE id=?", (order_id,)
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Supplier purchase not found.")
        if str(order["status"] or "").upper() not in ACTUAL_COST_STATUSES:
            raise HTTPException(
                status_code=409,
                detail="Actual costs may be confirmed only after supplier purchase placement.",
            )
        item_id = None
        if kind == "ITEM":
            item = connection.execute(
                "SELECT * FROM supplier_order_items WHERE id=? AND order_id=?",
                (supplier_order_item_id, order_id),
            ).fetchone()
            if item is None:
                raise HTTPException(status_code=404, detail="Supplier purchase item not found.")
            item_id = int(item["id"])
            old_amount = item["actual_unit_cost"]
            connection.execute(
                "UPDATE supplier_order_items SET actual_unit_cost=? WHERE id=?",
                (amount, item_id),
            )
            subject = item["description"]
        else:
            old_amount = order["actual_shipping_total"]
            connection.execute(
                "UPDATE supplier_orders SET actual_shipping_total=? WHERE id=?",
                (amount, order_id),
            )
            subject = "Shipping"
        connection.execute(
            """
            INSERT INTO supplier_cost_adjustments (
                supplier_order_id,supplier_order_item_id,cost_kind,
                old_amount,new_amount,reason,actor,request_id,supplier_reference
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                order_id,item_id,kind,old_amount,amount,reason,actor,
                request_id,supplier_reference,
            ),
        )
        summary = (
            f"{order['po_number']} actual {subject} cost confirmed at "
            f"${amount:.2f}."
        )
        write_audit(
            connection,
            action="SUPPLIER_ACTUAL_COST_ADJUSTED",
            entity_type="SUPPLIER_ORDER",
            entity_id=order_id,
            summary=summary,
            metadata={
                "invoice_id": order["invoice_id"], "job_id": int(order["job_id"]),
                "cost_kind": kind, "supplier_order_item_id": item_id,
                "old_amount": old_amount, "new_amount": amount,
                "reason": reason, "supplier_reference": supplier_reference,
            },
            actor=actor,
            request_id=request_id,
        )
        log_job_event(
            connection, job_id=int(order["job_id"]),
            event_type="SUPPLIER_ACTUAL_COST_ADJUSTED", icon="💲",
            message=summary,
        )
        generated_internal_path = None
        try:
            if order["invoice_id"] is not None:
                from legacy_app import load_invoice
                from plg_core.admin.service import invoice_financial_state
                from plg_core.documents.integrity import issue_current_internal_invoice_document
                invoice, invoice_items = load_invoice(connection, int(order["invoice_id"]))
                financial_state = invoice_financial_state(connection, int(order["invoice_id"]))
                generated_internal_path = issue_current_internal_invoice_document(
                    connection, invoice, invoice_items, financial_state
                )
            connection.commit()
        except Exception as exc:
            connection.rollback()
            if generated_internal_path:
                from pathlib import Path
                generated_path = Path(generated_internal_path)
                if generated_path.exists():
                    generated_path.unlink()
            raise HTTPException(
                status_code=500,
                detail=f"Actual cost was not recorded because the current internal invoice document could not be generated and verified: {exc}",
            ) from exc
    return get_order(order_id)


def get_receipt(receipt_id: int):
    with closing(get_connection()) as connection:
        receipt = connection.execute(
            """
            SELECT r.*,po.po_number,po.supplier_name,po.job_id,po.invoice_id,
                   j.job_number,i.invoice_number
            FROM receiving_events r
            JOIN supplier_orders po ON po.id=r.order_id
            JOIN jobs j ON j.id=po.job_id
            LEFT JOIN invoices i ON i.id=po.invoice_id
            WHERE r.id=?
            """,
            (receipt_id,),
        ).fetchone()
        if receipt is None:
            raise HTTPException(status_code=404, detail="Receipt not found.")
        items = connection.execute(
            """
            SELECT ri.id,ri.order_item_id,ri.quantity_received,
                   oi.supplier_part_number,oi.description,oi.quantity_ordered,
                   ja.manufacturer AS asset_manufacturer,
                   ja.model AS asset_model,ja.name AS asset_name,
                   ja.vin_pin_serial AS asset_serial,
                   COALESCE((
                       SELECT SUM(previous.quantity_received)
                       FROM receiving_event_items previous
                       JOIN receiving_events previous_receipt
                         ON previous_receipt.id=previous.receipt_id
                       WHERE previous.order_item_id=ri.order_item_id
                         AND previous_receipt.order_id=r.order_id
                         AND previous_receipt.id<=r.id
                   ),0) AS cumulative_received
            FROM receiving_event_items ri
            JOIN receiving_events r ON r.id=ri.receipt_id
            JOIN supplier_order_items oi ON oi.id=ri.order_item_id
            LEFT JOIN job_assets ja ON ja.id=oi.job_asset_id
            WHERE ri.receipt_id=?
            ORDER BY ri.id
            """,
            (receipt_id,),
        ).fetchall()
        order_items = connection.execute(
            """
            SELECT oi.id,oi.quantity_ordered,
                   COALESCE((
                       SELECT SUM(ri.quantity_received)
                       FROM receiving_event_items ri
                       JOIN receiving_events r ON r.id=ri.receipt_id
                       WHERE ri.order_item_id=oi.id AND r.id<=?
                   ),0) AS cumulative_received
            FROM supplier_order_items oi
            WHERE oi.order_id=?
            """,
            (receipt_id, receipt["order_id"]),
        ).fetchall()
        exceptions = connection.execute(
            """
            SELECT rei.*,oi.supplier_part_number,oi.description
            FROM receiving_exception_items rei
            JOIN supplier_order_items oi ON oi.id=rei.order_item_id
            WHERE rei.receipt_id=?
            ORDER BY rei.id
            """,
            (receipt_id,),
        ).fetchall()
    item_rows = []
    for source in items:
        item = dict(source)
        item["remaining"] = max(
            int(item["quantity_ordered"] or 0)
            - int(item["cumulative_received"] or 0),
            0,
        )
        item_rows.append(item)
    total_remaining = sum(
        max(
            int(row["quantity_ordered"] or 0)
            - int(row["cumulative_received"] or 0),
            0,
        )
        for row in order_items
    )
    total_accepted = sum(int(row["cumulative_received"] or 0) for row in order_items)
    result = dict(receipt)
    result["items"] = item_rows
    result["exceptions"] = [dict(row) for row in exceptions]
    result["quantity_received"] = sum(
        int(row["quantity_received"] or 0) for row in item_rows
    )
    result["total_remaining"] = total_remaining
    result["status_after"] = (
        "RECEIVED" if total_remaining == 0
        else "ORDERED" if total_accepted == 0
        else "PARTIAL"
    )
    result["status"] = result["status_after"]
    return result

def update_order_item_cost(
    order_id: int,
    item_id: int,
    unit_cost: float,
):
    unit_cost = round(
        float(unit_cost or 0),
        2,
    )

    if unit_cost < 0:
        raise HTTPException(
            status_code=400,
            detail="Supplier unit cost cannot be negative.",
        )

    with closing(get_connection()) as connection:
        order = connection.execute(
            """
            SELECT *
            FROM supplier_orders
            WHERE id=?
            """,
            (order_id,),
        ).fetchone()

        if order is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier purchase not found.",
            )

        status = str(
            order["status"] or ""
        ).strip().upper()

        if status != "DRAFT":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Supplier costs are locked once "
                    "the supplier purchase is marked ordered."
                ),
            )

        item = connection.execute(
            """
            SELECT *
            FROM supplier_order_items
            WHERE id=?
              AND order_id=?
            """,
            (item_id, order_id),
        ).fetchone()

        if item is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier purchase item not found.",
            )

        old_unit_cost = round(
            float(item["unit_cost"] or 0),
            2,
        )

        quantity = max(
            1,
            int(item["quantity_ordered"] or 1),
        )

        line_cost = round(
            unit_cost * quantity,
            2,
        )

        connection.execute(
            """
            UPDATE supplier_order_items
            SET unit_cost=?,
                line_cost=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                unit_cost,
                line_cost,
                item_id,
            ),
        )

        totals = connection.execute(
            """
            SELECT COALESCE(SUM(line_cost), 0) AS parts_total
            FROM supplier_order_items
            WHERE order_id=?
            """,
            (order_id,),
        ).fetchone()

        parts_total = round(
            float(totals["parts_total"] or 0),
            2,
        )

        shipping_total = round(
            float(order["shipping_total"] or 0),
            2,
        )

        order_total = round(
            parts_total + shipping_total,
            2,
        )

        connection.execute(
            """
            UPDATE supplier_orders
            SET parts_total=?,
                order_total=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                parts_total,
                order_total,
                order_id,
            ),
        )

        difference = round(
            unit_cost - old_unit_cost,
            2,
        )

        message = (
            f"{order['po_number']} supplier cost updated: "
            f"{item['description']} "
            f"${old_unit_cost:.2f} → ${unit_cost:.2f} "
            f"per unit. PO total ${order_total:.2f}."
        )

        write_audit(
            connection,
            action="SUPPLIER_ORDER_COST_UPDATED",
            entity_type="SUPPLIER_ORDER",
            entity_id=order_id,
            summary=message,
            metadata={
                "job_id": int(order["job_id"]),
                "invoice_id": order["invoice_id"],
                "order_item_id": int(item_id),
                "invoice_item_id": item["invoice_item_id"],
                "quantity": quantity,
                "old_unit_cost": old_unit_cost,
                "new_unit_cost": unit_cost,
                "unit_cost_variance": difference,
                "line_cost": line_cost,
                "parts_total": parts_total,
                "order_total": order_total,
            },
        )

        log_job_event(
            connection,
            job_id=int(order["job_id"]),
            event_type="SUPPLIER_ORDER_COST_UPDATED",
            icon="💲",
            message=message,
        )

        connection.commit()

    return get_order(order_id)


def update_order(
    order_id: int,
    shipping_total: float = 0,
    expected_at: str = "",
    notes: str = "",
):
    shipping_total = round(
        float(shipping_total or 0),
        2,
    )
    expected_at = str(expected_at or "").strip()
    notes = str(notes or "").strip()

    if shipping_total < 0:
        raise HTTPException(
            status_code=400,
            detail="Shipping total cannot be negative.",
        )

    with closing(get_connection()) as connection:
        order = connection.execute(
            """
            SELECT *
            FROM supplier_orders
            WHERE id=?
            """,
            (order_id,),
        ).fetchone()

        if order is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier purchase not found.",
            )

        status = str(
            order["status"] or ""
        ).strip().upper()

        if status != "DRAFT":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Only a draft supplier purchase can be edited."
                ),
            )

        parts_total = round(
            float(order["parts_total"] or 0),
            2,
        )

        order_total = round(
            parts_total + shipping_total,
            2,
        )

        connection.execute(
            """
            UPDATE supplier_orders
            SET shipping_total=?,
                order_total=?,
                expected_at=?,
                notes=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                shipping_total,
                order_total,
                expected_at or None,
                notes,
                order_id,
            ),
        )

        message = (
            f"{order['po_number']} updated for "
            f"{order['supplier_name']}. "
            f"Order total ${order_total:.2f}."
        )

        write_audit(
            connection,
            action="SUPPLIER_ORDER_UPDATED",
            entity_type="SUPPLIER_ORDER",
            entity_id=order_id,
            summary=message,
            metadata={
                "job_id": int(order["job_id"]),
                "invoice_id": order["invoice_id"],
                "shipping_total": shipping_total,
                "expected_at": expected_at,
                "order_total": order_total,
            },
        )

        log_job_event(
            connection,
            job_id=int(order["job_id"]),
            event_type="SUPPLIER_ORDER_UPDATED",
            icon="✎",
            message=message,
        )

        connection.commit()

    return get_order(order_id)


def place_order(
    order_id: int,
    *,
    actor: str = "system",
    request_id: str = "",
    source_path: str = "",
):
    with closing(get_connection()) as connection:
        order = connection.execute(
            """
            SELECT po.*, i.invoice_number
            FROM supplier_orders po
            LEFT JOIN invoices i
              ON i.id=po.invoice_id
            WHERE po.id=?
            """,
            (order_id,),
        ).fetchone()

        if order is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier purchase not found.",
            )

        current_status = str(
            order["status"] or ""
        ).strip().upper()

        if current_status == "ORDERED":
            return get_order(order_id)

        if current_status != "DRAFT":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Only a draft supplier purchase can be "
                    "marked ordered."
                ),
            )

        connection.execute(
            """
            UPDATE supplier_orders
            SET status='ORDERED',
                ordered_at=COALESCE(
                    ordered_at,
                    CURRENT_TIMESTAMP
                ),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (order_id,),
        )

        # The authoritative ORDERED state and timestamp are established in
        # this transaction before the immutable purchasing document is built.
        from plg_core.documents.integrity import issue_supplier_order_document
        issue_supplier_order_document(connection, order_id)

        write_audit(
            connection,
            action="SUPPLIER_ORDER_PLACED",
            entity_type="SUPPLIER_ORDER",
            entity_id=order_id,
            summary=f"{order['po_number']} marked ordered",
            metadata={
                "invoice_id": order["invoice_id"],
                "job_id": int(order["job_id"]),
                "source_path": str(source_path or ""),
            },
            actor=str(actor or "system"),
            request_id=str(request_id or ""),
        )

        log_job_event(
            connection,
            job_id=int(order["job_id"]),
            event_type="SUPPLIER_ORDER_PLACED",
            icon="🛒",
            message=(
                f"{order['po_number']} placed with "
                f"{order['supplier_name']}"
            ),
        )

        if order["invoice_id"]:
            remaining = connection.execute(
                """
                SELECT COUNT(*)
                FROM supplier_orders
                WHERE invoice_id=?
                  AND UPPER(
                      COALESCE(status, 'DRAFT')
                  )='DRAFT'
                """,
                (order["invoice_id"],),
            ).fetchone()[0]

            if int(remaining or 0) == 0:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status='ORDERED'
                    WHERE id=?
                    """,
                    (order["job_id"],),
                )

                log_job_event(
                    connection,
                    job_id=int(order["job_id"]),
                    event_type="PURCHASING_STARTED",
                    icon="✅",
                    message=(
                        "All supplier purchases placed for "
                        f"{order['invoice_number'] or 'invoice'}"
                    ),
                )

        connection.commit()

    return get_order(order_id)

def record_receipt(
    order_id: int,
    payload: ReceiptCreate,
    *,
    actor: str = "system",
    request_id: str = "",
    source_path: str = "",
):
    if not payload.items and not payload.exceptions:
        raise HTTPException(
            status_code=400,
            detail="Receipt requires an accepted quantity or an exception.",
        )

    incoming_ids = [
        int(item.order_item_id)
        for item in payload.items
    ]

    if len(set(incoming_ids)) != len(incoming_ids):
        raise HTTPException(
            status_code=400,
            detail=(
                "Each supplier purchase item may appear only "
                "once on a receipt."
            ),
        )

    exception_keys = [
        (int(item.order_item_id), str(item.disposition).upper())
        for item in payload.exceptions
    ]
    if len(set(exception_keys)) != len(exception_keys):
        raise HTTPException(
            status_code=400,
            detail="Each exception disposition may appear once per supplier purchase item.",
        )

    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        idempotency_key = str(payload.idempotency_key or "").strip()
        request_fingerprint = _receipt_request_fingerprint(order_id, payload)
        if idempotency_key:
            existing_receipt = connection.execute(
                "SELECT id,order_id,request_fingerprint FROM receiving_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing_receipt is not None:
                if int(existing_receipt["order_id"]) != int(order_id):
                    raise HTTPException(
                        status_code=409,
                        detail="Receipt idempotency key belongs to another Supplier Order.",
                    )
                existing_fingerprint = str(
                    existing_receipt["request_fingerprint"] or ""
                )
                if existing_fingerprint and existing_fingerprint != request_fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Receipt idempotency key was already used with different content.",
                    )
                connection.rollback()
                _ensure_receiving_document(int(existing_receipt["id"]))
                result = get_receipt(int(existing_receipt["id"]))
                result["replayed"] = True
                return result
        order = connection.execute(
            """
            SELECT *
            FROM supplier_orders
            WHERE id=?
            """,
            (order_id,),
        ).fetchone()

        if order is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier purchase not found.",
            )

        current_status = str(
            order["status"] or ""
        ).strip().upper()

        if current_status == "DRAFT":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Parts cannot be received until the "
                    "supplier purchase has been marked ordered."
                ),
            )

        if current_status == "RECEIVED":
            raise HTTPException(
                status_code=409,
                detail=(
                    "This supplier purchase is already fully received."
                ),
            )

        if current_status not in {
            "ORDERED",
            "PARTIAL",
        }:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Supplier purchase is not in a receivable status."
                ),
            )

        validated = []
        validated_items = {}

        for incoming in payload.items:
            item = connection.execute(
                """
                SELECT *
                FROM supplier_order_items
                WHERE id=?
                  AND order_id=?
                """,
                (
                    incoming.order_item_id,
                    order_id,
                ),
            ).fetchone()

            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"Order item "
                        f"{incoming.order_item_id} not found."
                    ),
                )

            ordered = int(
                item["quantity_ordered"] or 0
            )
            already_received = int(
                item["quantity_received"] or 0
            )
            remaining = (
                ordered - already_received
            )

            if remaining <= 0:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"{item['description']} is already "
                        "fully received."
                    ),
                )

            quantity = int(
                incoming.quantity_received
            )

            if quantity > remaining:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Only {remaining} remain for "
                        f"{item['description']}."
                    ),
                )

            validated.append(
                (
                    int(item["id"]),
                    quantity,
                    str(item["description"] or "Part"),
                )
            )
            validated_items[int(item["id"])] = item

        validated_exceptions = []
        accepted_by_item = {item_id: quantity for item_id, quantity, _ in validated}
        for incoming in payload.exceptions:
            item_id = int(incoming.order_item_id)
            item = validated_items.get(item_id)
            if item is None:
                item = connection.execute(
                    "SELECT * FROM supplier_order_items WHERE id=? AND order_id=?",
                    (item_id, order_id),
                ).fetchone()
            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Order item {item_id} not found.",
                )
            validated_items[item_id] = item
            if str(incoming.disposition).upper() == "SHORT":
                remaining_after_accepted = (
                    int(item["quantity_ordered"] or 0)
                    - int(item["quantity_received"] or 0)
                    - int(accepted_by_item.get(item_id, 0))
                )
                if int(incoming.quantity) > remaining_after_accepted:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Short quantity exceeds the remaining ordered quantity "
                            "after accepted units on this receipt."
                        ),
                    )
            validated_exceptions.append(
                (
                    item_id,
                    str(incoming.disposition).upper(),
                    int(incoming.quantity),
                    incoming.reason.strip(),
                    incoming.notes.strip(),
                    incoming.supplier_reference.strip(),
                    incoming.evidence_reference.strip(),
                )
            )

        cur = connection.execute(
            """
            INSERT INTO receiving_events (
                order_id,notes,receiver,request_id,source_path,idempotency_key,
                event_kind,request_fingerprint
            )
            VALUES (?, ?, ?, ?, ?, ?, 'RECEIPT', ?)
            """,
            (
                order_id,
                payload.notes.strip(),
                payload.receiver.strip(),
                str(request_id or ""),
                str(source_path or ""),
                idempotency_key or None,
                request_fingerprint,
            ),
        )

        receipt_id = int(cur.lastrowid)
        receipt_number = (
            f"PPS-RCPT-{receipt_id:04d}"
        )

        connection.execute(
            """
            UPDATE receiving_events
            SET receipt_number=?
            WHERE id=?
            """,
            (
                receipt_number,
                receipt_id,
            ),
        )

        total_received = 0

        for item_id, quantity, _description in validated:
            connection.execute(
                """
                INSERT INTO receiving_event_items (
                    receipt_id,
                    order_item_id,
                    quantity_received
                )
                VALUES (?, ?, ?)
                """,
                (
                    receipt_id,
                    item_id,
                    quantity,
                ),
            )

            updated = connection.execute(
                """
                UPDATE supplier_order_items
                SET quantity_received=
                        quantity_received+?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                  AND quantity_received+?<=quantity_ordered
                """,
                (
                    quantity,
                    item_id,
                    quantity,
                ),
            )
            if updated.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Receipt conflicts with another receiving update. Reload and retry.",
                )

            total_received += quantity

        for (
            item_id,
            disposition,
            quantity,
            reason,
            notes,
            supplier_reference,
            evidence_reference,
        ) in validated_exceptions:
            connection.execute(
                """
                INSERT INTO receiving_exception_items (
                    receipt_id,order_item_id,disposition,quantity,reason,notes,
                    supplier_reference,evidence_reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    item_id,
                    disposition,
                    quantity,
                    reason,
                    notes,
                    supplier_reference,
                    evidence_reference,
                ),
            )

        new_status = _accepted_order_status(connection, order_id)

        summary = (
            f"{receipt_number}: "
            f"{total_received} "
            f"part{'s' if total_received != 1 else ''} "
            f"received from {order['supplier_name']}. "
            f"Purchase status {new_status}."
        )
        if total_received:
            write_audit(
                connection,
                action="PARTS_RECEIVED",
                entity_type="SUPPLIER_ORDER",
                entity_id=order_id,
                summary=summary,
                metadata={
                    "receipt_id": receipt_id,
                    "receipt_number": receipt_number,
                    "quantity_received": total_received,
                    "status": new_status,
                    "job_id": int(order["job_id"]),
                    "invoice_id": order["invoice_id"],
                    "receiver": payload.receiver.strip(),
                    "source_path": str(source_path or ""),
                },
                actor=str(actor or "system"),
                request_id=str(request_id or ""),
            )
            log_job_event(
                connection,
                job_id=int(order["job_id"]),
                event_type="PARTS_RECEIVED",
                icon="📦",
                message=summary,
            )
        if validated_exceptions:
            exception_total = sum(row[2] for row in validated_exceptions)
            write_audit(
                connection,
                action="RECEIVING_EXCEPTION_RECORDED",
                entity_type="SUPPLIER_ORDER",
                entity_id=order_id,
                summary=f"{receipt_number}: {exception_total} exception unit(s) recorded.",
                metadata={
                    "receipt_id": receipt_id,
                    "receipt_number": receipt_number,
                    "exceptions": [
                        {"order_item_id": row[0], "disposition": row[1], "quantity": row[2]}
                        for row in validated_exceptions
                    ],
                    "status": new_status,
                },
                actor=str(actor or "system"),
                request_id=str(request_id or ""),
            )

        _apply_receiving_completion(connection, order, new_status)

        connection.commit()
    _ensure_receiving_document(receipt_id)
    result = get_receipt(receipt_id)
    result["replayed"] = False
    return result


def resolve_receiving_exception(
    exception_item_id: int,
    payload: ReceivingExceptionResolutionCreate,
    *,
    actor: str = "system",
    request_id: str = "",
):
    """Append a non-inventory resolution to a receiving exception."""
    if payload.resolution == "CLEARED_TO_ACCEPTED":
        raise HTTPException(
            status_code=400,
            detail="Use clear_quarantined_exception to move cleared units into accepted inventory.",
        )
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        exception = connection.execute(
            """
            SELECT rei.*,r.order_id,po.job_id
            FROM receiving_exception_items rei
            JOIN receiving_events r ON r.id=rei.receipt_id
            JOIN supplier_orders po ON po.id=r.order_id
            WHERE rei.id=?
            """,
            (exception_item_id,),
        ).fetchone()
        if exception is None:
            raise HTTPException(status_code=404, detail="Receiving exception not found.")
        allowed_resolutions = EXCEPTION_RESOLUTION_MATRIX.get(
            str(exception["disposition"]), set()
        )
        if payload.resolution not in allowed_resolutions:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{payload.resolution} is not valid for "
                    f"{exception['disposition']}."
                ),
            )
        existing = connection.execute(
            """
            SELECT * FROM receiving_exception_resolutions
            WHERE exception_item_id=? AND idempotency_key=?
            """,
            (exception_item_id, payload.idempotency_key.strip()),
        ).fetchone()
        if existing is not None:
            expected = (
                int(payload.quantity), payload.resolution, payload.related_receipt_id,
                payload.reason.strip(), payload.notes.strip(),
                payload.supplier_reference.strip(), payload.evidence_reference.strip(),
            )
            actual = (
                int(existing["quantity"]), existing["resolution"],
                existing["related_receipt_id"], existing["reason"], existing["notes"],
                existing["supplier_reference"], existing["evidence_reference"],
            )
            if actual != expected:
                raise HTTPException(
                    status_code=409,
                    detail="Resolution idempotency key was already used with different content.",
                )
            connection.rollback()
            result = dict(existing)
            result["replayed"] = True
            result["order_id"] = int(exception["order_id"])
            return result
        if payload.resolution == "REPLACED_BY_RECEIPT":
            _validate_related_accepted_receipt(
                connection,
                exception=exception,
                related_receipt_id=payload.related_receipt_id,
                quantity=int(payload.quantity),
            )
        elif payload.related_receipt_id is not None:
            related = connection.execute(
                "SELECT order_id FROM receiving_events WHERE id=?",
                (payload.related_receipt_id,),
            ).fetchone()
            if related is None or int(related["order_id"]) != int(exception["order_id"]):
                raise HTTPException(status_code=409, detail="Related receipt belongs to another order.")
        consuming_placeholders = ",".join(
            "?" for _ in INTERIM_EXCEPTION_RESOLUTIONS
        )
        resolved = connection.execute(
            f"""
            SELECT COALESCE(SUM(quantity),0)
            FROM receiving_exception_resolutions
            WHERE exception_item_id=?
              AND resolution NOT IN ({consuming_placeholders})
            """,
            (exception_item_id, *sorted(INTERIM_EXCEPTION_RESOLUTIONS)),
        ).fetchone()[0]
        if (
            payload.resolution not in INTERIM_EXCEPTION_RESOLUTIONS
            and int(resolved or 0) + int(payload.quantity) > int(exception["quantity"])
        ):
            raise HTTPException(status_code=409, detail="Resolution exceeds unresolved exception quantity.")
        if (
            payload.resolution in INTERIM_EXCEPTION_RESOLUTIONS
            and int(payload.quantity) > int(exception["quantity"]) - int(resolved or 0)
        ):
            raise HTTPException(status_code=409, detail="Interim state exceeds unresolved exception quantity.")
        cur = connection.execute(
            """
            INSERT INTO receiving_exception_resolutions (
                exception_item_id,quantity,resolution,related_receipt_id,actor,reason,
                notes,supplier_reference,evidence_reference,request_id,idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exception_item_id, payload.quantity, payload.resolution,
                payload.related_receipt_id, str(actor or "system"), payload.reason.strip(),
                payload.notes.strip(), payload.supplier_reference.strip(),
                payload.evidence_reference.strip(), str(request_id or ""),
                payload.idempotency_key.strip(),
            ),
        )
        resolution_id = int(cur.lastrowid)
        write_audit(
            connection,
            action="RECEIVING_EXCEPTION_RESOLVED",
            entity_type="RECEIVING_EXCEPTION",
            entity_id=exception_item_id,
            summary=f"Resolved {payload.quantity} {exception['disposition']} unit(s) as {payload.resolution}.",
            metadata={
                "order_id": int(exception["order_id"]),
                "order_item_id": int(exception["order_item_id"]),
                "quantity": int(payload.quantity),
                "resolution": payload.resolution,
                "related_receipt_id": payload.related_receipt_id,
            },
            actor=str(actor or "system"),
            request_id=str(request_id or ""),
        )
        connection.commit()
        result = dict(connection.execute(
            "SELECT * FROM receiving_exception_resolutions WHERE id=?",
            (resolution_id,),
        ).fetchone())
    result["replayed"] = False
    result["order_id"] = int(exception["order_id"])
    return result


def record_backorder_event(
    order_item_id: int,
    payload: BackorderEventCreate,
    *,
    actor: str = "system",
    request_id: str = "",
):
    """Append a supplier-line backorder state without changing ordered inventory."""
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute(
            """
            SELECT oi.*,po.job_id
            FROM supplier_order_items oi
            JOIN supplier_orders po ON po.id=oi.order_id
            WHERE oi.id=?
            """,
            (order_item_id,),
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Supplier purchase item not found.")
        existing = connection.execute(
            """
            SELECT * FROM supplier_order_item_backorder_events
            WHERE order_item_id=? AND idempotency_key=?
            """,
            (order_item_id, payload.idempotency_key.strip()),
        ).fetchone()
        if existing is not None:
            expected = (
                payload.event_kind, int(payload.backordered_quantity), payload.reason.strip(),
                payload.notes.strip(), payload.supplier_reference.strip(), payload.related_receipt_id,
            )
            actual = (
                existing["event_kind"], int(existing["backordered_quantity"]),
                existing["reason"], existing["notes"], existing["supplier_reference"],
                existing["related_receipt_id"],
            )
            if actual != expected:
                raise HTTPException(
                    status_code=409,
                    detail="Backorder idempotency key was already used with different content.",
                )
            connection.rollback()
            result = dict(existing)
            result["replayed"] = True
            result["order_id"] = int(item["order_id"])
            return result
        remaining = int(item["quantity_ordered"] or 0) - int(item["quantity_received"] or 0)
        quantity = int(payload.backordered_quantity)
        latest = connection.execute(
            """
            SELECT * FROM supplier_order_item_backorder_events
            WHERE order_item_id=? ORDER BY id DESC LIMIT 1
            """,
            (order_item_id,),
        ).fetchone()
        if payload.event_kind == "RESOLVED" and quantity != 0:
            raise HTTPException(status_code=400, detail="Resolved backorders must record zero quantity.")
        if payload.event_kind != "RESOLVED" and (quantity <= 0 or quantity > remaining):
            raise HTTPException(
                status_code=409,
                detail=f"Backordered quantity must be between 1 and the remaining {remaining} unit(s).",
            )
        fulfilled_quantity = 0
        if payload.event_kind == "RESOLVED":
            if latest is None or str(latest["event_kind"]) not in {"DECLARED", "UPDATED"}:
                raise HTTPException(
                    status_code=409,
                    detail="There is no active backorder declaration to resolve.",
                )
            if payload.related_receipt_id is not None:
                if int(payload.related_receipt_id) <= int(latest["receipt_id_cutoff"] or 0):
                    raise HTTPException(
                        status_code=409,
                        detail="Fulfillment receipt must postdate the active backorder declaration.",
                    )
                capacity = _accepted_receipt_capacity(
                    connection,
                    related_receipt_id=int(payload.related_receipt_id),
                    order_item_id=int(order_item_id),
                    order_id=int(item["order_id"]),
                )
                accepted_before_receipt = connection.execute(
                    """
                    SELECT COALESCE(SUM(ri.quantity_received),0)
                    FROM receiving_event_items ri
                    JOIN receiving_events r ON r.id=ri.receipt_id
                    WHERE ri.order_item_id=? AND r.id<?
                    """,
                    (order_item_id, int(payload.related_receipt_id)),
                ).fetchone()[0]
                remaining_before_receipt = max(
                    int(item["quantity_ordered"] or 0)
                    - int(accepted_before_receipt or 0),
                    0,
                )
                fulfilled_quantity = min(
                    int(latest["backordered_quantity"] or 0),
                    remaining_before_receipt,
                )
                if fulfilled_quantity <= 0 or int(capacity["remaining"]) < fulfilled_quantity:
                    raise HTTPException(
                        status_code=409,
                        detail="Related receipt has insufficient unallocated accepted quantity to fulfill the active backorder.",
                    )
        receipt_id_cutoff = connection.execute(
            """
            SELECT COALESCE(MAX(id),0) FROM receiving_events WHERE order_id=?
            """,
            (int(item["order_id"]),),
        ).fetchone()[0]
        cur = connection.execute(
            """
            INSERT INTO supplier_order_item_backorder_events (
                order_item_id,event_kind,backordered_quantity,reason,notes,
                supplier_reference,actor,request_id,idempotency_key,related_receipt_id,
                fulfilled_quantity,receipt_id_cutoff
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_item_id, payload.event_kind, quantity, payload.reason.strip(),
                payload.notes.strip(), payload.supplier_reference.strip(),
                str(actor or "system"), str(request_id or ""),
                payload.idempotency_key.strip(), payload.related_receipt_id,
                fulfilled_quantity, int(receipt_id_cutoff or 0),
            ),
        )
        event_id = int(cur.lastrowid)
        write_audit(
            connection,
            action="SUPPLIER_BACKORDER_UPDATED",
            entity_type="SUPPLIER_ORDER_ITEM",
            entity_id=order_item_id,
            summary=f"Backorder {payload.event_kind.lower()}: {quantity} unit(s).",
            metadata={
                "order_id": int(item["order_id"]),
                "event_kind": payload.event_kind,
                "backordered_quantity": quantity,
            },
            actor=str(actor or "system"),
            request_id=str(request_id or ""),
        )
        connection.commit()
        result = dict(connection.execute(
            "SELECT * FROM supplier_order_item_backorder_events WHERE id=?",
            (event_id,),
        ).fetchone())
    result["replayed"] = False
    result["order_id"] = int(item["order_id"])
    return result


def get_current_backorder(order_item_id: int):
    """Return immutable declared state clamped to the current remaining obligation."""
    with closing(get_connection()) as connection:
        item = connection.execute(
            "SELECT quantity_ordered,quantity_received FROM supplier_order_items WHERE id=?",
            (order_item_id,),
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Supplier purchase item not found.")
        latest = connection.execute(
            """
            SELECT * FROM supplier_order_item_backorder_events
            WHERE order_item_id=? ORDER BY id DESC LIMIT 1
            """,
            (order_item_id,),
        ).fetchone()
    remaining = max(
        int(item["quantity_ordered"] or 0) - int(item["quantity_received"] or 0),
        0,
    )
    declared = int(latest["backordered_quantity"] or 0) if latest else 0
    active = min(declared, remaining)
    return {
        "order_item_id": int(order_item_id),
        "latest_event": dict(latest) if latest else None,
        "declared_quantity": declared,
        "remaining_quantity": remaining,
        "active_quantity": active,
        "quantity_satisfied_by_receiving": max(declared - active, 0),
        "partially_satisfied_by_receiving": declared > active,
    }


def clear_quarantined_exception(
    exception_item_id: int,
    *,
    quantity: int,
    reason: str,
    idempotency_key: str,
    receiver: str = "",
    notes: str = "",
    actor: str = "system",
    request_id: str = "",
):
    """Atomically clear quarantined units onto the accepted-inventory path."""
    if int(quantity) <= 0 or not str(reason or "").strip() or not str(idempotency_key or "").strip():
        raise HTTPException(status_code=400, detail="Clearance requires quantity, reason, and idempotency key.")
    receipt_key = f"exception-clearance:{exception_item_id}:{idempotency_key.strip()}"
    fingerprint = _clearance_request_fingerprint(
        exception_item_id,
        quantity=quantity,
        reason=reason,
        receiver=receiver,
        notes=notes,
    )
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        exception = connection.execute(
            """
            SELECT rei.*,r.order_id,po.id AS supplier_order_id,po.job_id,
                   po.supplier_name,oi.description,
                   oi.quantity_ordered,oi.quantity_received
            FROM receiving_exception_items rei
            JOIN receiving_events r ON r.id=rei.receipt_id
            JOIN supplier_orders po ON po.id=r.order_id
            JOIN supplier_order_items oi ON oi.id=rei.order_item_id
            WHERE rei.id=?
            """,
            (exception_item_id,),
        ).fetchone()
        if exception is None:
            raise HTTPException(status_code=404, detail="Receiving exception not found.")
        if exception["disposition"] != "QUARANTINED":
            raise HTTPException(status_code=409, detail="Only quarantined quantities can be cleared to accepted.")
        existing = connection.execute(
            """
            SELECT rr.*,r.id AS clearance_receipt_id,r.request_fingerprint
            FROM receiving_exception_resolutions rr
            JOIN receiving_events r ON r.id=rr.related_receipt_id
            WHERE rr.exception_item_id=? AND rr.idempotency_key=?
            """,
            (exception_item_id, idempotency_key.strip()),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"] or "") != fingerprint:
                raise HTTPException(status_code=409, detail="Clearance key was already used with different content.")
            connection.rollback()
            _ensure_receiving_document(int(existing["clearance_receipt_id"]))
            result = get_receipt(int(existing["clearance_receipt_id"]))
            result["replayed"] = True
            return result
        consuming_placeholders = ",".join(
            "?" for _ in INTERIM_EXCEPTION_RESOLUTIONS
        )
        resolved = connection.execute(
            f"""
            SELECT COALESCE(SUM(quantity),0)
            FROM receiving_exception_resolutions
            WHERE exception_item_id=?
              AND resolution NOT IN ({consuming_placeholders})
            """,
            (exception_item_id, *sorted(INTERIM_EXCEPTION_RESOLUTIONS)),
        ).fetchone()[0]
        if int(resolved or 0) + int(quantity) > int(exception["quantity"]):
            raise HTTPException(status_code=409, detail="Clearance exceeds unresolved quarantine quantity.")
        remaining = int(exception["quantity_ordered"]) - int(exception["quantity_received"])
        if int(quantity) > remaining:
            raise HTTPException(status_code=409, detail="Clearance exceeds remaining ordered quantity.")
        cur = connection.execute(
            """
            INSERT INTO receiving_events (
                order_id,notes,receiver,request_id,source_path,idempotency_key,
                event_kind,request_fingerprint
            ) VALUES (?, ?, ?, ?, '', ?, 'EXCEPTION_CLEARANCE', ?)
            """,
            (
                exception["order_id"], notes.strip(), receiver.strip(), str(request_id or ""),
                receipt_key, fingerprint,
            ),
        )
        receipt_id = int(cur.lastrowid)
        receipt_number = f"PPS-RCPT-{receipt_id:04d}"
        connection.execute("UPDATE receiving_events SET receipt_number=? WHERE id=?", (receipt_number, receipt_id))
        connection.execute(
            "INSERT INTO receiving_event_items(receipt_id,order_item_id,quantity_received) VALUES (?, ?, ?)",
            (receipt_id, exception["order_item_id"], quantity),
        )
        updated = connection.execute(
            """
            UPDATE supplier_order_items
            SET quantity_received=quantity_received+?,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND quantity_received+?<=quantity_ordered
            """,
            (quantity, exception["order_item_id"], quantity),
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=409, detail="Clearance conflicts with another receiving update.")
        connection.execute(
            """
            INSERT INTO receiving_exception_resolutions (
                exception_item_id,quantity,resolution,related_receipt_id,actor,reason,
                request_id,idempotency_key
            ) VALUES (?, ?, 'CLEARED_TO_ACCEPTED', ?, ?, ?, ?, ?)
            """,
            (
                exception_item_id, quantity, receipt_id, str(actor or "system"),
                reason.strip(), str(request_id or ""), idempotency_key.strip(),
            ),
        )
        new_status = _accepted_order_status(connection, int(exception["order_id"]))
        order_context = {
            "id": int(exception["supplier_order_id"]),
            "job_id": int(exception["job_id"]),
        }
        accepted_summary = (
            f"{receipt_number}: {quantity} quarantined unit(s) cleared and accepted. "
            f"Purchase status {new_status}."
        )
        write_audit(
            connection,
            action="PARTS_RECEIVED",
            entity_type="SUPPLIER_ORDER",
            entity_id=int(exception["order_id"]),
            summary=accepted_summary,
            metadata={
                "receipt_id": receipt_id,
                "receipt_number": receipt_number,
                "quantity_received": int(quantity),
                "status": new_status,
                "job_id": int(exception["job_id"]),
                "event_kind": "EXCEPTION_CLEARANCE",
            },
            actor=str(actor or "system"),
            request_id=str(request_id or ""),
        )
        log_job_event(
            connection,
            job_id=int(exception["job_id"]),
            event_type="PARTS_RECEIVED",
            icon="📦",
            message=accepted_summary,
        )
        _apply_receiving_completion(connection, order_context, new_status)
        write_audit(
            connection,
            action="RECEIVING_EXCEPTION_RESOLVED",
            entity_type="RECEIVING_EXCEPTION",
            entity_id=exception_item_id,
            summary=f"Cleared {quantity} quarantined unit(s) to accepted inventory.",
            metadata={"receipt_id": receipt_id, "quantity": quantity, "status": new_status},
            actor=str(actor or "system"), request_id=str(request_id or ""),
        )
        connection.commit()
    _ensure_receiving_document(receipt_id)
    result = get_receipt(receipt_id)
    result["replayed"] = False
    return result


def _delivery_availability_rows(connection, job_id: int) -> list[dict]:
    """The single received - delivered - READY reservation projection."""
    rows = connection.execute(
        """
        SELECT oi.id,oi.description,oi.supplier_part_number,
               oi.quantity_ordered,oi.quantity_received,oi.job_asset_id,
               po.id AS order_id,po.po_number,po.supplier_name,
               COALESCE(SUM(CASE WHEN d.status='DELIVERED'
                   THEN di.quantity_delivered ELSE 0 END),0) AS quantity_delivered,
               COALESCE(SUM(CASE WHEN d.status='READY'
                   THEN di.quantity_delivered ELSE 0 END),0) AS quantity_reserved,
               ja.name AS asset_name,ja.manufacturer AS asset_manufacturer,
               ja.model AS asset_model,ja.vin_pin_serial AS asset_serial
        FROM supplier_order_items oi
        JOIN supplier_orders po ON po.id=oi.order_id
        LEFT JOIN delivery_items di ON di.order_item_id=oi.id
        LEFT JOIN deliveries d ON d.id=di.delivery_id
        LEFT JOIN job_assets ja ON ja.id=oi.job_asset_id
        WHERE po.job_id=?
        GROUP BY oi.id
        ORDER BY po.id,oi.id
        """,
        (job_id,),
    ).fetchall()
    result = []
    for source in rows:
        item = dict(source)
        item["available_to_deliver"] = max(
            int(item["quantity_received"] or 0)
            - int(item["quantity_delivered"] or 0)
            - int(item["quantity_reserved"] or 0),
            0,
        )
        result.append(item)
    return result


def get_delivery_availability(job_id: int) -> list[dict]:
    with closing(get_connection()) as connection:
        return _delivery_availability_rows(connection, job_id)


def _delivery_machine_context(items: list[dict], fallback: dict) -> str:
    machines = []
    for item in items:
        identity = " ".join(
            str(value).strip()
            for value in (
                item.get("asset_manufacturer"),
                item.get("asset_model") or item.get("asset_name"),
            )
            if str(value or "").strip()
        )
        serial = str(item.get("asset_serial") or "").strip()
        value = identity + (f" · {serial}" if serial else "")
        if value and value not in machines:
            machines.append(value)
    if len(machines) > 1:
        return "Multiple job assets"
    if machines:
        return machines[0]
    identity = " ".join(
        str(value).strip()
        for value in (fallback.get("manufacturer"), fallback.get("machine"))
        if str(value or "").strip()
    )
    serial = str(fallback.get("pin_serial") or "").strip()
    return identity + (f" · {serial}" if serial else "") or "Not assigned"


def get_delivery(delivery_id: int):
    with closing(get_connection()) as connection:
        delivery = connection.execute(
            """
            SELECT d.*,j.job_number,j.customer,j.company,j.manufacturer,
                   j.machine,j.pin_serial,j.status AS job_status,i.invoice_number
            FROM deliveries d
            JOIN jobs j ON j.id=d.job_id
            LEFT JOIN invoices i ON i.id=d.invoice_id
            WHERE d.id=?
            """,
            (delivery_id,),
        ).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        items = connection.execute(
            """
            SELECT di.*,oi.description,oi.supplier_part_number,oi.job_asset_id,
                   po.po_number,po.supplier_name,
                   ja.name AS asset_name,ja.manufacturer AS asset_manufacturer,
                   ja.model AS asset_model,ja.vin_pin_serial AS asset_serial
            FROM delivery_items di
            JOIN supplier_order_items oi ON oi.id=di.order_item_id
            JOIN supplier_orders po ON po.id=oi.order_id
            LEFT JOIN job_assets ja ON ja.id=oi.job_asset_id
            WHERE di.delivery_id=? ORDER BY di.id
            """,
            (delivery_id,),
        ).fetchall()
    result = dict(delivery)
    result["delivery_id"] = int(result["id"])
    result["delivery_number"] = f"PPS-DEL-{int(result['id']):04d}"
    result["items"] = [dict(row) for row in items]
    result["quantity_total"] = sum(
        int(row["quantity_delivered"] or 0) for row in items
    )
    result["machine_context"] = _delivery_machine_context(result["items"], result)
    return result


def get_delivery_workspace(job_id: int):
    with closing(get_connection()) as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        items = _delivery_availability_rows(connection, job_id)
        delivery_ids = [
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM deliveries WHERE job_id=? ORDER BY id DESC",
                (job_id,),
            ).fetchall()
        ]
        invoice = connection.execute(
            "SELECT invoice_number FROM invoices WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    all_deliveries = [get_delivery(delivery_id) for delivery_id in delivery_ids]
    ready = next((row for row in all_deliveries if row["status"] == "READY"), None)
    deliveries = [row for row in all_deliveries if row["status"] != "READY"]
    job_row = dict(job)
    job_row["invoice_number"] = invoice["invoice_number"] if invoice else ""
    job_row["machine_context"] = _delivery_machine_context(items, job_row)
    return {
        "job": job_row,
        "items": items,
        "deliveries": deliveries,
        "ready_delivery": ready,
        "has_available": any(int(item["available_to_deliver"]) > 0 for item in items),
        "available_total": sum(int(item["available_to_deliver"]) for item in items),
    }


def create_delivery(
    job_id: int,
    payload: DeliveryCreate,
    *,
    actor: str = "system",
    request_id: str = "",
    source_path: str = "",
):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Select at least one quantity to deliver.")
    item_ids = [int(item.order_item_id) for item in payload.items]
    if len(item_ids) != len(set(item_ids)):
        raise HTTPException(status_code=400, detail="Each delivery item may appear only once.")
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        key = str(payload.idempotency_key or "").strip()
        if key:
            existing = connection.execute(
                "SELECT id,job_id FROM deliveries WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if int(existing["job_id"]) != int(job_id):
                    raise HTTPException(status_code=409, detail="Delivery idempotency key belongs to another Job.")
                connection.rollback()
                result = get_delivery(int(existing["id"]))
                result["replayed"] = True
                return result
        job = connection.execute(
            "SELECT id,job_number,customer,status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if connection.execute(
            "SELECT 1 FROM deliveries WHERE job_id=? AND status='READY' LIMIT 1",
            (job_id,),
        ).fetchone():
            raise HTTPException(status_code=409, detail="Complete or cancel the existing READY delivery first.")
        availability = {
            int(row["id"]): row for row in _delivery_availability_rows(connection, job_id)
        }
        selected = []
        for incoming in payload.items:
            item = availability.get(int(incoming.order_item_id))
            if item is None:
                raise HTTPException(status_code=404, detail="Delivery item is not attached to this Job.")
            quantity = int(incoming.quantity)
            available = int(item["available_to_deliver"] or 0)
            if quantity > available:
                raise HTTPException(
                    status_code=409,
                    detail=f"Only {available} are currently available for {item['description']}.",
                )
            selected.append((int(item["id"]), quantity))
        invoice = connection.execute(
            "SELECT id FROM invoices WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
        recipient = payload.recipient.strip() or str(job["customer"] or "").strip() or "Customer"
        try:
            cursor = connection.execute(
                """
                INSERT INTO deliveries (
                    job_id,invoice_id,recipient,notes,request_id,source_path,idempotency_key
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (job_id, invoice["id"] if invoice else None, recipient,
                 payload.notes.strip(), str(request_id or ""), str(source_path or ""), key or None),
            )
        except Exception as exc:
            connection.rollback()
            if "uq_deliveries_one_ready_per_job" in str(exc) or "deliveries.job_id" in str(exc):
                raise HTTPException(status_code=409, detail="Another READY delivery already reserves this Job.") from exc
            raise
        delivery_id = int(cursor.lastrowid)
        for order_item_id, quantity in selected:
            connection.execute(
                "INSERT INTO delivery_items(delivery_id,order_item_id,quantity_delivered) VALUES (?,?,?)",
                (delivery_id, order_item_id, quantity),
            )
        quantity_total = sum(quantity for _, quantity in selected)
        message = f"PPS-DEL-{delivery_id:04d} prepared for {recipient}: {quantity_total} units reserved."
        write_audit(
            connection, action="DELIVERY_CREATED", entity_type="DELIVERY",
            entity_id=delivery_id, summary=message,
            metadata={"job_id": job_id, "quantity": quantity_total,
                      "recipient": recipient, "source_path": str(source_path or "")},
            actor=str(actor or "system"), request_id=str(request_id or ""),
        )
        log_job_event(connection, job_id=job_id, event_type="DELIVERY_CREATED", icon="🚚", message=message)
        connection.commit()
    result = get_delivery(delivery_id)
    result["replayed"] = False
    return result


def cancel_delivery(
    delivery_id: int,
    *,
    actor: str = "system",
    request_id: str = "",
    source_path: str = "",
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        delivery = connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        if str(delivery["status"] or "").upper() != "READY":
            raise HTTPException(status_code=409, detail="Only a READY delivery can be cancelled.")
        connection.execute(
            "UPDATE deliveries SET status='CANCELLED',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='READY'",
            (delivery_id,),
        )
        message = f"PPS-DEL-{delivery_id:04d} cancelled; reserved quantities released."
        write_audit(
            connection, action="DELIVERY_CANCELLED", entity_type="DELIVERY",
            entity_id=delivery_id, summary=message,
            metadata={"job_id": int(delivery["job_id"]), "source_path": str(source_path or "")},
            actor=str(actor or "system"), request_id=str(request_id or ""),
        )
        log_job_event(connection, job_id=int(delivery["job_id"]), event_type="DELIVERY_CANCELLED", icon="↩", message=message)
        connection.commit()
    return get_delivery(delivery_id)


def complete_delivery(
    delivery_id: int,
    *,
    actor: str = "system",
    request_id: str = "",
    source_path: str = "",
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        delivery = connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        status = str(delivery["status"] or "").strip().upper()
        if status == "DELIVERED":
            connection.rollback()
            from plg_core.documents.integrity import verified_delivery_document
            with closing(get_connection()) as verify_connection:
                verified_delivery_document(verify_connection, delivery_id)
            result = get_delivery(delivery_id)
            result["job_complete"] = result["job_status"] in {"DELIVERED", "COMPLETED"}
            result["replayed"] = True
            return result
        if status != "READY":
            raise HTTPException(status_code=409, detail="Only a READY delivery can be marked delivered.")
        quantity_total = int(connection.execute(
            "SELECT COALESCE(SUM(quantity_delivered),0) FROM delivery_items WHERE delivery_id=?",
            (delivery_id,),
        ).fetchone()[0] or 0)
        if quantity_total <= 0:
            raise HTTPException(status_code=409, detail="Delivery has no parts.")
        connection.execute(
            "UPDATE deliveries SET status='DELIVERED',delivery_date=COALESCE(delivery_date,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='READY'",
            (delivery_id,),
        )
        remaining = connection.execute(
            """
            SELECT COUNT(*) FROM supplier_order_items oi
            JOIN supplier_orders po ON po.id=oi.order_id
            WHERE po.job_id=? AND (
                oi.quantity_received<oi.quantity_ordered OR
                COALESCE((SELECT SUM(di.quantity_delivered) FROM delivery_items di
                    JOIN deliveries d ON d.id=di.delivery_id
                    WHERE di.order_item_id=oi.id AND d.status='DELIVERED'),0)<oi.quantity_ordered)
            """, (delivery["job_id"],),
        ).fetchone()[0]
        receiving_remaining = connection.execute(
            "SELECT COUNT(*) FROM supplier_order_items oi JOIN supplier_orders po ON po.id=oi.order_id WHERE po.job_id=? AND oi.quantity_received<oi.quantity_ordered",
            (delivery["job_id"],),
        ).fetchone()[0]
        job_complete = int(remaining or 0) == 0
        new_job_status = "COMPLETED" if job_complete else ("ORDERED" if int(receiving_remaining or 0)>0 else "RECEIVED")
        connection.execute("UPDATE jobs SET status=? WHERE id=?", (new_job_status, delivery["job_id"]))
        message = f"PPS-DEL-{delivery_id:04d} delivered to {delivery['recipient'] or 'customer'}: {quantity_total} units."
        write_audit(
            connection, action="DELIVERY_COMPLETED", entity_type="DELIVERY",
            entity_id=delivery_id, summary=message,
            metadata={"job_id": int(delivery["job_id"]), "quantity": quantity_total,
                      "job_complete": job_complete, "source_path": str(source_path or "")},
            actor=str(actor or "system"), request_id=str(request_id or ""),
        )
        log_job_event(connection, job_id=int(delivery["job_id"]), event_type="DELIVERY_COMPLETED", icon="📬", message=message)
        if job_complete:
            log_job_event(connection, job_id=int(delivery["job_id"]), event_type="JOB_DELIVERED", icon="✅", message="All purchased parts have been delivered to the customer.")
        connection.commit()
    from plg_core.documents.integrity import issue_delivery_document
    with closing(get_connection()) as connection:
        issue_delivery_document(connection, delivery_id)
        connection.commit()
    result = get_delivery(delivery_id)
    result["job_complete"] = job_complete
    result["replayed"] = False
    return result
