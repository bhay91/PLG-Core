from __future__ import annotations

from contextlib import closing
import sqlite3
from typing import Any

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.pricing import effective_customer_unit_price
from plg_core.timeline import log_job_event


STALE_DETAIL = "This work changed in another tab. Reload the current revision before saving."


def _begin_immediate(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")


def _job(connection: sqlite3.Connection, job_id: int):
    row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return row


def _basket(connection: sqlite3.Connection, job_id: int):
    row = connection.execute("SELECT * FROM baskets WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        connection.execute("INSERT INTO baskets (job_id) VALUES (?)", (job_id,))
        row = connection.execute(
            "SELECT * FROM baskets WHERE job_id=?", (job_id,)
        ).fetchone()
    return row


def _active_revision(connection: sqlite3.Connection, job_id: int):
    return connection.execute(
        """
        SELECT wr.* FROM work_revisions wr
        JOIN jobs j ON j.active_work_revision_id = wr.id
        WHERE j.id=? AND wr.job_id=?
        """,
        (job_id, job_id),
    ).fetchone()


def ensure_initial_revision(
    connection: sqlite3.Connection,
    job_id: int,
):
    """Create compatibility metadata for a newly-created pre-revision Job."""
    active = _active_revision(connection, job_id)
    if active is not None:
        return active
    job = _job(connection, job_id)
    basket = _basket(connection, job_id)
    state = "COMMITTED" if str(basket["status"]).upper() == "COMMITTED" else "EDITABLE"
    existing = connection.execute(
        "SELECT * FROM work_revisions WHERE job_id=? ORDER BY revision_number DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO work_revisions (
                job_id, revision_number, state, reason, is_synthetic,
                service_charge, service_charge_description,
                sourcing_fee, sourcing_fee_description, committed_at
            ) VALUES (?, 1, ?, 'Initial work', 1, ?, ?, ?, ?,
                      CASE WHEN ?='COMMITTED' THEN CURRENT_TIMESTAMP END)
            """,
            (
                job_id,
                state,
                float(job["service_charge"] or 0),
                str(job["service_charge_description"] or ""),
                float(job["sourcing_fee"] or 0),
                str(job["sourcing_fee_description"] or ""),
                state,
            ),
        )
        revision_id = int(cursor.lastrowid)
    else:
        revision_id = int(existing["id"])
    connection.execute(
        "UPDATE jobs SET active_work_revision_id=? WHERE id=?",
        (revision_id, job_id),
    )
    return connection.execute(
        "SELECT * FROM work_revisions WHERE id=?", (revision_id,)
    ).fetchone()


def ensure_revision_mutable(
    connection: sqlite3.Connection,
    job_id: int,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
):
    revision = ensure_initial_revision(connection, job_id)
    if str(revision["state"]).upper() != "EDITABLE":
        raise HTTPException(
            status_code=409,
            detail="Committed work is immutable. A governed Work Revision is required.",
        )
    if bool(revision["is_synthetic"]) and connection.execute(
        """
        SELECT 1 FROM (
            SELECT job_id FROM quotes
            UNION ALL SELECT job_id FROM invoices
            UNION ALL SELECT job_id FROM supplier_orders
            UNION ALL SELECT job_id FROM deliveries
        ) durable WHERE durable.job_id=? LIMIT 1
        """,
        (job_id,),
    ).fetchone():
        raise HTTPException(
            status_code=409,
            detail=(
                "Historical work is immutable. Start a governed Work Revision "
                "before making corrections."
            ),
        )
    # Initial legacy-compatible work predates revision tokens. Every governed
    # revision requires both tokens, which prevents old tabs from mutating it.
    require_token = not bool(revision["is_synthetic"])
    if require_token and (expected_revision_id is None or expected_version is None):
        raise HTTPException(status_code=409, detail=STALE_DETAIL)
    if expected_revision_id is not None and int(revision["id"]) != int(expected_revision_id):
        raise HTTPException(status_code=409, detail=STALE_DETAIL)
    if expected_version is not None and int(revision["lock_version"]) != int(expected_version):
        raise HTTPException(status_code=409, detail=STALE_DETAIL)
    return revision


def touch_revision(
    connection: sqlite3.Connection,
    revision_id: int,
    expected_version: int,
) -> int:
    cursor = connection.execute(
        """
        UPDATE work_revisions
        SET lock_version=lock_version+1, updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND state='EDITABLE' AND lock_version=?
        """,
        (revision_id, expected_version),
    )
    if cursor.rowcount != 1:
        raise HTTPException(status_code=409, detail=STALE_DETAIL)
    return expected_version + 1


def _pricing_mode(item) -> str:
    keys = set(item.keys())
    if "pricing_mode" in keys and item["pricing_mode"]:
        return str(item["pricing_mode"]).upper()
    return "OVERRIDE" if item["customer_unit_price_override"] is not None else "AUTO"


def _snapshot_revision(
    connection: sqlite3.Connection,
    revision_id: int,
    basket,
) -> dict[int, int]:
    if connection.execute(
        "SELECT 1 FROM work_revision_items WHERE work_revision_id=? LIMIT 1",
        (revision_id,),
    ).fetchone():
        return {}

    source_map: dict[int, int] = {}
    sources = connection.execute(
        "SELECT * FROM basket_sources WHERE basket_id=? ORDER BY id", (basket["id"],)
    ).fetchall()
    for source in sources:
        cursor = connection.execute(
            """
            INSERT INTO work_revision_sources (
                work_revision_id, source_key, source_name, source_url,
                trust_level, shipping_total, currency, original_basket_source_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id, source["source_key"], source["source_name"],
                source["source_url"], source["trust_level"],
                float(source["shipping_total"] or 0), source["currency"], source["id"],
            ),
        )
        source_map[int(source["id"])] = int(cursor.lastrowid)

    item_map: dict[int, int] = {}
    ad_hoc_source_map: dict[tuple[str, str], int] = {}
    items = connection.execute(
        "SELECT * FROM basket_items WHERE basket_id=? ORDER BY id", (basket["id"],)
    ).fetchall()
    for item in items:
        revision_source_id = (
            source_map.get(int(item["source_id"])) if item["source_id"] else None
        )
        if revision_source_id is None and str(item["supplier_name"] or "").strip():
            source_key = (
                str(item["supplier_name"] or "").strip().lower(),
                str(item["source_url"] or "").strip(),
            )
            revision_source_id = ad_hoc_source_map.get(source_key)
            if revision_source_id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO work_revision_sources (
                        work_revision_id, source_key, source_name, source_url,
                        trust_level, shipping_total, currency
                    ) VALUES (?, 'manual_item', ?, ?, 'MANUAL', 0, ?)
                    """,
                    (
                        revision_id,
                        str(item["supplier_name"] or "").strip(),
                        str(item["source_url"] or "").strip(),
                        basket["currency"] or "USD",
                    ),
                )
                revision_source_id = int(cursor.lastrowid)
                ad_hoc_source_map[source_key] = revision_source_id
        cost = float(item["supplier_unit_cost"] or 0)
        effective = effective_customer_unit_price(
            cost, item["markup_percent"], item["customer_unit_price_override"]
        )
        cursor = connection.execute(
            """
            INSERT INTO work_revision_items (
                work_revision_id, revision_source_id, source_revision_item_id, job_asset_id,
                requested_description, internal_part_number, manufacturer_part_number,
                alternate_part_number, supplier_part_number, supplier_name,
                source_type, brand, quantity, supplier_unit_cost, markup_percent,
                pricing_mode, customer_unit_price_override,
                effective_customer_unit_price, recommended_markup_percent,
                part_status, verification_status, verification_note,
                availability, lead_time, selected, confidence, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                revision_source_id,
                item["origin_work_revision_item_id"],
                item["job_asset_id"],
                item["requested_description"], item["internal_part_number"] or "",
                item["manufacturer_part_number"] or "",
                item["alternate_part_number"] or "", item["supplier_part_number"] or "",
                item["supplier_name"] or "", item["source_type"] or "AFTERMARKET",
                item["brand"] or "", int(item["quantity"]), item["supplier_unit_cost"],
                item["markup_percent"], _pricing_mode(item),
                item["customer_unit_price_override"], effective, item["markup_percent"],
                item["part_status"] or "RESEARCH", item["verification_status"] or "UNVERIFIED",
                item["verification_note"] or "", item["availability"] or "",
                item["lead_time"] or "", int(item["selected"]), item["confidence"],
                item["source_url"] or "",
            ),
        )
        item_map[int(item["id"])] = int(cursor.lastrowid)

    attachments = connection.execute(
        "SELECT * FROM basket_attachments WHERE basket_id=? ORDER BY id", (basket["id"],)
    ).fetchall()
    for attachment in attachments:
        connection.execute(
            """
            INSERT INTO work_revision_attachments (
                work_revision_id, original_basket_attachment_id,
                original_filename, stored_filename, file_path, media_type, source_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id, attachment["id"], attachment["original_filename"],
                attachment["stored_filename"], attachment["file_path"],
                attachment["media_type"] or "", attachment["source_name"] or "",
            ),
        )
    return item_map


def _clear_projection(connection: sqlite3.Connection, basket_id: int) -> None:
    connection.execute("DELETE FROM basket_activity WHERE basket_id=?", (basket_id,))
    connection.execute("DELETE FROM basket_attachments WHERE basket_id=?", (basket_id,))
    connection.execute("DELETE FROM basket_items WHERE basket_id=?", (basket_id,))
    connection.execute("DELETE FROM basket_sources WHERE basket_id=?", (basket_id,))


def _clone_snapshot_to_basket(
    connection: sqlite3.Connection,
    source_revision_id: int,
    basket_id: int,
) -> None:
    source_map: dict[int, int] = {}
    for source in connection.execute(
        "SELECT * FROM work_revision_sources WHERE work_revision_id=? ORDER BY id",
        (source_revision_id,),
    ).fetchall():
        cursor = connection.execute(
            """
            INSERT INTO basket_sources (
                basket_id, source_key, source_name, source_url,
                trust_level, shipping_total, currency
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                basket_id, source["source_key"], source["source_name"], source["source_url"],
                source["trust_level"], source["shipping_total"], source["currency"],
            ),
        )
        source_map[int(source["id"])] = int(cursor.lastrowid)
    for item in connection.execute(
        "SELECT * FROM work_revision_items WHERE work_revision_id=? ORDER BY id",
        (source_revision_id,),
    ).fetchall():
        if str(item["disposition"]).upper() != "ACTIVE":
            continue
        connection.execute(
            """
            INSERT INTO basket_items (
                basket_id, source_id, job_asset_id, origin_work_revision_item_id,
                requested_description, internal_part_number,
                manufacturer_part_number, alternate_part_number,
                supplier_part_number, supplier_name, source_type, brand,
                quantity, supplier_unit_cost, markup_percent,
                customer_unit_price_override, pricing_mode, part_status,
                verification_status, verification_note, availability,
                lead_time, selected, confidence, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                basket_id, source_map.get(int(item["revision_source_id"]))
                if item["revision_source_id"] else None,
                item["job_asset_id"],
                item["id"],
                item["requested_description"], item["internal_part_number"],
                item["manufacturer_part_number"],
                item["alternate_part_number"], item["supplier_part_number"],
                item["supplier_name"], item["source_type"], item["brand"],
                item["quantity"], item["supplier_unit_cost"], item["markup_percent"],
                item["customer_unit_price_override"], item["pricing_mode"], item["part_status"],
                item["verification_status"], item["verification_note"],
                item["availability"], item["lead_time"], item["selected"],
                item["confidence"], item["source_url"],
            ),
        )


def _clone_quote_to_basket(
    connection: sqlite3.Connection,
    quote_id: int,
    basket_id: int,
) -> None:
    for item in connection.execute(
        "SELECT * FROM quote_items WHERE quote_id=? ORDER BY id", (quote_id,)
    ).fetchall():
        # Legacy quote items do not prove whether pricing was automatic or
        # promised, so their exact customer price is conservatively fixed.
        connection.execute(
            """
            INSERT INTO basket_items (
                basket_id, job_asset_id, requested_description, internal_part_number,
                supplier_part_number,
                supplier_name, source_type, brand, quantity,
                supplier_unit_cost, markup_percent,
                customer_unit_price_override, pricing_mode, part_status,
                verification_status, verification_note, selected, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'LEGACY_FIXED', 'QUOTED',
                      'VERIFIED', 'Cloned from historical quote', 1, '')
            """,
            (
                basket_id, item["job_asset_id"], item["description"], item["internal_part_number"],
                item["supplier_part_number"],
                item["supplier_name"], item["source_type"], item["brand"],
                item["quantity"], item["supplier_unit_cost"], item["customer_unit_price"],
            ),
        )


def get_revision_context(job_id: int) -> dict[str, Any]:
    with closing(get_connection()) as connection:
        revision = ensure_initial_revision(connection, job_id)
        connection.commit()
        return dict(revision)


def start_work_revision(
    job_id: int,
    *,
    reason: str,
    based_on_quote_id: int | None = None,
    _source_revision_id: int | None = None,
    _allow_cancelled: bool = False,
) -> dict[str, Any]:
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise HTTPException(status_code=400, detail="Revision reason is required.")
    with closing(get_connection()) as connection:
        try:
            _begin_immediate(connection)
            job = _job(connection, job_id)
            if (
                str(job["status"] or "").upper() == "CANCELLED"
                and not _allow_cancelled
            ):
                raise HTTPException(status_code=409, detail="Reopen the cancelled Job first.")
            existing = connection.execute(
                "SELECT * FROM work_revisions WHERE job_id=? AND state='EDITABLE'",
                (job_id,),
            ).fetchone()
            if existing is not None and not bool(existing["is_synthetic"]):
                requested_quote = int(based_on_quote_id or 0)
                existing_quote = int(existing["based_on_quote_id"] or 0)
                if requested_quote and existing_quote != requested_quote:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This Job already has different editable work. Finish or cancel "
                            "that work before editing this Draft Quote; PPS will not mix them."
                        ),
                    )
                connection.commit()
                return dict(existing)

            quote = None
            if based_on_quote_id is not None:
                quote = connection.execute(
                    "SELECT * FROM quotes WHERE id=? AND job_id=?",
                    (based_on_quote_id, job_id),
                ).fetchone()
                if quote is None:
                    raise HTTPException(status_code=404, detail="Source quote not found for this Job.")
                if connection.execute(
                    "SELECT 1 FROM invoices WHERE quote_id=? OR job_id=? LIMIT 1",
                    (based_on_quote_id, job_id),
                ).fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail="Work revision is blocked because an invoice already exists.",
                    )
            elif connection.execute(
                "SELECT 1 FROM invoices WHERE job_id=? LIMIT 1", (job_id,)
            ).fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="Work revision is blocked because an invoice already exists.",
                )

            parent = ensure_initial_revision(connection, job_id)
            if _source_revision_id is not None:
                requested_parent = connection.execute(
                    "SELECT * FROM work_revisions WHERE id=? AND job_id=?",
                    (_source_revision_id, job_id),
                ).fetchone()
                if requested_parent is None:
                    raise HTTPException(
                        status_code=404, detail="Source Work Revision not found."
                    )
                parent = requested_parent
            basket = _basket(connection, job_id)
            if str(parent["state"]).upper() == "EDITABLE":
                _snapshot_revision(connection, int(parent["id"]), basket)
                connection.execute(
                    "UPDATE work_revisions SET state='COMMITTED', committed_at=CURRENT_TIMESTAMP, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (parent["id"],),
                )
            next_number = int(connection.execute(
                "SELECT COALESCE(MAX(revision_number),0)+1 FROM work_revisions WHERE job_id=?",
                (job_id,),
            ).fetchone()[0])
            cursor = connection.execute(
                """
                INSERT INTO work_revisions (
                    job_id, revision_number, state, parent_revision_id,
                    based_on_quote_id, reason, lock_version, is_synthetic,
                    service_charge, service_charge_description,
                    sourcing_fee, sourcing_fee_description
                ) VALUES (?, ?, 'EDITABLE', ?, ?, ?, 1, 0, ?, ?, ?, ?)
                """,
                (
                    job_id, next_number, parent["id"], based_on_quote_id,
                    normalized_reason, float(job["service_charge"] or 0),
                    str(job["service_charge_description"] or ""),
                    float(job["sourcing_fee"] or 0),
                    str(job["sourcing_fee_description"] or ""),
                ),
            )
            revision_id = int(cursor.lastrowid)
            _clear_projection(connection, int(basket["id"]))
            if quote is not None:
                _clone_quote_to_basket(connection, int(quote["id"]), int(basket["id"]))
            else:
                _clone_snapshot_to_basket(connection, int(parent["id"]), int(basket["id"]))
            connection.execute(
                "UPDATE baskets SET status='OPEN', committed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (basket["id"],),
            )
            connection.execute(
                "UPDATE jobs SET active_work_revision_id=? WHERE id=?", (revision_id, job_id)
            )
            write_audit(
                connection, action="WORK_REVISION_STARTED", entity_type="WORK_REVISION",
                entity_id=revision_id, summary=f"Started Work Revision {next_number}",
                metadata={"job_id": job_id, "based_on_quote_id": based_on_quote_id,
                          "reason": normalized_reason},
            )
            log_job_event(
                connection, job_id=job_id, event_type="WORK_REVISION_STARTED",
                icon="✏️", message=f"Quote changes started: {normalized_reason}",
            )
            connection.commit()
            return dict(connection.execute(
                "SELECT * FROM work_revisions WHERE id=?", (revision_id,)
            ).fetchone())
        except sqlite3.IntegrityError:
            connection.rollback()
            winner = connection.execute(
                "SELECT * FROM work_revisions WHERE job_id=? AND state='EDITABLE'",
                (job_id,),
            ).fetchone()
            if winner is not None:
                return dict(winner)
            raise HTTPException(status_code=409, detail="Revision could not be started safely.")


def clone_work_revision(
    job_id: int,
    source_revision_id: int,
    *,
    reason: str,
) -> dict[str, Any]:
    with closing(get_connection()) as connection:
        source = connection.execute(
            "SELECT * FROM work_revisions WHERE id=? AND job_id=?",
            (source_revision_id, job_id),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="Source Work Revision not found.")
    return start_work_revision(
        job_id, reason=reason, _source_revision_id=source_revision_id
    )


def _validate_selected_items(connection: sqlite3.Connection, basket_id: int):
    items = connection.execute(
        "SELECT * FROM basket_items WHERE basket_id=? AND selected=1 ORDER BY id",
        (basket_id,),
    ).fetchall()
    if not items:
        raise HTTPException(status_code=400, detail="Select at least one basket item.")
    invalid = [item for item in items if str(item["verification_status"] or "UNVERIFIED").upper()
               not in {"VERIFIED", "OVERRIDE"} or
               (str(item["verification_status"] or "").upper() == "OVERRIDE" and
                not str(item["verification_note"] or "").strip())]
    if invalid:
        raise HTTPException(status_code=400, detail="All selected parts must be verified or have a documented manual override before commit.")
    return items


def commit_work_revision(
    job_id: int,
    *,
    expected_revision_id: int | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    with closing(get_connection()) as connection:
        _begin_immediate(connection)
        revision = ensure_initial_revision(connection, job_id)
        if str(revision["state"]).upper() == "COMMITTED":
            connection.commit()
            return {"ok": True, "job_id": job_id, "created_parts": 0,
                    "already_committed": True, "revision_id": int(revision["id"])}
        revision = ensure_revision_mutable(
            connection, job_id, expected_revision_id=expected_revision_id,
            expected_version=expected_version,
        )
        basket = _basket(connection, job_id)
        items = _validate_selected_items(connection, int(basket["id"]))
        item_map = _snapshot_revision(connection, int(revision["id"]), basket)
        created = 0
        for item in items:
            revision_item_id = item_map[int(item["id"])]
            source_type = str(item["source_type"] or "AFTERMARKET").upper()
            cost = float(item["supplier_unit_cost"] or 0)
            price = effective_customer_unit_price(
                cost, item["markup_percent"], item["customer_unit_price_override"]
            )
            part_cursor = connection.execute(
                """
                INSERT INTO job_parts (
                    job_id, job_asset_id, requested_description, internal_part_number,
                    quantity, oem_part_number,
                    alternate_part_number, oem_description, customer_unit_price,
                    verification_status, verification_source, verification_notes,
                    oem_dealer_name, oem_dealer_price, oem_dealer_availability,
                    source_url, product_url, captured_at,
                    work_revision_id, work_revision_item_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'VERIFIED', ?, ?, ?, ?, ?, ?, ?,
                          CURRENT_TIMESTAMP, ?, ?)
                """,
                (
                    job_id, item["job_asset_id"], item["requested_description"],
                    item["internal_part_number"], item["quantity"],
                    item["manufacturer_part_number"] if source_type == "OEM" else "",
                    item["alternate_part_number"] or "",
                    item["requested_description"] if source_type == "OEM" else "", price,
                    "Manual Override" if str(item["verification_status"]).upper() == "OVERRIDE"
                    else item["supplier_name"] or "Basket",
                    item["verification_note"] or "",
                    item["supplier_name"] if source_type == "OEM" else "",
                    item["supplier_unit_cost"] if source_type == "OEM" else None,
                    item["availability"] if source_type == "OEM" else "",
                    item["source_url"], item["source_url"], revision["id"], revision_item_id,
                ),
            )
            part_id = int(part_cursor.lastrowid)
            revision_source_id = connection.execute(
                "SELECT revision_source_id FROM work_revision_items WHERE id=?",
                (revision_item_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO part_sources (
                    part_id, supplier_name, source_type, brand,
                    supplier_part_number, supplier_cost, availability, lead_time,
                    trust_level, verification_status, verification_note,
                    confidence, selected_for_quote, source_url,
                    work_revision_source_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    part_id, item["supplier_name"] or "Basket Source", source_type,
                    item["brand"], item["supplier_part_number"] or item["manufacturer_part_number"],
                    item["supplier_unit_cost"], item["availability"], item["lead_time"],
                    "OEM_VERIFIED" if source_type == "OEM" else "SUPPLIER_VERIFIED",
                    item["verification_status"], item["verification_note"],
                    item["confidence"], item["source_url"], revision_source_id,
                ),
            )
            connection.execute(
                "UPDATE work_revision_items SET generated_job_part_id=? WHERE id=?",
                (part_id, revision_item_id),
            )
            supplier_name = str(item["supplier_name"] or "Basket Source").strip()
            connection.execute(
                "INSERT INTO suppliers (name) SELECT ? WHERE NOT EXISTS "
                "(SELECT 1 FROM suppliers WHERE name=? COLLATE NOCASE)",
                (supplier_name, supplier_name),
            )
            created += 1
        connection.execute(
            "UPDATE work_revisions SET state='COMMITTED', committed_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND state='EDITABLE'",
            (revision["id"],),
        )
        connection.execute(
            "UPDATE baskets SET status='COMMITTED', committed_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (basket["id"],)
        )
        connection.execute("UPDATE jobs SET status='VERIFIED' WHERE id=?", (job_id,))
        connection.execute(
            "INSERT INTO basket_activity (basket_id, activity_type, details) "
            "VALUES (?, 'COMMITTED', ?)", (basket["id"], f"{created} part(s) committed")
        )
        write_audit(
            connection, action="WORK_REVISION_COMMITTED", entity_type="WORK_REVISION",
            entity_id=revision["id"], summary=f"Committed {created} selected part(s)",
            metadata={"job_id": job_id, "revision_number": revision["revision_number"]},
        )
        log_job_event(
            connection, job_id=job_id, event_type="WORK_REVISION_COMMITTED", icon="✅",
            message=f"Updated work saved with {created} part(s)",
        )
        connection.commit()
        return {"ok": True, "job_id": job_id, "created_parts": created,
                "already_committed": False, "revision_id": int(revision["id"])}


def cancel_work_revision(
    revision_id: int,
    *,
    reason: str,
    expected_version: int,
) -> dict[str, Any]:
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise HTTPException(status_code=400, detail="Revision cancellation reason is required.")
    with closing(get_connection()) as connection:
        _begin_immediate(connection)
        revision = connection.execute(
            "SELECT * FROM work_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        if revision is None:
            raise HTTPException(status_code=404, detail="Work Revision not found.")
        if str(revision["state"]).upper() == "CANCELLED":
            connection.commit()
            return dict(revision)
        if str(revision["state"]).upper() != "EDITABLE" or int(revision["lock_version"]) != int(expected_version):
            raise HTTPException(status_code=409, detail=STALE_DETAIL)
        basket = _basket(connection, int(revision["job_id"]))
        _snapshot_revision(connection, revision_id, basket)
        connection.execute(
            "UPDATE work_revisions SET state='CANCELLED', reason=?, cancelled_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND state='EDITABLE' AND lock_version=?",
            (normalized_reason, revision_id, expected_version),
        )
        connection.execute(
            "UPDATE baskets SET status='COMMITTED', committed_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (basket["id"],)
        )
        parent_id = revision["parent_revision_id"]
        if parent_id:
            _clear_projection(connection, int(basket["id"]))
            _clone_snapshot_to_basket(
                connection, int(parent_id), int(basket["id"])
            )
            connection.execute(
                """
                UPDATE jobs
                SET active_work_revision_id=?,
                    service_charge=(
                        SELECT service_charge FROM work_revisions WHERE id=?
                    ),
                    service_charge_description=(
                        SELECT service_charge_description FROM work_revisions WHERE id=?
                    ),
                    sourcing_fee=(
                        SELECT sourcing_fee FROM work_revisions WHERE id=?
                    ),
                    sourcing_fee_description=(
                        SELECT sourcing_fee_description FROM work_revisions WHERE id=?
                    )
                WHERE id=?
                """,
                (
                    parent_id, parent_id, parent_id, parent_id, parent_id,
                    revision["job_id"],
                ),
            )
        write_audit(
            connection, action="WORK_REVISION_CANCELLED", entity_type="WORK_REVISION",
            entity_id=revision_id, summary="Cancelled Work Revision",
            metadata={"job_id": revision["job_id"], "reason": normalized_reason},
        )
        log_job_event(
            connection, job_id=int(revision["job_id"]), event_type="WORK_REVISION_CANCELLED",
            icon="↩️", message=f"Quote changes cancelled: {normalized_reason}",
        )
        connection.commit()
        return dict(connection.execute(
            "SELECT * FROM work_revisions WHERE id=?", (revision_id,)
        ).fetchone())


def revision_diff(revision_id: int) -> dict[str, Any]:
    with closing(get_connection()) as connection:
        revision = connection.execute(
            "SELECT * FROM work_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        if revision is None:
            raise HTTPException(status_code=404, detail="Work Revision not found.")
        if str(revision["state"]).upper() == "EDITABLE":
            current = [dict(row) for row in connection.execute(
                """
                SELECT
                    bi.requested_description, bi.supplier_part_number,
                    bi.supplier_name, bi.quantity, bi.supplier_unit_cost,
                    bi.pricing_mode, bi.customer_unit_price_override, bi.selected
                FROM basket_items bi
                JOIN baskets b ON b.id=bi.basket_id
                JOIN jobs j ON j.id=b.job_id
                WHERE j.active_work_revision_id=?
                ORDER BY bi.id
                """,
                (revision_id,),
            ).fetchall()]
        else:
            current = [dict(row) for row in connection.execute(
                "SELECT * FROM work_revision_items WHERE work_revision_id=? ORDER BY id",
                (revision_id,),
            ).fetchall()]
        parent = []
        if revision["parent_revision_id"]:
            parent = [dict(row) for row in connection.execute(
                "SELECT * FROM work_revision_items WHERE work_revision_id=? ORDER BY id",
                (revision["parent_revision_id"],),
            ).fetchall()]
        def signature(item: dict[str, Any]) -> tuple[Any, ...]:
            return (
                item["requested_description"], item["supplier_part_number"],
                item["supplier_name"], item["quantity"], item["supplier_unit_cost"],
                item["pricing_mode"], item["customer_unit_price_override"], item["selected"],
            )
        current_signatures = [signature(item) for item in current]
        parent_signatures = [signature(item) for item in parent]
        return {
            "revision_id": revision_id,
            "added": [item for item in current if signature(item) not in parent_signatures],
            "removed": [item for item in parent if signature(item) not in current_signatures],
            "unchanged_count": sum(1 for item in current if signature(item) in parent_signatures),
        }
