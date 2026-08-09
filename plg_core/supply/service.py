from contextlib import closing
from fastapi import HTTPException
from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.timeline import log_job_event
from plg_core.supply.models import ReceiptCreate, DeliveryCreate

def create_orders_from_paid_invoice(invoice_id: int):
    with closing(get_connection()) as connection:
        invoice = connection.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        if str(invoice["status"] or "").upper() != "PAID" or float(invoice["balance_due"] or 0) > 0:
            raise HTTPException(status_code=409, detail="Supplier orders require a fully paid invoice.")
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
                "Generated from paid invoice; invoice-level shipping is not allocated to supplier POs.",
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
                        quantity_ordered,unit_cost,line_cost
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id, item["id"], item["description"],
                    item["supplier_part_number"] or "", qty, unit,
                    round(unit * qty, 2),
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
            raise HTTPException(status_code=404, detail="Supplier order not found.")
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
        receipts = connection.execute(
            "SELECT * FROM receiving_events WHERE order_id=? ORDER BY id DESC",
            (order_id,),
        ).fetchall()
    result = dict(order)
    result["items"] = [dict(row) for row in items]
    result["receipts"] = [dict(row) for row in receipts]
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
                detail="Supplier order not found.",
            )

        status = str(
            order["status"] or ""
        ).strip().upper()

        if status != "DRAFT":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Supplier costs are locked once "
                    "the purchase order is placed."
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
                detail="Supplier order item not found.",
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
                detail="Supplier order not found.",
            )

        status = str(
            order["status"] or ""
        ).strip().upper()

        if status != "DRAFT":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Only a draft supplier order can be edited."
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


def place_order(order_id: int):
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
                detail="Supplier order not found.",
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
                    "Only a draft supplier order can be "
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

        write_audit(
            connection,
            action="SUPPLIER_ORDER_PLACED",
            entity_type="SUPPLIER_ORDER",
            entity_id=order_id,
            summary=f"{order['po_number']} marked ordered",
            metadata={
                "invoice_id": order["invoice_id"],
                "job_id": int(order["job_id"]),
            },
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
                        "All supplier orders placed for "
                        f"{order['invoice_number'] or 'invoice'}"
                    ),
                )

        connection.commit()

    return get_order(order_id)

def record_receipt(order_id: int, payload: ReceiptCreate):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Receipt requires at least one item.")
    with closing(get_connection()) as connection:
        order = connection.execute("SELECT * FROM supplier_orders WHERE id=?", (order_id,)).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Supplier order not found.")
        cur = connection.execute(
            "INSERT INTO receiving_events (order_id,notes) VALUES (?,?)",
            (order_id, payload.notes.strip()),
        )
        receipt_id = int(cur.lastrowid)
        receipt_number = f"PPS-RCPT-{receipt_id:04d}"
        connection.execute("UPDATE receiving_events SET receipt_number=? WHERE id=?", (receipt_number, receipt_id))
        for incoming in payload.items:
            item = connection.execute(
                "SELECT * FROM supplier_order_items WHERE id=? AND order_id=?",
                (incoming.order_item_id, order_id),
            ).fetchone()
            if item is None:
                raise HTTPException(status_code=404, detail=f"Order item {incoming.order_item_id} not found.")
            remaining = int(item["quantity_ordered"] or 0) - int(item["quantity_received"] or 0)
            if incoming.quantity_received > remaining:
                raise HTTPException(status_code=409, detail=f"Only {remaining} remain for order item {item['id']}.")
            connection.execute("""
                INSERT INTO receiving_event_items (receipt_id,order_item_id,quantity_received)
                VALUES (?, ?, ?)
            """, (receipt_id, item["id"], incoming.quantity_received))
            connection.execute("""
                UPDATE supplier_order_items
                SET quantity_received=quantity_received+?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (incoming.quantity_received, item["id"]))
        remaining_rows = connection.execute("""
            SELECT COUNT(*) FROM supplier_order_items
            WHERE order_id=? AND quantity_received < quantity_ordered
        """, (order_id,)).fetchone()[0]
        status = "RECEIVED" if remaining_rows == 0 else "PARTIAL"
        connection.execute("""
            UPDATE supplier_orders
            SET status=?, received_at=CASE WHEN ?='RECEIVED' THEN CURRENT_TIMESTAMP ELSE received_at END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (status, status, order_id))
        write_audit(connection, action="PARTS_RECEIVED", entity_type="SUPPLIER_ORDER",
                    entity_id=order_id, summary=f"{receipt_number} recorded for {order['po_number']}")
        connection.commit()
    return get_order(order_id)

def create_delivery(job_id: int, payload: DeliveryCreate):
    with closing(get_connection()) as connection:
        job = connection.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        existing = connection.execute(
            "SELECT * FROM deliveries WHERE job_id=? AND status='READY' ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        if existing:
            return {"delivery_id": existing["id"], "status": existing["status"], "existing": True}
        available = connection.execute("""
            SELECT oi.id, oi.quantity_received,
                   COALESCE((
                       SELECT SUM(di.quantity_delivered)
                       FROM delivery_items di
                       JOIN deliveries d ON d.id=di.delivery_id
                       WHERE di.order_item_id=oi.id AND d.status!='CANCELLED'
                   ),0) AS delivered
            FROM supplier_order_items oi
            JOIN supplier_orders po ON po.id=oi.order_id
            WHERE po.job_id=? AND oi.quantity_received>0
        """, (job_id,)).fetchall()
        deliverable = [(int(row["id"]), int(row["quantity_received"])-int(row["delivered"])) for row in available
                       if int(row["quantity_received"])-int(row["delivered"]) > 0]
        if not deliverable:
            raise HTTPException(status_code=409, detail="No received parts are ready for delivery.")
        invoice = connection.execute(
            "SELECT id FROM invoices WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        cur = connection.execute("""
            INSERT INTO deliveries (job_id,invoice_id,recipient,notes)
            VALUES (?, ?, ?, ?)
        """, (job_id, invoice["id"] if invoice else None, payload.recipient.strip(), payload.notes.strip()))
        delivery_id = int(cur.lastrowid)
        for order_item_id, qty in deliverable:
            connection.execute("""
                INSERT INTO delivery_items (delivery_id,order_item_id,quantity_delivered)
                VALUES (?, ?, ?)
            """, (delivery_id, order_item_id, qty))
        write_audit(connection, action="DELIVERY_CREATED", entity_type="DELIVERY",
                    entity_id=delivery_id, summary=f"Delivery prepared for job {job_id}")
        connection.commit()
    return {"delivery_id": delivery_id, "status": "READY", "existing": False}

def complete_delivery(delivery_id: int):
    with closing(get_connection()) as connection:
        delivery = connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if delivery is None:
            raise HTTPException(status_code=404, detail="Delivery not found.")
        connection.execute("""
            UPDATE deliveries SET status='DELIVERED',
                delivery_date=COALESCE(delivery_date,CURRENT_TIMESTAMP),
                updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (delivery_id,))
        connection.execute("UPDATE jobs SET status='DELIVERED' WHERE id=?", (delivery["job_id"],))
        write_audit(connection, action="DELIVERY_COMPLETED", entity_type="DELIVERY",
                    entity_id=delivery_id, summary=f"Job {delivery['job_id']} delivered")
        connection.commit()
    return {"delivery_id": delivery_id, "status": "DELIVERED", "job_id": delivery["job_id"]}
