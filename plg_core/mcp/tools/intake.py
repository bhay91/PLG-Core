from __future__ import annotations

from contextlib import closing
import logging

from fastapi import HTTPException
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from legacy_app import get_connection
from plg_core.intake.service import load_proposal, submit_structured_intake
from plg_core.mcp.auth import require_proposal_create
from plg_core.mcp.models import (
    CreateIntakeProposalInput,
    CreateIntakeProposalOutput,
    IntakeCustomerCandidate,
    IntakeMachineCandidate,
    IntakeRequestedNeedCandidate,
    IntakeResearchEvidence,
)


LOGGER = logging.getLogger("pps.mcp")


def _input_digest(proposal: CreateIntakeProposalInput) -> str:
    return proposal.normalized_digest()


async def create_intake_proposal(
    client_reference: str,
    original_input: str,
    customer: IntakeCustomerCandidate | None,
    machines: list[IntakeMachineCandidate],
    requested_needs: list[IntakeRequestedNeedCandidate],
    additional_notes: str,
    ctx: Context,
    research_evidence: IntakeResearchEvidence | None = None,
) -> CreateIntakeProposalOutput:
    """Create only an internal DRAFT proposal; authoritative records remain untouched."""
    require_proposal_create(ctx)
    proposal_input = CreateIntakeProposalInput(
        client_reference=client_reference,
        original_input=original_input,
        customer=customer,
        machines=machines,
        requested_needs=requested_needs,
        additional_notes=additional_notes,
        research_evidence=research_evidence,
    )
    structured = proposal_input.model_dump(
        mode="json",
        include={"customer", "machines", "requested_needs", "additional_notes", "research_evidence"},
    )
    try:
        with closing(get_connection()) as connection:
            proposal_id, duplicate = submit_structured_intake(
                connection,
                origin="CHATGPT_MCP",
                client_reference=proposal_input.client_reference,
                input_digest=_input_digest(proposal_input),
                original_input=proposal_input.original_input,
                structured_candidates=structured,
                actor="mcp-development",
                evidence="Submitted through the authenticated PPS MCP proposal scope.",
            )
            proposal = load_proposal(connection, proposal_id)
    except HTTPException as error:
        if error.status_code == 409:
            raise ToolError(str(error.detail)) from error
        LOGGER.warning("MCP intake proposal rejected status=%s", error.status_code)
        raise ToolError("PPS rejected the intake proposal.") from error
    except ToolError:
        raise
    except Exception as error:
        LOGGER.exception("MCP intake proposal service failure")
        raise ToolError("PPS could not create the DRAFT intake proposal.") from error

    analysis = proposal.get("document_analysis") or {}
    return CreateIntakeProposalOutput(
        proposal_id=proposal_id,
        review_url=f"/requests/smart-intake/proposals/{proposal_id}",
        blockers=list(analysis.get("blockers") or []),
        review_count=int((proposal.get("review_summary") or {}).get("review_count") or 0),
        client_reference=proposal_input.client_reference,
        duplicate=duplicate,
    )
