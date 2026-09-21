from fastapi import APIRouter, Request
from plg_core.supply.models import ActualCostAdjustment, DeliveryCreate, ReceiptCreate, SupplierOrderItemCostUpdate, SupplierOrderUpdate
from plg_core.supply.service import (
    cancel_delivery, complete_delivery, create_delivery, create_orders_from_paid_invoice,
    get_order, list_orders, place_order, record_actual_cost_adjustment,
    record_receipt, update_order, update_order_item_cost,
)

router = APIRouter(prefix="/api/v1/supply", tags=["alpha14-15-supply"])

@router.get("/orders")
def orders(limit: int = 100):
    return {"items": list_orders(limit)}

@router.get("/orders/{order_id}")
def order_detail(order_id: int):
    return get_order(order_id)

@router.post("/orders/from-invoice/{invoice_id}")
def orders_from_invoice(invoice_id: int):
    return {"items": create_orders_from_paid_invoice(invoice_id)}

@router.patch(
    "/orders/{order_id}/items/{item_id}"
)
def order_item_cost_update(
    order_id: int,
    item_id: int,
    payload: SupplierOrderItemCostUpdate,
):
    return update_order_item_cost(
        order_id=order_id,
        item_id=item_id,
        unit_cost=payload.unit_cost,
    )

@router.patch("/orders/{order_id}")
def order_update(
    order_id: int,
    payload: SupplierOrderUpdate,
):
    return update_order(
        order_id=order_id,
        shipping_total=payload.shipping_total,
        expected_at=payload.expected_at,
        notes=payload.notes,
    )

@router.post("/orders/{order_id}/place")
def order_place(request: Request, order_id: int):
    from plg_core.web_security import request_actor, request_id

    return place_order(
        order_id,
        actor=request_actor(request),
        request_id=request_id(request),
        source_path=str(request.url.path),
    )


@router.post("/orders/{order_id}/actual-cost-adjustments")
def actual_cost_adjustment(
    request: Request,
    order_id: int,
    payload: ActualCostAdjustment,
):
    from plg_core.web_security import request_actor, request_id

    header_key = str(request.headers.get("Idempotency-Key", "") or "").strip()
    actor = request_actor(request)
    return record_actual_cost_adjustment(
        order_id,
        cost_kind=payload.cost_kind,
        new_amount=payload.new_amount,
        supplier_order_item_id=payload.supplier_order_item_id,
        reason=payload.reason,
        actor=actor if actor != "system" else payload.actor,
        request_id=header_key or payload.request_id or request_id(request),
        supplier_reference=payload.supplier_reference,
    )

@router.post("/orders/{order_id}/receipts")
def receive(request: Request, order_id: int, payload: ReceiptCreate):
    from plg_core.web_security import request_actor, request_id

    header_key = str(request.headers.get("Idempotency-Key", "") or "").strip()
    if header_key and not payload.idempotency_key:
        payload = payload.model_copy(update={"idempotency_key": header_key})
    return record_receipt(
        order_id,
        payload,
        actor=request_actor(request),
        request_id=request_id(request),
        source_path=str(request.url.path),
    )

@router.post("/deliveries/from-job/{job_id}")
def delivery_prepare(request: Request, job_id: int, payload: DeliveryCreate):
    from plg_core.web_security import request_actor, request_id
    header_key = str(request.headers.get("Idempotency-Key", "") or "").strip()
    if header_key and not payload.idempotency_key:
        payload = payload.model_copy(update={"idempotency_key": header_key})
    return create_delivery(
        job_id, payload, actor=request_actor(request), request_id=request_id(request),
        source_path=str(request.url.path),
    )

@router.post("/deliveries/{delivery_id}/complete")
def delivery_complete(request: Request, delivery_id: int):
    from plg_core.web_security import request_actor, request_id
    return complete_delivery(
        delivery_id, actor=request_actor(request), request_id=request_id(request),
        source_path=str(request.url.path),
    )

@router.post("/deliveries/{delivery_id}/cancel")
def delivery_cancel(request: Request, delivery_id: int):
    from plg_core.web_security import request_actor, request_id
    return cancel_delivery(
        delivery_id, actor=request_actor(request), request_id=request_id(request),
        source_path=str(request.url.path),
    )
