from pydantic import BaseModel, Field, field_validator


class _ReasonValidatedModel(BaseModel):
    @field_validator("reason", check_fields=False)
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized

class ReceiptItem(BaseModel):
    order_item_id: int
    quantity_received: int = Field(ge=1)


class ReceiptExceptionItem(_ReasonValidatedModel):
    order_item_id: int
    disposition: str = Field(
        pattern="^(DAMAGED|WRONG_ITEM|QUARANTINED|REJECTED|SHORT)$"
    )
    quantity: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)
    notes: str = Field(default="", max_length=1000)
    supplier_reference: str = Field(default="", max_length=300)
    evidence_reference: str = Field(default="", max_length=500)


class ReceiptCreate(BaseModel):
    items: list[ReceiptItem] = Field(default_factory=list)
    exceptions: list[ReceiptExceptionItem] = Field(default_factory=list)
    notes: str = Field(default="", max_length=1000)
    receiver: str = Field(default="", max_length=200)
    idempotency_key: str = Field(default="", max_length=200)


class ReceivingExceptionResolutionCreate(_ReasonValidatedModel):
    quantity: int = Field(gt=0)
    resolution: str = Field(
        pattern=(
            "^(RETURNED|DISPOSED|REPLACEMENT_EXPECTED|REPLACED_BY_RECEIPT|"
            "CLEARED_TO_ACCEPTED|RECLASSIFIED_REJECTED|BACKORDER_CONFIRMED|CLOSED)$"
        )
    )
    related_receipt_id: int | None = None
    reason: str = Field(min_length=1, max_length=1000)
    notes: str = Field(default="", max_length=1000)
    supplier_reference: str = Field(default="", max_length=300)
    evidence_reference: str = Field(default="", max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)


class BackorderEventCreate(_ReasonValidatedModel):
    event_kind: str = Field(pattern="^(DECLARED|UPDATED|RESOLVED)$")
    backordered_quantity: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)
    notes: str = Field(default="", max_length=1000)
    supplier_reference: str = Field(default="", max_length=300)
    related_receipt_id: int | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)

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
