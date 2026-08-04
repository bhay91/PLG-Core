from __future__ import annotations

from contextlib import closing
import math
import sqlite3
from typing import Any

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate


def _customer_unit_price(cost: float) -> float:
    if cost <= 50:
        markup = 0.40
    elif cost <= 200:
        markup = 0.30
    elif cost <= 500:
        markup = 0.25
    else:
        markup = 0.20

    return float(math.ceil(cost * (1 + markup)))


def _get_or_create_basket(
    connection: sqlite3.Connection,
    job_id: int,
) -> sqlite3.Row:
    job = connection.execute(
        "SELECT id FROM jobs WHERE id = ?",
        (job_id,),
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
        "SELECT * FROM baskets WHERE job_id = ?",
        (job_id,),
    ).fetchone()

    if basket is None:
        raise RuntimeError("Basket could not be created.")

    return basket


def _serialize_basket(
    connection: sqlite3.Connection,
    basket: sqlite3.Row,
) -> dict[str, Any]:
    items = connection.execute(
        """
        SELECT *
        FROM basket_items
        WHERE basket_id = ?
        ORDER BY id
        """,
        (basket["id"],),
    ).fetchall()

    sources = connection.execute(
        """
        SELECT *
        FROM basket_sources
        WHERE basket_id = ?
        ORDER BY id
        """,
        (basket["id"],),
    ).fetchall()

    selected_items = [row for row in items if row["selected"]]
    supplier_parts_total = sum(
        (row["supplier_unit_cost"] or 0) * row["quantity"]
        for row in selected_items
    )
    customer_parts_total = sum(
        _customer_unit_price(row["supplier_unit_cost"] or 0)
        * row["quantity"]
        for row in selected_items
    )
    shipping_total = sum(row["shipping_total"] or 0 for row in sources)
    supplier_total = supplier_parts_total + shipping_total
    customer_total = customer_parts_total + shipping_total
    estimated_profit = customer_total - supplier_total

    return {
        "id": basket["id"],
        "job_id": basket["job_id"],
        "status": basket["status"],
        "currency": basket["currency"],
        "created_at": basket["created_at"],
        "updated_at": basket["updated_at"],
        "items": [dict(row) for row in items],
        "sources": [dict(row) for row in sources],
        "totals": {
            "supplier_parts_total": round(supplier_parts_total, 2),
            "shipping_total": round(shipping_total, 2),
            "supplier_total": round(supplier_total, 2),
            "customer_parts_total": round(customer_parts_total, 2),
            "customer_total": round(customer_total, 2),
            "estimated_profit": round(estimated_profit, 2),
            "selected_items": len(selected_items),
            "all_items": len(items),
        },
    }


def get_basket(job_id: int) -> dict[str, Any]:
    with closing(get_connection()) as connection:
        basket = _get_or_create_basket(connection, job_id)
        connection.commit()
        return _serialize_basket(connection, basket)


def add_item(job_id: int, payload: BasketItemCreate) -> dict[str, Any]:
    with closing(get_connection()) as connection:
        basket = _get_or_create_basket(connection, job_id)

        cursor = connection.execute(
            """
            INSERT INTO basket_items (
                basket_id,
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
                selected,
                confidence,
                source_url
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
            """
            UPDATE baskets
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (basket["id"],),
        )

        connection.execute(
            """
            INSERT INTO basket_activity (
                basket_id,
                activity_type,
                details
            )
            VALUES (?, 'ITEM_ADDED', ?)
            """,
            (
                basket["id"],
                f"Basket item {cursor.lastrowid} added",
            ),
        )

        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id = ?",
            (basket["id"],),
        ).fetchone()
        return _serialize_basket(connection, basket)


def update_item(
    item_id: int,
    payload: BasketItemUpdate,
) -> dict[str, Any]:
    updates = payload.model_dump(exclude_unset=True)

    if not updates:
        raise HTTPException(
            status_code=400,
            detail="No basket item fields were supplied.",
        )

    with closing(get_connection()) as connection:
        item = connection.execute(
            "SELECT * FROM basket_items WHERE id = ?",
            (item_id,),
        ).fetchone()

        if item is None:
            raise HTTPException(
                status_code=404,
                detail="Basket item not found.",
            )

        allowed = {
            "requested_description",
            "manufacturer_part_number",
            "supplier_part_number",
            "supplier_name",
            "source_type",
            "brand",
            "quantity",
            "supplier_unit_cost",
            "availability",
            "lead_time",
            "selected",
            "confidence",
            "source_url",
        }

        assignments = []
        values: list[Any] = []

        for field, value in updates.items():
            if field not in allowed:
                continue

            if field == "selected":
                value = int(bool(value))
            elif isinstance(value, str):
                value = value.strip()

            assignments.append(f"{field} = ?")
            values.append(value)

        assignments.append("updated_at = CURRENT_TIMESTAMP")
        values.append(item_id)

        connection.execute(
            f"""
            UPDATE basket_items
            SET {", ".join(assignments)}
            WHERE id = ?
            """,
            values,
        )

        connection.execute(
            """
            UPDATE baskets
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (item["basket_id"],),
        )

        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id = ?",
            (item["basket_id"],),
        ).fetchone()
        return _serialize_basket(connection, basket)


def delete_item(item_id: int) -> dict[str, Any]:
    with closing(get_connection()) as connection:
        item = connection.execute(
            "SELECT * FROM basket_items WHERE id = ?",
            (item_id,),
        ).fetchone()

        if item is None:
            raise HTTPException(
                status_code=404,
                detail="Basket item not found.",
            )

        connection.execute(
            "DELETE FROM basket_items WHERE id = ?",
            (item_id,),
        )
        connection.execute(
            """
            UPDATE baskets
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (item["basket_id"],),
        )
        connection.commit()

        basket = connection.execute(
            "SELECT * FROM baskets WHERE id = ?",
            (item["basket_id"],),
        ).fetchone()
        return _serialize_basket(connection, basket)


def clear_basket(job_id: int) -> dict[str, Any]:
    with closing(get_connection()) as connection:
        basket = _get_or_create_basket(connection, job_id)

        connection.execute(
            "DELETE FROM basket_items WHERE basket_id = ?",
            (basket["id"],),
        )
        connection.execute(
            "DELETE FROM basket_sources WHERE basket_id = ?",
            (basket["id"],),
        )
        connection.execute(
            """
            UPDATE baskets
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (basket["id"],),
        )
        connection.commit()

        basket = connection.execute(
            "SELECT * FROM baskets WHERE id = ?",
            (basket["id"],),
        ).fetchone()
        return _serialize_basket(connection, basket)
