from __future__ import annotations

from contextlib import closing
import json
import sqlite3
from typing import Any

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.timeline import log_job_event
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.pricing import effective_customer_unit_price


def get_customer_display_fx(connection: sqlite3.Connection, basket_id: int):
    """Resolve customer-display currency overrides without changing source currency."""
    from plg_core.currency.service import resolve_basket_currency_config
    return resolve_basket_currency_config(connection, basket_id)


def set_customer_display_fx(connection: sqlite3.Connection, basket_id: int, *,
                             display_mode=None, jmd_rate=None):
    from plg_core.currency.service import UNSET, set_basket_currency_overrides
    if display_mode is None:
        display_mode = UNSET
    if jmd_rate is None:
        jmd_rate = UNSET
    return set_basket_currency_overrides(
        connection, basket_id, display_mode=display_mode, jmd_rate=jmd_rate
    )


def clear_customer_display_fx(connection: sqlite3.Connection, basket_id: int, *,
                               display_mode=False, jmd_rate=False):
    from plg_core.currency.service import clear_basket_currency_overrides
    return clear_basket_currency_overrides(
        connection, basket_id, display_mode=display_mode, jmd_rate=jmd_rate
    )


def _next_internal_part_number(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "UPDATE internal_part_number_sequence SET last_number=last_number+1 "
        "WHERE singleton=1 RETURNING last_number"
    ).fetchone()
    if row is None:
        raise RuntimeError("Internal part number sequence is unavailable.")
    return f"PPS-MAN-{int(row[0]):06d}"


def get_or_create_basket(connection: sqlite3.Connection, job_id: int):
    job = connection.execute(
        "SELECT id FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    basket = connection.execute(
        "SELECT * FROM baskets WHERE job_id = ?", (job_id,)
    ).fetchone()
    if basket is not None:
        return basket

    # The second concurrent creator may still win between SELECT and INSERT.
    # The unique job_id constraint is authoritative; ordinary reads no longer
    # perform a pointless AUTOINCREMENT-consuming insert.
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


def ensure_basket_mutable(
    basket,
    connection: sqlite3.Connection | None = None,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
):
    """Enforce Job lifecycle plus the active Work Revision token."""
    from plg_core.lifecycle import ensure_job_allows_new_business
    from plg_core.revisions.service import ensure_revision_mutable

    if connection is None:
        with closing(get_connection()) as owned_connection:
            ensure_job_allows_new_business(
                owned_connection, int(basket["job_id"]), "change its working basket"
            )
            revision = ensure_revision_mutable(
                owned_connection,
                int(basket["job_id"]),
                expected_revision_id=expected_revision_id,
                expected_version=expected_version,
            )
            owned_connection.commit()
            return revision
    ensure_job_allows_new_business(
        connection, int(basket["job_id"]), "change its working basket"
    )
    return ensure_revision_mutable(
        connection,
        int(basket["job_id"]),
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )


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
        effective_customer_unit_price(
            row["supplier_unit_cost"] or 0,
            row["markup_percent"],
            row["customer_unit_price_override"],
        ) * row["quantity"]
        for row in selected
    )
    shipping = sum(row["shipping_total"] or 0 for row in sources)
    supplier_total = supplier_parts + shipping
    customer_total = customer_parts + shipping
    revision = connection.execute(
        """
        SELECT wr.id, wr.revision_number, wr.state, wr.lock_version,
               wr.is_synthetic
        FROM jobs j
        LEFT JOIN work_revisions wr ON wr.id=j.active_work_revision_id
        WHERE j.id=?
        """,
        (basket["job_id"],),
    ).fetchone()

    return {
        "id": basket["id"],
        "job_id": basket["job_id"],
        "status": basket["status"],
        "currency": basket["currency"],
        "work_revision": dict(revision) if revision is not None else None,
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
        from plg_core.revisions.service import ensure_initial_revision
        ensure_initial_revision(connection, job_id)
        connection.commit()
        return serialize_basket(connection, basket)


def get_existing_basket(job_id: int):
    """Return an existing basket for display without initializing state."""
    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        basket = connection.execute(
            "SELECT * FROM baskets WHERE job_id = ?", (job_id,)
        ).fetchone()
        return serialize_basket(connection, basket) if basket is not None else None


def add_item_with_connection(
    connection: sqlite3.Connection,
    job_id: int,
    payload: BasketItemCreate,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
):
    """Add one item using an existing database transaction."""

    basket = get_or_create_basket(connection, job_id)
    revision = ensure_basket_mutable(
        basket,
        connection,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
    internal_part_number = payload.internal_part_number.strip()
    if not internal_part_number and not payload.manufacturer_part_number.strip():
        internal_part_number = _next_internal_part_number(connection)

    if payload.job_asset_id is not None and connection.execute(
        "SELECT 1 FROM job_assets WHERE id=? AND job_id=? AND state='ACTIVE'",
        (payload.job_asset_id, job_id),
    ).fetchone() is None:
        raise HTTPException(status_code=409, detail="Select an active asset from this Job.")
    research_state = str(payload.research_state or "LEGACY_CANDIDATE").upper()
    if research_state not in {"RESEARCH_RESULT", "QUOTE_CANDIDATE", "LEGACY_CANDIDATE"}:
        raise HTTPException(status_code=400, detail="Invalid research-result state.")
    if payload.primary_requested_need_id is not None and connection.execute(
        "SELECT 1 FROM requested_needs WHERE id=? AND job_id=?",
        (payload.primary_requested_need_id, job_id),
    ).fetchone() is None:
        raise HTTPException(status_code=409, detail="Requested Need does not belong to this Job.")
    if payload.primary_requested_need_id is not None and "quantity" not in payload.model_fields_set:
        inherited = connection.execute(
            "SELECT quantity FROM requested_needs WHERE id=? AND job_id=?",
            (payload.primary_requested_need_id, job_id),
        ).fetchone()
        if inherited and inherited["quantity"] is not None:
            payload.quantity = int(inherited["quantity"])
    if payload.research_session_id is not None:
        session = connection.execute(
            "SELECT * FROM verification_sessions WHERE id=? AND job_id=?",
            (payload.research_session_id, job_id),
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=409, detail="Research Session does not belong to this Job.")
        if session["job_asset_id"] != payload.job_asset_id:
            raise HTTPException(status_code=409, detail="Research Session belongs to a different machine.")
        if session["requested_need_id"] != payload.primary_requested_need_id:
            raise HTTPException(status_code=409, detail="Research Session belongs to a different Requested Need.")

    connection.execute(
        """
        INSERT INTO basket_items (
            basket_id, job_asset_id, primary_requested_need_id, research_state,
            requested_description, internal_part_number,
            manufacturer_part_number, alternate_part_number,
            supplier_part_number, supplier_name, source_type, brand, quantity,
            supplier_unit_cost, markup_percent,
            customer_unit_price_override, pricing_mode, verification_status,
            verification_note, availability, lead_time,
            selected, confidence, source_url, research_session_id,
            research_evidence, research_notes, identified_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            basket["id"],
            payload.job_asset_id,
            payload.primary_requested_need_id,
            research_state,
            payload.requested_description.strip(),
            internal_part_number,
            payload.manufacturer_part_number.strip(),
            payload.alternate_part_number.strip(),
            payload.supplier_part_number.strip(),
            payload.supplier_name.strip(),
            payload.source_type.strip().upper() or "AFTERMARKET",
            payload.brand.strip(),
            payload.quantity,
            payload.supplier_unit_cost,
            payload.markup_percent,
            payload.customer_unit_price_override,
            "OVERRIDE" if payload.customer_unit_price_override is not None else "AUTO",
            payload.verification_status.strip().upper() or "UNVERIFIED",
            payload.verification_note.strip(),
            payload.availability.strip(),
            payload.lead_time.strip(),
            int(payload.selected),
            payload.confidence,
            payload.source_url.strip(),
            payload.research_session_id,
            payload.research_evidence.strip(),
            payload.research_notes.strip(),
            payload.identified_at,
        ),
    )
    item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    if payload.primary_requested_need_id is not None:
        connection.execute(
            "INSERT OR IGNORE INTO basket_item_need_links "
            "(basket_item_id,requested_need_id) VALUES (?,?)",
            (item_id, payload.primary_requested_need_id),
        )

    from plg_core.revisions.service import touch_revision
    touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))

    connection.execute(
        """
        UPDATE baskets
        SET status='OPEN',
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
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

    basket = connection.execute(
        "SELECT * FROM baskets WHERE id=?",
        (basket["id"],),
    ).fetchone()

    return serialize_basket(connection, basket)


def add_item(
    job_id: int,
    payload: BasketItemCreate,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
):
    with closing(get_connection()) as connection:
        result = add_item_with_connection(
            connection,
            job_id,
            payload,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        connection.commit()
        return result

def update_item(
    item_id: int,
    payload: BasketItemUpdate,
    expected_job_id: int | None = None,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
):
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields supplied.")

    allowed = {
        "requested_description", "job_asset_id", "primary_requested_need_id", "research_state", "manufacturer_part_number",
        "alternate_part_number", "supplier_part_number",
        "supplier_name", "source_type",
        "brand", "quantity", "supplier_unit_cost", "markup_percent", "customer_unit_price_override", "part_status", "verification_status", "verification_note", "availability",
        "lead_time", "selected", "confidence", "source_url", "research_session_id",
        "research_evidence", "research_notes", "identified_at",
    }

    with closing(get_connection()) as connection:
        item = connection.execute(
            """
            SELECT
                basket_items.*,
                baskets.job_id,
                baskets.status AS basket_status
            FROM basket_items
            JOIN baskets
              ON baskets.id = basket_items.basket_id
            WHERE basket_items.id = ?
            """,
            (item_id,),
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="Basket item not found.")

        if (
            expected_job_id is not None
            and int(item["job_id"]) != expected_job_id
        ):
            raise HTTPException(
                status_code=404,
                detail="Basket item not found for this job.",
            )

        if "research_state" in updates:
            state = str(updates["research_state"] or "").upper()
            if state not in {"RESEARCH_RESULT", "QUOTE_CANDIDATE", "LEGACY_CANDIDATE"}:
                raise HTTPException(status_code=400, detail="Invalid research-result state.")
            updates["research_state"] = state
        if updates.get("selected") and str(
            updates.get("research_state") or item["research_state"] or "LEGACY_CANDIDATE"
        ).upper() == "RESEARCH_RESULT":
            raise HTTPException(
                status_code=409,
                detail="Confirm this Research Result as a Quote Candidate before selecting it.",
            )
        if updates.get("primary_requested_need_id") is not None and connection.execute(
            "SELECT 1 FROM requested_needs WHERE id=? AND job_id=?",
            (updates["primary_requested_need_id"], item["job_id"]),
        ).fetchone() is None:
            raise HTTPException(status_code=409, detail="Requested Need does not belong to this Job.")

        revision = ensure_basket_mutable(
            item,
            connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        if "job_asset_id" in updates and updates["job_asset_id"] is not None:
            if connection.execute(
                "SELECT 1 FROM job_assets WHERE id=? AND job_id=? AND state='ACTIVE'",
                (updates["job_asset_id"], item["job_id"]),
            ).fetchone() is None:
                raise HTTPException(status_code=409, detail="Select an active asset from this Job.")

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

        if "customer_unit_price_override" in updates:
            assignments.append("pricing_mode=?")
            values.append(
                "OVERRIDE"
                if updates["customer_unit_price_override"] is not None
                else "AUTO"
            )

        assignments.append("updated_at=CURRENT_TIMESTAMP")
        values.append(item_id)
        connection.execute(
            f"UPDATE basket_items SET {', '.join(assignments)} WHERE id=?",
            values,
        )
        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))

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

def delete_item(
    item_id: int,
    expected_job_id: int | None = None,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
    require_unpromoted_research_result: bool = False,
):
    with closing(get_connection()) as connection:
        item = connection.execute(
            """
            SELECT
                basket_items.*,
                baskets.job_id,
                baskets.status AS status
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

        if (
            expected_job_id is not None
            and int(item["job_id"]) != expected_job_id
        ):
            raise HTTPException(
                status_code=404,
                detail="Basket item not found for this job.",
            )

        if require_unpromoted_research_result and (
            str(item["research_state"] or "").upper() != "RESEARCH_RESULT"
            or bool(item["selected"])
        ):
            raise HTTPException(
                status_code=409,
                detail="Only an unpromoted Research Result can be removed here.",
            )

        revision = ensure_basket_mutable(
            item,
            connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )

        description = (
            item["requested_description"]
            or "Unnamed part"
        ).strip()

        connection.execute(
            "DELETE FROM basket_items WHERE id=?",
            (item_id,),
        )
        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))

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


def clear_basket(
    job_id: int,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
):
    with closing(get_connection()) as connection:
        basket = get_or_create_basket(connection, job_id)
        revision = ensure_basket_mutable(
            basket,
            connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
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
        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id=?", (basket["id"],)
        ).fetchone()
        return serialize_basket(connection, basket)


def import_cart(
    job_id: int,
    payload: dict[str, Any],
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
    job_asset_id: int | None = None,
    requested_need_id: int | None = None,
    verification_session_id: int | None = None,
    require_capture_context: bool = False,
):
    source_key = str(payload.get("source_key", "")).strip()
    source_name = str(payload.get("source_name", "")).strip()
    from plg_core.sources.service import validate_source_url
    source_url = validate_source_url(str(payload.get("source_url", "")).strip())
    trust_level = str(
        payload.get("trust_level", "SUPPLIER_VERIFIED")
    ).strip()
    currency = str(payload.get("currency", "USD")).strip() or "USD"
    capture_mode = str(payload.get("capture_mode") or "").strip().upper()
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
        connection.execute("BEGIN IMMEDIATE")
        basket = get_or_create_basket(connection, job_id)
        revision = ensure_basket_mutable(
            basket,
            connection,
            expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        import_context = connection.execute(
            "SELECT job_asset_id,requested_need_id,verification_session_id FROM active_source_import "
            "WHERE id=1 AND job_id=?",
            (job_id,),
        ).fetchone()
        if require_capture_context:
            if import_context is None:
                raise HTTPException(
                    status_code=409,
                    detail="The active PPS capture context is no longer available. Reload and review before sending.",
                )
            supplied_context = (job_asset_id, requested_need_id, verification_session_id)
            active_context = (
                import_context["job_asset_id"], import_context["requested_need_id"],
                import_context["verification_session_id"],
            )
            if supplied_context != active_context:
                raise HTTPException(
                    status_code=409,
                    detail="The active Job, machine, Need, or research session changed before import.",
                )
            asset = connection.execute(
                "SELECT 1 FROM job_assets WHERE id=? AND job_id=? AND state='ACTIVE'",
                (job_asset_id, job_id),
            ).fetchone()
            session = connection.execute(
                "SELECT 1 FROM verification_sessions WHERE id=? AND job_id=? "
                "AND job_asset_id=? AND requested_need_id IS ?",
                (verification_session_id, job_id, job_asset_id, requested_need_id),
            ).fetchone()
            need = None if requested_need_id is None else connection.execute(
                "SELECT 1 FROM requested_needs WHERE id=? AND job_id=? AND job_asset_id=?",
                (requested_need_id, job_id, job_asset_id),
            ).fetchone()
            if asset is None or session is None or (requested_need_id is not None and need is None):
                raise HTTPException(
                    status_code=409,
                    detail="The supplied machine, Need, or research session does not belong to this Job.",
                )
        import_asset_id = job_asset_id if require_capture_context else (import_context["job_asset_id"] if import_context else None)
        import_need_id = requested_need_id if require_capture_context else (import_context["requested_need_id"] if import_context else None)
        import_session_id = verification_session_id if require_capture_context else (import_context["verification_session_id"] if import_context else None)
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
                raw.get("supplier_part_number") or raw.get("sku") or
                raw.get("asin") or raw.get("listing_id") or raw.get("item_id") or ""
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
            product_page_url = validate_source_url(str(raw.get("product_page_url") or "").strip()) if raw.get("product_page_url") else ""
            cart_page_url = validate_source_url(str(raw.get("cart_page_url") or "").strip()) if raw.get("cart_page_url") else ""
            item_source_url = validate_source_url(str(product_page_url or raw.get("source_url") or cart_page_url or source_url).strip())
            item_supplier_name = str(raw.get("supplier_name") or source_name).strip()
            evidence_parts = []
            if capture_mode:
                evidence_parts.append(f"Capture mode: {capture_mode}")
            evidence_parts.append(f"Source: {source_name}")
            raw_evidence = str(raw.get("evidence") or "").strip()
            if raw_evidence:
                evidence_parts.append(f"Evidence: {raw_evidence}")
            for label, key in (("MPN", "manufacturer_part_number"), ("Supplier part", "supplier_part_number"), ("SKU", "sku"), ("ASIN", "asin"), ("Listing ID", "listing_id")):
                value = str(raw.get(key) or "").strip()
                if value:
                    evidence_parts.append(f"{label}: {value}")
            if raw.get("item_id"):
                evidence_parts.append(f"Item ID: {str(raw.get('item_id')).strip()}")
            if product_page_url:
                evidence_parts.append(f"Product page: {product_page_url}")
            if cart_page_url:
                evidence_parts.append(f"Cart page: {cart_page_url}")
            evidence_fields = raw.get("evidence_fields")
            if isinstance(evidence_fields, dict) and evidence_fields:
                evidence_parts.append("Field evidence: " + json.dumps(evidence_fields, sort_keys=True))
            evidence = " | ".join(part for part in evidence_parts if part)
            connection.execute(
                """
                INSERT INTO basket_items (
                    basket_id, source_id, job_asset_id, primary_requested_need_id,
                    research_session_id,
                    research_state, requested_description,
                    manufacturer_part_number, supplier_part_number,
                    supplier_name, source_type, brand, quantity,
                    supplier_unit_cost, availability, lead_time,
                    selected, confidence, source_url, research_evidence
                )
                VALUES (?, ?, ?, ?, ?, 'RESEARCH_RESULT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1.0, ?, ?)
                """,
                (
                    basket["id"], source_id, import_asset_id, import_need_id,
                    import_session_id, description,
                    manufacturer_part, supplier_part, item_supplier_name,
                    source_type, str(raw.get("brand", "")).strip(),
                    quantity, cost,
                    str(raw.get("availability", "")).strip(),
                    str(raw.get("lead_time", "")).strip(),
                    item_source_url, evidence,
                ),
            )
            item_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            if import_need_id is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO basket_item_need_links "
                    "(basket_item_id,requested_need_id) VALUES (?,?)",
                    (item_id, import_need_id),
                )
            shipping_values = (
                raw.get("weight"), raw.get("length"), raw.get("width"), raw.get("height")
            )
            if any(value not in (None, "") for value in shipping_values):
                def optional_number(value):
                    try:
                        return float(value) if value not in (None, "") else None
                    except (TypeError, ValueError):
                        return None
                connection.execute(
                    """INSERT INTO part_shipping_data (
                           basket_item_id,manufacturer_part_number,unit_weight,weight_unit,
                           length,width,height,dimension_unit,quality,provenance,notes
                       ) VALUES (?,?,?,?,?,?,?,?, 'VERIFIED', ?, ?)""",
                    (item_id, manufacturer_part, optional_number(raw.get("weight")),
                     str(raw.get("weight_unit") or "").strip(),
                     optional_number(raw.get("length")), optional_number(raw.get("width")),
                     optional_number(raw.get("height")), str(raw.get("dimension_unit") or "").strip(),
                     item_source_url,
                     "Firefox source-reported data; "
                     f"weight_type={str(raw.get('weight_type') or 'UNKNOWN').upper()}; "
                     f"dimension_type={str(raw.get('dimension_type') or 'UNKNOWN').upper()}"),
                )
            imported += 1

        if imported == 0:
            raise HTTPException(
                status_code=400,
                detail="No parts with a usable identifier were accepted for import.",
            )

        connection.execute(
            """
            INSERT INTO basket_activity (basket_id, activity_type, details)
            VALUES (?, 'SOURCE_IMPORTED', ?)
            """,
            (basket["id"], f"{source_name}: {imported} item(s)"),
        )
        from plg_core.revisions.service import touch_revision
        touch_revision(connection, int(revision["id"]), int(revision["lock_version"]))
        connection.commit()
        basket = connection.execute(
            "SELECT * FROM baskets WHERE id=?", (basket["id"],)
        ).fetchone()
        result = serialize_basket(connection, basket)
        result.update({"ok": True, "job_id": job_id, "imported_count": imported})
        return result


def commit_basket(job_id: int):
    with closing(get_connection()) as connection:
        from plg_core.lifecycle import ensure_job_allows_new_business
        ensure_job_allows_new_business(connection, job_id, "commit parts")
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

            research_state = str(item["research_state"] or "LEGACY_CANDIDATE").upper()
            if research_state == "RESEARCH_RESULT" or (
                research_state == "LEGACY_CANDIDATE"
                and (
                    candidate_status not in {"VERIFIED", "OVERRIDE"}
                    or (candidate_status == "OVERRIDE" and not candidate_note)
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
                    "Selected work must be an explicit Quote Candidate; legacy "
                    f"items must retain accepted verification metadata: {descriptions}"
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

            # Verification remains historical metadata; Quote Candidate
            # promotion is the authority boundary for current research work.
            committed_verification_status = candidate_verification_status
            committed_verification_source = (
                "Manual Override"
                if candidate_verification_status == "OVERRIDE"
                else item["supplier_name"] or "Basket"
            )

            cursor = connection.execute(
                """
                INSERT INTO job_parts (
                    job_id, requested_description, quantity,
                    oem_part_number, alternate_part_number, oem_description,
                    customer_unit_price,
                    verification_status, verification_source,
                    verification_notes,
                    oem_dealer_name, oem_dealer_price,
                    oem_dealer_availability, source_url,
                    product_url, captured_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        CURRENT_TIMESTAMP)
                """,
                (
                    job_id, item["requested_description"], item["quantity"],
                    oem_number,
                    item["alternate_part_number"] or "",
                    item["requested_description"] if oem_number else "",
                    effective_customer_unit_price(
                        float(item["supplier_unit_cost"] or 0),
                        item["markup_percent"],
                        item["customer_unit_price_override"],
                    ),
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
                confidence,
                selected_for_quote, source_url
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
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
                    item["confidence"],
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


# Batch 2A uses the revision service as the authoritative commit transaction.
# Keeping the compatibility implementation above during this staged foundation
# avoids a risky mechanical rewrite; this final definition is the exported one.
def commit_basket(
    job_id: int,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
):
    from plg_core.revisions.service import commit_work_revision

    return commit_work_revision(
        job_id,
        expected_revision_id=expected_revision_id,
        expected_version=expected_version,
    )
