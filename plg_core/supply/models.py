from pydantic import BaseModel, Field

class ReceiptItem(BaseModel):
    order_item_id: int
    quantity_received: int = Field(ge=1)

class ReceiptCreate(BaseModel):
    items: list[ReceiptItem]
    notes: str = Field(default="", max_length=1000)
    receiver: str = Field(default="", max_length=200)
    idempotency_key: str = Field(default="", max_length=200)

class DeliveryItemCreate(BaseModel):
    order_item_id: int
    quantity: int = Field(ge=1)


class DeliveryCreate(BaseModel):
    items: list[DeliveryItemCreate] = Field(default_factory=list)
    recipient: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(default="", max_length=200)

class SupplierOrderUpdate(BaseModel):
    shipping_total: float = Field(default=0, ge=0)
    expected_at: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=1000)

class SupplierOrderItemCostUpdate(BaseModel):
    unit_cost: float = Field(ge=0)


class ActualCostAdjustment(BaseModel):
    cost_kind: str = Field(pattern="^(ITEM|SHIPPING)$")
    new_amount: float = Field(ge=0)
    supplier_order_item_id: int | None = None
    reason: str = Field(min_length=1, max_length=1000)
    actor: str = Field(default="", max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    supplier_reference: str = Field(default="", max_length=300)
