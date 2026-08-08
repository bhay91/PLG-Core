from __future__ import annotations

from pydantic import BaseModel, Field


class BasketItemCreate(BaseModel):
    requested_description: str = Field(min_length=1)
    manufacturer_part_number: str = ""
    supplier_part_number: str = ""
    supplier_name: str = ""
    source_type: str = "AFTERMARKET"
    brand: str = ""
    quantity: int = Field(default=1, ge=1)
    supplier_unit_cost: float | None = Field(default=None, ge=0)
    markup_percent: float | None = Field(default=None, ge=0)
    part_status: str | None = None
    verification_status: str = "UNVERIFIED"
    verification_note: str = ""
    availability: str = ""
    lead_time: str = ""
    selected: bool = True
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_url: str = ""


class BasketItemUpdate(BaseModel):
    requested_description: str | None = Field(default=None, min_length=1)
    manufacturer_part_number: str | None = None
    supplier_part_number: str | None = None
    supplier_name: str | None = None
    source_type: str | None = None
    brand: str | None = None
    quantity: int | None = Field(default=None, ge=1)
    supplier_unit_cost: float | None = Field(default=None, ge=0)
    markup_percent: float | None = Field(default=None, ge=0)
    part_status: str | None = None
    verification_status: str | None = None
    verification_note: str | None = None
    availability: str | None = None
    lead_time: str | None = None
    selected: bool | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_url: str | None = None
