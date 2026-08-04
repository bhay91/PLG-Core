from __future__ import annotations

from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from legacy_app import get_connection, templates
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.basket.service import (
    add_item,
    clear_basket,
    commit_basket,
    delete_item,
    get_basket,
    get_or_create_basket,
    import_cart,
    update_item,
)


router = APIRouter(tags=["basket"])


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
                "source_url": item.get("source_url", ""),
            }
            for item in (payload.get("items") or [])
        ],
    }
    return import_cart(int(payload.get("job_id")), normalized)


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

    return templates.TemplateResponse(
        request=request,
        name="basket.html",
        context={
            "job": job,
            "basket": basket,
            "basket_items": basket_items,
            "vendor_carts": vendor_carts,
            "source_lookup": source_lookup,
            "connectors": connectors,
            "active_page": "jobs",
        },
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


@router.post("/jobs/{job_id}/basket/commit")
def commit_form(job_id: int):
    commit_basket(job_id)
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )
