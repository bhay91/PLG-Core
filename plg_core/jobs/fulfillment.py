from __future__ import annotations

from contextlib import closing

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.supply.models import (
    DeliveryCreate,
    DeliveryItemCreate,
    ReceiptCreate,
    ReceiptItem,
)
from plg_core.supply.service import (
    complete_delivery,
    create_delivery,
    create_orders_from_paid_invoice,
    place_order,
    record_receipt,
)


def _paid_invoice(connection, job_id: int):
    invoice = connection.execute(
        "SELECT * FROM invoices WHERE job_id=? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if invoice is None or str(invoice["status"] or "").upper() != "PAID" or float(invoice["balance_due"] or 0) > 0:
        raise HTTPException(status_code=409, detail="Fulfillment requires a fully paid invoice.")
    return invoice


def fulfillment_snapshot(job_id: int, *, connection=None) -> dict:
    owned = connection is None
    connection = connection or get_connection()
    try:
        invoice = connection.execute(
            "SELECT id,status,balance_due FROM invoices WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        paid = bool(
            invoice
            and str(invoice["status"] or "").upper() == "PAID"
            and float(invoice["balance_due"] or 0) <= 0
        )
        rows = connection.execute(
            """
            SELECT oi.id,oi.order_id,oi.invoice_item_id,oi.description,
                   oi.supplier_part_number,oi.quantity_ordered,oi.quantity_received,
                   po.po_number,po.supplier_name,UPPER(COALESCE(po.status,'DRAFT')) order_status,
                   COALESCE((SELECT SUM(di.quantity_delivered)
                     FROM delivery_items di JOIN deliveries d ON d.id=di.delivery_id
                     WHERE di.order_item_id=oi.id AND d.status='DELIVERED'),0) quantity_delivered
            FROM supplier_order_items oi
            JOIN supplier_orders po ON po.id=oi.order_id
            WHERE po.job_id=?
            ORDER BY po.id,oi.id
            """,
            (job_id,),
        ).fetchall()
        items = []
        for source in rows:
            item = dict(source)
            quantity_ordered = int(item["quantity_ordered"] or 0)
            quantity_received = int(item["quantity_received"] or 0)
            quantity_delivered = int(item["quantity_delivered"] or 0)
            ordered = item["order_status"] in {"ORDERED", "PARTIAL", "RECEIVED"}
            received = quantity_received >= quantity_ordered
            delivered = quantity_delivered >= quantity_ordered
            item.update(
                ordered=ordered,
                received=received,
                delivered=delivered,
                quantity_remaining=max(quantity_ordered - quantity_delivered, 0),
                quantity_to_receive=max(quantity_ordered - quantity_received, 0),
                quantity_available_to_deliver=max(quantity_received - quantity_delivered, 0),
            )
            items.append(item)
        total = len(items)
        counts = {
            "total": total,
            "ordered": sum(item["ordered"] for item in items),
            "received": sum(item["received"] for item in items),
            "delivered": sum(item["delivered"] for item in items),
        }
        if total and counts["delivered"] == total:
            stage = "COMPLETED"
        elif any(int(item["quantity_delivered"] or 0) > 0 for item in items):
            stage = "DELIVERED"
        elif any(int(item["quantity_received"] or 0) > 0 for item in items):
            stage = "RECEIVING"
        elif counts["ordered"]:
            stage = "ORDERED"
        else:
            stage = "PAID" if paid else "UNAVAILABLE"
        return {
            "available": paid,
            "stage": stage,
            "items": items,
            "counts": counts,
            "has_draft_orders": any(item["order_status"] == "DRAFT" for item in items),
        }
    finally:
        if owned:
            connection.close()


def mark_job_ordered(job_id: int, **audit) -> dict:
    with closing(get_connection()) as connection:
        invoice = _paid_invoice(connection, job_id)
        invoice_id = int(invoice["id"])
    orders = create_orders_from_paid_invoice(invoice_id)
    for order in orders:
        if str(order["status"] or "").upper() == "DRAFT":
            place_order(int(order["id"]), **audit)
    return fulfillment_snapshot(job_id)


def mark_fulfillment_item_received(
    job_id: int,
    order_item_id: int,
    quantity: int = 1,
    idempotency_key: str = "",
    **audit,
) -> dict:
    with closing(get_connection()) as connection:
        _paid_invoice(connection, job_id)
        item = connection.execute(
            """SELECT oi.*,po.job_id,po.status order_status FROM supplier_order_items oi
               JOIN supplier_orders po ON po.id=oi.order_id
               WHERE oi.id=? AND po.job_id=?""",
            (order_item_id, job_id),
        ).fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="Fulfillment item not found for Job.")
    if str(item["order_status"] or "").upper() == "DRAFT":
        raise HTTPException(status_code=409, detail="Mark the order placed before receiving items.")
    if int(quantity) < 1:
        raise HTTPException(status_code=400, detail="Received quantity must be at least one.")
    remaining = int(item["quantity_ordered"] or 0) - int(item["quantity_received"] or 0)
    if remaining <= 0:
        return fulfillment_snapshot(job_id)
    record_receipt(
        int(item["order_id"]),
        ReceiptCreate(
            items=[ReceiptItem(order_item_id=order_item_id, quantity_received=int(quantity))],
            notes="Received from Job fulfillment checklist.",
            idempotency_key=idempotency_key,
        ),
        **audit,
    )
    return fulfillment_snapshot(job_id)


def mark_fulfillment_item_delivered(
    job_id: int,
    order_item_id: int,
    quantity: int = 1,
    idempotency_key: str = "",
    **audit,
) -> dict:
    with closing(get_connection()) as connection:
        _paid_invoice(connection, job_id)
        item = connection.execute(
            """SELECT oi.*,
                 COALESCE((SELECT SUM(di.quantity_delivered)
                   FROM delivery_items di JOIN deliveries d ON d.id=di.delivery_id
                   WHERE di.order_item_id=oi.id AND d.status='DELIVERED'),0) delivered
               FROM supplier_order_items oi JOIN supplier_orders po ON po.id=oi.order_id
               WHERE oi.id=? AND po.job_id=?""",
            (order_item_id, job_id),
        ).fetchone()
        job = connection.execute("SELECT customer FROM jobs WHERE id=?", (job_id,)).fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="Fulfillment item not found for Job.")
    if int(quantity) < 1:
        raise HTTPException(status_code=400, detail="Delivered quantity must be at least one.")
    available = int(item["quantity_received"] or 0) - int(item["delivered"] or 0)
    if available <= 0:
        if int(item["delivered"] or 0) >= int(item["quantity_ordered"] or 0):
            return fulfillment_snapshot(job_id)
        raise HTTPException(status_code=409, detail="No received units are available to deliver.")
    if int(quantity) > available:
        raise HTTPException(status_code=409, detail=f"Only {available} received units are available to deliver.")
    if int(item["quantity_ordered"] or 0) - int(item["delivered"] or 0) <= 0:
        return fulfillment_snapshot(job_id)
    delivery = create_delivery(
        job_id,
        DeliveryCreate(
            items=[DeliveryItemCreate(order_item_id=order_item_id, quantity=int(quantity))],
            recipient=str(job["customer"] or "Customer") if job else "Customer",
            notes="Delivered from Job fulfillment checklist.",
            idempotency_key=idempotency_key,
        ),
        **audit,
    )
    complete_delivery(int(delivery["id"]), **audit)
    return fulfillment_snapshot(job_id)
