from fastapi import APIRouter
from plg_core.supply.models import DeliveryCreate, ReceiptCreate, SupplierOrderUpdate
from plg_core.supply.service import (
    complete_delivery, create_delivery, create_orders_from_paid_invoice,
    get_order, list_orders, place_order, record_receipt, update_order,
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
def order_place(order_id: int):
    return place_order(order_id)

@router.post("/orders/{order_id}/receipts")
def receive(order_id: int, payload: ReceiptCreate):
    return record_receipt(order_id, payload)

@router.post("/deliveries/from-job/{job_id}")
def delivery_prepare(job_id: int, payload: DeliveryCreate):
    return create_delivery(job_id, payload)

@router.post("/deliveries/{delivery_id}/complete")
def delivery_complete(delivery_id: int):
    return complete_delivery(delivery_id)
