from __future__ import annotations

from contextlib import closing
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from legacy_app import get_connection
from plg_core.intake.service import (
    load_proposal, submit_research_import, submit_structured_intake,
    validate_research_import_uploads,
)
from plg_core.intake.submission_models import FirefoxIntakePackage, IntakeSubmissionResult
from plg_core.requests.extension_auth import (
    require_firefox_inbox_authorization,
    require_firefox_job_update_authorization,
    require_firefox_research_import_authorization,
)
from plg_core.requests.extension_job_models import (
    ExtensionItemAction, ExtensionJobAction, ExtensionPaymentAction,
)
from plg_core.requests.extension_job_service import (
    deliver_job_item, mark_job_orders_placed, receive_job_item, record_job_payment,
)


router = APIRouter(tags=["firefox-inbox"])
MAX_FIREFOX_INBOX_BODY = 64 * 1024
MAX_RESEARCH_IMPORT_BODY = 10 * 1024 * 1024


@router.post("/api/extension/v1/inbox/intake-proposals")
async def create_firefox_inbox_proposal(request: Request):
    require_firefox_inbox_authorization(request)
    content_length = request.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_FIREFOX_INBOX_BODY:
        raise HTTPException(status_code=413, detail="Firefox Inbox package is too large.")
    body = await request.body()
    if len(body) > MAX_FIREFOX_INBOX_BODY:
        raise HTTPException(status_code=413, detail="Firefox Inbox package is too large.")
    try:
        decoded = json.loads(body)
        package = FirefoxIntakePackage.model_validate(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Firefox Inbox package is invalid.") from None

    try:
        with closing(get_connection()) as connection:
            proposal_id, duplicate = submit_structured_intake(
                connection,
                origin="CHATGPT_FIREFOX",
                client_reference=package.client_reference,
                input_digest=package.normalized_digest(),
                original_input=package.original_input,
                structured_candidates=package.structured_candidates(),
                actor="firefox-extension-local",
                evidence="Submitted through the authenticated PPS Firefox Inbox bridge.",
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
        origin="CHATGPT_FIREFOX",
        client_reference=package.client_reference,
        duplicate=duplicate,
    )
    return JSONResponse(result.model_dump(mode="json"))


@router.post("/api/extension/v1/research-import/packages")
async def create_firefox_research_import_package(request: Request):
    """Accept one validated PDF+sidecar package and stage a DRAFT only."""
    require_firefox_research_import_authorization(request)
    content_length = request.headers.get("Content-Length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_RESEARCH_IMPORT_BODY:
        raise HTTPException(status_code=413, detail="Research Import package is too large.")
    try:
        form = await request.form()
    except Exception:
        raise HTTPException(status_code=400, detail="Research Import package upload is invalid.") from None
    items = list(form.multi_items())
    if {key for key, _ in items} != {"research_pdf", "sidecar"} or len(items) != 2:
        raise HTTPException(status_code=400, detail="Research Import requires exactly one PDF and one JSON sidecar.")
    uploads = {key: value for key, value in items}
    if not isinstance(uploads.get("research_pdf"), UploadFile) or not isinstance(uploads.get("sidecar"), UploadFile):
        raise HTTPException(status_code=400, detail="Research Import requires file uploads for both fields.")
    package, pdf = await validate_research_import_uploads(uploads["research_pdf"], uploads["sidecar"])
    try:
        with closing(get_connection()) as connection:
            proposal_id, duplicate = submit_research_import(
                connection, package, pdf=pdf, actor="firefox-research-import",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="PPS could not create the DRAFT research proposal.") from None
    return JSONResponse({
        "schema_version": package.schema_version,
        "status": "DRAFT",
        "proposal_id": proposal_id,
        "package_id": package.package_id,
        "target_mode": package.target.mode,
        "review_url": f"/requests/smart-intake/proposals/{proposal_id}",
        "duplicate": duplicate,
        "pdf_filename": pdf.original_filename,
        "pdf_sha256": package.source_pdf.sha256.lower(),
    })


@router.post("/api/extension/v1/jobs/payment-received")
def extension_payment_received(
    payload: ExtensionPaymentAction,
    _scope: str = Depends(require_firefox_job_update_authorization),
):
    return record_job_payment(payload)


@router.post("/api/extension/v1/jobs/order-placed")
def extension_order_placed(
    payload: ExtensionJobAction,
    _scope: str = Depends(require_firefox_job_update_authorization),
):
    return mark_job_orders_placed(payload)


@router.post("/api/extension/v1/jobs/item-received")
def extension_item_received(
    payload: ExtensionItemAction,
    _scope: str = Depends(require_firefox_job_update_authorization),
):
    return receive_job_item(payload)


@router.post("/api/extension/v1/jobs/item-delivered")
def extension_item_delivered(
    payload: ExtensionItemAction,
    _scope: str = Depends(require_firefox_job_update_authorization),
):
    return deliver_job_item(payload)
