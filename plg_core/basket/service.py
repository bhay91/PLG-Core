from __future__ import annotations

from contextlib import closing
import math
import sqlite3
from typing import Any

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate


def customer_unit_price(cost: float) -> float:
    if cost <= 50:
        markup = 0.40
    elif cost <= 200:
        markup = 0.30
    elif cost <= 500:
        markup = 0.25
    else:
        markup = 0.20
    return float(math.ceil(cost * (1 + markup)))


def get_or_create_basket(connection: sqlite3.Connection, job_id: int):
    job = connection.execute(
        "SELECT id FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    connection.execute(
        """
        INSERT INTO baskets (job_id)
        VALUES (?)
        ON CONFLICT(job_id) DO NOTHING
        """,
        (job_id,),
    )
    basket = connection.execute(
        "SELECT * FROM baskets WHERE job_id = ?", (job_id,)
    ).fetchone()
    if basket is None:
        raise RuntimeError("Basket could not be created.")
    return basket


def serialize_basket(connection: sqlite3.Connection, basket) -> dict[str, Any]:
    items = connection.execute(
        "SELECT * FROM basket_items WHERE basket_id = ? ORDER BY id",
        (basket["id"],),
    ).fetchall()
    sources = connection.execute(
        "SELECT * FROM basket_sources WHERE basket_id = ? ORDER BY id",
        (basket["id"],),
    ).fetchall()

    selected = [row for row in items if row["selected"]]
    supplier_parts = sum(
        (row["supplier_unit_cost"] or 0) * row["quantity"]
        for row in selected
    )
    customer_parts = sum(
        customer_unit_price(row["supplier_unit_cost"] or 0) * row["quantity"]
        for row in selected
    )
    shipping = sum(row["shipping_total"] or 0 for row in sources)
    supplier_total = supplier_parts + shipping
    customer_total = customer_parts + shipping

    return {
        "id": basket["id"],
        "job_id": basket["job_id"],
        "status": basket["status"],
        "currency": basket["currency"],
        "items": [dict(row) for row in items],
        "sources": [dict(row) for row in sources],
        "totals": {
            "supplier_parts_total": round(supplier_parts, 2),
            "shipping_total": round(shipping, 2),
            "supplier_total": round(supplier_total, 2),
            "customer_parts_total": round(customer_parts, 2),
            "customer_total": round(customer_total, 2),
            "estimated_profit": round(customer_total - supplier_total, 2),
            "selected_items": len(selected),
            "all_items": len(items),
        },
    }


def get_basket(job_id: int):
    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        connection.commit()
        return serialize_basket(connection, basket)


def add_item(job_id: int, payload: BasketItemCreate):
    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        connection.execute(
            """
            INSERT INTO basket_items (
                basket_id, requested_description,
                manufacturer_part_number, supplier_part_number,
                supplier_name, source_type, brand, quantity,
                supplier_unit_cost, availability, lead_time,
                selected, confidence, source_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                basket["id"],
                payload.requested_description.strip(),
                payload.manufacturer_part_number.strip(),
                payload.supplier_part_number.strip(),
                payload.supplier_name.strip(),
                payload.source_type.strip().upper() or "AFTERMARKET",
                payload.brand.strip(),
                payload.quantity,
                payload.supplier_unit_cost,
                payload.availability.strip(),
                payload.lead_time.strip(),
                int(payload.selected),
                payload.confidence,
                payload.source_url.strip(),
            ),
        )
        connection.execute(
            "UPDATE baskets SET status='OPEN', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (basket["id"],),
        )
        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id=?", (basket["id"],)
        ).fetchone()
        return serialize_basket(connection, basket)


def update_item(item_id: int, payload: BasketItemUpdate):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields supplied.")

    allowed = {
        "requested_description", "manufacturer_part_number",
        "supplier_part_number", "supplier_name", "source_type",
        "brand", "quantity", "supplier_unit_cost", "availability",
        "lead_time", "selected", "confidence", "source_url",
    }

    with closing(get_connection()) as connection:
        item = connection.execute(
            "SELECT * FROM basket_items WHERE id=?", (item_id,)
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Basket item not found.")

        assignments = []
        values: list[Any] = []
        for field, value in updates.items():
            if field not in allowed:
                continue
            if field == "selected":
                value = int(bool(value))
            elif isinstance(value, str):
                value = value.strip()
            assignments.append(f"{field}=?")
            values.append(value)

        assignments.append("updated_at=CURRENT_TIMESTAMP")
        values.append(item_id)
        connection.execute(
            f"UPDATE basket_items SET {', '.join(assignments)} WHERE id=?",
            values,
        )
        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id=?", (item["basket_id"],)
        ).fetchone()
        return serialize_basket(connection, basket)


def delete_item(item_id: int):
    with closing(get_connection()) as connection:
        item = connection.execute(
            "SELECT * FROM basket_items WHERE id=?", (item_id,)
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Basket item not found.")
        connection.execute("DELETE FROM basket_items WHERE id=?", (item_id,))
        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id=?", (item["basket_id"],)
        ).fetchone()
        return serialize_basket(connection, basket)


def clear_basket(job_id: int):
    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        connection.execute(
            "DELETE FROM basket_items WHERE basket_id=?", (basket["id"],)
        )
        connection.execute(
            "DELETE FROM basket_sources WHERE basket_id=?", (basket["id"],)
        )
        connection.execute(
            """
            UPDATE baskets
            SET status='OPEN', committed_at=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (basket["id"],),
        )
        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id=?", (basket["id"],)
        ).fetchone()
        return serialize_basket(connection, basket)


def import_cart(job_id: int, payload: dict[str, Any]):
    source_key = str(payload.get("source_key", "")).strip()
    source_name = str(payload.get("source_name", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    trust_level = str(
        payload.get("trust_level", "SUPPLIER_VERIFIED")
    ).strip()
    currency = str(payload.get("currency", "USD")).strip() or "USD"
    items = payload.get("items") or []
    charges = payload.get("charges") or []

    if not source_name:
        raise HTTPException(status_code=400, detail="source_name is required.")
    if not items:
        raise HTTPException(status_code=400, detail="At least one item is required.")

    shipping = 0.0
    for charge in charges:
        if str(charge.get("charge_type", "")).upper() == "SHIPPING":
            try:
                shipping = float(charge.get("amount") or 0)
            except (TypeError, ValueError):
                shipping = 0.0

    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        cursor = connection.execute(
            """
            INSERT INTO basket_sources (
                basket_id, source_key, source_name, source_url,
                trust_level, shipping_total, currency
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                basket["id"], source_key, source_name, source_url,
                trust_level, shipping, currency,
            ),
        )
        source_id = cursor.lastrowid

        imported = 0
        for raw in items:
            description = str(
                raw.get("description", "")
            ).strip() or "Imported Part"
            supplier_part = str(
                raw.get("supplier_part_number", "")
            ).strip()
            manufacturer_part = str(
                raw.get("manufacturer_part_number", "")
            ).strip()
            if not supplier_part and not manufacturer_part:
                continue

            try:
                quantity = max(1, int(raw.get("quantity", 1)))
            except (TypeError, ValueError):
                quantity = 1
            try:
                cost = float(raw.get("supplier_cost"))
            except (TypeError, ValueError):
                cost = None

            source_type = "OEM" if source_key == "cat_sis" else "AFTERMARKET"
            connection.execute(
                """
                INSERT INTO basket_items (
                    basket_id, source_id, requested_description,
                    manufacturer_part_number, supplier_part_number,
                    supplier_name, source_type, brand, quantity,
                    supplier_unit_cost, availability, lead_time,
                    selected, confidence, source_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, ?)
                """,
                (
                    basket["id"], source_id, description,
                    manufacturer_part, supplier_part, source_name,
                    source_type, str(raw.get("brand", "")).strip(),
                    quantity, cost,
                    str(raw.get("availability", "")).strip(),
                    str(raw.get("lead_time", "")).strip(),
                    str(raw.get("source_url", source_url)).strip(),
                ),
            )
            imported += 1

        connection.execute(
            """
            INSERT INTO basket_activity (basket_id, activity_type, details)
            VALUES (?, 'SOURCE_IMPORTED', ?)
            """,
            (basket["id"], f"{source_name}: {imported} item(s)"),
        )
        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id=?", (basket["id"],)
        ).fetchone()
        result = serialize_basket(connection, basket)
        result.update({"ok": True, "job_id": job_id, "imported_count": imported})
        return result


def commit_basket(job_id: int):
    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        items = connection.execute(
            """
            SELECT * FROM basket_items
            WHERE basket_id=? AND selected=1
            ORDER BY id
            """,
            (basket["id"],),
        ).fetchall()

        if not items:
            raise HTTPException(
                status_code=400,
                detail="Select at least one basket item.",
            )

        created = 0
        for item in items:
            source_type = (item["source_type"] or "AFTERMARKET").upper()
            oem_number = (
                item["manufacturer_part_number"]
                if source_type == "OEM" else ""
            )
            cursor = connection.execute(
                """
                INSERT INTO job_parts (
                    job_id, requested_description, quantity,
                    oem_part_number, oem_description,
                    verification_status, verification_source,
                    oem_dealer_name, oem_dealer_price,
                    oem_dealer_availability, source_url,
                    product_url, captured_at
                )
                VALUES (?, ?, ?, ?, ?, 'VERIFIED', ?, ?, ?, ?, ?, ?,
                        CURRENT_TIMESTAMP)
                """,
                (
                    job_id, item["requested_description"], item["quantity"],
                    oem_number,
                    item["requested_description"] if oem_number else "",
                    item["supplier_name"] or "Basket",
                    item["supplier_name"] if source_type == "OEM" else "",
                    item["supplier_unit_cost"] if source_type == "OEM" else None,
                    item["availability"] if source_type == "OEM" else "",
                    item["source_url"], item["source_url"],
                ),
            )
            part_id = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO part_sources (
                    part_id, supplier_name, source_type, brand,
                    supplier_part_number, supplier_cost,
                    availability, lead_time, trust_level,
                    selected_for_quote, source_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    part_id, item["supplier_name"] or "Basket Source",
                    source_type, item["brand"],
                    item["supplier_part_number"] or item["manufacturer_part_number"],
                    item["supplier_unit_cost"], item["availability"],
                    item["lead_time"],
                    "OEM_VERIFIED" if source_type == "OEM"
                    else "SUPPLIER_VERIFIED",
                    item["source_url"],
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO suppliers (name) VALUES (?)",
                (item["supplier_name"] or "Basket Source",),
            )
            created += 1

        sources = connection.execute(
            "SELECT * FROM basket_sources WHERE basket_id=?",
            (basket["id"],),
        ).fetchall()
        for source in sources:
            connection.execute(
                """
                INSERT INTO source_cart_imports (
                    job_id, source_key, source_name,
                    source_url, shipping_total, currency
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, source["source_key"], source["source_name"],
                    source["source_url"], source["shipping_total"],
                    source["currency"],
                ),
            )

        connection.execute(
            """
            UPDATE baskets
            SET status='COMMITTED', committed_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (basket["id"],),
        )
        connection.execute(
            "UPDATE jobs SET status='VERIFIED' WHERE id=?", (job_id,)
        )
        connection.execute(
            """
            INSERT INTO basket_activity (basket_id, activity_type, details)
            VALUES (?, 'COMMITTED', ?)
            """,
            (basket["id"], f"{created} part(s) committed"),
        )
        connection.commit()
        return {"ok": True, "job_id": job_id, "created_parts": created}
