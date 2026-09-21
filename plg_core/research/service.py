from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import re

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.basket.service import add_item, update_item
from plg_core.sources.service import validate_source_url
from plg_core.revisions.service import ensure_revision_mutable, touch_revision
from plg_core.timeline import log_job_event


SHIPPING_RANK = {
    "ESTIMATED_LOW": 10,
    "ESTIMATED_MEDIUM": 20,
    "ESTIMATED_HIGH": 30,
    "MANUAL": 35,
    "VERIFIED": 40,
    "ACTUAL": 50,
}
_QUANTITY_UNSET = object()


def normalize_requested_quantity(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise HTTPException(400, "Quantity must be a whole number of 1 or more.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Quantity must be a whole number of 1 or more.") from exc
    if str(value).strip() != str(parsed) and not isinstance(value, int):
        raise HTTPException(400, "Quantity must be a whole number of 1 or more.")
    if parsed < 1:
        raise HTTPException(400, "Quantity must be a whole number of 1 or more.")
    return parsed


def derive_result_visibility(item: dict, source: dict | None = None, shipping: dict | None = None) -> dict:
    """Derive operator-facing observability without creating workflow state."""
    source, shipping = source or {}, shipping or {}
    state = str(item.get("research_state") or "").upper()
    if item.get("selected") and state in {"QUOTE_CANDIDATE", "LEGACY_CANDIDATE"}:
        next_action = "READY FOR QUOTE"
    elif item.get("supplier_unit_cost") is None:
        next_action = "ADD SUPPLIER PRICE"
    elif not (item.get("research_evidence") or item.get("research_notes") or item.get("source_url")):
        next_action = "REVIEW FITMENT / EVIDENCE"
    else:
        next_action = "CONFIRM FOR QUOTE"
    evidence = str(item.get("research_evidence") or "")
    match = re.search(r"Capture mode:\s*(PAGE|CART|MERGED)\b", evidence, re.I)
    source_name = str(source.get("source_name") or item.get("supplier_name") or "").strip()
    origin = "One-time Website" if str(source.get("source_key") or "").lower() == "one_time_website" or source_name == "One-time Website" else (match.group(1).upper() if match else "")
    provenance = str(shipping.get("provenance") or "").strip()
    if not provenance and shipping:
        provenance = str(shipping.get("quality") or "").replace("_", " ").title()
    return {"next_action": next_action, "source_display_name": source_name,
            "capture_origin": origin, "shipping_provenance": provenance}


def _revision(connection, job_id, expected_revision_id, expected_version):
    return ensure_revision_mutable(
        connection, job_id,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )


def _active_asset(connection, job_id: int, asset_id: int | None):
    if asset_id is None:
        return None
    asset = connection.execute(
        "SELECT * FROM job_assets WHERE id=? AND job_id=? AND state='ACTIVE'",
        (asset_id, job_id),
    ).fetchone()
    if asset is None:
        raise HTTPException(status_code=409, detail="Select an active asset from this Job.")
    return asset


def create_requested_need(
    job_id: int, *, job_asset_id: int | None, wording: str, notes: str = "", quantity: int | None = None,
    expected_revision_id: int | None = None, expected_version: int | None = None,
):
    wording = str(wording or "").strip()
    quantity = normalize_requested_quantity(quantity)
    if not wording:
        raise HTTPException(status_code=400, detail="Requested Need wording is required.")
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        revision = _revision(connection, job_id, expected_revision_id, expected_version)
        _active_asset(connection, job_id, job_asset_id)
        request = connection.execute(
            "SELECT id FROM customer_requests WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
        need_id = int(connection.execute(
            "INSERT INTO requested_needs(job_id,job_asset_id,customer_request_id,wording,notes,quantity) VALUES (?,?,?,?,?,?)",
            (job_id, job_asset_id, request["id"] if request else None, wording, str(notes or "").strip(), quantity),
        ).lastrowid)
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        write_audit(connection, action="REQUESTED_NEED_CREATED", entity_type="REQUESTED_NEED",
                    entity_id=need_id, summary=f"Customer Need added: {wording}",
                    metadata={"job_id": job_id, "job_asset_id": job_asset_id})
        log_job_event(connection, job_id=job_id, event_type="REQUESTED_NEED_CREATED",
                      icon="?", message=f"Customer Need added: {wording}")
        connection.commit()
        return dict(connection.execute("SELECT * FROM requested_needs WHERE id=?", (need_id,)).fetchone())


def update_requested_need(
    job_id: int, need_id: int, *, wording: str | None = None,
    state: str | None = None, resolution: str = "", quantity=_QUANTITY_UNSET,
    expected_revision_id: int | None = None, expected_version: int | None = None,
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        revision = _revision(connection, job_id, expected_revision_id, expected_version)
        need = connection.execute(
            "SELECT * FROM requested_needs WHERE id=? AND job_id=?", (need_id, job_id)
        ).fetchone()
        if need is None:
            raise HTTPException(status_code=404, detail="Requested Need not found.")
        historical = connection.execute(
            """
            SELECT 1 FROM quote_items qi
            JOIN work_revision_item_need_links link
              ON link.work_revision_item_id=qi.origin_work_revision_item_id
            JOIN quotes q ON q.id=qi.quote_id
            WHERE link.requested_need_id=? AND q.status!='DRAFT' LIMIT 1
            """, (need_id,),
        ).fetchone()
        if quantity is _QUANTITY_UNSET:
            quantity = need["quantity"]
        else:
            quantity = normalize_requested_quantity(quantity)
        new_wording = str(wording if wording is not None else need["wording"]).strip()
        if not new_wording:
            raise HTTPException(status_code=400, detail="Requested Need wording is required.")
        if historical and new_wording != need["wording"]:
            raise HTTPException(status_code=409, detail="Issued quote history protects this Customer Need wording.")
        new_state = str(state or need["state"]).upper()
        if new_state not in {"OPEN", "SATISFIED", "ARCHIVED"}:
            raise HTTPException(status_code=400, detail="Invalid Requested Need state.")
        connection.execute(
            "UPDATE requested_needs SET wording=?,quantity=?,state=?,resolution=?,lock_version=lock_version+1,"
            "resolved_at=CASE WHEN ?='SATISFIED' THEN COALESCE(resolved_at,CURRENT_TIMESTAMP) ELSE NULL END,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_wording, quantity, new_state, str(resolution or "").strip(), new_state, need_id),
        )
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        write_audit(connection, action="REQUESTED_NEED_UPDATED", entity_type="REQUESTED_NEED",
                    entity_id=need_id, summary=f"Customer Need {new_state.lower()}: {new_wording}",
                    metadata={"job_id": job_id, "state": new_state})
        connection.commit()
        return dict(connection.execute("SELECT * FROM requested_needs WHERE id=?", (need_id,)).fetchone())


def create_manual_research_result(
    job_id: int, *, job_asset_id: int | None, requested_need_id: int | None,
    description: str, manufacturer_part_number: str = "", quantity: int | None = None,
    supplier_name: str = "", supplier_part_number: str = "",
    alternate_part_number: str = "",
    supplier_unit_cost: float | None = None, customer_unit_price_override: float | None = None, source_type: str = "AFTERMARKET",
    availability: str = "", lead_time: str = "",
    research_session_id: int | None = None, source_url: str = "",
    verification_status: str = "NEEDS_REVIEW", research_evidence: str = "",
    research_notes: str = "",
    expected_revision_id: int | None = None, expected_version: int | None = None,
):
    if quantity is None and requested_need_id is not None:
        with closing(get_connection()) as connection:
            row = connection.execute("SELECT quantity FROM requested_needs WHERE id=? AND job_id=?", (requested_need_id, job_id)).fetchone()
            quantity = row["quantity"] if row and row["quantity"] is not None else 1
    if quantity is None:
        quantity = 1
    verification_status = str(verification_status or "NEEDS_REVIEW").upper()
    if verification_status not in {"VERIFIED", "PROVISIONAL", "NEEDS_REVIEW", "UNVERIFIED", "REJECTED"}:
        raise HTTPException(status_code=400, detail="Invalid Research Result verification status.")
    if research_session_id is None and job_asset_id is not None:
        with closing(get_connection()) as connection:
            active = connection.execute(
                "SELECT id FROM verification_sessions WHERE job_id=? AND job_asset_id=? "
                "AND COALESCE(requested_need_id,0)=COALESCE(?,0) AND status='ACTIVE' "
                "ORDER BY id DESC LIMIT 1",
                (job_id, job_asset_id, requested_need_id),
            ).fetchone()
            research_session_id = int(active["id"]) if active else None
    source_url = validate_source_url(source_url)
    return add_item(
        job_id,
        BasketItemCreate(
            job_asset_id=job_asset_id,
            primary_requested_need_id=requested_need_id,
            research_state="RESEARCH_RESULT",
            requested_description=description,
            manufacturer_part_number=manufacturer_part_number,
            quantity=quantity,
            supplier_name=supplier_name,
            supplier_part_number=supplier_part_number,
            alternate_part_number=alternate_part_number,
            supplier_unit_cost=supplier_unit_cost,
            customer_unit_price_override=customer_unit_price_override,
            source_type=source_type,
            availability=availability,
            lead_time=lead_time,
            source_url=source_url,
            verification_status=verification_status,
            verification_note=str(research_evidence or "").strip(),
            research_session_id=research_session_id,
            research_evidence=str(research_evidence or "").strip(),
            research_notes=str(research_notes or "").strip(),
            identified_at=datetime.now(timezone.utc).isoformat(),
            selected=False,
        ),
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )


def add_supplier_quote_to_result(
    job_id: int, item_id: int, *, supplier_name: str,
    supplier_unit_cost: float | None = None, supplier_part_number: str = "",
    availability: str = "", source_url: str = "", evidence: str = "",
    expected_revision_id: int | None = None, expected_version: int | None = None,
):
    supplier_name = str(supplier_name or "").strip()
    if not supplier_name:
        raise HTTPException(status_code=400, detail="Supplier name is required.")
    source_url = validate_source_url(source_url)
    with closing(get_connection()) as connection:
        item = connection.execute(
            "SELECT bi.* FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id "
            "WHERE bi.id=? AND b.job_id=?", (item_id, job_id),
        ).fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="Research Result not found.")
    if str(item["research_state"] or "").upper() != "RESEARCH_RESULT":
        raise HTTPException(status_code=409, detail="Supplier pricing can only be added to a Research Result here.")
    updates = {
        "supplier_name": supplier_name,
        "supplier_part_number": str(supplier_part_number or "").strip(),
        "supplier_unit_cost": supplier_unit_cost,
        "availability": str(availability or "").strip(),
        "source_url": source_url,
    }
    evidence = str(evidence or "").strip()
    if evidence:
        updates["research_evidence"] = evidence
    basket = update_item(
        item_id, BasketItemUpdate(**updates), expected_job_id=job_id,
        expected_revision_id=expected_revision_id, expected_version=expected_version,
    )
    with closing(get_connection()) as connection:
        write_audit(
            connection, action="RESEARCH_RESULT_SUPPLIER_QUOTED",
            entity_type="BASKET_ITEM", entity_id=item_id,
            summary=f"Supplier quote recorded for {item['requested_description']}",
            metadata={"job_id": job_id, "supplier_name": supplier_name},
        )
        log_job_event(
            connection, job_id=job_id, event_type="RESEARCH_RESULT_SUPPLIER_QUOTED",
            icon="$", message=f"Supplier quote added for {item['requested_description']} from {supplier_name}",
        )
        connection.commit()
    return next(item for item in basket["items"] if int(item["id"]) == int(item_id))


def set_quote_candidate(
    job_id: int, item_id: int, *, candidate: bool,
    requested_need_ids: list[int] | None = None,
    expected_revision_id: int | None = None, expected_version: int | None = None,
):
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        revision = _revision(connection, job_id, expected_revision_id, expected_version)
        item = connection.execute(
            "SELECT bi.* FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id "
            "WHERE bi.id=? AND b.job_id=?", (item_id, job_id),
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Research Result not found.")
        # The persisted basket-item id is the stable PPS identity for every
        # candidate. Product references remain available for parts-specialist
        # work, but universal goods and services do not need an invented part
        # number in order to receive explicit operator approval.
        if candidate and not str(item["requested_description"] or "").strip():
            raise HTTPException(
                status_code=409,
                detail="A Quote Candidate needs a description.",
            )
        need_ids = sorted({int(value) for value in (requested_need_ids or [])})
        if item["primary_requested_need_id"] and not need_ids:
            need_ids = [int(item["primary_requested_need_id"])]
        for need_id in need_ids:
            need = connection.execute(
                "SELECT job_asset_id FROM requested_needs WHERE id=? AND job_id=?", (need_id, job_id)
            ).fetchone()
            if need is None:
                raise HTTPException(status_code=409, detail="Requested Need does not belong to this Job.")
            if need["job_asset_id"] is not None and item["job_asset_id"] != need["job_asset_id"]:
                raise HTTPException(status_code=409, detail="Research Result and Requested Need must belong to the same asset.")
        state = "QUOTE_CANDIDATE" if candidate else "RESEARCH_RESULT"
        connection.execute(
            "UPDATE basket_items SET research_state=?,selected=?,primary_requested_need_id=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (state, int(candidate), need_ids[0] if need_ids else None, item_id),
        )
        connection.execute("DELETE FROM basket_item_need_links WHERE basket_item_id=?", (item_id,))
        for need_id in need_ids:
            connection.execute(
                "INSERT INTO basket_item_need_links(basket_item_id,requested_need_id,relationship) "
                "VALUES (?,?,'SATISFIES')", (item_id, need_id),
            )
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        action = "RESEARCH_RESULT_PROMOTED" if candidate else "QUOTE_CANDIDATE_RETURNED"
        write_audit(connection, action=action, entity_type="BASKET_ITEM", entity_id=item_id,
                    summary=f"{item['requested_description']} {'confirmed as a Quote Candidate' if candidate else 'returned to Research Results'}",
                    metadata={"job_id": job_id, "requested_need_ids": need_ids})
        log_job_event(connection, job_id=job_id, event_type=action, icon="✓" if candidate else "↩",
                      message=f"{item['requested_description']} {'ready for quote' if candidate else 'returned to research'}")
        connection.commit()
        return dict(connection.execute("SELECT * FROM basket_items WHERE id=?", (item_id,)).fetchone())


def set_preferred_sourcing_option(
    job_id: int, requested_need_id: int, basket_item_id: int, *,
    expected_revision_id: int | None = None, expected_version: int | None = None,
):
    """Set one preferred candidate for one requested need atomically."""
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        revision = _revision(connection, job_id, expected_revision_id, expected_version)
        need = connection.execute(
            "SELECT id FROM requested_needs WHERE id=? AND job_id=?", (requested_need_id, job_id)
        ).fetchone()
        if need is None:
            raise HTTPException(status_code=404, detail="Requested Need not found.")
        item = connection.execute(
            "SELECT bi.* FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id "
            "WHERE bi.id=? AND b.job_id=?", (basket_item_id, job_id)
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Sourcing option not found.")
        link = connection.execute(
            "SELECT 1 FROM basket_item_need_links WHERE basket_item_id=? AND requested_need_id=?",
            (basket_item_id, requested_need_id),
        ).fetchone()
        if link is None:
            raise HTTPException(status_code=409, detail="Sourcing option is not linked to this requested item.")
        if str(item["verification_status"] or "UNVERIFIED").upper() == "REJECTED":
            raise HTTPException(status_code=409, detail="Rejected sourcing options cannot be preferred.")
        connection.execute(
            "UPDATE basket_items SET research_state='QUOTE_CANDIDATE', selected=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (basket_item_id,),
        )
        connection.execute(
            "UPDATE basket_item_need_links SET preferred=0 WHERE requested_need_id=?", (requested_need_id,)
        )
        connection.execute(
            "UPDATE basket_item_need_links SET preferred=1 WHERE basket_item_id=? AND requested_need_id=?",
            (basket_item_id, requested_need_id),
        )
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        write_audit(connection, action="SOURCING_OPTION_PREFERRED", entity_type="BASKET_ITEM",
                    entity_id=basket_item_id, summary=f"Sourcing option preferred for requested need {requested_need_id}",
                    metadata={"job_id": job_id, "requested_need_id": requested_need_id})
        log_job_event(connection, job_id=job_id, event_type="SOURCING_OPTION_PREFERRED", icon="✓",
                      message=f"Sourcing option preferred for requested need {requested_need_id}")
        connection.commit()
        return dict(connection.execute("SELECT * FROM basket_items WHERE id=?", (basket_item_id,)).fetchone())


def save_shipping_data(
    job_id: int, item_id: int, *, quality: str, unit_weight: float | None = None,
    weight_unit: str = "lb", length: float | None = None, width: float | None = None,
    height: float | None = None, dimension_unit: str = "in", provenance: str = "",
    confidence: float | None = None, notes: str = "",
):
    quality = str(quality or "MANUAL").upper()
    if quality not in SHIPPING_RANK:
        raise HTTPException(status_code=400, detail="Invalid shipping-data quality.")
    values = (unit_weight, length, width, height)
    if any(value is not None and float(value) < 0 for value in values):
        raise HTTPException(status_code=400, detail="Shipping measurements cannot be negative.")
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute(
            "SELECT bi.* FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id "
            "WHERE bi.id=? AND b.job_id=?", (item_id, job_id),
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Research Result not found.")
        current = connection.execute(
            "SELECT * FROM part_shipping_data WHERE basket_item_id=? AND is_current=1 "
            "ORDER BY id DESC LIMIT 1", (item_id,),
        ).fetchone()
        if current and SHIPPING_RANK[quality] < SHIPPING_RANK[str(current["quality"])]:
            raise HTTPException(
                status_code=409,
                detail=f"{quality.replace('_',' ').title()} data cannot replace existing {current['quality'].replace('_',' ').title()} data.",
            )
        connection.execute("UPDATE part_shipping_data SET is_current=0 WHERE basket_item_id=? AND is_current=1", (item_id,))
        measured = datetime.now(timezone.utc).isoformat() if quality == "ACTUAL" else None
        shipping_id = int(connection.execute(
            """
            INSERT INTO part_shipping_data (
                basket_item_id,manufacturer_part_number,unit_weight,weight_unit,
                length,width,height,dimension_unit,quality,provenance,confidence,
                measured_at,notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (item_id, item["manufacturer_part_number"] or "", unit_weight, weight_unit,
             length, width, height, dimension_unit, quality, str(provenance or "").strip(),
             confidence, measured, str(notes or "").strip()),
        ).lastrowid)
        write_audit(connection, action="PART_SHIPPING_DATA_SAVED", entity_type="PART_SHIPPING_DATA",
                    entity_id=shipping_id, summary=f"Shipping data saved as {quality}",
                    metadata={"job_id": job_id, "basket_item_id": item_id})
        connection.commit()
        return dict(connection.execute("SELECT * FROM part_shipping_data WHERE id=?", (shipping_id,)).fetchone())
