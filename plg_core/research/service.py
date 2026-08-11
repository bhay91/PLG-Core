from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.basket.models import BasketItemCreate
from plg_core.basket.service import add_item
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
    job_id: int, *, job_asset_id: int | None, wording: str, notes: str = "",
    expected_revision_id: int | None = None, expected_version: int | None = None,
):
    wording = str(wording or "").strip()
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
            "INSERT INTO requested_needs(job_id,job_asset_id,customer_request_id,wording,notes) "
            "VALUES (?,?,?,?,?)",
            (job_id, job_asset_id, request["id"] if request else None, wording, str(notes or "").strip()),
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
    state: str | None = None, resolution: str = "",
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
        new_wording = str(wording if wording is not None else need["wording"]).strip()
        if not new_wording:
            raise HTTPException(status_code=400, detail="Requested Need wording is required.")
        if historical and new_wording != need["wording"]:
            raise HTTPException(status_code=409, detail="Issued quote history protects this Customer Need wording.")
        new_state = str(state or need["state"]).upper()
        if new_state not in {"OPEN", "SATISFIED", "ARCHIVED"}:
            raise HTTPException(status_code=400, detail="Invalid Requested Need state.")
        connection.execute(
            "UPDATE requested_needs SET wording=?,state=?,resolution=?,lock_version=lock_version+1,"
            "resolved_at=CASE WHEN ?='SATISFIED' THEN COALESCE(resolved_at,CURRENT_TIMESTAMP) ELSE NULL END,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_wording, new_state, str(resolution or "").strip(), new_state, need_id),
        )
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        write_audit(connection, action="REQUESTED_NEED_UPDATED", entity_type="REQUESTED_NEED",
                    entity_id=need_id, summary=f"Customer Need {new_state.lower()}: {new_wording}",
                    metadata={"job_id": job_id, "state": new_state})
        connection.commit()
        return dict(connection.execute("SELECT * FROM requested_needs WHERE id=?", (need_id,)).fetchone())


def create_manual_research_result(
    job_id: int, *, job_asset_id: int | None, requested_need_id: int | None,
    description: str, manufacturer_part_number: str = "", quantity: int = 1,
    supplier_name: str = "", supplier_part_number: str = "",
    supplier_unit_cost: float | None = None, source_type: str = "AFTERMARKET",
    expected_revision_id: int | None = None, expected_version: int | None = None,
):
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
            supplier_unit_cost=supplier_unit_cost,
            source_type=source_type,
            selected=False,
        ),
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )


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
        if candidate and not (
            str(item["manufacturer_part_number"] or item["internal_part_number"] or "").strip()
            and str(item["requested_description"] or "").strip()
        ):
            raise HTTPException(status_code=409, detail="A Quote Candidate needs a part number or PPS internal reference and description.")
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
