from __future__ import annotations

from contextlib import closing
from datetime import date
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from legacy_app import get_connection, next_job_number, templates
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.jobs.engine import JobEngine
from plg_core.jobs.service import get_job_operational_snapshot
from plg_core.jobs.workflow import derive_machine_work_status, summarize_job_work
from plg_core.machines.identifiers import find_machine_by_identifier
from plg_core.timeline import log_job_event
from plg_core.pricing import pricing_assessment
from plg_core.basket.service import (
    add_item,
    advance_all_parts_workflow,
    advance_part_workflow,
    clear_basket,
    commit_basket,
    delete_item,
    ensure_basket_mutable,
    get_basket,
    get_existing_basket,
    get_or_create_basket,
    import_cart,
    update_item,
)
from plg_core.sources.service import SOURCE_TYPES, list_sources_for_context, validate_source_url
from plg_core.research.service import derive_result_visibility
from plg_core.research.service import create_requested_need, update_requested_need
from plg_core.research.service import create_manual_research_result, set_preferred_sourcing_option
from plg_core.assets.service import add_job_asset, edit_job_asset


router = APIRouter(tags=["basket"])


@router.post("/jobs/{job_id}/center/edit")
async def center_edit_job(request: Request, job_id: int):
    """Thin Job Center adapter over the established job edit guard/audit path."""
    from legacy_app import update_job
    from plg_core.web_security import require_valid_csrf
    form = await request.form()
    require_valid_csrf(request, form.get("csrf_token", ""))
    customer_id = int(form["customer_id"]) if form.get("customer_id") else None
    with closing(get_connection()) as connection:
        current = connection.execute("SELECT machine_id, customer_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    machine_id = int(form["machine_id"]) if form.get("machine_id") else (current["machine_id"] if current else None)
    customer_id = customer_id if customer_id is not None else (current["customer_id"] if current else None)
    update_job(job_id, customer_id=customer_id, machine_id=machine_id,
               notes=str(form.get("notes", "")))
    return RedirectResponse(f"/jobs/{job_id}/center", status_code=303)


@router.post("/jobs/{job_id}/center/customer")
async def center_edit_customer(request: Request, job_id: int):
    from plg_core.web_security import require_valid_csrf
    form = await request.form()
    require_valid_csrf(request, form.get("csrf_token", ""))
    with closing(get_connection()) as connection:
        row = connection.execute("SELECT customer_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or row["customer_id"] is None:
        raise HTTPException(404, "Customer is not linked to this job.")
    from legacy_app import update_customer
    update_customer(int(row["customer_id"]), name=form.get("name", ""), company=form.get("company", ""),
                    phone=form.get("phone", ""), email=form.get("email", ""), address=form.get("address", ""))
    return RedirectResponse(f"/jobs/{job_id}/center", status_code=303)


@router.post("/jobs/{job_id}/center/assets")
async def center_add_asset(request: Request, job_id: int):
    from plg_core.web_security import require_valid_csrf
    form = await request.form()
    require_valid_csrf(request, form.get("csrf_token", ""))
    add_job_asset(job_id, manufacturer=form.get("manufacturer", ""), model=form.get("model", ""),
                  year=form.get("year", ""), vin_pin_serial=form.get("vin_pin_serial", ""),
                  name=form.get("name", ""), notes=form.get("notes", ""))
    return RedirectResponse(f"/jobs/{job_id}/center", status_code=303)


@router.post("/jobs/{job_id}/center/assets/{asset_id}")
async def center_edit_asset(request: Request, job_id: int, asset_id: int):
    from plg_core.web_security import require_valid_csrf
    form = await request.form()
    require_valid_csrf(request, form.get("csrf_token", ""))
    edit_job_asset(job_id, asset_id, manufacturer=form.get("manufacturer", ""), model=form.get("model", ""),
                   year=form.get("year", ""), vin_pin_serial=form.get("vin_pin_serial", ""),
                   name=form.get("name", ""), notes=form.get("notes", ""))
    return RedirectResponse(f"/jobs/{job_id}/center", status_code=303)


@router.post("/jobs/{job_id}/center/needs")
async def center_add_need(request: Request, job_id: int):
    from plg_core.web_security import require_valid_csrf
    form = await request.form()
    require_valid_csrf(request, form.get("csrf_token", ""))
    raw_qty = form.get("quantity")
    try: quantity = int(raw_qty) if raw_qty not in (None, "") else None
    except (TypeError, ValueError) as exc: raise HTTPException(400, "Quantity must be a whole number of 1 or more.") from exc
    create_requested_need(job_id, job_asset_id=int(form["job_asset_id"]) if form.get("job_asset_id") else None,
                          wording=form.get("wording", ""), notes=form.get("notes", ""),
                          quantity=quantity,
                          expected_revision_id=int(form["expected_revision_id"]) if form.get("expected_revision_id") else None,
                          expected_version=int(form["expected_version"]) if form.get("expected_version") else None)
    return RedirectResponse(f"/jobs/{job_id}/center", status_code=303)


@router.post("/jobs/{job_id}/center/needs/{need_id}")
async def center_edit_need(request: Request, job_id: int, need_id: int):
    from plg_core.web_security import require_valid_csrf
    form = await request.form()
    require_valid_csrf(request, form.get("csrf_token", ""))
    raw_qty = form.get("quantity")
    try: quantity = int(raw_qty) if raw_qty not in (None, "") else None
    except (TypeError, ValueError) as exc: raise HTTPException(400, "Quantity must be a whole number of 1 or more.") from exc
    kwargs = dict(wording=form.get("wording", ""), state=form.get("state", "OPEN"),
                  expected_revision_id=int(form["expected_revision_id"]) if form.get("expected_revision_id") else None,
                  expected_version=int(form["expected_version"]) if form.get("expected_version") else None)
    if raw_qty not in (None, ""):
        kwargs["quantity"] = quantity
    try:
        update_requested_need(job_id, need_id, **kwargs)
    except HTTPException as exc:
        detail = str(exc.detail)
        if "protects" in detail or "history" in detail:
            detail = "This item is part of committed history and cannot be changed directly."
        elif "revision" in detail.lower() or "version" in detail.lower() or "changed" in detail.lower():
            detail = "This job changed after you opened it. Refresh and review the latest values before saving."
        return RedirectResponse(f"/jobs/{job_id}/center?message={quote_plus(detail)}", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}/center", status_code=303)


@router.post("/jobs/{job_id}/center/needs/{need_id}/sourcing")
async def center_add_sourcing_option(request: Request, job_id: int, need_id: int):
    from plg_core.web_security import require_valid_csrf
    form = await request.form()
    require_valid_csrf(request, form.get("csrf_token", ""))
    try:
        cost = float(form["supplier_unit_cost"]) if form.get("supplier_unit_cost") not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Supplier unit cost must be a valid number.") from exc
    try:
        create_manual_research_result(
        job_id, job_asset_id=int(form["job_asset_id"]) if form.get("job_asset_id") else None,
        requested_need_id=need_id, description=form.get("description") or form.get("supplier_name") or "Research option",
        supplier_name=form.get("supplier_name", ""), supplier_unit_cost=cost,
        source_url=form.get("source_url", ""), availability=form.get("availability", ""),
        verification_status=form.get("verification_status", "NEEDS_REVIEW"),
        research_evidence=form.get("research_evidence", ""), research_notes=form.get("research_notes", ""),
        expected_revision_id=int(form["expected_revision_id"]) if form.get("expected_revision_id") else None,
        expected_version=int(form["expected_version"]) if form.get("expected_version") else None,
        )
    except HTTPException as exc:
        detail = str(exc.detail)
        if "revision" in detail.lower() or "version" in detail.lower() or "changed" in detail.lower():
            detail = "This job changed after you opened it. Refresh and review the latest values before saving."
        return RedirectResponse(f"/jobs/{job_id}/center?message={quote_plus(detail)}", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}/center", status_code=303)


@router.post("/jobs/{job_id}/center/needs/{need_id}/sourcing/{item_id}/select")
async def center_select_sourcing_option(request: Request, job_id: int, need_id: int, item_id: int):
    from plg_core.web_security import require_valid_csrf
    form = await request.form()
    require_valid_csrf(request, form.get("csrf_token", ""))
    try:
        set_preferred_sourcing_option(
            job_id, need_id, item_id,
            expected_revision_id=int(form["expected_revision_id"]) if form.get("expected_revision_id") else None,
            expected_version=int(form["expected_version"]) if form.get("expected_version") else None,
        )
    except HTTPException as exc:
        detail = str(exc.detail)
        if "revision" in detail.lower() or "version" in detail.lower() or "changed" in detail.lower():
            detail = "This job changed after you opened it. Refresh and review the latest values before saving."
        return RedirectResponse(f"/jobs/{job_id}/center?message={quote_plus(detail)}", status_code=303)
    return RedirectResponse(f"/jobs/{job_id}/center", status_code=303)


def normalize_source_url(value):
    url = str(value or "").strip()
    if url.startswith("[") and "](" in url and url.endswith(")"):
        url = url.split("](", 1)[1][:-1].strip()
    return url


def _display_specifications(notes: str) -> list[dict[str, str]]:
    """Present simple Label: Value notes without rewriting stored text."""
    specifications = []
    for raw_line in str(notes or "").splitlines():
        label, separator, value = raw_line.partition(":")
        if separator and label.strip() and value.strip():
            specifications.append({"label": label.strip(), "value": value.strip()})
    return specifications


@router.get("/api/baskets/{job_id}")
def read_basket(job_id: int):
    return get_basket(job_id)


@router.post("/api/baskets/{job_id}/items")
def create_basket_item(job_id: int, payload: BasketItemCreate):
    return add_item(job_id, payload)


@router.patch("/api/baskets/items/{item_id}")
def edit_basket_item(item_id: int, payload: BasketItemUpdate):
    return update_item(item_id, payload)


@router.delete("/api/baskets/items/{item_id}")
def remove_basket_item(item_id: int):
    return delete_item(item_id)


@router.delete("/api/baskets/{job_id}")
def remove_all_basket_items(job_id: int):
    return clear_basket(job_id)


@router.post("/api/basket/import-source-cart")
async def import_source_cart(request: Request):
    payload = await request.json()
    try:
        job_id = int(payload.get("job_id"))
        job_asset_id = int(payload.get("job_asset_id"))
        verification_session_id = int(payload.get("verification_session_id"))
        requested_need_id = (
            int(payload["requested_need_id"])
            if payload.get("requested_need_id") not in (None, "") else None
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Complete PPS capture context is required.") from error
    return import_cart(
        job_id, payload,
        expected_revision_id=payload.get("expected_revision_id"),
        expected_version=payload.get("expected_version"),
        job_asset_id=job_asset_id,
        requested_need_id=requested_need_id,
        verification_session_id=verification_session_id,
        require_capture_context=True,
    )


@router.post("/api/basket/import-sis-cart")
async def import_sis_cart(request: Request):
    payload = await request.json()
    normalized = {
        "source_key": "cat_sis",
        "source_name": "CAT SIS",
        "source_url": "https://sis2.cat.com/#/cart",
        "trust_level": "OEM_VERIFIED",
        "currency": "USD",
        "charges": [],
        "items": [
            {
                "description": item.get("oem_description") or "CAT SIS Part",
                "manufacturer_part_number": item.get("oem_part_number") or "",
                "supplier_part_number": item.get("oem_part_number") or "",
                "brand": "CAT",
                "quantity": item.get("quantity", 1),
                "supplier_cost": item.get("oem_price"),
                "availability": item.get("availability", ""),
                "source_url": normalize_source_url(item.get("source_url", "")),
            }
            for item in (payload.get("items") or [])
        ],
    }
    return import_cart(int(payload.get("job_id")), normalized)


@router.get("/jobs/{job_id}/basket", response_class=HTMLResponse)
def basket_page(
    request: Request,
    job_id: int,
    asset_id: int | None = None,
    need_id: int | None = None,
    view: str = "",
):
    if view not in {"advanced", "legacy"}:
        query = ["view=advanced"]
        if asset_id is not None:
            query.append(f"asset_id={int(asset_id)}")
        if need_id is not None:
            query.append(f"need_id={int(need_id)}")
        return RedirectResponse(
            url=f"/jobs/{job_id}/basket?{'&'.join(query)}",
            status_code=303,
        )
    basket = get_existing_basket(job_id)
    if basket is None:
        basket = {
            "id": None,
            "job_id": job_id,
            "status": "OPEN",
            "currency": "USD",
            "work_revision": None,
            "items": [],
            "sources": [],
            "totals": {
                "supplier_parts_total": 0,
                "shipping_total": 0,
                "supplier_total": 0,
                "customer_parts_total": 0,
                "customer_total": 0,
                "estimated_profit": 0,
                "selected_items": 0,
                "all_items": 0,
            },
        }

    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

        connectors = []

        customer_request = connection.execute(
            """
            SELECT *
            FROM customer_requests
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()

        asset_rows = connection.execute(
            "SELECT * FROM job_assets WHERE job_id=? AND state='ACTIVE' "
            "ORDER BY is_primary DESC,id",
            (job_id,),
        ).fetchall()
        from plg_core.research.branding import manufacturer_brand
        job_assets = []
        for row in asset_rows:
            item = dict(row)
            item["brand"] = manufacturer_brand(connection, item["manufacturer"])
            item["needs"] = [dict(need) for need in connection.execute(
                "SELECT * FROM requested_needs WHERE job_id=? AND job_asset_id=? "
                "AND state!='ARCHIVED' ORDER BY id", (job_id, item["id"]),
            ).fetchall()]
            for need in item["needs"]:
                need_result_count = int(connection.execute(
                    "SELECT COUNT(DISTINCT bi.id) FROM basket_items bi "
                    "LEFT JOIN basket_item_need_links link ON link.basket_item_id=bi.id "
                    "WHERE bi.basket_id=? AND bi.job_asset_id=? "
                    "AND (bi.primary_requested_need_id=? OR link.requested_need_id=?) "
                    "AND bi.research_state IN ('RESEARCH_RESULT','QUOTE_CANDIDATE','LEGACY_CANDIDATE')",
                    (basket["id"], item["id"], need["id"], need["id"]),
                ).fetchone()[0])
                need_candidate_count = int(connection.execute(
                    "SELECT COUNT(DISTINCT bi.id) FROM basket_items bi "
                    "LEFT JOIN basket_item_need_links link ON link.basket_item_id=bi.id "
                    "WHERE bi.basket_id=? AND bi.job_asset_id=? "
                    "AND (bi.primary_requested_need_id=? OR link.requested_need_id=?) "
                    "AND bi.selected=1 "
                    "AND bi.research_state IN ('QUOTE_CANDIDATE','LEGACY_CANDIDATE')",
                    (basket["id"], item["id"], need["id"], need["id"]),
                ).fetchone()[0])
                need["work_status_label"] = (
                    "Ready for Quote" if need_candidate_count else
                    "Parts Found" if need_result_count else
                    "Needs Research"
                )
            item["need_count"] = len(item["needs"])
            item["open_need_count"] = sum(
                1 for need in item["needs"] if need["state"] == "OPEN"
            )
            item["parts_found_count"] = int(connection.execute(
                "SELECT COUNT(*) FROM basket_items WHERE basket_id=? AND job_asset_id=? "
                "AND research_state IN ('RESEARCH_RESULT','QUOTE_CANDIDATE','LEGACY_CANDIDATE')",
                (basket["id"], item["id"]),
            ).fetchone()[0])
            item["quote_candidate_count"] = int(connection.execute(
                "SELECT COUNT(*) FROM basket_items WHERE basket_id=? AND job_asset_id=? "
                "AND selected=1 AND research_state IN ('QUOTE_CANDIDATE','LEGACY_CANDIDATE')",
                (basket["id"], item["id"]),
            ).fetchone()[0])
            item["covered_need_count"] = int(connection.execute(
                """
                SELECT COUNT(DISTINCT links.requested_need_id)
                FROM basket_item_need_links links
                JOIN basket_items bi ON bi.id=links.basket_item_id
                JOIN requested_needs rn ON rn.id=links.requested_need_id
                WHERE bi.basket_id=? AND bi.job_asset_id=? AND bi.selected=1
                  AND bi.research_state IN ('QUOTE_CANDIDATE','LEGACY_CANDIDATE')
                  AND rn.state!='ARCHIVED'
                """,
                (basket["id"], item["id"]),
            ).fetchone()[0])
            active_research = connection.execute(
                "SELECT 1 FROM verification_sessions WHERE job_id=? AND job_asset_id=? "
                "AND status='ACTIVE' LIMIT 1",
                (job_id, item["id"]),
            ).fetchone() is not None
            item["work_status"], item["work_status_label"] = derive_machine_work_status(
                need_count=item["need_count"],
                parts_found_count=item["parts_found_count"],
                quote_candidate_count=item["quote_candidate_count"],
                covered_need_count=item["covered_need_count"],
                active_research=active_research,
            )
            job_assets.append(item)
        selected_asset = next(
            (item for item in job_assets if int(item["id"]) == int(asset_id or 0)),
            job_assets[0] if job_assets else None,
        )
        selected_asset_id = int(selected_asset["id"]) if selected_asset else None
        selected_asset_index = next(
            (index for index, item in enumerate(job_assets) if item["id"] == selected_asset_id),
            0,
        )
        previous_asset = job_assets[selected_asset_index - 1] if selected_asset_index > 0 else None
        next_asset = (
            job_assets[selected_asset_index + 1]
            if selected_asset_index + 1 < len(job_assets) else None
        )
        requested_needs = [dict(row) for row in connection.execute(
            "SELECT * FROM requested_needs WHERE job_id=? AND "
            "((? IS NULL AND job_asset_id IS NULL) OR job_asset_id=?) "
            "AND state!='ARCHIVED' ORDER BY CASE state WHEN 'OPEN' THEN 0 ELSE 1 END,id",
            (job_id, selected_asset_id, selected_asset_id),
        ).fetchall()]
        for need in requested_needs:
            result_count = int(connection.execute(
                "SELECT COUNT(DISTINCT bi.id) FROM basket_items bi "
                "LEFT JOIN basket_item_need_links link ON link.basket_item_id=bi.id "
                "WHERE bi.basket_id=? AND ((? IS NULL AND bi.job_asset_id IS NULL) OR bi.job_asset_id=?) "
                "AND (bi.primary_requested_need_id=? OR link.requested_need_id=?) "
                "AND bi.research_state IN ('RESEARCH_RESULT','QUOTE_CANDIDATE','LEGACY_CANDIDATE')",
                (basket["id"], selected_asset_id, selected_asset_id, need["id"], need["id"]),
            ).fetchone()[0])
            candidate_count = int(connection.execute(
                "SELECT COUNT(DISTINCT bi.id) FROM basket_items bi "
                "LEFT JOIN basket_item_need_links link ON link.basket_item_id=bi.id "
                "WHERE bi.basket_id=? AND ((? IS NULL AND bi.job_asset_id IS NULL) OR bi.job_asset_id=?) "
                "AND (bi.primary_requested_need_id=? OR link.requested_need_id=?) "
                "AND bi.selected=1 AND bi.research_state IN ('QUOTE_CANDIDATE','LEGACY_CANDIDATE')",
                (basket["id"], selected_asset_id, selected_asset_id, need["id"], need["id"]),
            ).fetchone()[0])
            need["research_candidate_count"] = result_count
            need["quote_candidate_count"] = candidate_count
            need["specifications"] = _display_specifications(need.get("notes") or "")
            need["work_status_label"] = (
                "Ready for Quote" if candidate_count else
                "Options Found" if result_count else
                "Needs Research"
            )
        open_requested_needs = [need for need in requested_needs if need["state"] == "OPEN"]
        selected_need_id = None
        if need_id is not None:
            selected_need = next(
                (
                    need for need in open_requested_needs
                    if int(need["id"]) == int(need_id)
                ),
                None,
            )
            if selected_need is None:
                raise HTTPException(
                    status_code=404,
                    detail="Requested Need not found for this Job asset.",
                )
            selected_need_id = int(selected_need["id"])
        connectors = list_sources_for_context(
            connection,
            manufacturer=selected_asset.get("manufacturer") or "" if selected_asset else "",
            asset_category=selected_asset.get("asset_type") or "other" if selected_asset else "other",
            market=selected_asset.get("market_region") or "UNKNOWN" if selected_asset else "UNKNOWN",
        )
        active_research_context = connection.execute(
            """SELECT vs.*,COALESCE(NULLIF(vs.source_name_snapshot,''),cp.display_name) AS source_name,
                      COALESCE(NULLIF(vs.source_url_snapshot,''),cp.launch_url) AS launch_url,
                      cp.source_type,rn.wording AS current_need_wording
               FROM verification_sessions vs
               JOIN connector_profiles cp ON cp.id=vs.connector_profile_id
               LEFT JOIN requested_needs rn ON rn.id=vs.requested_need_id
               WHERE vs.job_id=?
                 AND ((? IS NULL AND vs.job_asset_id IS NULL) OR vs.job_asset_id=?)
                 AND vs.status='ACTIVE'
               ORDER BY vs.id DESC LIMIT 1""",
            (job_id, selected_asset_id, selected_asset_id),
        ).fetchone()
        if selected_need_id is None and active_research_context is not None:
            active_need_id = active_research_context["requested_need_id"]
            if active_need_id is not None and any(
                int(need["id"]) == int(active_need_id)
                for need in open_requested_needs
            ):
                selected_need_id = int(active_need_id)
        if selected_need_id is None and len(open_requested_needs) == 1:
            selected_need_id = int(open_requested_needs[0]["id"])

        request_attachment_count = 0
        if customer_request is not None:
            request_attachment_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM customer_request_attachments
                WHERE request_id = ?
                """,
                (customer_request["id"],),
            ).fetchone()[0]

        quote = connection.execute(
            """
            SELECT *
            FROM quotes
            WHERE job_id = ? AND COALESCE(is_current, 1) = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        quote_history = connection.execute(
            """
            SELECT q.*, predecessor.quote_number AS predecessor_number
            FROM quotes q
            LEFT JOIN quotes predecessor ON predecessor.id=q.supersedes_quote_id
            WHERE q.job_id=?
            ORDER BY q.id DESC
            """,
            (job_id,),
        ).fetchall()
        active_revision = connection.execute(
            """
            SELECT wr.*, q.quote_number AS source_quote_number
            FROM work_revisions wr
            LEFT JOIN quotes q ON q.id=wr.based_on_quote_id
            WHERE wr.id=?
            """,
            (
                basket["work_revision"]["id"]
                if basket.get("work_revision") else 0,
            ),
        ).fetchone()
        follow_ups = connection.execute(
            """
            SELECT * FROM job_follow_ups
            WHERE job_id=? AND status IN ('OPEN','RECEIVED')
            ORDER BY
              CASE status WHEN 'RECEIVED' THEN 0 WHEN 'OPEN' THEN 1 ELSE 2 END,
              id DESC
            """,
            (job_id,),
        ).fetchall()

        invoice = connection.execute(
            """
            SELECT *
            FROM invoices
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()


        timeline = connection.execute(
            """
            SELECT *
            FROM job_timeline
            WHERE job_id = ?
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 100
            """,
            (job_id,),
        ).fetchall()
        shipping_rows = connection.execute(
            "SELECT * FROM part_shipping_data WHERE basket_item_id IS NOT NULL "
            "AND is_current=1 ORDER BY id DESC"
        ).fetchall()

    source_lookup = {
        source["id"]: source
        for source in basket["sources"]
    }

    vendor_carts: list[dict] = []
    for source in basket["sources"]:
        vendor_carts.append(
            {
                "source": source,
                "items": [
                    item
                    for item in basket["items"]
                    if item["source_id"] == source["id"]
                    and item.get("job_asset_id") == selected_asset_id
                ],
            }
        )

    unassigned_items = [
        item
        for item in basket["items"]
        if item["source_id"] is None and item.get("job_asset_id") == selected_asset_id
    ]
    if unassigned_items:
        vendor_carts.append(
            {
                "source": {
                    "id": None,
                    "source_key": "manual",
                    "source_name": "Manual Vendor",
                    "source_url": "",
                    "trust_level": "MANUAL",
                    "shipping_total": 0,
                    "currency": "USD",
                },
                "items": unassigned_items,
            }
        )

    all_quote_candidates = [
        item for item in basket["items"]
        if item["selected"] and str(item.get("research_state") or "LEGACY_CANDIDATE")
        in {"QUOTE_CANDIDATE", "LEGACY_CANDIDATE"}
    ]
    basket_items = [item for item in all_quote_candidates if item.get("job_asset_id") == selected_asset_id]
    research_results = [
        item for item in basket["items"]
        if item.get("job_asset_id") == selected_asset_id
        and str(item.get("research_state") or "LEGACY_CANDIDATE") == "RESEARCH_RESULT"
    ]
    need_wording_by_id = {int(need["id"]): need["wording"] for need in requested_needs}
    shipping_by_item = {int(row["basket_item_id"]): dict(row) for row in shipping_rows}
    for item in research_results:
        item["requested_need_wording"] = need_wording_by_id.get(
            int(item["primary_requested_need_id"]) if item.get("primary_requested_need_id") else -1
        )
        try:
            item["safe_evidence_url"] = validate_source_url(item.get("source_url") or "")
        except HTTPException:
            item["safe_evidence_url"] = ""
        item.update(derive_result_visibility(
            item, source_lookup.get(item.get("source_id")) or {}, shipping_by_item.get(int(item["id"])) or {}
        ))
    unassigned_parts_found_count = sum(
        1 for item in basket["items"]
        if item.get("job_asset_id") is None
        and str(item.get("research_state") or "LEGACY_CANDIDATE")
        in {"RESEARCH_RESULT", "QUOTE_CANDIDATE", "LEGACY_CANDIDATE"}
    )
    job_summary = summarize_job_work(
        job_assets, len(all_quote_candidates), unassigned_parts_found_count
    )

    for item in all_quote_candidates:
        item["pricing"] = pricing_assessment(
            item.get("supplier_unit_cost") or 0,
            item.get("markup_percent"),
            item.get("customer_unit_price_override"),
        )

    selected_status_items = [
        item
        for item in basket_items
        if item.get("selected")
    ]

    research_items = sum(
        1
        for item in selected_status_items
        if str(item.get("part_status") or "RESEARCH").upper()
        == "RESEARCH"
    )

    quoted_items = sum(
        1
        for item in selected_status_items
        if str(item.get("part_status") or "").upper()
        == "QUOTED"
    )

    ordered_items = sum(
        1
        for item in selected_status_items
        if str(item.get("part_status") or "").upper()
        == "ORDERED"
    )

    received_items = sum(
        1
        for item in selected_status_items
        if str(item.get("part_status") or "").upper()
        == "RECEIVED"
    )

    outstanding_parts = [
        item.get("requested_description") or "Part"
        for item in selected_status_items
        if str(item.get("part_status") or "RESEARCH").upper()
        != "RECEIVED"
    ]

    intelligence = JobEngine.evaluate(
        job,
        selected_items=basket["totals"]["selected_items"],
        research_items=research_items,
        quoted_items=quoted_items,
        ordered_items=ordered_items,
        received_items=received_items,
        open_requested_needs=sum(
            1 for need in requested_needs
            if str(need.get("state") or "").upper() == "OPEN"
        ),
        outstanding_parts=outstanding_parts,
        basket_status=basket["status"],
        customer_request=customer_request,
        quote=quote,
        invoice=invoice,
    ).to_dict()
    operational_snapshot = get_job_operational_snapshot(job_id)
    from plg_core.lifecycle import get_job_delete_eligibility
    delete_eligibility = get_job_delete_eligibility(job_id)

    from plg_core.web_security import CSRF_COOKIE_NAME, csrf_token_for_request
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="job_command_center_legacy.html" if view == "legacy" else "job_command_center_advanced.html",
        context={
            "timeline": timeline,
            "job": job,
            "basket": basket,
            "basket_items": basket_items,
            "all_quote_candidates": all_quote_candidates,
            "research_results": research_results,
            "shipping_by_item": shipping_by_item,
            "vendor_carts": vendor_carts,
            "source_lookup": source_lookup,
            "connectors": connectors,
            "source_types": SOURCE_TYPES,
            "active_research_context": active_research_context,
            "customer_request": customer_request,
            "job_assets": job_assets,
            "selected_asset": selected_asset,
            "selected_asset_id": selected_asset_id,
            "previous_asset": previous_asset,
            "next_asset": next_asset,
            "requested_needs": requested_needs,
            "open_requested_needs": open_requested_needs,
            "selected_need_id": selected_need_id,
            "job_summary": job_summary,
            "request_attachment_count": request_attachment_count,
            "quote": quote,
            "quote_history": quote_history,
            "active_revision": active_revision,
            "follow_ups": follow_ups,
            "invoice": invoice,
            "intelligence": intelligence,
            "operational_snapshot": operational_snapshot,
            "delete_eligibility": delete_eligibility,
            "csrf_token": csrf_token,
            "active_page": "jobs",
        },
    )
    response.set_cookie(
        CSRF_COOKIE_NAME, csrf_token, httponly=True, samesite="strict",
        secure=request.url.scheme == "https",
    )
    return response


def _fulfillment_audit(request: Request) -> dict:
    from plg_core.web_security import request_actor, request_id
    return {
        "actor": request_actor(request),
        "request_id": request_id(request),
        "source_path": str(request.url.path),
    }


@router.post("/jobs/{job_id}/fulfillment/order")
def mark_job_fulfillment_ordered(
    request: Request,
    job_id: int,
    csrf_token: Annotated[str, Form()] = "",
):
    from plg_core.jobs.fulfillment import mark_job_ordered
    from plg_core.web_security import require_valid_csrf
    require_valid_csrf(request, csrf_token)
    mark_job_ordered(job_id, **_fulfillment_audit(request))
    return RedirectResponse(url=f"/jobs/{job_id}/basket?view=advanced#fulfillment-checklist", status_code=303)


@router.post("/jobs/{job_id}/fulfillment/items/{item_id}/received")
def mark_job_fulfillment_received(
    request: Request,
    job_id: int,
    item_id: int,
    quantity: Annotated[int, Form()] = 1,
    csrf_token: Annotated[str, Form()] = "",
):
    from plg_core.jobs.fulfillment import mark_fulfillment_item_received
    from plg_core.web_security import require_valid_csrf
    require_valid_csrf(request, csrf_token)
    mark_fulfillment_item_received(job_id, item_id, quantity=quantity, **_fulfillment_audit(request))
    return RedirectResponse(url=f"/jobs/{job_id}/basket?view=advanced#fulfillment-checklist", status_code=303)


@router.post("/jobs/{job_id}/fulfillment/items/{item_id}/delivered")
def mark_job_fulfillment_delivered(
    request: Request,
    job_id: int,
    item_id: int,
    quantity: Annotated[int, Form()] = 1,
    csrf_token: Annotated[str, Form()] = "",
):
    from plg_core.jobs.fulfillment import mark_fulfillment_item_delivered
    from plg_core.web_security import require_valid_csrf
    require_valid_csrf(request, csrf_token)
    mark_fulfillment_item_delivered(job_id, item_id, quantity=quantity, **_fulfillment_audit(request))
    return RedirectResponse(url=f"/jobs/{job_id}/basket?view=advanced#fulfillment-checklist", status_code=303)


@router.post("/jobs/{job_id}/vendor-carts/clone")
def clone_supplier_quote(
    job_id: int,
    vendor_name: Annotated[str, Form()],
    source_type: Annotated[str, Form()] = "AFTERMARKET",
    job_asset_id: Annotated[int | None, Form()] = None,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    vendor_name = vendor_name.strip()
    source_type = source_type.strip().upper()

    if not vendor_name:
        raise HTTPException(
            status_code=400,
            detail="Supplier name is required.",
        )

    if source_type not in {
        "OEM",
        "AFTERMARKET",
        "REMAN",
        "USED",
    }:
        source_type = "AFTERMARKET"

    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        revision = ensure_basket_mutable(
            basket, connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )

        source = connection.execute(
            """
            SELECT *
            FROM basket_sources
            WHERE basket_id = ?
              AND LOWER(TRIM(source_name)) = LOWER(TRIM(?))
            ORDER BY id DESC
            LIMIT 1
            """,
            (basket["id"], vendor_name),
        ).fetchone()

        if source is None:
            cursor = connection.execute(
                """
                INSERT INTO basket_sources (
                    basket_id,
                    source_key,
                    source_name,
                    trust_level,
                    shipping_total,
                    currency
                )
                VALUES (
                    ?,
                    'supplier_quote',
                    ?,
                    'MANUAL',
                    0,
                    'USD'
                )
                """,
                (basket["id"], vendor_name),
            )
            source_id = cursor.lastrowid
        else:
            source_id = source["id"]

            connection.execute(
                """
                UPDATE basket_sources
                SET source_key = 'supplier_quote'
                WHERE id = ?
                """,
                (source_id,),
            )

        # First use the parts already selected for the customer quote.
        base_items = connection.execute(
            """
            SELECT *
            FROM basket_items
            WHERE basket_id = ?
              AND selected = 1
              AND ((? IS NULL AND job_asset_id IS NULL) OR job_asset_id=?)
              AND (
                    source_id IS NULL
                    OR source_id != ?
                  )
            ORDER BY id
            """,
            (basket["id"], job_asset_id, job_asset_id, source_id),
        ).fetchall()

        # If nothing is selected yet, copy all researched options.
        if not base_items:
            base_items = connection.execute(
                """
                SELECT *
                FROM basket_items
                WHERE basket_id = ?
                  AND ((? IS NULL AND job_asset_id IS NULL) OR job_asset_id=?)
                  AND (
                        source_id IS NULL
                        OR source_id != ?
                      )
                ORDER BY id
                """,
                (basket["id"], job_asset_id, job_asset_id, source_id),
            ).fetchall()

        if not base_items:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Research or import at least one part before "
                    "adding a supplier quote."
                ),
            )

        seen: set[str] = set()
        added = 0

        for item in base_items:
            part_number = (
                item["supplier_part_number"]
                or item["manufacturer_part_number"]
                or ""
            ).strip()

            description = (
                item["requested_description"] or ""
            ).strip()

            dedupe_key = (
                part_number.lower()
                if part_number
                else description.lower()
            )

            if not dedupe_key or dedupe_key in seen:
                continue

            seen.add(dedupe_key)

            existing = connection.execute(
                """
                SELECT id
                FROM basket_items
                WHERE basket_id = ?
                  AND source_id = ?
                  AND (
                        (
                          ? != ''
                          AND LOWER(
                                TRIM(
                                  COALESCE(
                                    supplier_part_number,
                                    manufacturer_part_number,
                                    ''
                                  )
                                )
                              ) = LOWER(TRIM(?))
                        )
                        OR
                        (
                          ? = ''
                          AND LOWER(
                                TRIM(requested_description)
                              ) = LOWER(TRIM(?))
                        )
                      )
                LIMIT 1
                """,
                (
                    basket["id"],
                    source_id,
                    part_number,
                    part_number,
                    part_number,
                    description,
                ),
            ).fetchone()

            if existing is not None:
                continue

            connection.execute(
                """
                INSERT INTO basket_items (
                    basket_id,
                    source_id,
                    job_asset_id,
                    primary_requested_need_id,
                    research_state,
                    requested_description,
                    manufacturer_part_number,
                    supplier_part_number,
                    supplier_name,
                    source_type,
                    brand,
                    quantity,
                    supplier_unit_cost,
                    availability,
                    lead_time,
                    selected
                )
                VALUES (
                    ?, ?, ?, ?, 'RESEARCH_RESULT', ?, ?, ?, ?, ?, ?, ?,
                    NULL, '', '', 0
                )
                """,
                (
                    basket["id"],
                    source_id,
                    item["job_asset_id"],
                    item["primary_requested_need_id"],
                    description,
                    (
                        item["manufacturer_part_number"]
                        or part_number
                    ),
                    part_number,
                    vendor_name,
                    source_type,
                    item["brand"] or "",
                    max(1, int(item["quantity"] or 1)),
                ),
            )
            added += 1

        connection.execute(
            """
            UPDATE baskets
            SET status = 'OPEN',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (basket["id"],),
        )

        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced#parts-research",
        status_code=303,
    )


@router.post(
    "/jobs/{job_id}/vendor-carts/items/{item_id}/quote-update"
)
def update_supplier_quote_item(
    job_id: int,
    item_id: int,
    supplier_unit_cost: Annotated[str, Form()] = "",
    availability: Annotated[str, Form()] = "",
    lead_time: Annotated[str, Form()] = "",
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    raw_cost = supplier_unit_cost.strip()

    try:
        cost = float(raw_cost) if raw_cost else None
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail="Supplier cost must be a valid number.",
        ) from error

    if cost is not None and cost < 0:
        raise HTTPException(
            status_code=400,
            detail="Supplier cost cannot be negative.",
        )

    with closing(get_connection()) as connection:
        item = connection.execute(
            """
            SELECT
                basket_items.id,
                baskets.status AS status
            FROM basket_items
            JOIN baskets
              ON baskets.id = basket_items.basket_id
            WHERE basket_items.id = ?
              AND baskets.job_id = ?
            """,
            (item_id, job_id),
        ).fetchone()

        if item is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier quote item not found.",
            )

        revision = ensure_basket_mutable(
            item, connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )

        connection.execute(
            """
            UPDATE basket_items
            SET supplier_unit_cost = ?,
                availability = ?,
                lead_time = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                cost,
                availability.strip(),
                lead_time.strip(),
                item_id,
            ),
        )

        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced#parts-research",
        status_code=303,
    )


@router.post(
    "/jobs/{job_id}/vendor-carts/{source_id}/quote-save-all"
)
def save_supplier_quote_bulk(
    job_id: int,
    source_id: int,
    item_id: Annotated[list[int], Form()],
    supplier_unit_cost: Annotated[list[str], Form()],
    availability: Annotated[list[str], Form()],
    lead_time: Annotated[list[str], Form()],
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    field_lengths = {
        len(item_id),
        len(supplier_unit_cost),
        len(availability),
        len(lead_time),
    }

    if len(field_lengths) != 1:
        raise HTTPException(
            status_code=400,
            detail="Supplier quote rows are incomplete.",
        )

    saved_items: list[int] = []

    with closing(get_connection()) as connection:
        source = connection.execute(
            """
            SELECT
                basket_sources.id,
                baskets.status AS status
            FROM basket_sources
            JOIN baskets
              ON baskets.id = basket_sources.basket_id
            WHERE basket_sources.id = ?
              AND baskets.job_id = ?
              AND basket_sources.source_key = 'supplier_quote'
            """,
            (source_id, job_id),
        ).fetchone()

        if source is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier quote was not found.",
            )

        revision = ensure_basket_mutable(
            source, connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )

        for row_id, raw_cost, row_availability, row_lead_time in zip(
            item_id,
            supplier_unit_cost,
            availability,
            lead_time,
        ):
            raw_cost = (raw_cost or "").strip()

            if raw_cost:
                try:
                    cost = float(raw_cost)
                except ValueError as error:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Item {row_id} has an invalid supplier cost."
                        ),
                    ) from error

                if cost < 0:
                    raise HTTPException(
                        status_code=400,
                        detail="Supplier cost cannot be negative.",
                    )
            else:
                cost = None

            result = connection.execute(
                """
                UPDATE basket_items
                SET supplier_unit_cost = ?,
                    availability = ?,
                    lead_time = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND source_id = ?
                  AND basket_id = (
                      SELECT id
                      FROM baskets
                      WHERE job_id = ?
                      ORDER BY id DESC
                      LIMIT 1
                  )
                """,
                (
                    cost,
                    (row_availability or "").strip(),
                    (row_lead_time or "").strip(),
                    row_id,
                    source_id,
                    job_id,
                ),
            )

            if result.rowcount:
                saved_items.append(row_id)

        connection.execute(
            """
            UPDATE baskets
            SET updated_at = CURRENT_TIMESTAMP
            WHERE job_id = ?
            """,
            (job_id,),
        )

        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()

    return JSONResponse(
        {
            "ok": True,
            "saved_items": saved_items,
            "message": "Supplier quote saved.",
        }
    )


@router.post("/jobs/{job_id}/vendor-carts/manual")
def add_manual_vendor_line(
    job_id: int,
    vendor_name: Annotated[str, Form()],
    requested_description: Annotated[str, Form()],
    quantity: Annotated[int, Form()] = 1,
    manufacturer_part_number: Annotated[str, Form()] = "",
    supplier_part_number: Annotated[str, Form()] = "",
    supplier_unit_cost: Annotated[float | None, Form()] = None,
    source_type: Annotated[str, Form()] = "AFTERMARKET",
    brand: Annotated[str, Form()] = "",
    availability: Annotated[str, Form()] = "",
    lead_time: Annotated[str, Form()] = "",
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    vendor_name = vendor_name.strip() or "Manual Vendor"

    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        revision = ensure_basket_mutable(
            basket, connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )

        source = connection.execute(
            """
            SELECT *
            FROM basket_sources
            WHERE basket_id = ?
              AND LOWER(TRIM(source_name)) = LOWER(TRIM(?))
            ORDER BY id DESC
            LIMIT 1
            """,
            (basket["id"], vendor_name),
        ).fetchone()

        if source is None:
            cursor = connection.execute(
                """
                INSERT INTO basket_sources (
                    basket_id,
                    source_key,
                    source_name,
                    trust_level,
                    shipping_total,
                    currency
                )
                VALUES (?, 'manual_vendor', ?, 'MANUAL', 0, 'USD')
                """,
                (basket["id"], vendor_name),
            )
            source_id = cursor.lastrowid
        else:
            source_id = source["id"]

        connection.execute(
            """
            INSERT INTO basket_items (
                basket_id,
                source_id,
                requested_description,
                supplier_part_number,
                supplier_name,
                source_type,
                brand,
                quantity,
                supplier_unit_cost,
                availability,
                lead_time,
                selected
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                basket["id"],
                source_id,
                requested_description.strip(),
                supplier_part_number.strip(),
                vendor_name,
                source_type.strip().upper() or "AFTERMARKET",
                brand.strip(),
                max(1, quantity),
                supplier_unit_cost,
                availability.strip(),
                lead_time.strip(),
            ),
        )

        connection.execute(
            """
            UPDATE baskets
            SET status = 'OPEN',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (basket["id"],),
        )
        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items")
def add_manual_item(
    job_id: int,
    requested_description: Annotated[str, Form()],
    manufacturer_part_number: Annotated[str, Form()] = "",
    job_asset_id: Annotated[int | None, Form()] = None,
    requested_need_id: Annotated[int | None, Form()] = None,
    quantity: Annotated[int, Form()] = 1,
    supplier_name: Annotated[str, Form()] = "",
    supplier_part_number: Annotated[str, Form()] = "",
    supplier_unit_cost: Annotated[float | None, Form()] = None,
    source_type: Annotated[str, Form()] = "AFTERMARKET",
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    add_item(
        job_id,
        BasketItemCreate(
            requested_description=requested_description,
            job_asset_id=job_asset_id,
            primary_requested_need_id=requested_need_id,
            research_state="RESEARCH_RESULT",
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
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced" + (f"&asset_id={job_asset_id}" if job_asset_id else ""),
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items/{item_id}/toggle")
def toggle_item(
    job_id: int,
    item_id: int,
    selected: Annotated[int, Form()],
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    update_item(
        item_id,
        BasketItemUpdate(selected=bool(selected)),
        expected_job_id=job_id,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced",
        status_code=303,
    )



@router.post("/jobs/{job_id}/basket/items/{item_id}/quantity")
def update_item_quantity(
    job_id: int,
    item_id: int,
    quantity: Annotated[int, Form()],
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    update_item(
        item_id,
        BasketItemUpdate(quantity=max(1, quantity)),
        expected_job_id=job_id,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced",
        status_code=303,
    )

@router.post("/jobs/{job_id}/basket/workflow")
def advance_all_parts_workflow_form(
    job_id: int,
    action: Annotated[str, Form()],
):
    advance_all_parts_workflow(
        job_id=job_id,
        action=action,
    )

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced#parts-ready",
        status_code=303,
    )


@router.post(
    "/jobs/{job_id}/basket/items/{item_id}/workflow"
)
def advance_part_workflow_form(
    job_id: int,
    item_id: int,
    action: Annotated[str, Form()],
):
    advance_part_workflow(
        job_id=job_id,
        item_id=item_id,
        action=action,
    )

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced#parts-ready",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items/{item_id}/delete")
def delete_item_form(
    job_id: int, item_id: int,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    delete_item(
        item_id, expected_job_id=job_id,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
        require_unpromoted_research_result=True,
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/clear")
def clear_form(
    job_id: int,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    clear_basket(
        job_id,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced",
        status_code=303,
    )



@router.post("/jobs/{job_id}/revenue-adjustments")
def update_revenue_adjustments(
    job_id: int,
    service_charge: Annotated[str, Form()] = "0",
    service_charge_description: Annotated[str, Form()] = "",
    sourcing_fee: Annotated[str, Form()] = "0",
    sourcing_fee_description: Annotated[str, Form()] = "",
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    """Save optional job-level Service Charge and Sourcing Fee values."""

    def parse_amount(raw_value: str, label: str) -> float:
        value = (raw_value or "").strip()

        if value == "":
            return 0.0

        try:
            amount = round(float(value), 2)
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail=f"{label} must be a valid number.",
            ) from error

        if amount < 0:
            raise HTTPException(
                status_code=400,
                detail=f"{label} cannot be negative.",
            )

        return amount

    parsed_service_charge = parse_amount(
        service_charge,
        "Service Charge",
    )
    parsed_sourcing_fee = parse_amount(
        sourcing_fee,
        "Sourcing Fee",
    )

    if 0 < parsed_service_charge < 50:
        raise HTTPException(
            status_code=400,
            detail=(
                "Service Charge must be $0 or at least $50.00."
            ),
        )

    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        revision = ensure_basket_mutable(
            basket, connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        job = connection.execute(
            """
            SELECT
                id,
                service_charge,
                sourcing_fee
            FROM jobs
            WHERE id = ?
            """,
            (job_id,),
        ).fetchone()

        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Job not found.",
            )

        connection.execute(
            """
            UPDATE jobs
            SET service_charge = ?,
                service_charge_description = ?,
                sourcing_fee = ?,
                sourcing_fee_description = ?
            WHERE id = ?
            """,
            (
                parsed_service_charge,
                service_charge_description.strip(),
                parsed_sourcing_fee,
                sourcing_fee_description.strip(),
                job_id,
            ),
        )
        connection.execute(
            """
            UPDATE work_revisions
            SET service_charge=?, service_charge_description=?,
                sourcing_fee=?, sourcing_fee_description=?
            WHERE id=?
            """,
            (
                parsed_service_charge,
                service_charge_description.strip(),
                parsed_sourcing_fee,
                sourcing_fee_description.strip(),
                revision["id"],
            ),
        )

        old_service_charge = round(float(job["service_charge"] or 0), 2)
        old_sourcing_fee = round(float(job["sourcing_fee"] or 0), 2)

        if old_service_charge != parsed_service_charge:
            if parsed_service_charge > 0:
                service_message = (
                    f"Service Charge updated to "
                    f"${parsed_service_charge:,.2f}"
                )
            else:
                service_message = "Service Charge removed"

            log_job_event(
                connection,
                job_id=job_id,
                event_type="SERVICE_CHARGE_UPDATED",
                icon="💼",
                message=service_message,
            )

        if old_sourcing_fee != parsed_sourcing_fee:
            if parsed_sourcing_fee > 0:
                sourcing_message = (
                    f"Sourcing Fee updated to "
                    f"${parsed_sourcing_fee:,.2f}"
                )
            else:
                sourcing_message = "Sourcing Fee removed"

            log_job_event(
                connection,
                job_id=job_id,
                event_type="SOURCING_FEE_UPDATED",
                icon="🔎",
                message=sourcing_message,
            )

        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced&saved=1#revenue-adjustments",
        status_code=303,
    )

@router.get("/jobs/{job_id}/center", response_class=HTMLResponse)
def job_center_v2_page(request: Request, job_id: int, tab: str = "job", message: str = ""):
    from plg_core.jobs.workspace import render_job_center_v2
    return render_job_center_v2(request, job_id, tab=tab, message=message)

@router.get("/jobs/{job_id}/shipping", response_class=HTMLResponse)
def job_shipping_page(request: Request, job_id: int):
    from plg_core.jobs.workspace import build_workspace
    with closing(get_connection()) as connection:
        model = build_workspace(connection, job_id)
        sources = [dict(row) for row in connection.execute("SELECT id,source_name,shipping_total,currency FROM basket_sources WHERE basket_id=? ORDER BY id", (model["basket"]["id"] or 0,))]
    from plg_core.web_security import CSRF_COOKIE_NAME, csrf_token_for_request
    token = csrf_token_for_request(request)
    response = templates.TemplateResponse(request=request, name="job_shipping.html", context={"job": model["job"], "basket": model["basket"], "sources": sources, "csrf_token": token, "revision": model["revision"]})
    response.set_cookie(CSRF_COOKIE_NAME, token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return response

@router.post("/jobs/{job_id}/shipping")
async def save_job_shipping(request: Request, job_id: int):
    form = await request.form()
    from plg_core.web_security import require_valid_csrf
    require_valid_csrf(request, str(form.get("csrf_token", "") or ""))
    try: amount = round(float(str(form.get("customer_shipping", "0") or "0").strip()), 2)
    except ValueError: raise HTTPException(400, "Shipping must be a valid amount.")
    if amount < 0: raise HTTPException(400, "Shipping cannot be negative.")
    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        ensure_basket_mutable(basket, connection, expected_revision_id=form.get("expected_revision_id"), expected_version=form.get("expected_version"))
        connection.execute("UPDATE basket_sources SET shipping_total=? WHERE basket_id=?", (amount, basket["id"]))
        connection.commit()
    return RedirectResponse(f"/jobs/{job_id}/basket?view=advanced#quote", status_code=303)


@router.post("/jobs/{job_id}/basket/checkout")
def checkout_basket(
    job_id: int,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    commit_basket(
        job_id, expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/commit")
def commit_form(
    job_id: int,
    expected_revision_id: Annotated[int | None, Form()] = None,
    expected_version: Annotated[int | None, Form()] = None,
):
    commit_basket(
        job_id, expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items/{item_id}/update")
def update_basket_item_form(
    job_id: int,
    item_id: int,
    quantity: int = Form(...),
    supplier_unit_cost: float = Form(...),
    markup_percent: float = Form(...),
    customer_unit_price_override: str = Form(""),
    manufacturer_part_number: str = Form(""),
    alternate_part_number: str = Form(""),
    supplier_part_number: str = Form(""),
    part_status: str = Form(""),
    verification_status: str | None = Form(None),
    verification_note: str | None = Form(None),
    confidence: float | None = Form(None),
    requested_description: str | None = Form(None),
    supplier_name: str | None = Form(None),
    availability: str | None = Form(None),
    expected_revision_id: int | None = Form(None),
    expected_version: int | None = Form(None),
):
    # Direct service/test calls may retain FastAPI's Form sentinel defaults;
    # treat those as omitted optional fields just as the HTTP parser does.
    requested_description = requested_description if isinstance(requested_description, str) else None
    supplier_name = supplier_name if isinstance(supplier_name, str) else None
    availability = availability if isinstance(availability, str) else None
    verification_status = verification_status if isinstance(verification_status, str) else None
    verification_note = verification_note if isinstance(verification_note, str) else None
    valid_statuses = {
        "RESEARCH",
        "QUOTED",
        "ORDERED",
        "RECEIVED",
    }
    requested_status = part_status.strip().upper()

    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise HTTPException(
            status_code=400,
            detail="Confidence must be between 0.0 and 1.0.",
        )

    override_text = customer_unit_price_override.strip()
    if override_text:
        try:
            parsed_customer_unit_price_override = float(override_text)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Customer Unit must be a valid dollar amount.",
            )

        if parsed_customer_unit_price_override < 0:
            raise HTTPException(
                status_code=400,
                detail="Customer Unit cannot be negative.",
            )
    else:
        parsed_customer_unit_price_override = None

    with closing(get_connection()) as connection:
        old_item = connection.execute(
            """
            SELECT requested_description, part_status,
                   verification_status, verification_note
            FROM basket_items
            WHERE id = ?
            """,
            (item_id,),
        ).fetchone()

    if old_item is None:
        raise HTTPException(
            status_code=404,
            detail="Basket item not found.",
        )

    old_status = (
        old_item["part_status"] or "RESEARCH"
    ).strip().upper()

    old_verification_status = (
        old_item["verification_status"] or "UNVERIFIED"
    ).strip().upper()
    old_verification_note = (
        old_item["verification_note"] or ""
    ).strip()
    requested_verification_status = old_verification_status
    if verification_status is not None:
        requested_verification_status = verification_status.strip().upper() or "UNVERIFIED"
        if requested_verification_status not in {
            "UNVERIFIED", "VERIFIED", "REJECTED", "OVERRIDE",
            "NEEDS_REVIEW", "PROVISIONAL",
        }:
            raise HTTPException(status_code=400, detail="Invalid verification status.")
    requested_verification_note = (
        old_verification_note if verification_note is None else verification_note.strip()
    )
    if requested_verification_status == "OVERRIDE" and not requested_verification_note:
        raise HTTPException(
            status_code=400,
            detail="Manual Override requires a verification note.",
        )

    new_status = (
        requested_status
        if requested_status in valid_statuses
        else old_status
    )

    # Pricing a researched part means it is ready for the
    # customer quote. The user does not need to change status.
    if (
        old_status == "RESEARCH"
        and supplier_unit_cost > 0
    ):
        new_status = "QUOTED"

    update_values = {
        "quantity": quantity,
        "supplier_unit_cost": supplier_unit_cost,
        "markup_percent": markup_percent,
        "customer_unit_price_override": parsed_customer_unit_price_override,
        "manufacturer_part_number": manufacturer_part_number.strip(),
        "alternate_part_number": alternate_part_number.strip(),
        "supplier_part_number": supplier_part_number.strip(),
        "part_status": new_status,
        "confidence": confidence,
    }
    if requested_description is not None:
        update_values["requested_description"] = requested_description.strip()
    if supplier_name is not None:
        update_values["supplier_name"] = supplier_name.strip()
    if availability is not None:
        update_values["availability"] = availability.strip()
    if verification_status is not None:
        update_values["verification_status"] = requested_verification_status
    if verification_note is not None:
        update_values["verification_note"] = requested_verification_note
    update_item(
        item_id,
        BasketItemUpdate(**update_values),
        expected_job_id=job_id,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )

    if (verification_status is not None or verification_note is not None) and (
        old_verification_status != requested_verification_status
        or old_verification_note != requested_verification_note
    ):
        verification_labels = {
            "UNVERIFIED": "Unverified",
            "VERIFIED": "Verified",
            "REJECTED": "Rejected",
            "OVERRIDE": "Manual Override",
            "NEEDS_REVIEW": "Needs Review",
            "PROVISIONAL": "Provisional",
        }

        description = (
            old_item["requested_description"]
            or "Part"
        ).strip()

        with closing(get_connection()) as connection:
            log_job_event(
                connection,
                job_id=job_id,
                event_type="PART_VERIFICATION_CHANGED",
                icon="✓",
                message=(
                    (
                        f"{description} verification changed from "
                        f"{verification_labels.get(old_verification_status, old_verification_status.title())} "
                        f"to {verification_labels[requested_verification_status]}"
                        if old_verification_status != requested_verification_status
                        else f"{description} verification note updated"
                    )
                    + (
                        f" — Note: {requested_verification_note}"
                        if requested_verification_note
                        else ""
                    )
                ),
            )
            connection.commit()

    if old_status != new_status:
        status_icons = {
            "RESEARCH": "🔍",
            "QUOTED": "📝",
            "ORDERED": "🛒",
            "RECEIVED": "📦",
        }
        status_labels = {
            "RESEARCH": "Research",
            "QUOTED": "Quoted",
            "ORDERED": "Ordered",
            "RECEIVED": "Received",
        }

        description = (
            old_item["requested_description"]
            or "Part"
        ).strip()

        message = (
            f"{description} changed from "
            f"{status_labels.get(old_status, old_status.title())} "
            f"to {status_labels[new_status]}"
        )

        with closing(get_connection()) as connection:
            log_job_event(
                connection,
                job_id=job_id,
                event_type="PART_STATUS_CHANGED",
                icon=status_icons[new_status],
                message=message,
            )
            connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?view=advanced#parts-ready",
        status_code=303,
    )
