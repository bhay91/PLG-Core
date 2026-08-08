from __future__ import annotations

from contextlib import closing
import math
import sqlite3
from typing import Any

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.timeline import log_job_event
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
                supplier_unit_cost, verification_status,
                verification_note, availability, lead_time,
                selected, confidence, source_url
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                payload.verification_status.strip().upper() or "UNVERIFIED",
                payload.verification_note.strip(),
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

        description = (
            payload.requested_description.strip()
            or "Unnamed part"
        )

        log_job_event(
            connection,
            job_id=job_id,
            event_type="PART_ADDED",
            icon="➕",
            message=f"Part added: {description}",
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
        "brand", "quantity", "supplier_unit_cost", "markup_percent", "part_status", "verification_status", "verification_note", "availability",
        "lead_time", "selected", "confidence", "source_url",
    }

    with closing(get_connection()) as connection:
        item = connection.execute(
            """
            SELECT
                basket_items.*,
                baskets.job_id
            FROM basket_items
            JOIN baskets
              ON baskets.id = basket_items.basket_id
            WHERE basket_items.id = ?
            """,
            (item_id,),
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

        if "selected" in updates:
            old_selected = bool(item["selected"])
            new_selected = bool(updates["selected"])

            if old_selected != new_selected:
                description = (
                    item["requested_description"]
                    or "Unnamed part"
                ).strip()

                if new_selected:
                    log_job_event(
                        connection,
                        job_id=int(item["job_id"]),
                        event_type="PART_SELECTED",
                        icon="✅",
                        message=f"Part added to quote: {description}",
                    )
                else:
                    log_job_event(
                        connection,
                        job_id=int(item["job_id"]),
                        event_type="PART_UNSELECTED",
                        icon="↩️",
                        message=f"Part removed from quote: {description}",
                    )

        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id=?", (item["basket_id"],)
        ).fetchone()
        return serialize_basket(connection, basket)



def _require_paid_invoice_for_order(
    connection,
    job_id: int,
) -> None:
    """Prevent purchasing before the customer invoice is paid."""

    invoice = connection.execute(
        """
        SELECT id, invoice_number, status, balance_due
        FROM invoices
        WHERE job_id = ?
          AND UPPER(COALESCE(status, '')) != 'VOID'
        ORDER BY id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()

    if invoice is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Parts cannot be ordered because this job "
                "does not have an invoice yet."
            ),
        )

    status = str(invoice["status"] or "").strip().upper()
    balance_due = round(float(invoice["balance_due"] or 0), 2)

    if status != "PAID" or balance_due > 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Parts cannot be ordered until "
                f"{invoice['invoice_number']} is paid in full."
            ),
        )


def advance_part_workflow(
    job_id: int,
    item_id: int,
    action: str,
):
    """Advance one selected part through ordering and receiving."""

    normalized_action = str(action or "").strip().upper()

    transitions = {
        "ORDER": {
            "from": "QUOTED",
            "to": "ORDERED",
            "event_type": "PART_ORDERED",
            "icon": "🛒",
            "verb": "ordered",
        },
        "RECEIVE": {
            "from": "ORDERED",
            "to": "RECEIVED",
            "event_type": "PART_RECEIVED",
            "icon": "📦",
            "verb": "received",
        },
    }

    transition = transitions.get(normalized_action)

    if transition is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid part workflow action.",
        )

    with closing(get_connection()) as connection:
        if normalized_action == "ORDER":
            _require_paid_invoice_for_order(
                connection,
                job_id,
            )

        item = connection.execute(
            """
            SELECT
                basket_items.*,
                baskets.job_id
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
                detail="Basket item not found for this job.",
            )

        current_status = (
            item["part_status"] or "RESEARCH"
        ).strip().upper()

        required_status = transition["from"]

        if current_status != required_status:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Part must be {required_status.title()} "
                    f"before it can be {transition['verb']}."
                ),
            )

        new_status = transition["to"]

        connection.execute(
            """
            UPDATE basket_items
            SET part_status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_status, item_id),
        )

        description = (
            item["requested_description"]
            or "Unnamed part"
        ).strip()

        supplier = (
            item["supplier_name"]
            or "supplier"
        ).strip()

        if normalized_action == "ORDER":
            message = (
                f"{description} ordered from {supplier}"
            )
        else:
            message = f"{description} received"

        log_job_event(
            connection,
            job_id=job_id,
            event_type=transition["event_type"],
            icon=transition["icon"],
            message=message,
        )

        connection.commit()

        basket = connection.execute(
            """
            SELECT *
            FROM baskets
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()

        return serialize_basket(connection, basket)


def advance_all_parts_workflow(
    job_id: int,
    action: str,
):
    """Advance every eligible selected part for one Job."""

    normalized_action = str(action or "").strip().upper()

    transitions = {
        "ORDER_ALL": {
            "from": "QUOTED",
            "to": "ORDERED",
            "event_type": "PARTS_ORDERED",
            "icon": "🛒",
            "verb": "ordered",
        },
        "RECEIVE_ALL": {
            "from": "ORDERED",
            "to": "RECEIVED",
            "event_type": "PARTS_RECEIVED",
            "icon": "📦",
            "verb": "received",
        },
    }

    transition = transitions.get(normalized_action)

    if transition is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid batch part workflow action.",
        )

    with closing(get_connection()) as connection:
        if normalized_action == "ORDER_ALL":
            _require_paid_invoice_for_order(
                connection,
                job_id,
            )

        items = connection.execute(
            """
            SELECT
                basket_items.id,
                basket_items.requested_description
            FROM basket_items
            JOIN baskets
              ON baskets.id = basket_items.basket_id
            WHERE baskets.job_id = ?
              AND basket_items.selected = 1
              AND UPPER(
                    COALESCE(
                        basket_items.part_status,
                        'RESEARCH'
                    )
                  ) = ?
            ORDER BY basket_items.id
            """,
            (job_id, transition["from"]),
        ).fetchall()

        if not items:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No selected parts are ready to be "
                    f"{transition['verb']}."
                ),
            )

        item_ids = [int(item["id"]) for item in items]
        placeholders = ",".join("?" for _ in item_ids)

        connection.execute(
            f"""
            UPDATE basket_items
            SET part_status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            (transition["to"], *item_ids),
        )

        count = len(item_ids)

        log_job_event(
            connection,
            job_id=job_id,
            event_type=transition["event_type"],
            icon=transition["icon"],
            message=(
                f"{count} selected "
                f"part{'s' if count != 1 else ''} "
                f"{transition['verb']}"
            ),
        )

        connection.commit()

        basket = connection.execute(
            """
            SELECT *
            FROM baskets
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()

        return serialize_basket(connection, basket)

def delete_item(item_id: int):
    with closing(get_connection()) as connection:
        item = connection.execute(
            """
            SELECT
                basket_items.*,
                baskets.job_id
            FROM basket_items
            JOIN baskets
              ON baskets.id = basket_items.basket_id
            WHERE basket_items.id = ?
            """,
            (item_id,),
        ).fetchone()

        if item is None:
            raise HTTPException(
                status_code=404,
                detail="Basket item not found.",
            )

        description = (
            item["requested_description"]
            or "Unnamed part"
        ).strip()

        connection.execute(
            "DELETE FROM basket_items WHERE id=?",
            (item_id,),
        )

        log_job_event(
            connection,
            job_id=int(item["job_id"]),
            event_type="PART_REMOVED",
            icon="🗑️",
            message=f"Part removed: {description}",
        )

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

        if basket["status"] == "COMMITTED":
            return {
                "ok": True,
                "job_id": job_id,
                "created_parts": 0,
                "already_committed": True,
            }
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

        invalid_items = []
        for item in items:
            candidate_status = (
                item["verification_status"] or "UNVERIFIED"
            ).strip().upper()
            candidate_note = (
                item["verification_note"] or ""
            ).strip()

            if (
                candidate_status not in {"VERIFIED", "OVERRIDE"}
                or (
                    candidate_status == "OVERRIDE"
                    and not candidate_note
                )
            ):
                invalid_items.append(item)

        if invalid_items:
            descriptions = ", ".join(
                item["requested_description"] or f"Item {item['id']}"
                for item in invalid_items
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "All selected parts must be verified or have a documented "
                    f"manual override before commit: {descriptions}"
                ),
            )

        created = 0
        for item in items:
            source_type = (item["source_type"] or "AFTERMARKET").upper()
            oem_number = (
                item["manufacturer_part_number"]
                if source_type == "OEM" else ""
            )
            candidate_verification_status = (
                item["verification_status"] or "UNVERIFIED"
            ).strip().upper()
            verification_note = (
                item["verification_note"] or ""
            ).strip()

            # Legacy Job Parts treat VERIFIED as the accepted gate.
            # Preserve manual override provenance separately.
            committed_verification_status = "VERIFIED"
            committed_verification_source = (
                "Manual Override"
                if candidate_verification_status == "OVERRIDE"
                else item["supplier_name"] or "Basket"
            )

            cursor = connection.execute(
                """
                INSERT INTO job_parts (
                    job_id, requested_description, quantity,
                    oem_part_number, oem_description,
                    verification_status, verification_source,
                    verification_notes,
                    oem_dealer_name, oem_dealer_price,
                    oem_dealer_availability, source_url,
                    product_url, captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        CURRENT_TIMESTAMP)
                """,
                (
                    job_id, item["requested_description"], item["quantity"],
                    oem_number,
                    item["requested_description"] if oem_number else "",
                    committed_verification_status,
                    committed_verification_source,
                    verification_note,
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
                verification_status, verification_note,
                selected_for_quote, source_url
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    part_id, item["supplier_name"] or "Basket Source",
                    source_type, item["brand"],
                    item["supplier_part_number"] or item["manufacturer_part_number"],
                    item["supplier_unit_cost"], item["availability"],
                    item["lead_time"],
                    "OEM_VERIFIED" if source_type == "OEM"
                    else "SUPPLIER_VERIFIED",
                candidate_verification_status,
                verification_note,
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
