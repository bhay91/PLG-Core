from pydantic import BaseModel, Field

class ReceiptItem(BaseModel):
    order_item_id: int
    quantity_received: int = Field(ge=1)

class ReceiptCreate(BaseModel):
    items: list[ReceiptItem]
    notes: str = Field(default="", max_length=1000)

class DeliveryCreate(BaseModel):
    recipient: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=1000)

class SupplierOrderUpdate(BaseModel):
    shipping_total: float = Field(default=0, ge=0)
    expected_at: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=1000)

class SupplierOrderItemCostUpdate(BaseModel):
    unit_cost: float = Field(ge=0)
