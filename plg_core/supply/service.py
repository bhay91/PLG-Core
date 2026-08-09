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
        receipts = connection.execute(
            """
            SELECT
                r.*,
                COALESCE(
                    SUM(ri.quantity_received),
                    0
                ) AS quantity_received
            FROM receiving_events r
            LEFT JOIN receiving_event_items ri
              ON ri.receipt_id=r.id
            WHERE r.order_id=?
            GROUP BY r.id
            ORDER BY r.id DESC
            """,
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
                        "All supplier purchases placed for "
                        f"{order['invoice_number'] or 'invoice'}"
                    ),
                )

        connection.commit()

    return get_order(order_id)

def record_receipt(order_id: int, payload: ReceiptCreate):
    if not payload.items:
        raise HTTPException(
            status_code=400,
            detail="Receipt requires at least one item.",
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

        cur = connection.execute(
            """
            INSERT INTO receiving_events (
                order_id,
                notes
            )
            VALUES (?, ?)
            """,
            (
                order_id,
                payload.notes.strip(),
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

            connection.execute(
                """
                UPDATE supplier_order_items
                SET quantity_received=
                        quantity_received+?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    quantity,
                    item_id,
                ),
            )

            total_received += quantity

        remaining_rows = connection.execute(
            """
            SELECT COUNT(*)
            FROM supplier_order_items
            WHERE order_id=?
              AND quantity_received < quantity_ordered
            """,
            (order_id,),
        ).fetchone()[0]

        new_status = (
            "RECEIVED"
            if int(remaining_rows or 0) == 0
            else "PARTIAL"
        )

        connection.execute(
            """
            UPDATE supplier_orders
            SET status=?,
                received_at=
                    CASE
                        WHEN ?='RECEIVED'
                        THEN COALESCE(
                            received_at,
                            CURRENT_TIMESTAMP
                        )
                        ELSE received_at
                    END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                new_status,
                new_status,
                order_id,
            ),
        )

        summary = (
            f"{receipt_number}: "
            f"{total_received} "
            f"part{'s' if total_received != 1 else ''} "
            f"received from {order['supplier_name']}. "
            f"Purchase status {new_status}."
        )

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
            },
        )

        log_job_event(
            connection,
            job_id=int(order["job_id"]),
            event_type="PARTS_RECEIVED",
            icon="📦",
            message=summary,
        )

        outstanding_orders = connection.execute(
            """
            SELECT COUNT(*)
            FROM supplier_orders
            WHERE job_id=?
              AND UPPER(
                    COALESCE(status, 'DRAFT')
                  ) != 'RECEIVED'
            """,
            (order["job_id"],),
        ).fetchone()[0]

        if int(outstanding_orders or 0) == 0:
            connection.execute(
                """
                UPDATE jobs
                SET status='RECEIVED'
                WHERE id=?
                """,
                (order["job_id"],),
            )

            log_job_event(
                connection,
                job_id=int(order["job_id"]),
                event_type="RECEIVING_COMPLETE",
                icon="✅",
                message=(
                    "All supplier purchases have "
                    "been received."
                ),
            )

        connection.commit()

    return get_order(order_id)


def get_delivery(delivery_id: int):
    with closing(get_connection()) as connection:
        delivery = connection.execute(
            """
            SELECT
                d.*,
                j.job_number,
                j.customer,
                j.company,
                i.invoice_number
            FROM deliveries d
            JOIN jobs j
              ON j.id=d.job_id
            LEFT JOIN invoices i
              ON i.id=d.invoice_id
            WHERE d.id=?
            """,
            (delivery_id,),
        ).fetchone()

        if delivery is None:
            raise HTTPException(
                status_code=404,
                detail="Delivery not found.",
            )

        items = connection.execute(
            """
            SELECT
                di.*,
                oi.description,
                oi.supplier_part_number,
                po.po_number,
                po.supplier_name
            FROM delivery_items di
            JOIN supplier_order_items oi
              ON oi.id=di.order_item_id
            JOIN supplier_orders po
              ON po.id=oi.order_id
            WHERE di.delivery_id=?
            ORDER BY di.id
            """,
            (delivery_id,),
        ).fetchall()

    result = dict(delivery)
    result["delivery_id"] = int(result["id"])
    result["items"] = [dict(row) for row in items]
    result["quantity_total"] = sum(
        int(row["quantity_delivered"] or 0)
        for row in items
    )
    return result


def get_delivery_workspace(job_id: int):
    with closing(get_connection()) as connection:
        job = connection.execute(
            """
            SELECT *
            FROM jobs
            WHERE id=?
            """,
            (job_id,),
        ).fetchone()

        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Job not found.",
            )

        items = connection.execute(
            """
            SELECT
                oi.id,
                oi.description,
                oi.supplier_part_number,
                oi.quantity_ordered,
                oi.quantity_received,
                po.po_number,
                po.supplier_name,
                COALESCE((
                    SELECT SUM(di.quantity_delivered)
                    FROM delivery_items di
                    JOIN deliveries d
                      ON d.id=di.delivery_id
                    WHERE di.order_item_id=oi.id
                      AND d.status='DELIVERED'
                ),0) AS quantity_delivered,
                COALESCE((
                    SELECT SUM(di.quantity_delivered)
                    FROM delivery_items di
                    JOIN deliveries d
                      ON d.id=di.delivery_id
                    WHERE di.order_item_id=oi.id
                      AND d.status='READY'
                ),0) AS quantity_reserved
            FROM supplier_order_items oi
            JOIN supplier_orders po
              ON po.id=oi.order_id
            WHERE po.job_id=?
            ORDER BY po.id, oi.id
            """,
            (job_id,),
        ).fetchall()

        deliveries = connection.execute(
            """
            SELECT
                d.*,
                COALESCE(
                    SUM(di.quantity_delivered),
                    0
                ) AS quantity_total
            FROM deliveries d
            LEFT JOIN delivery_items di
              ON di.delivery_id=d.id
            WHERE d.job_id=?
            GROUP BY d.id
            ORDER BY d.id DESC
            """,
            (job_id,),
        ).fetchall()

        ready = connection.execute(
            """
            SELECT id
            FROM deliveries
            WHERE job_id=?
              AND status='READY'
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()

    item_rows = []

    for row in items:
        item = dict(row)

        item["available_to_deliver"] = max(
            int(item["quantity_received"] or 0)
            - int(item["quantity_delivered"] or 0)
            - int(item["quantity_reserved"] or 0),
            0,
        )

        item_rows.append(item)

    return {
        "job": dict(job),
        "items": item_rows,
        "deliveries": [
            dict(row)
            for row in deliveries
        ],
        "ready_delivery": (
            get_delivery(int(ready["id"]))
            if ready
            else None
        ),
        "has_available": any(
            int(item["available_to_deliver"]) > 0
            for item in item_rows
        ),
    }


def create_delivery(job_id: int, payload: DeliveryCreate):
    with closing(get_connection()) as connection:
        job = connection.execute(
            """
            SELECT id, job_number, customer, status
            FROM jobs
            WHERE id=?
            """,
            (job_id,),
        ).fetchone()

        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Job not found.",
            )

        existing = connection.execute(
            """
            SELECT id
            FROM deliveries
            WHERE job_id=?
              AND status='READY'
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()

        if existing:
            result = get_delivery(
                int(existing["id"])
            )
            result["existing"] = True
            return result

        available = connection.execute(
            """
            SELECT
                oi.id,
                oi.description,
                oi.quantity_received,
                COALESCE((
                    SELECT SUM(di.quantity_delivered)
                    FROM delivery_items di
                    JOIN deliveries d
                      ON d.id=di.delivery_id
                    WHERE di.order_item_id=oi.id
                      AND d.status!='CANCELLED'
                ),0) AS committed
            FROM supplier_order_items oi
            JOIN supplier_orders po
              ON po.id=oi.order_id
            WHERE po.job_id=?
              AND oi.quantity_received>0
            ORDER BY po.id, oi.id
            """,
            (job_id,),
        ).fetchall()

        deliverable = []

        for row in available:
            quantity = (
                int(row["quantity_received"] or 0)
                - int(row["committed"] or 0)
            )

            if quantity > 0:
                deliverable.append(
                    (
                        int(row["id"]),
                        quantity,
                    )
                )

        if not deliverable:
            raise HTTPException(
                status_code=409,
                detail=(
                    "No received parts are ready "
                    "for customer delivery."
                ),
            )

        invoice = connection.execute(
            """
            SELECT id
            FROM invoices
            WHERE job_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()

        recipient = (
            payload.recipient.strip()
            or str(job["customer"] or "").strip()
            or "Customer"
        )

        cursor = connection.execute(
            """
            INSERT INTO deliveries (
                job_id,
                invoice_id,
                recipient,
                notes
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                job_id,
                invoice["id"] if invoice else None,
                recipient,
                payload.notes.strip(),
            ),
        )

        delivery_id = int(cursor.lastrowid)

        quantity_total = 0

        for order_item_id, quantity in deliverable:
            connection.execute(
                """
                INSERT INTO delivery_items (
                    delivery_id,
                    order_item_id,
                    quantity_delivered
                )
                VALUES (?, ?, ?)
                """,
                (
                    delivery_id,
                    order_item_id,
                    quantity,
                ),
            )

            quantity_total += quantity

        message = (
            f"Delivery #{delivery_id} prepared for "
            f"{recipient}: {quantity_total} "
            f"part{'s' if quantity_total != 1 else ''}."
        )

        write_audit(
            connection,
            action="DELIVERY_CREATED",
            entity_type="DELIVERY",
            entity_id=delivery_id,
            summary=message,
            metadata={
                "job_id": job_id,
                "quantity": quantity_total,
                "recipient": recipient,
            },
        )

        log_job_event(
            connection,
            job_id=job_id,
            event_type="DELIVERY_CREATED",
            icon="🚚",
            message=message,
        )

        connection.commit()

    result = get_delivery(delivery_id)
    result["existing"] = False
    return result


def complete_delivery(delivery_id: int):
    with closing(get_connection()) as connection:
        delivery = connection.execute(
            """
            SELECT *
            FROM deliveries
            WHERE id=?
            """,
            (delivery_id,),
        ).fetchone()

        if delivery is None:
            raise HTTPException(
                status_code=404,
                detail="Delivery not found.",
            )

        status = str(
            delivery["status"] or ""
        ).strip().upper()

        if status == "DELIVERED":
            result = get_delivery(delivery_id)
            result["job_complete"] = (
                str(
                    connection.execute(
                        "SELECT status FROM jobs WHERE id=?",
                        (delivery["job_id"],),
                    ).fetchone()["status"]
                    or ""
                ).upper()
                == "DELIVERED"
            )
            return result

        if status != "READY":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Only a ready delivery can be "
                    "marked delivered."
                ),
            )

        quantity_total = connection.execute(
            """
            SELECT COALESCE(
                SUM(quantity_delivered),
                0
            )
            FROM delivery_items
            WHERE delivery_id=?
            """,
            (delivery_id,),
        ).fetchone()[0]

        if int(quantity_total or 0) <= 0:
            raise HTTPException(
                status_code=409,
                detail="Delivery has no parts.",
            )

        connection.execute(
            """
            UPDATE deliveries
            SET status='DELIVERED',
                delivery_date=COALESCE(
                    delivery_date,
                    CURRENT_TIMESTAMP
                ),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (delivery_id,),
        )

        remaining = connection.execute(
            """
            SELECT COUNT(*)
            FROM supplier_order_items oi
            JOIN supplier_orders po
              ON po.id=oi.order_id
            WHERE po.job_id=?
              AND (
                    oi.quantity_received
                        < oi.quantity_ordered
                    OR
                    COALESCE((
                        SELECT SUM(
                            di.quantity_delivered
                        )
                        FROM delivery_items di
                        JOIN deliveries d
                          ON d.id=di.delivery_id
                        WHERE di.order_item_id=oi.id
                          AND d.status='DELIVERED'
                    ),0) < oi.quantity_ordered
              )
            """,
            (delivery["job_id"],),
        ).fetchone()[0]

        receiving_remaining = connection.execute(
            """
            SELECT COUNT(*)
            FROM supplier_order_items oi
            JOIN supplier_orders po
              ON po.id=oi.order_id
            WHERE po.job_id=?
              AND oi.quantity_received
                    < oi.quantity_ordered
            """,
            (delivery["job_id"],),
        ).fetchone()[0]

        job_complete = (
            int(remaining or 0) == 0
        )

        if job_complete:
            new_job_status = "DELIVERED"
        elif int(receiving_remaining or 0) > 0:
            new_job_status = "ORDERED"
        else:
            new_job_status = "RECEIVED"

        connection.execute(
            """
            UPDATE jobs
            SET status=?
            WHERE id=?
            """,
            (
                new_job_status,
                delivery["job_id"],
            ),
        )

        message = (
            f"Delivery #{delivery_id} delivered to "
            f"{delivery['recipient'] or 'customer'}: "
            f"{int(quantity_total)} "
            f"part{'s' if int(quantity_total) != 1 else ''}."
        )

        write_audit(
            connection,
            action="DELIVERY_COMPLETED",
            entity_type="DELIVERY",
            entity_id=delivery_id,
            summary=message,
            metadata={
                "job_id": int(delivery["job_id"]),
                "quantity": int(quantity_total),
                "job_complete": job_complete,
            },
        )

        log_job_event(
            connection,
            job_id=int(delivery["job_id"]),
            event_type="DELIVERY_COMPLETED",
            icon="📬",
            message=message,
        )

        if job_complete:
            log_job_event(
                connection,
                job_id=int(delivery["job_id"]),
                event_type="JOB_DELIVERED",
                icon="✅",
                message=(
                    "All purchased parts have been "
                    "delivered to the customer."
                ),
            )

        connection.commit()

    result = get_delivery(delivery_id)
    result["job_complete"] = job_complete
    return result
