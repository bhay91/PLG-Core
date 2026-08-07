from __future__ import annotations

from contextlib import closing
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from legacy_app import get_connection, next_job_number, templates
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.jobs.engine import JobEngine
from plg_core.timeline import log_job_event
from plg_core.basket.service import (
    add_item,
    advance_all_parts_workflow,
    advance_part_workflow,
    clear_basket,
    commit_basket,
    delete_item,
    get_basket,
    get_or_create_basket,
    import_cart,
    update_item,
)


router = APIRouter(tags=["basket"])


def normalize_source_url(value):
    url = str(value or "").strip()
    if url.startswith("[") and "](" in url and url.endswith(")"):
        url = url.split("](", 1)[1][:-1].strip()
    return url


@router.get("/opportunities/new", response_class=HTMLResponse)
def new_opportunity_page(request: Request):
    with closing(get_connection()) as connection:
        customers = connection.execute("SELECT id, customer_number, name FROM customers WHERE active=1 ORDER BY name").fetchall()
    return templates.TemplateResponse(
        request=request,
        name="opportunity_new.html",
        context={"customers": customers},
    )


@router.post("/opportunities")
def create_opportunity(customer_id: Annotated[int, Form()], title: Annotated[str, Form()], request_text: Annotated[str, Form()] = "", follow_up_date: Annotated[str, Form()] = "", estimated_value: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = ""):
    with closing(get_connection()) as connection:
        cursor = connection.execute("INSERT INTO opportunities (customer_id, title, request_text, follow_up_date, estimated_value, notes) VALUES (?, ?, ?, NULLIF(?, ''), ?, ?)", (customer_id, title, request_text, follow_up_date, float(estimated_value or 0), notes))
        opportunity_id = cursor.lastrowid
        connection.execute("UPDATE opportunities SET opportunity_number = 'OPP-' || strftime('%Y','now') || '-' || printf('%03d', id) WHERE id = ?", (opportunity_id,))
        connection.commit()
    return RedirectResponse(url="/opportunities", status_code=303)


@router.post("/api/opportunities/import")
async def import_opportunity(request: Request):
    payload = await request.json()
    with closing(get_connection()) as connection:
        cursor = connection.execute("INSERT INTO opportunities (customer_id, title, request_text, follow_up_date, estimated_value, notes) VALUES (?, ?, ?, NULLIF(?, ''), ?, ?)", (payload.get("customer_id"), payload.get("title", ""), payload.get("request_text", ""), payload.get("follow_up_date", ""), float(payload.get("estimated_value") or 0), payload.get("notes", "")))
        opportunity_id = cursor.lastrowid
        connection.execute("UPDATE opportunities SET opportunity_number = 'OPP-' || strftime('%Y','now') || '-' || printf('%03d', id) WHERE id = ?", (opportunity_id,))
        machine_ids = []
        for machine in payload.get("machines") or []:
            cursor = connection.execute("INSERT INTO opportunity_machines (opportunity_id, manufacturer, model, vin_pin_serial, engine, notes) VALUES (?, ?, ?, ?, ?, ?)", (opportunity_id, machine.get("manufacturer", ""), machine.get("model", ""), machine.get("vin_pin_serial", ""), machine.get("engine", ""), machine.get("notes", "")))
            machine_ids.append(cursor.lastrowid)
        research_saved = 0
        for item in payload.get("research") or []:
            machine_index = item.get("machine_index")
            machine_id = machine_ids[machine_index] if isinstance(machine_index, int) and 0 <= machine_index < len(machine_ids) else None
            connection.execute("INSERT INTO opportunity_research (opportunity_id, opportunity_machine_id, part_description, oem_part_number, alternate_part_number, supplier_name, source_url, source_type, confidence, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (opportunity_id, machine_id, item.get("part_description", ""), item.get("oem_part_number", ""), item.get("alternate_part_number", ""), item.get("supplier_name", ""), normalize_source_url(item.get("source_url", "")), item.get("source_type", "chatgpt"), item.get("confidence"), item.get("notes", "")))
            research_saved += 1
        connection.commit()
    return {"status": "created", "opportunity_id": opportunity_id, "machine_ids": machine_ids, "research_saved": research_saved}


@router.get("/opportunities", response_class=HTMLResponse)
def opportunities_page(request: Request):
    with closing(get_connection()) as connection:
        opportunities = connection.execute(
            "SELECT * FROM opportunities ORDER BY created_at DESC"
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="opportunities.html",
        context={"opportunities": opportunities},
    )


@router.get("/opportunities/{opportunity_id}", response_class=HTMLResponse)
def opportunity_detail_page(request: Request, opportunity_id: int):
    with closing(get_connection()) as connection:
        opportunity = connection.execute(
            """
            SELECT opportunities.*,
                   customers.name AS customer_name,
                   customers.customer_number AS customer_number,
                   jobs.job_number AS converted_job_number
            FROM opportunities
            LEFT JOIN customers ON customers.id = opportunities.customer_id
            LEFT JOIN jobs ON jobs.id = opportunities.converted_job_id
            WHERE opportunities.id = ?
            """,
            (opportunity_id,),
        ).fetchone()
        machines = connection.execute(
            "SELECT * FROM opportunity_machines WHERE opportunity_id = ? ORDER BY id",
            (opportunity_id,),
        ).fetchall()
        research = connection.execute(
            "SELECT * FROM opportunity_research WHERE opportunity_id = ? ORDER BY id DESC",
            (opportunity_id,),
        ).fetchall()

    if opportunity is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    return templates.TemplateResponse(
        request=request,
        name="opportunity_detail.html",
        context={"opportunity": opportunity, "machines": machines, "research": research},
    )


@router.post("/opportunities/{opportunity_id}/machines")
def add_opportunity_machine(opportunity_id: int, manufacturer: Annotated[str, Form()] = "", model: Annotated[str, Form()] = "", vin_pin_serial: Annotated[str, Form()] = "", engine: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = ""):
    with closing(get_connection()) as connection:
        opportunity = connection.execute("SELECT id FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        if opportunity is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        connection.execute("INSERT INTO opportunity_machines (opportunity_id, manufacturer, model, vin_pin_serial, engine, notes) VALUES (?, ?, ?, ?, ?, ?)", (opportunity_id, manufacturer, model, vin_pin_serial, engine, notes))
        connection.commit()
    return RedirectResponse(url=f"/opportunities/{opportunity_id}", status_code=303)


@router.post("/opportunities/{opportunity_id}/research")
def add_opportunity_research(opportunity_id: int, opportunity_machine_id: Annotated[str, Form()] = "", part_description: Annotated[str, Form()] = "", oem_part_number: Annotated[str, Form()] = "", alternate_part_number: Annotated[str, Form()] = "", supplier_name: Annotated[str, Form()] = "", source_url: Annotated[str, Form()] = "", source_type: Annotated[str, Form()] = "", confidence: Annotated[str, Form()] = "", notes: Annotated[str, Form()] = ""):
    with closing(get_connection()) as connection:
        opportunity = connection.execute("SELECT id FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        if opportunity is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        connection.execute("INSERT INTO opportunity_research (opportunity_id, opportunity_machine_id, part_description, oem_part_number, alternate_part_number, supplier_name, source_url, source_type, confidence, notes) VALUES (?, NULLIF(?, ''), ?, ?, ?, ?, ?, ?, NULLIF(?, ''), ?)", (opportunity_id, opportunity_machine_id, part_description, oem_part_number, alternate_part_number, supplier_name, source_url, source_type, confidence, notes))
        connection.commit()
    return RedirectResponse(url=f"/opportunities/{opportunity_id}", status_code=303)


@router.post("/opportunities/{opportunity_id}/follow-up")
def update_opportunity_follow_up(opportunity_id: int, status: Annotated[str, Form()], follow_up_date: Annotated[str, Form()] = ""):
    with closing(get_connection()) as connection:
        opportunity = connection.execute("SELECT id FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        if opportunity is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        connection.execute("UPDATE opportunities SET status = ?, follow_up_date = NULLIF(?, ''), updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, follow_up_date, opportunity_id))
        connection.commit()
    return RedirectResponse(url=f"/opportunities/{opportunity_id}", status_code=303)


@router.post("/opportunities/{opportunity_id}/convert")
def convert_opportunity_to_job(opportunity_id: int, opportunity_machine_id: Annotated[str, Form()] = ""):
    with closing(get_connection()) as connection:
        opportunity = connection.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        if opportunity is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        if opportunity["converted_job_id"]:
            return RedirectResponse(url=f"/jobs/{opportunity['converted_job_id']}/basket", status_code=303)
        if not opportunity["customer_id"]:
            raise HTTPException(status_code=400, detail="Opportunity must have a customer before conversion")
        customer = connection.execute("SELECT * FROM customers WHERE id = ? AND active = 1", (opportunity["customer_id"],)).fetchone()
        if customer is None:
            raise HTTPException(status_code=400, detail="Opportunity customer not found")
        machine = None
        machine_id = None
        if opportunity_machine_id:
            machine = connection.execute("SELECT * FROM opportunity_machines WHERE id = ? AND opportunity_id = ?", (opportunity_machine_id, opportunity_id)).fetchone()
            if machine is None:
                raise HTTPException(status_code=400, detail="Selected opportunity machine not found")
            machine_id = machine["machine_id"]
            if not machine_id:
                display_name = " ".join(part for part in ((machine["manufacturer"] or "").strip(), (machine["model"] or "").strip()) if part).strip() or (machine["vin_pin_serial"] or "").strip()
                machine_cursor = connection.execute("INSERT INTO machines (customer_id,name,manufacturer,model,vin_pin_serial,engine,notes) VALUES (?,?,?,?,?,?,?)", (customer["id"], display_name, (machine["manufacturer"] or "").strip(), (machine["model"] or "").strip(), (machine["vin_pin_serial"] or "").strip(), (machine["engine"] or "").strip(), machine["notes"] or ""))
                machine_id = machine_cursor.lastrowid
                connection.execute("UPDATE machines SET machine_number=? WHERE id=?", (f"PLG-M{machine_id:05d}", machine_id))
                connection.execute("UPDATE opportunity_machines SET machine_id=? WHERE id=?", (machine_id, machine["id"]))
        job_number = next_job_number(connection)
        cursor = connection.execute("INSERT INTO jobs (job_number, created_date, customer_id, machine_id, customer, company, phone, email, address, manufacturer, machine, pin_serial, status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'REQUESTED', ?)", (job_number, date.today().isoformat(), customer["id"], machine_id, customer["name"], customer["company"] or "", customer["phone"] or "", customer["email"] or "", customer["address"] or "", machine["manufacturer"] if machine else "", machine["model"] if machine else "", machine["vin_pin_serial"] if machine else "", opportunity["notes"] or opportunity["request_text"] or ""))
        job_id = cursor.lastrowid
        research = connection.execute("SELECT * FROM opportunity_research WHERE opportunity_id = ? ORDER BY id", (opportunity_id,)).fetchall()
        connection.execute("UPDATE opportunities SET status = 'CONVERTED', converted_job_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id, opportunity_id))
        connection.commit()
    for item in research:
        add_item(job_id, BasketItemCreate(requested_description=item["part_description"] or "Research candidate", manufacturer_part_number=item["oem_part_number"] or "", supplier_part_number=item["alternate_part_number"] or "", supplier_name=item["supplier_name"] or "", source_type=(item["source_type"] or "RESEARCH").upper(), selected=False, confidence=item["confidence"], source_url=item["source_url"] or ""))
    return RedirectResponse(url=f"/jobs/{job_id}/basket", status_code=303)


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
    return import_cart(int(payload.get("job_id")), payload)


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


@router.post("/api/opportunities/{opportunity_id}/research")
async def import_opportunity_research(request: Request, opportunity_id: int):
    payload = await request.json()
    with closing(get_connection()) as connection:
        opportunity = connection.execute("SELECT id FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        if opportunity is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        connection.execute("INSERT INTO opportunity_research (opportunity_id, opportunity_machine_id, part_description, oem_part_number, alternate_part_number, supplier_name, source_url, source_type, confidence, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (opportunity_id, payload.get("opportunity_machine_id"), payload.get("part_description", ""), payload.get("oem_part_number", ""), payload.get("alternate_part_number", ""), payload.get("supplier_name", ""), normalize_source_url(payload.get("source_url", "")), payload.get("source_type", "chatgpt"), payload.get("confidence"), payload.get("notes", "")))
        connection.commit()
    return {"status": "saved", "opportunity_id": opportunity_id}


@router.post("/api/opportunities/{opportunity_id}/research/batch")
async def import_opportunity_research_batch(request: Request, opportunity_id: int):
    payload = await request.json()
    items = payload.get("items") or []
    with closing(get_connection()) as connection:
        opportunity = connection.execute("SELECT id FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        if opportunity is None:
            raise HTTPException(status_code=404, detail="Opportunity not found")
        for item in items:
            connection.execute("INSERT INTO opportunity_research (opportunity_id, opportunity_machine_id, part_description, oem_part_number, alternate_part_number, supplier_name, source_url, source_type, confidence, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (opportunity_id, item.get("opportunity_machine_id"), item.get("part_description", ""), item.get("oem_part_number", ""), item.get("alternate_part_number", ""), item.get("supplier_name", ""), normalize_source_url(item.get("source_url", "")), item.get("source_type", "chatgpt"), item.get("confidence"), item.get("notes", "")))
        connection.commit()
    return {"status": "saved", "opportunity_id": opportunity_id, "items_saved": len(items)}


@router.get("/jobs/{job_id}/basket", response_class=HTMLResponse)
def basket_page(request: Request, job_id: int):
    basket = get_basket(job_id)

    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

        connectors = connection.execute(
            """
            SELECT *
            FROM connector_profiles
            WHERE is_enabled = 1
            ORDER BY display_name
            """
        ).fetchall()

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
            WHERE job_id = ? AND COALESCE(is_archived, 0) = 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()

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
                ],
            }
        )

    unassigned_items = [
        item
        for item in basket["items"]
        if item["source_id"] is None
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

    basket_items = [
        item for item in basket["items"]
        if item["selected"]
    ]

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
        outstanding_parts=outstanding_parts,
        basket_status=basket["status"],
        customer_request=customer_request,
        quote=quote,
        invoice=invoice,
    ).to_dict()

    return templates.TemplateResponse(
        request=request,
        name="job_command_center.html",
        context={
            "timeline": timeline,
            "job": job,
            "basket": basket,
            "basket_items": basket_items,
            "vendor_carts": vendor_carts,
            "source_lookup": source_lookup,
            "connectors": connectors,
            "customer_request": customer_request,
            "request_attachment_count": request_attachment_count,
            "quote": quote,
            "invoice": invoice,
            "intelligence": intelligence,
            "active_page": "jobs",
        },
    )


@router.post("/jobs/{job_id}/vendor-carts/clone")
def clone_supplier_quote(
    job_id: int,
    vendor_name: Annotated[str, Form()],
    source_type: Annotated[str, Form()] = "AFTERMARKET",
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
              AND (
                    source_id IS NULL
                    OR source_id != ?
                  )
            ORDER BY id
            """,
            (basket["id"], source_id),
        ).fetchall()

        # If nothing is selected yet, copy all researched options.
        if not base_items:
            base_items = connection.execute(
                """
                SELECT *
                FROM basket_items
                WHERE basket_id = ?
                  AND (
                        source_id IS NULL
                        OR source_id != ?
                      )
                ORDER BY id
                """,
                (basket["id"], source_id),
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
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, '', '', 0
                )
                """,
                (
                    basket["id"],
                    source_id,
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

        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket#parts-research",
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
            SELECT basket_items.id
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

        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket#parts-research",
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
            SELECT basket_sources.id
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
    supplier_part_number: Annotated[str, Form()] = "",
    supplier_unit_cost: Annotated[float | None, Form()] = None,
    source_type: Annotated[str, Form()] = "AFTERMARKET",
    brand: Annotated[str, Form()] = "",
    availability: Annotated[str, Form()] = "",
    lead_time: Annotated[str, Form()] = "",
):
    vendor_name = vendor_name.strip() or "Manual Vendor"

    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)

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
        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items")
def add_manual_item(
    job_id: int,
    requested_description: Annotated[str, Form()],
    quantity: Annotated[int, Form()] = 1,
    supplier_name: Annotated[str, Form()] = "",
    supplier_part_number: Annotated[str, Form()] = "",
    supplier_unit_cost: Annotated[float | None, Form()] = None,
    source_type: Annotated[str, Form()] = "AFTERMARKET",
):
    add_item(
        job_id,
        BasketItemCreate(
            requested_description=requested_description,
            quantity=quantity,
            supplier_name=supplier_name,
            supplier_part_number=supplier_part_number,
            supplier_unit_cost=supplier_unit_cost,
            source_type=source_type,
            selected=False,
        ),
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items/{item_id}/toggle")
def toggle_item(
    job_id: int,
    item_id: int,
    selected: Annotated[int, Form()],
):
    update_item(
        item_id,
        BasketItemUpdate(selected=bool(selected)),
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )



@router.post("/jobs/{job_id}/basket/items/{item_id}/quantity")
def update_item_quantity(
    job_id: int,
    item_id: int,
    quantity: Annotated[int, Form()],
):
    update_item(
        item_id,
        BasketItemUpdate(quantity=max(1, quantity)),
    )
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
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
        url=f"/jobs/{job_id}/basket#parts-ready",
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
        url=f"/jobs/{job_id}/basket#parts-ready",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items/{item_id}/delete")
def delete_item_form(job_id: int, item_id: int):
    delete_item(item_id)
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/clear")
def clear_form(job_id: int):
    clear_basket(job_id)
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )



@router.post("/jobs/{job_id}/revenue-adjustments")
def update_revenue_adjustments(
    job_id: int,
    service_charge: Annotated[str, Form()] = "0",
    service_charge_description: Annotated[str, Form()] = "",
    sourcing_fee: Annotated[str, Form()] = "0",
    sourcing_fee_description: Annotated[str, Form()] = "",
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

    if 0 < parsed_service_charge < 150:
        raise HTTPException(
            status_code=400,
            detail=(
                "Service Charge must be $0 or at least $150.00."
            ),
        )

    with closing(get_connection()) as connection:
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

        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{job_id}/basket?saved=1#revenue-adjustments",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/checkout")
def checkout_basket(job_id: int):
    commit_basket(job_id)
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/commit")
def commit_form(job_id: int):
    commit_basket(job_id)
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )


@router.post("/jobs/{job_id}/basket/items/{item_id}/update")
def update_basket_item_form(
    job_id: int,
    item_id: int,
    quantity: int = Form(...),
    supplier_unit_cost: float = Form(...),
    markup_percent: float = Form(...),
    part_status: str = Form(""),
):
    valid_statuses = {
        "RESEARCH",
        "QUOTED",
        "ORDERED",
        "RECEIVED",
    }
    requested_status = part_status.strip().upper()

    with closing(get_connection()) as connection:
        old_item = connection.execute(
            """
            SELECT requested_description, part_status
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

    update_item(
        item_id,
        BasketItemUpdate(
            quantity=quantity,
            supplier_unit_cost=supplier_unit_cost,
            markup_percent=markup_percent,
            part_status=new_status,
        ),
    )

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
        url=f"/jobs/{job_id}/basket#parts-ready",
        status_code=303,
    )
