from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pathlib import Path

from legacy_app import get_connection, templates
from plg_core.intake.identifiers import IDENTIFIER_TYPES, MARKETS
from plg_core.intake.service import (
    confirm_proposal, load_proposal, record_customer_resolution, record_machine_resolution,
    refresh_proposal_analysis,
)


router = APIRouter(prefix="/requests/smart-intake/proposals", tags=["smart-intake"])


def _draft(connection, proposal_id: int):
    row = connection.execute("SELECT * FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Smart Intake proposal not found.")
    if row["status"] != "DRAFT":
        raise HTTPException(status_code=409, detail="This proposal is no longer editable.")
    return row


@router.get("/{proposal_id}", response_class=HTMLResponse)
def review(request: Request, proposal_id: int):
    with closing(get_connection()) as connection:
        proposal = load_proposal(connection, proposal_id)
        customers = connection.execute("SELECT id,name,company,customer_number FROM customers WHERE active=1 ORDER BY name").fetchall()
    return templates.TemplateResponse(
        request=request,
        name="smart_intake_proposal.html",
        context={"active_page": "requests", "proposal": proposal, "identifier_types": IDENTIFIER_TYPES,
                 "markets": MARKETS, "customers": customers},
    )


@router.get("/{proposal_id}/attachments/{attachment_id}")
def preview_attachment(proposal_id: int, attachment_id: int):
    with closing(get_connection()) as connection:
        row = connection.execute(
            "SELECT * FROM intake_proposal_attachments WHERE id=? AND proposal_id=?",
            (attachment_id, proposal_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Proposal attachment not found.")
    path = Path(row["stored_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Proposal attachment file is missing.")
    return FileResponse(path, media_type=row["media_type"], filename=row["original_filename"], content_disposition_type="inline")


@router.post("/{proposal_id}/attachments/{attachment_id}/remove")
def remove_attachment(proposal_id: int, attachment_id: int, lock_version: int = Form(...)):
    with closing(get_connection()) as connection:
        _draft(connection, proposal_id)
        row = connection.execute(
            "SELECT * FROM intake_proposal_attachments WHERE id=? AND proposal_id=?",
            (attachment_id, proposal_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Proposal attachment not found.")
        changed = connection.execute(
            "UPDATE intake_proposals SET lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lock_version=? AND status='DRAFT'",
            (proposal_id, lock_version),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before removing the image.")
        connection.execute("DELETE FROM intake_proposal_attachments WHERE id=? AND proposal_id=?", (attachment_id, proposal_id))
        connection.commit()
    Path(row["stored_path"]).unlink(missing_ok=True)
    return RedirectResponse(f"/requests/smart-intake/proposals/{proposal_id}", 303)


@router.post("/{proposal_id}/remove-from-inbox")
def remove_proposal_from_inbox(proposal_id: int, lock_version: int = Form(...)):
    with closing(get_connection()) as connection:
        _draft(connection, proposal_id)
        changed = connection.execute(
            """UPDATE intake_proposals
               SET status='CANCELLED',lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status='DRAFT' AND lock_version=?""",
            (proposal_id, lock_version),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="This proposal changed. Reload the Inbox before removing it.")
        from plg_core.audit import write_audit
        write_audit(connection, action="SMART_INTAKE_CANCELLED", entity_type="INTAKE_PROPOSAL",
                    entity_id=proposal_id,
                    summary=f"Smart Intake proposal {proposal_id} removed from Inbox; history preserved")
        connection.commit()
    return RedirectResponse("/requests", 303)


@router.post("/{proposal_id}/customer")
def update_customer(
    proposal_id: int, contact_name: str = Form(""), company_name: str = Form(""),
    location: str = Form(""), phone: str = Form(""), email: str = Form(""),
    matched_customer_id: str = Form(""), lock_version: int = Form(...),
):
    with closing(get_connection()) as connection:
        _draft(connection, proposal_id)
        matched = int(matched_customer_id) if matched_customer_id.strip().isdigit() else None
        changed = connection.execute(
            """UPDATE intake_proposals SET contact_name=?,company_name=?,location=?,phone=?,email=?,
            matched_customer_id=?,review_state='CONFIDENT',lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND lock_version=? AND status='DRAFT'""",
            (contact_name.strip(), company_name.strip(), location.strip(), phone.strip(), email.strip(), matched, proposal_id, lock_version),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before saving.")
        record_customer_resolution(connection, proposal_id, matched)
        refresh_proposal_analysis(connection, proposal_id)
        connection.commit()
    return RedirectResponse(f"/requests/smart-intake/proposals/{proposal_id}", 303)


@router.post("/{proposal_id}/assets/add")
def add_asset(proposal_id: int, lock_version: int = Form(...)):
    with closing(get_connection()) as connection:
        _draft(connection, proposal_id)
        sequence = int(connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM intake_proposal_assets WHERE proposal_id=?", (proposal_id,)).fetchone()[0])
        connection.execute("INSERT INTO intake_proposal_assets (proposal_id,sequence,review_state) VALUES (?,?,'REVIEW')", (proposal_id, sequence))
        changed = connection.execute(
            "UPDATE intake_proposals SET lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lock_version=?",
            (proposal_id, lock_version),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before adding an asset.")
        connection.commit()
    return RedirectResponse(f"/requests/smart-intake/proposals/{proposal_id}", 303)


@router.post("/{proposal_id}/assets/{asset_id}")
def update_asset(
    proposal_id: int, asset_id: int, manufacturer: str = Form(""), model: str = Form(""),
    year: str = Form(""), asset_category: str = Form("other"), market_region: str = Form("UNKNOWN"),
    model_code: str = Form(""), identifier_type: str = Form("UNKNOWN"), identifier_value: str = Form(""),
    engine_serial: str = Form(""), action: str = Form("save"), lock_version: int = Form(...),
):
    if identifier_type not in IDENTIFIER_TYPES or market_region not in MARKETS:
        raise HTTPException(status_code=400, detail="Invalid identifier or market selection.")
    with closing(get_connection()) as connection:
        _draft(connection, proposal_id)
        if action == "remove":
            connection.execute("UPDATE intake_proposal_assets SET included=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND proposal_id=?", (asset_id, proposal_id))
            connection.execute("UPDATE intake_proposal_needs SET proposal_asset_id=NULL,review_state='UNASSIGNED' WHERE proposal_asset_id=?", (asset_id,))
        else:
            connection.execute(
                """UPDATE intake_proposal_assets SET manufacturer=?,model=?,year=?,asset_category=?,market_region=?,
                model_code=?,matched_machine_id=NULL,review_state='CONFIDENT',updated_at=CURRENT_TIMESTAMP WHERE id=? AND proposal_id=?""",
                (manufacturer.strip(), model.strip(), year.strip(), asset_category.strip() or "other", market_region,
                 model_code.strip(), asset_id, proposal_id),
            )
            connection.execute("DELETE FROM intake_proposal_identifiers WHERE proposal_asset_id=?", (asset_id,))
            if identifier_value.strip():
                connection.execute(
                    """INSERT INTO intake_proposal_identifiers
                    (proposal_id,proposal_asset_id,identifier_type,identifier_value,is_primary,review_state)
                    VALUES (?,?,?,?,1,'CONFIDENT')""", (proposal_id, asset_id, identifier_type, identifier_value.strip().upper()),
                )
            if model_code.strip():
                connection.execute(
                    """INSERT INTO intake_proposal_identifiers
                    (proposal_id,proposal_asset_id,identifier_type,identifier_value,is_primary,review_state)
                    VALUES (?,?, 'MODEL_CODE',?,0,'CONFIDENT')""", (proposal_id, asset_id, model_code.strip().upper()),
                )
            if engine_serial.strip():
                connection.execute(
                    """INSERT INTO intake_proposal_identifiers
                    (proposal_id,proposal_asset_id,identifier_type,identifier_value,component_label,is_primary,review_state)
                    VALUES (?,?, 'ENGINE_SERIAL',?,'Engine',0,'CONFIDENT')""", (proposal_id, asset_id, engine_serial.strip().upper()),
                )
        changed = connection.execute("UPDATE intake_proposals SET lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lock_version=?", (proposal_id, lock_version))
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before saving.")
        refresh_proposal_analysis(connection, proposal_id)
        connection.commit()
    return RedirectResponse(f"/requests/smart-intake/proposals/{proposal_id}", 303)


@router.post("/{proposal_id}/assets/{asset_id}/match")
def resolve_machine_match(
    proposal_id: int,
    asset_id: int,
    machine_resolution: str = Form(...),
    lock_version: int = Form(...),
):
    with closing(get_connection()) as connection:
        _draft(connection, proposal_id)
        proposal = load_proposal(connection, proposal_id)
        assets = proposal.get("assets") or []
        asset_index = next((index for index, asset in enumerate(assets) if int(asset["id"]) == asset_id), None)
        if asset_index is None:
            raise HTTPException(status_code=404, detail="Proposed machine not found.")
        matches = (proposal.get("document_analysis") or {}).get("machine_matches") or []
        match = matches[asset_index] if asset_index < len(matches) else {}
        candidate_ids = {str(candidate["id"]) for candidate in match.get("candidates") or []}
        resolution = machine_resolution.strip().upper()
        if resolution != "NEW" and resolution not in candidate_ids:
            raise HTTPException(status_code=400, detail="Select an available machine match or create a new machine.")
        changed = connection.execute(
            """UPDATE intake_proposals SET lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND lock_version=? AND status='DRAFT'""",
            (proposal_id, lock_version),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before resolving the machine.")
        connection.execute(
            "UPDATE intake_proposal_assets SET review_state='CONFIDENT',updated_at=CURRENT_TIMESTAMP WHERE id=? AND proposal_id=?",
            (asset_id, proposal_id),
        )
        connection.execute(
            "UPDATE intake_proposal_identifiers SET review_state='CONFIDENT' WHERE proposal_asset_id=? AND proposal_id=?",
            (asset_id, proposal_id),
        )
        record_machine_resolution(
            connection, proposal_id, asset_id, "NEW" if resolution == "NEW" else int(resolution),
        )
        refresh_proposal_analysis(connection, proposal_id)
        connection.commit()
    return RedirectResponse(f"/requests/smart-intake/proposals/{proposal_id}", 303)


@router.post("/{proposal_id}/needs/add")
def add_need(
    proposal_id: int, wording: str = Form(...), proposal_asset_id: str = Form(""),
    lock_version: int = Form(...),
):
    wording = wording.strip()
    if not wording:
        raise HTTPException(status_code=400, detail="Requested need wording is required.")
    asset_id = int(proposal_asset_id) if proposal_asset_id.isdigit() else None
    with closing(get_connection()) as connection:
        _draft(connection, proposal_id)
        if asset_id and not connection.execute("SELECT 1 FROM intake_proposal_assets WHERE id=? AND proposal_id=? AND included=1", (asset_id, proposal_id)).fetchone():
            raise HTTPException(status_code=400, detail="Selected proposed asset is unavailable.")
        sequence = int(connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM intake_proposal_needs WHERE proposal_id=?", (proposal_id,)).fetchone()[0])
        connection.execute(
            "INSERT INTO intake_proposal_needs (proposal_id,proposal_asset_id,sequence,wording,original_wording,review_state) VALUES (?,?,?,?,?,?)",
            (proposal_id, asset_id, sequence, wording, wording, "CONFIDENT"),
        )
        changed = connection.execute(
            "UPDATE intake_proposals SET lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lock_version=?",
            (proposal_id, lock_version),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before adding a need.")
        connection.commit()
    return RedirectResponse(f"/requests/smart-intake/proposals/{proposal_id}", 303)


@router.post("/{proposal_id}/needs/{need_id}")
def update_need(
    proposal_id: int, need_id: int, wording: str = Form(""), proposal_asset_id: str = Form(""),
    action: str = Form("save"), lock_version: int = Form(...),
):
    asset_id = int(proposal_asset_id) if proposal_asset_id.isdigit() else None
    with closing(get_connection()) as connection:
        _draft(connection, proposal_id)
        if action == "remove":
            connection.execute("UPDATE intake_proposal_needs SET included=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND proposal_id=?", (need_id, proposal_id))
        elif wording.strip():
            connection.execute(
                "UPDATE intake_proposal_needs SET wording=?,proposal_asset_id=?,review_state=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND proposal_id=?",
                (wording.strip(), asset_id, "CONFIDENT", need_id, proposal_id),
            )
        else:
            raise HTTPException(status_code=400, detail="Requested need wording is required.")
        changed = connection.execute("UPDATE intake_proposals SET lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lock_version=?", (proposal_id, lock_version))
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before saving.")
        refresh_proposal_analysis(connection, proposal_id)
        connection.commit()
    return RedirectResponse(f"/requests/smart-intake/proposals/{proposal_id}", 303)


@router.post("/{proposal_id}/confirm")
def confirm(proposal_id: int, lock_version: int = Form(...)):
    with closing(get_connection()) as connection:
        try:
            job_id = confirm_proposal(connection, proposal_id, lock_version)
        except Exception:
            connection.rollback()
            raise
    return RedirectResponse(f"/jobs/{job_id}/basket?view=advanced", 303)
