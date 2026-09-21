from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AskPPSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(max_length=1000)

    @field_validator("question")
    @classmethod
    def question_must_have_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Question must not be empty.")
        return value


class AskPPSResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    job_number: str
    context_id: str
    answer: str
    source_facts: list[str]


class InternalJobContext(BaseModel):
    """Versioned internal contract; every field is populated by an allowlist."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    context_id: str
    generated_at: str
    audience: Literal["INTERNAL"] = "INTERNAL"
    job: dict[str, Any]
    customer: dict[str, Any] | None
    assets: list[dict[str, Any]]
    requested_needs: list[dict[str, Any]]
    quote: dict[str, Any] | None
    invoice: dict[str, Any] | None
    payment: dict[str, Any]
    supplier_orders: list[dict[str, Any]]
    movement: dict[str, int]
    financial: dict[str, Any] | None
    documents: list[dict[str, Any]]
    recent_timeline: list[dict[str, Any]]
