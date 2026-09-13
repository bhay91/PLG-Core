from __future__ import annotations

from contextlib import closing
from datetime import date
import hashlib
from pathlib import Path
import sqlite3

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.timeline import log_job_event
from plg_core.revisions.service import (
    cancel_work_revision,
    commit_work_revision,
    start_work_revision,
)


REVISABLE_ISSUED = {
    "SENT",
    "REJECTED",
    "REVISION_REQUIRED",
    "APPROVED",
}


def _quote(connection, quote_id: int):
    row = connection.execute(
        "SELECT * FROM quotes WHERE id=?", (quote_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Quote not found.")
    return row


def _ensure_no_downstream_history(connection, job_id: int) -> None:
    checks = (
        ("invoices", "SELECT 1 FROM invoices WHERE job_id=? LIMIT 1"),
        ("supplier order", "SELECT 1 FROM supplier_orders WHERE job_id=? LIMIT 1"),
        (
            "receipt",
            "SELECT 1 FROM receiving_events r "
            "JOIN supplier_orders o ON o.id=r.order_id "
            "WHERE o.job_id=? LIMIT 1",
        ),
        ("delivery", "SELECT 1 FROM deliveries WHERE job_id=? LIMIT 1"),
    )
    for label, sql in checks:
        if connection.execute(sql, (job_id,)).fetchone():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This Job has {label} history. Ordinary quote revision "
                    "cannot change downstream business records."
                ),
            )


def start_quote_revision(quote_id: int, reason: str) -> dict:
    reason = str(reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Reason for changes is required.")
    with closing(get_connection()) as connection:
        quote = _quote(connection, quote_id)
        status = str(quote["status"] or "DRAFT").upper()
        if status not in {"DRAFT", *REVISABLE_ISSUED}:
            raise HTTPException(
                status_code=409,
                detail=f"{status.title()} quote cannot be revised.",
            )
        _ensure_no_downstream_history(connection, int(quote["job_id"]))
        if not int(quote["is_current"] or 0):
            raise HTTPException(
                status_code=409,
                detail="This quote is historical. Revise the current quote instead.",
            )
        purpose = "DRAFT_CORRECTION" if status == "DRAFT" else "QUOTE_REVISION"
        job_id = int(quote["job_id"])

    revision = start_work_revision(
        job_id, reason=reason, based_on_quote_id=quote_id
    )
    with closing(get_connection()) as connection:
        connection.execute(
            """
            UPDATE work_revisions
            SET purpose=?, source_quote_status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND state='EDITABLE'
            """,
            (purpose, status, revision["id"]),
        )
        if status == "APPROVED":
            write_audit(
                connection,
                action="APPROVED_QUOTE_REVISION_STARTED",
                entity_type="QUOTE",
                entity_id=quote_id,
                summary=(
                    f"Revision started for approved quote {quote['quote_number']}; "
                    "a revised quote will require new approval"
                ),
                metadata={"reason": reason, "work_revision_id": revision["id"]},
            )
        connection.commit()
        return dict(connection.execute(
            "SELECT * FROM work_revisions WHERE id=?", (revision["id"],)
        ).fetchone())


def _revision_quote_rows(connection, revision_id: int):
    rows = connection.execute(
        """
        SELECT
            jp.id AS part_id, ps.id AS source_id, jp.quantity, jp.job_asset_id,
            jp.internal_part_number,
            jp.requested_description AS description,
            ps.supplier_name, ps.source_type, COALESCE(ps.brand,'') AS brand,
            COALESCE(ps.supplier_part_number,'') AS supplier_part_number,
            COALESCE(ps.supplier_cost,0) AS supplier_unit_cost,
            COALESCE(jp.customer_unit_price,0) AS customer_unit_price,
            wri.pricing_mode, wri.customer_unit_price_override,
            wri.recommended_markup_percent, wri.revision_source_id,
            wri.source_revision_item_id,
            wri.id AS origin_work_revision_item_id,
            wri.primary_requested_need_id,
            wri.research_session_id,wri.research_evidence,wri.research_notes,wri.identified_at,
            COALESCE(a.name,'') AS asset_name_snapshot,
            COALESCE(a.asset_type,'') AS asset_type_snapshot,
            COALESCE(a.manufacturer,'') AS asset_manufacturer_snapshot,
            COALESCE(a.model,'') AS asset_model_snapshot,
            COALESCE(a.year,'') AS asset_year_snapshot,
            COALESCE(a.vin_pin_serial,'') AS asset_serial_snapshot
        FROM job_parts jp
        JOIN part_sources ps
          ON ps.part_id=jp.id AND ps.selected_for_quote=1
        JOIN work_revision_items wri
          ON wri.id=jp.work_revision_item_id
        LEFT JOIN job_assets a ON a.id=jp.job_asset_id
        WHERE jp.work_revision_id=?
        ORDER BY wri.id
        """,
        (revision_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="Revised work has no selected quoted parts.",
        )
    return rows


def _totals(connection, revision, rows):
    parts = round(sum(
        float(row["customer_unit_price"] or 0) * int(row["quantity"] or 1)
        for row in rows
    ), 2)
    supplier_parts = round(sum(
        float(row["supplier_unit_cost"] or 0) * int(row["quantity"] or 1)
        for row in rows
    ), 2)
    source_ids = {
        int(row["revision_source_id"])
        for row in rows if row["revision_source_id"] is not None
    }
    shipping = 0.0
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        shipping = round(sum(float(row[0] or 0) for row in connection.execute(
            f"SELECT shipping_total FROM work_revision_sources "
            f"WHERE id IN ({placeholders})",
            tuple(sorted(source_ids)),
        ).fetchall()), 2)
    service = float(revision["service_charge"] or 0)
    sourcing = float(revision["sourcing_fee"] or 0)
    customer = round(parts + shipping + service + sourcing, 2)
    supplier = round(supplier_parts + shipping, 2)
    return {
        "parts_subtotal": parts,
        "shipping_total": shipping,
        "service_charge": service,
        "sourcing_fee": sourcing,
        "customer_total": customer,
        "supplier_total": supplier,
        "profit_total": round(customer - supplier, 2),
    }


def _insert_quote_items(connection, quote_id: int, rows) -> list[int]:
    inserted_ids = []
    for row in rows:
        quantity = int(row["quantity"] or 1)
        cost = float(row["supplier_unit_cost"] or 0)
        price = float(row["customer_unit_price"] or 0)
        cursor = connection.execute(
            """
            INSERT INTO quote_items (
                quote_id, part_id, source_id, job_asset_id,
                origin_work_revision_item_id,primary_requested_need_id, quantity, description,
                internal_part_number,
                supplier_name, source_type, brand, supplier_part_number,
                supplier_unit_cost, customer_unit_price, supplier_line_total,
                customer_line_total, line_profit, pricing_mode,
                customer_unit_price_override, recommended_markup_percent,
                research_session_id,research_evidence,research_notes,identified_at,
                asset_name_snapshot,asset_type_snapshot,asset_manufacturer_snapshot,
                asset_model_snapshot,asset_year_snapshot,asset_serial_snapshot
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                quote_id, row["part_id"], row["source_id"], row["job_asset_id"],
                row["origin_work_revision_item_id"], row["primary_requested_need_id"], quantity,
                row["description"], row["internal_part_number"] or "",
                row["supplier_name"], row["source_type"],
                row["brand"], row["supplier_part_number"], cost, price,
                round(cost * quantity, 2), round(price * quantity, 2),
                round((price - cost) * quantity, 2),
                row["pricing_mode"], row["customer_unit_price_override"],
                row["recommended_markup_percent"],
                row["research_session_id"], row["research_evidence"],
                row["research_notes"], row["identified_at"],
                row["asset_name_snapshot"], row["asset_type_snapshot"],
                row["asset_manufacturer_snapshot"], row["asset_model_snapshot"],
                row["asset_year_snapshot"], row["asset_serial_snapshot"],
            ),
        )
        inserted_ids.append(int(cursor.lastrowid))
    return inserted_ids


def _write_documents(quote_id: int) -> None:
    from legacy_app import load_quote
    from plg_core.documents.paths import portable_manifest_path
    from plg_core.documents.quote_pdf import DOCUMENT_ROOT, generate_quote_pdfs

    with closing(get_connection()) as connection:
        quote, items = load_quote(connection, quote_id)
        paths = generate_quote_pdfs(quote, items)
        for audience, path in paths.items():
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            connection.execute(
                """
                INSERT INTO quote_documents_manifest (
                    quote_id,audience,document_kind,file_path,sha256,is_issued,
                    version,quote_status,is_current
                ) VALUES (?,?, 'QUOTE',?,?,0,1,?,1)
                ON CONFLICT(quote_id,audience,document_kind,version)
                DO UPDATE SET file_path=excluded.file_path,
                              sha256=excluded.sha256,
                              generated_at=CURRENT_TIMESTAMP,
                              quote_status=excluded.quote_status,
                              is_current=1
                """,
                (
                    quote_id, audience.upper(),
                    portable_manifest_path(path, root=DOCUMENT_ROOT.parent), digest,
                    str(quote["status"] or "").upper(),
                ),
            )
        connection.commit()


def generate_quote_from_revision(
    revision_id: int,
    *,
    expected_version: int,
) -> dict:
    with closing(get_connection()) as connection:
        revision = connection.execute(
            "SELECT * FROM work_revisions WHERE id=?", (revision_id,)
        ).fetchone()
        if revision is None:
            raise HTTPException(status_code=404, detail="Work Revision not found.")
        job_id = int(revision["job_id"])
        source_quote_id = int(revision["based_on_quote_id"] or 0)
        existing = connection.execute(
            "SELECT * FROM quotes WHERE work_revision_id=?",
            (revision_id,),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        if not source_quote_id:
            raise HTTPException(status_code=409, detail="Revision has no source quote.")
        revision_state = str(revision["state"] or "").upper()
        if revision_state not in {"EDITABLE", "COMMITTED"}:
            raise HTTPException(
                status_code=409,
                detail="These changes are no longer editable and cannot generate a quote.",
            )

    if revision_state == "EDITABLE":
        from plg_core.revisions.service import validate_selected_items_for_quote
        with closing(get_connection()) as connection:
            basket = connection.execute(
                "SELECT id FROM baskets WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
            ).fetchone()
            if basket is not None:
                validate_selected_items_for_quote(connection, int(basket["id"]))
        commit_work_revision(
            job_id,
            expected_revision_id=revision_id,
            expected_version=expected_version,
        )

    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM quotes WHERE work_revision_id=?", (revision_id,)
        ).fetchone()
        if existing is not None:
            connection.commit()
            return dict(existing)
        revision = connection.execute(
            "SELECT * FROM work_revisions WHERE id=? AND state='COMMITTED'",
            (revision_id,),
        ).fetchone()
        source = _quote(connection, source_quote_id)
        _ensure_no_downstream_history(connection, job_id)
        rows = _revision_quote_rows(connection, revision_id)
        totals = _totals(connection, revision, rows)
        purpose = str(revision["purpose"] or "QUOTE_REVISION").upper()

        if purpose == "DRAFT_CORRECTION" and str(source["status"] or "").upper() != "DRAFT":
            raise HTTPException(
                status_code=409,
                detail="The source quote is no longer an editable draft.",
            )

        # Quote items are historical records.  A source quote may already have
        # decisions or lineage rows, so deleting and rebuilding them violates
        # the foreign-key graph.  Always preserve the source and project a
        # successor quote from the committed revision snapshot.
        from legacy_app import next_quote_number
        job = connection.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        quote_number = next_quote_number(connection)
        superseded = connection.execute(
            """
            UPDATE quotes
            SET status='SUPERSEDED',is_current=0,
                superseded_at=CURRENT_TIMESTAMP,
                supersession_reason=?
            WHERE id=? AND is_current=1
            """,
            (revision["reason"], source_quote_id),
        )
        if superseded.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="The source quote is no longer current. Refresh the Job.",
            )
        connection.execute(
            """
            INSERT INTO quote_events (
                quote_id,event_type,from_status,to_status,notes
            ) VALUES (?,'QUOTE_SUPERSEDED',?,'SUPERSEDED',?)
            """,
            (
                source_quote_id,
                str(source["status"] or ""),
                (
                    "Approved quote superseded before invoicing: "
                    if str(source["status"] or "").upper() == "APPROVED"
                    else "Quote superseded: "
                ) + str(revision["reason"] or "revision generated"),
            ),
        )
        cursor = connection.execute(
            """
            INSERT INTO quotes (
                quote_number,job_id,quote_date,status,parts_subtotal,
                shipping_total,service_charge,sourcing_fee,customer_total,
                supplier_total,profit_total,work_revision_id,
                supersedes_quote_id,is_current,quote_track_id,commercial_kind,
                bill_to_kind,bill_to_name_snapshot,bill_to_company_snapshot,
                bill_to_address_snapshot,bill_to_phone_snapshot,bill_to_email_snapshot,
                customer_name_snapshot,company_snapshot,phone_snapshot,
                email_snapshot,address_snapshot,manufacturer_snapshot,
                machine_snapshot,pin_serial_snapshot
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                quote_number, job_id, date.today().isoformat(), "DRAFT",
                totals["parts_subtotal"], totals["shipping_total"],
                totals["service_charge"], totals["sourcing_fee"],
                totals["customer_total"], totals["supplier_total"],
                totals["profit_total"], revision_id, source_quote_id, 1,
                source["quote_track_id"], "REVISION",
                source["bill_to_kind"], source["bill_to_name_snapshot"],
                source["bill_to_company_snapshot"], source["bill_to_address_snapshot"],
                source["bill_to_phone_snapshot"], source["bill_to_email_snapshot"],
                job["customer"], job["company"], job["phone"], job["email"],
                job["address"], job["manufacturer"], job["machine"],
                job["pin_serial"],
            ),
        )
        quote_id = int(cursor.lastrowid)
        successor_item_ids = _insert_quote_items(connection, quote_id, rows)
        connection.execute(
            "INSERT OR IGNORE INTO quote_item_decisions(quote_item_id) "
            "SELECT id FROM quote_items WHERE quote_id=?", (quote_id,)
        )
        predecessor_items = connection.execute(
            "SELECT id,part_id,origin_work_revision_item_id,quantity,description,"
            "supplier_part_number,supplier_name "
            "FROM quote_items WHERE quote_id=?",
            (source_quote_id,),
        ).fetchall()
        successor_by_origin = {
            int(row["origin_work_revision_item_id"]): item_id
            for row, item_id in zip(rows, successor_item_ids)
            if row["origin_work_revision_item_id"] is not None
        }
        successor_by_part = {
            int(row["part_id"]): item_id
            for row, item_id in zip(rows, successor_item_ids)
            if row["part_id"] is not None
        }
        successor_by_source = {
            int(row["source_revision_item_id"]): item_id
            for row, item_id in zip(rows, successor_item_ids)
            if row["source_revision_item_id"] is not None
        }
        successor_by_signature = {
            (
                str(row["description"] or "").strip().lower(),
                str(row["supplier_part_number"] or "").strip().lower(),
                str(row["supplier_name"] or "").strip().lower(),
            ): item_id
            for row, item_id in zip(rows, successor_item_ids)
        }
        predecessor_source_items = {
            int(item["id"]): item["source_revision_item_id"]
            for item in connection.execute(
                """
                SELECT qi.id, jp.work_revision_item_id AS source_revision_item_id
                FROM quote_items qi
                LEFT JOIN job_parts jp ON jp.id=qi.part_id
                WHERE qi.quote_id=?
                """,
                (source_quote_id,),
            ).fetchall()
        }
        for predecessor in predecessor_items:
            successor_item_id = successor_by_origin.get(
                predecessor["origin_work_revision_item_id"]
            ) or successor_by_source.get(
                predecessor_source_items.get(predecessor["id"])
            ) or successor_by_part.get(predecessor["part_id"])
            if successor_item_id is None:
                successor_item_id = successor_by_signature.get((
                    str(predecessor["description"] or "").strip().lower(),
                    str(predecessor["supplier_part_number"] or "").strip().lower(),
                    str(predecessor["supplier_name"] or "").strip().lower(),
                ))
            if successor_item_id is not None:
                connection.execute(
                    """
                    INSERT INTO quote_item_lineage(
                        split_id,predecessor_quote_item_id,successor_quote_item_id,
                        successor_quote_id,disposition,quantity
                    ) VALUES (NULL,?,?,?,?,?)
                    """,
                    (
                        predecessor["id"], successor_item_id, quote_id,
                        "MOVED", predecessor["quantity"],
                    ),
                )
        action = (
            "DRAFT_QUOTE_CORRECTED"
            if purpose == "DRAFT_CORRECTION"
            else "REVISED_QUOTE_GENERATED"
        )
        connection.execute(
            "UPDATE jobs SET status='QUOTED' WHERE id=?", (job_id,)
        )
        write_audit(
            connection, action=action, entity_type="QUOTE", entity_id=quote_id,
            summary=(
                f"Quote {source['quote_number']} corrected"
                if quote_id == source_quote_id
                else f"Revised quote generated from {source['quote_number']}"
            ),
            metadata={
                "job_id": job_id,
                "work_revision_id": revision_id,
                "source_quote_id": source_quote_id,
            },
        )
        log_job_event(
            connection, job_id=job_id, event_type=action, icon="📄",
            message=(
                f"Draft {source['quote_number']} updated"
                if quote_id == source_quote_id
                else f"New quote revises {source['quote_number']}"
            ),
        )
        from plg_core.requests.service import archive_originating_requests_for_quote
        archive_originating_requests_for_quote(
            connection, job_id=job_id, quote_id=quote_id
        )
        connection.commit()

    _write_documents(quote_id)
    with closing(get_connection()) as connection:
        return dict(_quote(connection, quote_id))


def cancel_quote_revision(
    revision_id: int,
    *,
    reason: str,
    expected_version: int,
) -> dict:
    return cancel_work_revision(
        revision_id, reason=reason, expected_version=expected_version
    )


def reopen_job_for_revision(job_id: int, reason: str) -> dict:
    reason = str(reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Reopen reason is required.")
    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        existing = connection.execute(
            "SELECT * FROM work_revisions WHERE job_id=? AND state='EDITABLE'",
            (job_id,),
        ).fetchone()
        if str(job["status"] or "").upper() != "CANCELLED":
            if existing is not None:
                return dict(existing)
            raise HTTPException(
                status_code=409, detail="Only a cancelled Job can be reopened."
            )
        _ensure_no_downstream_history(connection, job_id)
        quote = connection.execute(
            """
            SELECT * FROM quotes
            WHERE job_id=? AND is_current=1
              AND status IN ('DRAFT','SENT','REJECTED','REVISION_REQUIRED','APPROVED')
            ORDER BY id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if quote is None:
            raise HTTPException(
                status_code=409,
                detail="No current quote is available to reopen and revise.",
            )
    revision = start_work_revision(
        job_id,
        reason=reason,
        based_on_quote_id=int(quote["id"]),
        _allow_cancelled=True,
    )
    with closing(get_connection()) as connection:
        connection.execute(
            "UPDATE work_revisions SET purpose='REOPEN_REVISION', "
            "source_quote_status=? WHERE id=?",
            (quote["status"], revision["id"]),
        )
        connection.execute(
            "UPDATE jobs SET status='QUOTED',is_archived=0,cancelled_at=NULL,"
            "cancellation_reason='' WHERE id=?",
            (job_id,),
        )
        write_audit(
            connection, action="JOB_REOPENED_FOR_REVISION",
            entity_type="JOB", entity_id=job_id,
            summary=f"Job {job['job_number']} reopened for quote revision",
            metadata={"reason": reason, "work_revision_id": revision["id"]},
        )
        log_job_event(
            connection, job_id=job_id,
            event_type="JOB_REOPENED_FOR_REVISION", icon="↺",
            message=f"Job reopened to revise {quote['quote_number']}: {reason}",
        )
        connection.commit()
    return revision
