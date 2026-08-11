from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from legacy_app import get_connection, templates
from plg_core.intake.identifiers import IDENTIFIER_TYPES, MARKETS
from plg_core.intake.service import confirm_proposal, load_proposal


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
            matched_customer_id=?,lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND lock_version=? AND status='DRAFT'""",
            (contact_name.strip(), company_name.strip(), location.strip(), phone.strip(), email.strip(), matched, proposal_id, lock_version),
        )
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before saving.")
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
                model_code=?,matched_machine_id=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=? AND proposal_id=?""",
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
            (proposal_id, asset_id, sequence, wording, wording, "CONFIDENT" if asset_id else "UNASSIGNED"),
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
                (wording.strip(), asset_id, "CONFIDENT" if asset_id else "UNASSIGNED", need_id, proposal_id),
            )
        else:
            raise HTTPException(status_code=400, detail="Requested need wording is required.")
        changed = connection.execute("UPDATE intake_proposals SET lock_version=lock_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND lock_version=?", (proposal_id, lock_version))
        if changed.rowcount != 1:
            connection.rollback()
            raise HTTPException(status_code=409, detail="Proposal changed in another tab. Reload before saving.")
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
    return RedirectResponse(f"/jobs/{job_id}/basket", 303)
