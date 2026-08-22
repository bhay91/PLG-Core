from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .service import (
    start_asset_research,
    start_extension_one_time_research,
    start_one_time_research,
    start_part_verification,
)

router=APIRouter(tags=["verification"])


@router.post("/api/research/extension/one-time-context")
async def extension_one_time_context(request: Request):
    payload = await request.json()
    try:
        job_id = int(payload.get("job_id"))
        asset_id = int(payload["job_asset_id"]) if payload.get("job_asset_id") else None
        need_id = int(payload["requested_need_id"]) if payload.get("requested_need_id") else None
        revision_id = int(payload["expected_revision_id"]) if payload.get("expected_revision_id") is not None else None
        revision_version = int(payload["expected_version"]) if payload.get("expected_version") is not None else None
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Invalid PPS research context.") from error
    session = start_extension_one_time_research(
        job_id=job_id, page_url=str(payload.get("page_url") or ""),
        job_asset_id=asset_id, requested_need_id=need_id,
        expected_revision_id=revision_id, expected_version=revision_version,
    )
    return JSONResponse({
        "ok": True,
        "verification_session_id": session["id"],
        "job_id": session["job_id"],
        "job_asset_id": session["job_asset_id"],
        "requested_need_id": session["requested_need_id"],
        "source_name": session["source_name_snapshot"],
        "source_url": session["source_url_snapshot"],
    })


@router.post("/jobs/{job_id}/assets/{asset_id}/research")
def start_asset_research_route(
    job_id: int, asset_id: int,
    connector_profile_id: Annotated[int, Form()],
    requested_need_id: Annotated[int | None, Form()] = None,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    session = start_asset_research(
        job_id, asset_id, connector_profile_id,
        requested_need_id=requested_need_id,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
    return RedirectResponse(
        f"/jobs/{job_id}/research-sessions/{session['id']}/open",
        status_code=303,
    )


@router.post("/jobs/{job_id}/assets/{asset_id}/research/quick-open")
def open_one_time_website_route(
    job_id: int, asset_id: int, url: Annotated[str, Form()],
    requested_need_id: Annotated[int | None, Form()] = None,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    session = start_one_time_research(
        job_id, asset_id, url, requested_need_id=requested_need_id,
        expected_revision_id=expected_revision_id, expected_version=expected_version,
    )
    return RedirectResponse(
        f"/jobs/{job_id}/research-sessions/{session['id']}/open",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items/{item_id}/verify")
def verify_part_route(
    job_id:int,item_id:int,
    connector_profile_id:Annotated[int,Form()],
    expected_revision_id:Annotated[int|None,Form()]=None,
    expected_version:Annotated[int|None,Form()]=None,
):
    start_part_verification(
        job_id,item_id,connector_profile_id,
        expected_revision_id=expected_revision_id,expected_version=expected_version,
    )
    return RedirectResponse(f"/jobs/{job_id}/basket#parts-ready",status_code=303)
