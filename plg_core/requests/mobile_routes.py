from __future__ import annotations

from contextlib import closing
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from legacy_app import get_connection
from plg_core.intake.service import load_proposal, submit_structured_intake
from plg_core.intake.submission_models import IntakeSubmissionResult, MobileIntakePackage
from plg_core.requests.mobile_auth import require_mobile_inbox_authorization


router = APIRouter(tags=["mobile-inbox"])
MAX_MOBILE_INBOX_BODY = 64 * 1024


@router.post("/api/mobile/v1/inbox/intake-proposals")
async def create_mobile_inbox_proposal(request: Request):
    require_mobile_inbox_authorization(request)
    content_type = str(request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="PPS Mobile Inbox requires JSON.")
    content_length = request.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_MOBILE_INBOX_BODY:
        raise HTTPException(status_code=413, detail="Mobile Inbox package is too large.")
    body = await request.body()
    if len(body) > MAX_MOBILE_INBOX_BODY:
        raise HTTPException(status_code=413, detail="Mobile Inbox package is too large.")
    try:
        decoded = json.loads(body)
        package = MobileIntakePackage.model_validate(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Mobile Inbox package is invalid.") from None

    try:
        with closing(get_connection()) as connection:
            proposal_id, duplicate = submit_structured_intake(
                connection,
                origin="CHATGPT_MOBILE",
                client_reference=package.client_reference,
                input_digest=package.normalized_digest(),
                original_input=package.original_input,
                structured_candidates=package.structured_candidates(),
                actor="mobile-shortcut",
                evidence="Submitted through the authenticated PPS Mobile Inbox transport.",
            )
            proposal = load_proposal(connection, proposal_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="PPS could not create the DRAFT intake proposal.") from None

    result = IntakeSubmissionResult(
        proposal_id=proposal_id,
        review_url=f"/requests/smart-intake/proposals/{proposal_id}",
        blockers=list((proposal.get("document_analysis") or {}).get("blockers") or []),
        review_count=int((proposal.get("review_summary") or {}).get("review_count") or 0),
        origin="CHATGPT_MOBILE",
        client_reference=package.client_reference,
        duplicate=duplicate,
    )
    return JSONResponse(result.model_dump(mode="json"))
