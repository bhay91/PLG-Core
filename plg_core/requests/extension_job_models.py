from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtensionJobAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_number: str = Field(min_length=1, max_length=64)
    request_id: str = Field(min_length=1, max_length=200)


class ExtensionPaymentAction(ExtensionJobAction):
    amount: float = Field(gt=0, le=100000000, allow_inf_nan=False)
    payment_method: str = Field(min_length=1, max_length=50)
    reference: str = Field(default="", max_length=200)
    payment_date: str = Field(default="", max_length=10)


class ExtensionItemAction(ExtensionJobAction):
    item_id: int | None = Field(default=None, gt=0)
    item_reference: str = Field(default="", max_length=300)
    quantity: int = Field(ge=1, le=1000000)

    @model_validator(mode="after")
    def exactly_one_item_reference(self):
        if (self.item_id is None) == (not self.item_reference.strip()):
            raise ValueError("Supply exactly one of item_id or item_reference.")
        return self
