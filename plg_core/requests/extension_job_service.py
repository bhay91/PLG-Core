from __future__ import annotations

from contextlib import closing

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.jobs.fulfillment import (
    fulfillment_snapshot,
    mark_fulfillment_item_delivered,
    mark_fulfillment_item_received,
    mark_job_ordered,
)
from plg_core.requests.extension_job_models import (
    ExtensionItemAction,
    ExtensionJobAction,
    ExtensionPaymentAction,
)
from plg_core.sales.service import get_invoice, record_invoice_payment


AUDIT_ACTOR = "chatgpt-firefox-bridge"
SOURCE_PATH = "/api/extension/v1/jobs"


def _job(payload: ExtensionJobAction) -> dict:
    number = payload.job_number.strip()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            "SELECT id,job_number,customer,status FROM jobs WHERE UPPER(job_number)=UPPER(?)",
            (number,),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Job number not found.")
    if len(rows) != 1:
        raise HTTPException(status_code=409, detail="Job number is ambiguous.")
    return dict(rows[0])


def _item(job_id: int, payload: ExtensionItemAction) -> dict:
    snapshot = fulfillment_snapshot(job_id)
    if payload.item_id is not None:
        matches = [row for row in snapshot["items"] if int(row["id"]) == payload.item_id]
    else:
        reference = payload.item_reference.strip().casefold()
        matches = [
            row for row in snapshot["items"]
            if reference in {
                str(row.get("description") or "").strip().casefold(),
                str(row.get("supplier_part_number") or "").strip().casefold(),
            }
        ]
    if not matches:
        raise HTTPException(status_code=404, detail="Fulfillment item not found for Job.")
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail="Item reference is ambiguous for this Job.")
    return matches[0]


def record_job_payment(payload: ExtensionPaymentAction) -> dict:
    job = _job(payload)
    with closing(get_connection()) as connection:
        invoice = connection.execute(
            "SELECT * FROM invoices WHERE job_id=? ORDER BY id DESC LIMIT 1", (job["id"],)
        ).fetchone()
        if invoice is None:
            raise HTTPException(status_code=409, detail="Job has no invoice to pay.")
        replay_reference = f"CHATGPT:{payload.request_id.strip()}"
        replay = connection.execute(
            "SELECT id FROM customer_transactions WHERE invoice_id=? AND transaction_type='PAYMENT' "
            "AND (reference=? OR reference LIKE ?)",
            (invoice["id"], replay_reference, f"%[{replay_reference}]"),
        ).fetchone()
    if replay:
        result = get_invoice(int(invoice["id"]))
        return {"action": "PAYMENT_RECEIVED", "replayed": True, "job": job,
                "invoice_status": result["status"], "balance_due": result["balance_due"]}
    reference = payload.reference.strip()
    if reference:
        reference = f"{reference} [{replay_reference}]"
    else:
        reference = replay_reference
    result = record_invoice_payment(
        int(invoice["id"]), payload.amount, payload.payment_method,
        reference=reference, payment_date=payload.payment_date,
        actor=AUDIT_ACTOR, request_id=payload.request_id,
    )
    return {"action": "PAYMENT_RECEIVED", "replayed": False, "job": job,
            "invoice_status": result["status"], "balance_due": result["balance_due"]}


def mark_job_orders_placed(payload: ExtensionJobAction) -> dict:
    job = _job(payload)
    snapshot = mark_job_ordered(
        int(job["id"]), actor=AUDIT_ACTOR, request_id=payload.request_id,
        source_path=SOURCE_PATH + "/order-placed",
    )
    return {"action": "ORDER_PLACED", "job": job, "fulfillment": snapshot}


def receive_job_item(payload: ExtensionItemAction) -> dict:
    job = _job(payload)
    item = _item(int(job["id"]), payload)
    snapshot = mark_fulfillment_item_received(
        int(job["id"]), int(item["id"]), payload.quantity,
        idempotency_key=f"chatgpt-receive:{payload.request_id.strip()}",
        actor=AUDIT_ACTOR, request_id=payload.request_id,
        source_path=SOURCE_PATH + "/item-received",
    )
    return {"action": "ITEM_RECEIVED", "job": job, "item_id": int(item["id"]),
            "fulfillment": snapshot}


def deliver_job_item(payload: ExtensionItemAction) -> dict:
    job = _job(payload)
    item = _item(int(job["id"]), payload)
    snapshot = mark_fulfillment_item_delivered(
        int(job["id"]), int(item["id"]), payload.quantity,
        idempotency_key=f"chatgpt-deliver:{payload.request_id.strip()}",
        actor=AUDIT_ACTOR, request_id=payload.request_id,
        source_path=SOURCE_PATH + "/item-delivered",
    )
    return {"action": "ITEM_DELIVERED", "job": job, "item_id": int(item["id"]),
            "fulfillment": snapshot}
