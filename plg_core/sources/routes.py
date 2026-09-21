from __future__ import annotations

from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.sources.service import (
    build_launch_url,
    canonical_asset_category,
    create_capture_proposal,
    create_source,
)


router = APIRouter(tags=["research-sources"])


@router.post("/api/research/capture-proposals")
async def receive_research_capture_proposal(request: Request):
    """Review-only ingress contract for a future authenticated Firefox adapter."""
    payload = await request.json()
    try:
        job_id = int(payload.get("job_id"))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="A valid Job ID is required.") from error
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        proposal_id = create_capture_proposal(
            connection,
            proposal_type=str(payload.get("proposal_type") or "PART_RESULT"),
            job_id=job_id,
            verification_session_id=(int(payload["verification_session_id"]) if payload.get("verification_session_id") else None),
            job_asset_id=(int(payload["job_asset_id"]) if payload.get("job_asset_id") else None),
            requested_need_id=(int(payload["requested_need_id"]) if payload.get("requested_need_id") else None),
            connector_profile_id=(int(payload["connector_profile_id"]) if payload.get("connector_profile_id") else None),
            page_url=str(payload.get("page_url") or ""), payload=dict(payload.get("data") or {}),
        )
        write_audit(connection, action="RESEARCH_CAPTURE_PROPOSED", entity_type="RESEARCH_CAPTURE_PROPOSAL",
                    entity_id=proposal_id, summary="External research data received for operator review",
                    metadata={"job_id": job_id})
        connection.commit()
    return JSONResponse({"ok": True, "proposal_id": proposal_id, "status": "REVIEW"}, status_code=202)


@router.post("/jobs/{job_id}/research-sources")
def add_job_source(
    job_id: int, display_name: Annotated[str, Form()], source_type: Annotated[str, Form()],
    launch_url: Annotated[str, Form()] = "", manufacturer_applicability: Annotated[str, Form()] = "",
    asset_category_applicability: Annotated[str, Form()] = "", market_applicability: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "", job_asset_id: Annotated[int | None, Form()] = None,
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        asset = connection.execute(
            "SELECT * FROM job_assets WHERE id=? AND job_id=? AND state='ACTIVE'",
            (job_asset_id, job_id),
        ).fetchone()
        if asset is None:
            raise HTTPException(status_code=409, detail="Select an active asset from this Job.")
        source_id = create_source(
            connection, display_name=display_name, source_type=source_type, launch_url=launch_url,
            manufacturer_applicability=asset["manufacturer"] or "",
            asset_category_applicability=canonical_asset_category(asset["asset_type"] or "other"),
            market_applicability=market_applicability, notes=notes,
            provenance=f"JOB_WORKSPACE:{job_id}",
        )
        write_audit(connection, action="RESEARCH_SOURCE_CREATED", entity_type="CONNECTOR_PROFILE",
                    entity_id=source_id, summary=f"Research source saved: {display_name.strip()}",
                    metadata={"job_id": job_id, "job_asset_id": job_asset_id})
        connection.commit()
    suffix = f"&asset_id={job_asset_id}" if job_asset_id else ""
    return RedirectResponse(f"/jobs/{job_id}/basket?view=advanced{suffix}#research-results", status_code=303)


@router.get("/jobs/{job_id}/research-sessions/{session_id}/open")
def open_research_source(job_id: int, session_id: int):
    with closing(get_connection()) as connection:
        session = connection.execute(
            """SELECT vs.*,COALESCE(NULLIF(vs.source_url_snapshot,''),cp.launch_url) AS launch_url,
                      a.manufacturer,a.model,
                      COALESCE(mi.identifier_value,a.vin_pin_serial,'') AS identifier,
                      COALESCE(rn.wording,vs.need_wording_snapshot,'') AS need
               FROM verification_sessions vs
               JOIN connector_profiles cp ON cp.id=vs.connector_profile_id
               JOIN job_assets a ON a.id=vs.job_asset_id
               LEFT JOIN requested_needs rn ON rn.id=vs.requested_need_id
               LEFT JOIN machine_identifiers mi ON mi.machine_id=a.machine_id AND mi.is_primary=1
               WHERE vs.id=? AND vs.job_id=? AND vs.status='ACTIVE'
               ORDER BY mi.id LIMIT 1""",
            (session_id, job_id),
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="Active research context not found.")
        url = build_launch_url(session, dict(session))
        if not url:
            raise HTTPException(
                status_code=409,
                detail="No saved URL is configured for this source. Save a confirmed source from the Job workspace.",
            )
    return RedirectResponse(url, status_code=303)
