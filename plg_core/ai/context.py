from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable

from plg_core.ai.models import InternalJobContext
from plg_core.jobs.service import get_job_operational_snapshot


_TIMELINE_PREFIXES = (
    "JOB_", "REQUEST_", "NEED_", "RESEARCH_", "QUOTE_", "INVOICE_",
    "PAYMENT_", "SUPPLIER_", "ORDER_", "RECEIV", "PART_", "DELIVER",
    "ACTUAL_COST_",
)


def _money(value) -> float:
    return round(float(value or 0), 2)


def _projection(snapshot: dict) -> dict:
    customer = snapshot.get("customer") or None
    invoice = snapshot.get("invoice") or None
    financial = snapshot.get("financial") or None

    projected = {
        "schema_version": "1",
        "audience": "INTERNAL",
        "job": {
            "id": int(snapshot["job_id"]),
            "job_number": str(snapshot["job_number"]),
            "status": str(snapshot.get("job_status") or "UNKNOWN").upper(),
            "workflow_stage": str((snapshot.get("workflow") or {}).get("stage") or "UNKNOWN"),
            "next_action": str((snapshot.get("workflow") or {}).get("next_action") or "Not available"),
        },
        "customer": ({
            "id": int(customer["id"]) if customer.get("id") is not None else None,
            "customer_number": customer.get("customer_number"),
            "display_name": customer.get("name"),
            "company": customer.get("company"),
        } if customer else None),
        "assets": [{
            "id": int(asset["id"]),
            "manufacturer": asset.get("manufacturer"),
            "model": asset.get("model") or asset.get("name"),
            "year": asset.get("year"),
            "identifier": asset.get("vin_pin_serial"),
        } for asset in snapshot.get("assets", [])],
        "requested_needs": [{
            "id": int(need["id"]),
            "wording": need.get("wording"),
            "state": str(need.get("state") or "UNKNOWN").upper(),
            "asset_id": int(need["job_asset_id"]) if need.get("job_asset_id") is not None else None,
        } for need in snapshot.get("needs", [])],
        "quote": ({
            "number": snapshot["quote"].get("quote_number"),
            "status": str(snapshot["quote"].get("status") or "UNKNOWN").upper(),
        } if snapshot.get("quote") else None),
        "invoice": ({
            "number": invoice.get("invoice_number"),
            "status": str(invoice.get("status") or "UNKNOWN").upper(),
            "customer_total": _money(invoice.get("customer_total")),
            "balance": _money(invoice.get("balance_due")),
        } if invoice else None),
        "payment": {
            "status": str(invoice.get("payment_state") or "NOT INVOICED") if invoice else "NOT INVOICED",
            "amount_paid": _money(invoice.get("amount_paid")) if invoice else 0.0,
        },
        "supplier_orders": [{
            "supplier": order.get("supplier"),
            "po_number": order.get("po_number"),
            "status": str(order.get("status") or "UNKNOWN").upper(),
            "booked_cost": _money(order.get("booked_cost")),
            "placed_cost": _money(order.get("placed_cost")),
            "actual_cost": _money(order.get("actual_cost")),
            "actual_confirmation_state": str(order.get("actual_cost_state") or "NOT_CONFIRMED").upper(),
            "variance": _money(order.get("cost_variance")),
            "ordered": int(order.get("ordered_units") or 0),
            "received": int(order.get("received_units") or 0),
            "remaining": int(order.get("remaining_units") or 0),
            "delivered": int(order.get("delivered_units") or 0),
            "available": int(order.get("available_to_deliver_units") or 0),
        } for order in snapshot.get("supplier_orders", [])],
        "movement": {
            "ordered": int((snapshot.get("movement") or {}).get("ordered_units") or 0),
            "received": int((snapshot.get("movement") or {}).get("received_units") or 0),
            "remaining": int((snapshot.get("movement") or {}).get("remaining_units") or 0),
            "delivered": int((snapshot.get("movement") or {}).get("delivered_units") or 0),
            "available": int((snapshot.get("movement") or {}).get("available_to_deliver_units") or 0),
        },
        "financial": ({
            "revenue": _money(financial.get("customer_total")),
            "booked_cost": _money(financial.get("booked_supplier_cost")),
            "placed_cost": _money(financial.get("placed_supplier_cost")),
            "actual_cost": _money(financial.get("actual_supplier_cost")),
            "expected_profit": _money(financial.get("expected_profit")),
            "placed_cost_profit": _money(financial.get("placed_cost_profit")),
            "final_profit": _money(financial.get("actual_profit")),
            "cost_variance": _money(financial.get("cost_variance")),
            "profit_variance": _money(financial.get("profit_variance")),
            "actual_confirmation_state": str(financial.get("actual_cost_state") or "NOT_CONFIRMED").upper(),
        } if financial else None),
        "documents": [{
            "document_type": document.get("document_type"),
            "business_number": document.get("document_number"),
            "audience": document.get("audience"),
            "version": document.get("version"),
            "status": document.get("status"),
            "integrity": document.get("integrity"),
            "current": bool(document.get("is_current", True)),
        } for document in snapshot.get("documents", []) if bool(document.get("is_current", True))],
        "recent_timeline": [{
            "event_type": event.get("event_type"),
            "occurred_at": event.get("created_at"),
            "summary": str(event.get("message") or "")[:300],
        } for event in snapshot.get("activity", [])
            if str(event.get("event_type") or "").upper().startswith(_TIMELINE_PREFIXES)][:8],
    }
    return projected


def build_internal_job_context(
    job_id: int,
    *,
    snapshot_getter: Callable[[int], dict] = get_job_operational_snapshot,
    generated_at: datetime | None = None,
) -> InternalJobContext:
    """Build the one-Job, INTERNAL projection without querying another data layer."""
    projected = _projection(snapshot_getter(job_id))
    canonical = json.dumps(projected, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    context_id = "pps-ai-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    timestamp = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return InternalJobContext(
        **projected,
        context_id=context_id,
        generated_at=timestamp.isoformat().replace("+00:00", "Z"),
    )
