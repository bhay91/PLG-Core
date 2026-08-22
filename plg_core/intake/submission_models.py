from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictIntakeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntakeCustomerCandidate(StrictIntakeModel):
    name: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)


class IntakeIdentifierCandidate(StrictIntakeModel):
    type: Literal[
        "AUTOMOTIVE_VIN", "JDM_FRAME", "JDM_CHASSIS", "MODEL_CODE", "PIN",
        "MACHINE_SERIAL", "ENGINE_SERIAL", "COMPONENT_SERIAL", "OTHER_IDENTIFIER", "UNKNOWN",
    ] = "UNKNOWN"
    value: str = Field(min_length=1, max_length=128)
    component_label: str = Field(default="", max_length=100)
    primary: bool = False


class IntakeMachineCandidate(StrictIntakeModel):
    reference: str = Field(min_length=1, max_length=128)
    manufacturer: str = Field(default="", max_length=200)
    model: str = Field(default="", max_length=200)
    year: str | None = Field(default=None, max_length=10)
    asset_type: Literal["vehicle", "machine", "engine", "component", "other"] = "other"
    identifiers: list[IntakeIdentifierCandidate] = Field(default_factory=list, max_length=12)


class IntakeRequestedNeedCandidate(StrictIntakeModel):
    original_wording: str = Field(min_length=1, max_length=1000)
    quantity: float | None = Field(default=None, gt=0, le=1000000, allow_inf_nan=False)
    machine_reference: str | None = Field(default=None, max_length=128)


def safe_evidence_url(value: str) -> str:
    value = str(value or "").strip()
    if len(value) > 2048:
        raise ValueError("Research evidence URLs must not exceed 2048 characters.")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Research evidence URLs must use HTTP or HTTPS.")
    if parsed.username or parsed.password:
        raise ValueError("Research evidence URLs must not contain credentials.")
    return value


class IntakeResearchOption(StrictIntakeModel):
    source_url: str
    source_name: str = Field(default="", max_length=200)
    product_description: str = Field(default="", max_length=1000)
    part_number: str = Field(default="", max_length=200)
    price: float | None = Field(default=None, ge=0, le=100000000, allow_inf_nan=False)
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    research_notes: str = Field(default="", max_length=2000)
    confidence: Literal["UNKNOWN", "LOW", "MEDIUM", "HIGH"] = "UNKNOWN"
    verification_status: Literal["UNVERIFIED", "NEEDS_REVIEW", "VERIFIED"] = "UNVERIFIED"

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return safe_evidence_url(value)


class IntakeResearchEvidence(StrictIntakeModel):
    source_urls: list[str] = Field(default_factory=list, max_length=20)
    claims: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=50)
    quoted_evidence: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(default_factory=list, max_length=50)
    options: list[IntakeResearchOption] = Field(default_factory=list, max_length=25)

    @field_validator("source_urls")
    @classmethod
    def validate_source_urls(cls, values: list[str]) -> list[str]:
        return [safe_evidence_url(value) for value in values]


class StructuredIntakeContent(StrictIntakeModel):
    client_reference: str = Field(min_length=1, max_length=128)
    original_input: str = Field(min_length=1, max_length=10000)
    customer: IntakeCustomerCandidate | None = None
    machines: list[IntakeMachineCandidate] = Field(default_factory=list, max_length=10)
    requested_needs: list[IntakeRequestedNeedCandidate] = Field(default_factory=list, max_length=100)
    additional_notes: str = Field(default="", max_length=4000)
    research_evidence: IntakeResearchEvidence | None = None

    @field_validator("original_input")
    @classmethod
    def original_input_has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Original input must contain text.")
        return value

    @field_validator("client_reference")
    @classmethod
    def normalize_client_reference(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Client reference must contain text.")
        return value

    @field_validator("machines")
    @classmethod
    def machine_references_are_unique(cls, values: list[IntakeMachineCandidate]) -> list[IntakeMachineCandidate]:
        references = [value.reference for value in values]
        if len(references) != len(set(references)):
            raise ValueError("Machine references must be unique within the proposal.")
        return values

    def structured_candidates(self) -> dict:
        return self.model_dump(
            mode="json",
            include={"customer", "machines", "requested_needs", "additional_notes", "research_evidence"},
        )

    def normalized_digest(self) -> str:
        content = self.model_dump(mode="json", exclude={"client_reference"})
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FirefoxIntakePackage(StructuredIntakeContent):
    schema_version: Literal["1"]
    source: Literal["CHATGPT_FIREFOX"]

    def normalized_digest(self) -> str:
        content = self.model_dump(mode="json", exclude={"client_reference"})
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IntakeSubmissionResult(StrictIntakeModel):
    schema_version: Literal["1"] = "1"
    proposal_id: int
    status: Literal["DRAFT"] = "DRAFT"
    review_url: str
    blockers: list[str]
    review_count: int
    origin: Literal["CHATGPT_MCP", "CHATGPT_FIREFOX"]
    client_reference: str
    duplicate: bool
