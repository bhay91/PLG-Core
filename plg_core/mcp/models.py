from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from plg_core.intake.submission_models import (
    IntakeCustomerCandidate,
    IntakeIdentifierCandidate,
    IntakeMachineCandidate,
    IntakeRequestedNeedCandidate,
    IntakeResearchEvidence,
    StructuredIntakeContent,
)


JOB_NUMBER_PATTERN = re.compile(r"^PPS-J-[0-9]{4,12}$")


def normalize_job_number(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("A PPS Job business number is required.")
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("A PPS Job business number is required.")
    if len(normalized) > 32 or JOB_NUMBER_PATTERN.fullmatch(normalized) is None:
        raise ValueError("Use a PPS Job business number such as PPS-J-0007.")
    return normalized


JobNumber = Annotated[
    str,
    Field(
        min_length=10,
        max_length=32,
        pattern=r"^PPS-J-[0-9]{4,12}$",
        description="Exact PPS Job business number, for example PPS-J-0007. Database IDs are not accepted.",
    ),
    BeforeValidator(normalize_job_number),
]


class StrictMCPModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateIntakeProposalInput(StructuredIntakeContent):
    pass


class CreateIntakeProposalOutput(StrictMCPModel):
    schema_version: Literal["1"] = "1"
    proposal_id: int
    status: Literal["DRAFT"] = "DRAFT"
    review_url: str
    blockers: list[str]
    review_count: int
    origin: Literal["CHATGPT_MCP"] = "CHATGPT_MCP"
    client_reference: str
    duplicate: bool
