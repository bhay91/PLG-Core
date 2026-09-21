"""Validated, review-only Research Import Package contract.

This module intentionally contains no persistence or business-record mutation.
Packages are candidate data until an operator reviews and confirms them.
"""

from __future__ import annotations

import re
import json
from io import BytesIO
from zipfile import BadZipFile, ZipFile
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlsplit


PPSRESEARCH_MAX_MEMBERS = 16
PPSRESEARCH_MAX_MEMBER_BYTES = 8 * 1024 * 1024
PPSRESEARCH_MAX_TOTAL_BYTES = 24 * 1024 * 1024

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from plg_core.intake.identifiers import ASSET_TYPES, IDENTIFIER_TYPES


class ResearchImportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def unpack_ppsresearch(data: bytes) -> tuple[dict, str, bytes]:
    """Unpack a single-file transport into the existing package inputs."""
    try:
        archive = ZipFile(BytesIO(data))
    except BadZipFile:
        raise ValueError("Research package is not a valid .ppsresearch archive.") from None
    with archive:
        members = archive.infolist()
        if len(members) > PPSRESEARCH_MAX_MEMBERS:
            raise ValueError("Research package contains too many files.")
        names: list[str] = []
        total = 0
        for member in members:
            name = member.filename
            path = name.replace("\\", "/")
            if not path or path.startswith("/") or re.match(r"^[A-Za-z]:/", path) or path.startswith("../") or "/../" in path or path == "..":
                raise ValueError("Research package contains an unsafe path.")
            if member.is_dir() or (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Research package contains an unsupported member.")
            if path.lower().endswith((".zip", ".ppsresearch")):
                raise ValueError("Nested research packages are not supported.")
            if path in names:
                raise ValueError("Research package contains duplicate files.")
            names.append(path)
            if member.file_size > PPSRESEARCH_MAX_MEMBER_BYTES:
                raise ValueError("Research package member is too large.")
            total += member.file_size
            if total > PPSRESEARCH_MAX_TOTAL_BYTES:
                raise ValueError("Research package is too large.")
        required = {"manifest.json", "research.json", "source.pdf"}
        if not required.issubset(names):
            raise ValueError("Research package requires manifest.json, research.json, and source.pdf.")
        if any(name not in required for name in names):
            raise ValueError("Research package contains unsupported files.")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
            raise ValueError("Research package manifest is invalid.") from None
        if not isinstance(manifest, dict) or manifest.get("format") != "ppsresearch" or manifest.get("version") != 1:
            raise ValueError("Research package format version is unsupported.")
        if manifest.get("research") != "research.json" or manifest.get("source_pdf") != "source.pdf":
            raise ValueError("Research package manifest references unsupported members.")
        try:
            package = json.loads(archive.read("research.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
            raise ValueError("Research package research.json is invalid.") from None
        if not isinstance(package, dict):
            raise ValueError("Research package research.json is invalid.")
        return package, "source.pdf", archive.read("source.pdf")


def _https_url(value: str) -> str:
    value = str(value or "").strip()
    parsed = urlsplit(value)
    if (
        not value
        or parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or value.startswith("[")
        or "](" in value
    ):
        raise ValueError("URLs must be plain HTTPS URL strings, not Markdown links.")
    return value


def _decimal_string(value: str | None, *, positive: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("Monetary values must be valid decimal strings.")
    if not number.is_finite() or (positive and number <= 0) or (not positive and number < 0):
        raise ValueError("Monetary values must be finite and non-negative.")
    return format(number, "f")


class ResearchImportSourcePDF(ResearchImportModel):
    filename: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class ResearchImportTarget(ResearchImportModel):
    mode: Literal["EXISTING_JOB", "NEW_JOB"]
    job_number: str | None = Field(default=None, pattern=r"^PPS-J-[0-9]{4,12}$")

    @model_validator(mode="after")
    def require_existing_job_number(self) -> "ResearchImportTarget":
        if self.mode == "EXISTING_JOB" and not self.job_number:
            raise ValueError("EXISTING_JOB requires an exact PPS Job number.")
        if self.mode == "NEW_JOB" and self.job_number:
            raise ValueError("NEW_JOB must not include a Job number.")
        return self


class ResearchImportCustomer(ResearchImportModel):
    name: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)


class ResearchImportIdentifier(ResearchImportModel):
    type: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=128)
    primary: bool = False

    @field_validator("type")
    @classmethod
    def supported_type(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in IDENTIFIER_TYPES:
            raise ValueError("Identifier type is not supported by PPS.")
        return value


class ResearchImportMachine(ResearchImportModel):
    reference: str = Field(min_length=1, max_length=128)
    manufacturer: str = Field(default="", max_length=200)
    model: str = Field(default="", max_length=200)
    year: str | None = Field(default=None, max_length=10)
    asset_type: str = Field(default="vehicle", max_length=32)
    identifiers: list[ResearchImportIdentifier] = Field(default_factory=list, max_length=12)

    @field_validator("asset_type")
    @classmethod
    def supported_asset_type(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ASSET_TYPES:
            raise ValueError("Asset type is not supported by PPS.")
        return value


class ResearchImportNeed(ResearchImportModel):
    reference: str = Field(min_length=1, max_length=128)
    original_wording: str = Field(min_length=1, max_length=1000)
    quantity: Decimal = Field(gt=0, le=1000000)
    machine_reference: str | None = Field(default=None, max_length=128)


class ResearchImportFreight(ResearchImportModel):
    amount: str
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    status: Literal["ESTIMATED"] = "ESTIMATED"
    evidence: str = Field(default="", max_length=2000)

    _amount = field_validator("amount")(
        lambda value: _decimal_string(value, positive=False) or "0"
    )


class ResearchImportSourceEvidence(ResearchImportModel):
    pdf_reference: str = Field(min_length=1, max_length=255)
    page: int | None = Field(default=None, ge=1)
    quoted_text: str = Field(default="", max_length=2000)
    source_urls: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("source_urls")
    @classmethod
    def plain_https_urls(cls, values: list[str]) -> list[str]:
        return [_https_url(value) for value in values]


class ResearchImportOption(ResearchImportModel):
    reference: str = Field(min_length=1, max_length=128)
    requested_need_reference: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1000)
    manufacturer: str = Field(default="", max_length=200)
    oem_part_number: str = Field(default="", max_length=200)
    cross_reference_part_numbers: list[str] = Field(default_factory=list, max_length=20)
    supplier: str = Field(default="", max_length=200)
    supplier_url: str | None = None
    quantity: Decimal = Field(gt=0, le=1000000)
    supplier_base_unit_price: str | None = None
    supplier_base_extended_price: str | None = None
    tax_rate_percent: str | None = None
    tax_amount: str | None = None
    tax_inclusive_unit_cost: str | None = None
    tax_inclusive_extended_cost: str | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    fitment_evidence: str = Field(default="", max_length=4000)
    part_number_evidence: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=4000)
    verification_status: Literal["UNVERIFIED", "NEEDS_REVIEW", "VERIFIED"] = "UNVERIFIED"
    confidence: Literal["UNKNOWN", "LOW", "MEDIUM", "HIGH"] = "UNKNOWN"
    source_evidence: ResearchImportSourceEvidence

    @field_validator("supplier_url")
    @classmethod
    def plain_supplier_url(cls, value: str | None) -> str | None:
        return _https_url(value) if value is not None else None

    @field_validator(
        "supplier_base_unit_price", "supplier_base_extended_price", "tax_rate_percent",
        "tax_amount", "tax_inclusive_unit_cost", "tax_inclusive_extended_cost",
    )
    @classmethod
    def decimal_values(cls, value: str | None) -> str | None:
        return _decimal_string(value)


class ResearchImportPackage(ResearchImportModel):
    schema_version: Literal["1"] = "1"
    package_id: str = Field(min_length=1, max_length=128)
    target_proposal_id: int | None = Field(default=None, gt=0)
    source_pdf: ResearchImportSourcePDF
    target: ResearchImportTarget
    customer: ResearchImportCustomer
    machine: ResearchImportMachine
    requested_needs: list[ResearchImportNeed] = Field(min_length=1, max_length=100)
    estimated_inbound_freight: ResearchImportFreight | None = None
    research_options: list[ResearchImportOption] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_references(self) -> "ResearchImportPackage":
        need_refs = {need.reference for need in self.requested_needs}
        if len(need_refs) != len(self.requested_needs):
            raise ValueError("Requested need references must be unique.")
        option_refs = {option.reference for option in self.research_options}
        if len(option_refs) != len(self.research_options):
            raise ValueError("Research option references must be unique.")
        missing = {
            option.requested_need_reference
            for option in self.research_options
            if option.requested_need_reference not in need_refs
        }
        if missing:
            raise ValueError("Every research option must reference a requested need.")
        return self
