from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from legacy_app import templates
from .service import (
    DELETE_PHRASE,
    build_proposal_deletion_plan,
    build_request_deletion_plan,
    delete_disposable_proposal,
    delete_disposable_request,
)
from .test_chain import PURGE_PHRASE, build_test_chain_purge_plan, purge_test_chain

router = APIRouter(tags=["disposable-records"])


def _disposable_test_mode_enabled() -> bool:
    return os.getenv("PPS_ENABLE_DISPOSABLE_TEST_CHAIN", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _require_disposable_test_mode() -> None:
    if not _disposable_test_mode_enabled():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Disposable test utilities are unavailable.")


def _review(request: Request, plan: dict, post_url: str):
    if plan["blockers"]:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="Permanent deletion is blocked by " + ", ".join(plan["blockers"]) + ".")
    return templates.TemplateResponse(request=request, name="disposable_delete_review.html", context={
        "active_page": "requests", "plan": plan, "post_url": post_url,
        "required_number": plan["job_number"] or plan["request_number"], "delete_phrase": DELETE_PHRASE,
    })


@router.get("/requests/{request_id}/delete-disposable", response_class=HTMLResponse)
def review_request_delete(request: Request, request_id: int):
    return _review(request, build_request_deletion_plan(request_id), f"/requests/{request_id}/delete-disposable")


@router.post("/requests/{request_id}/delete-disposable")
def confirm_request_delete(request_id: int, reason: str = Form(...), confirmation_number: str = Form(...),
                           confirmation_phrase: str = Form(...), expected_token: str = Form(...)):
    delete_disposable_request(request_id, reason=reason, confirmation_number=confirmation_number,
                              confirmation_phrase=confirmation_phrase, expected_token=expected_token)
    return RedirectResponse(url="/requests?view=all", status_code=303)


@router.get("/requests/smart-intake/proposals/{proposal_id}/delete-disposable", response_class=HTMLResponse)
def review_proposal_delete(request: Request, proposal_id: int):
    return _review(request, build_proposal_deletion_plan(proposal_id),
                   f"/requests/smart-intake/proposals/{proposal_id}/delete-disposable")


@router.post("/requests/smart-intake/proposals/{proposal_id}/delete-disposable")
def confirm_proposal_delete(proposal_id: int, reason: str = Form(...), confirmation_number: str = Form(...),
                            confirmation_phrase: str = Form(...), expected_token: str = Form(...)):
    delete_disposable_proposal(proposal_id, reason=reason, confirmation_number=confirmation_number,
                               confirmation_phrase=confirmation_phrase, expected_token=expected_token)
    return RedirectResponse(url="/requests?view=all", status_code=303)


@router.get("/jobs/{job_id}/purge-test-chain", response_class=HTMLResponse)
def review_test_chain_purge(request: Request, job_id: int, invoice_number: str):
    _require_disposable_test_mode()
    from legacy_app import get_connection
    from contextlib import closing
    from fastapi import HTTPException
    with closing(get_connection()) as connection:
        row = connection.execute("SELECT job_number FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    plan = build_test_chain_purge_plan(str(row["job_number"]), invoice_number)
    if plan["blockers"]:
        raise HTTPException(status_code=409, detail="Test-chain purge is blocked by " + ", ".join(plan["blockers"]) + ".")
    return templates.TemplateResponse(request=request, name="test_chain_purge_review.html", context={
        "active_page": "jobs", "plan": plan, "purge_phrase": PURGE_PHRASE,
        "post_url": f"/jobs/{job_id}/purge-test-chain",
    })


@router.post("/jobs/{job_id}/purge-test-chain")
def confirm_test_chain_purge(job_id: int, job_number: str = Form(...), invoice_number: str = Form(...),
                             reason: str = Form(...), confirmation_job: str = Form(...),
                             confirmation_invoice: str = Form(...), confirmation_phrase: str = Form(...),
                             expected_token: str = Form(...)):
    _require_disposable_test_mode()
    from legacy_app import get_connection
    from contextlib import closing
    from fastapi import HTTPException
    with closing(get_connection()) as connection:
        row = connection.execute("SELECT job_number FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or str(row["job_number"]) != job_number:
        raise HTTPException(status_code=409, detail="The route and confirmed Job do not match.")
    purge_test_chain(job_number=job_number, invoice_number=invoice_number, reason=reason,
                     confirmation_job=confirmation_job, confirmation_invoice=confirmation_invoice,
                     confirmation_phrase=confirmation_phrase, expected_token=expected_token)
    return RedirectResponse(url="/jobs?view=all", status_code=303)
