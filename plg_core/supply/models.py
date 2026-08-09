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
