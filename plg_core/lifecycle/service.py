from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from fastapi import HTTPException

from legacy_app import get_connection
from plg_core.audit import write_audit
from plg_core.timeline import log_job_event


def _require_reason(reason: str, action: str) -> str:
    value = str(reason or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{action} reason is required.")
    return value


def _job(connection: sqlite3.Connection, job_id: int):
    row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return row


def job_has_durable_history(connection: sqlite3.Connection, job_id: int) -> bool:
    checks = (
        ("quotes", "job_id"),
        ("invoices", "job_id"),
        ("customer_transactions", "job_id"),
        ("supplier_orders", "job_id"),
        ("deliveries", "job_id"),
    )
    for table, column in checks:
        if connection.execute(
            f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (job_id,)
        ).fetchone():
            return True
    return False


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _job_delete_count(
    connection: sqlite3.Connection, table: str, where: str, params: tuple
) -> int:
    if not _table_exists(connection, table):
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])


def get_job_delete_eligibility(
    job_id: int, *, connection: sqlite3.Connection | None = None
) -> dict:
    """Describe whether an accidental empty Job can be permanently deleted.

    This is intentionally conservative. Any durable operator, commercial, sourcing,
    or fulfillment evidence makes archive/cancel the safe lifecycle choice.
    """
    owned = connection is None
    database = connection or get_connection()
    try:
        job = _job(database, job_id)
        blockers: list[str] = []

        checks = (
            ("Linked customer request", "customer_requests", "job_id=?"),
            ("Requested Need", "requested_needs", "job_id=?"),
            ("Asset / machine context", "job_assets", "job_id=?"),
            ("Job Part", "job_parts", "job_id=?"),
            ("Verification session", "verification_sessions", "job_id=?"),
            ("Research proposal", "research_capture_proposals", "job_id=?"),
            ("Supplier import", "source_cart_imports", "job_id=?"),
            ("Timeline event", "job_timeline", "job_id=?"),
            ("Follow-up", "job_follow_ups", "job_id=?"),
            ("Quote", "quotes", "job_id=?"),
            ("Invoice", "invoices", "job_id=?"),
            ("Payment / customer transaction", "customer_transactions", "job_id=?"),
            ("Supplier Order", "supplier_orders", "job_id=?"),
            ("Shipment", "consolidated_shipments", "job_id=?"),
            ("Delivery", "deliveries", "job_id=?"),
            ("External opportunity reference", "opportunities", "converted_job_id=?"),
            ("Quote track", "quote_tracks", "job_id=?"),
        )
        for label, table, where in checks:
            count = _job_delete_count(database, table, where, (job_id,))
            if count:
                blockers.append(f"{label} ({count})")

        if _table_exists(database, "work_revisions"):
            meaningful_revisions = int(database.execute(
                """SELECT COUNT(*) FROM work_revisions wr
                     WHERE wr.job_id=? AND (
                       UPPER(COALESCE(wr.state,''))!='EDITABLE'
                       OR wr.based_on_quote_id IS NOT NULL
                       OR EXISTS (SELECT 1 FROM work_revision_items i WHERE i.work_revision_id=wr.id)
                       OR EXISTS (SELECT 1 FROM work_revision_sources s WHERE s.work_revision_id=wr.id)
                       OR EXISTS (SELECT 1 FROM work_revision_attachments a WHERE a.work_revision_id=wr.id)
                     )""",
                (job_id,),
            ).fetchone()[0])
            if meaningful_revisions:
                blockers.append(f"Work revision evidence ({meaningful_revisions})")

        basket = database.execute("SELECT id FROM baskets WHERE job_id=?", (job_id,)).fetchone()
        if basket:
            basket_id = int(basket["id"])
            evidence = sum((
                _job_delete_count(database, "basket_items", "basket_id=? AND (TRIM(COALESCE(requested_description,''))!='' OR supplier_unit_cost IS NOT NULL OR COALESCE(verification_status,'UNVERIFIED')!='UNVERIFIED')", (basket_id,)),
                _job_delete_count(database, "basket_sources", "basket_id=?", (basket_id,)),
                _job_delete_count(database, "basket_attachments", "basket_id=?", (basket_id,)),
            ))
            if evidence:
                blockers.append(f"Basket / research evidence ({evidence})")

        machine_history = _job_delete_count(
            database, "machine_parts_history", "original_job_number=?",
            (str(job["job_number"] or ""),),
        )
        if machine_history:
            blockers.append(f"Machine parts history ({machine_history})")

        return {
            "can_permanently_delete": not blockers,
            "blockers": blockers,
            "safe_alternative": "Archive or cancel this Job to preserve its history.",
            "job": dict(job),
        }
    finally:
        if owned:
            database.close()


def ensure_job_allows_new_business(
    connection: sqlite3.Connection, job_id: int, action: str
) -> None:
    job = _job(connection, job_id)
    if str(job["status"] or "").upper() == "CANCELLED":
        raise HTTPException(
            status_code=409,
            detail=f"Cancelled Job {job['job_number']} cannot {action}. Reopen it first.",
        )


def ensure_job_pre_document_work(
    connection: sqlite3.Connection, job_id: int, action: str
) -> None:
    """Require an active Job whose mutable work has not entered business history."""
    ensure_job_allows_new_business(connection, job_id, action)
    if job_has_durable_history(connection, job_id):
        job = _job(connection, job_id)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job {job['job_number']} has durable quote or downstream history and cannot "
                f"{action}. Governed work revisions are deferred to Lifecycle Batch 2."
            ),
        )


def ensure_part_mutable(connection: sqlite3.Connection, part_id: int) -> sqlite3.Row:
    part = connection.execute(
        "SELECT * FROM job_parts WHERE id=?", (part_id,)
    ).fetchone()
    if part is None:
        raise HTTPException(status_code=404, detail="Part not found.")
    quote = connection.execute(
        "SELECT q.quote_number FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id "
        "WHERE qi.part_id=? LIMIT 1",
        (part_id,),
    ).fetchone()
    if quote:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This part is preserved in {quote['quote_number']} and cannot be changed or "
                "deleted. A governed revision will be available in Lifecycle Batch 2."
            ),
        )
    ensure_job_allows_new_business(connection, int(part["job_id"]), "change parts")
    return part


QUOTE_TRANSITIONS = {
    "DRAFT": {"SENT", "APPROVED", "REJECTED", "REVISION_REQUIRED"},
    "SENT": {"APPROVED", "REJECTED", "REVISION_REQUIRED"},
    "APPROVED": set(),
    "REJECTED": set(),
    "REVISION_REQUIRED": set(),
    "CONVERTED": set(),
}


def transition_quote(
    quote_id: int,
    target_status: str,
    notes: str = "",
    *,
    override_reason: str = "",
):
    target = str(target_status or "").strip().upper()
    with closing(get_connection()) as connection:
        quote = connection.execute(
            "SELECT * FROM quotes WHERE id=?", (quote_id,)
        ).fetchone()
        if quote is None:
            raise HTTPException(status_code=404, detail="Quote not found.")
        old = str(quote["status"] or "DRAFT").strip().upper()
        if old == target:
            return dict(quote)
        if connection.execute(
            "SELECT 1 FROM invoices WHERE quote_id=? LIMIT 1", (quote_id,)
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="Quote decisions are locked because an invoice already exists.",
            )
        revision_lock = connection.execute(
            """
            SELECT 1 FROM work_revisions wr
            WHERE wr.based_on_quote_id=?
              AND (
                wr.state='EDITABLE'
                OR (
                  wr.state='COMMITTED'
                  AND NOT EXISTS (
                    SELECT 1 FROM quotes generated
                    WHERE generated.work_revision_id=wr.id
                  )
                )
              )
            LIMIT 1
            """,
            (quote_id,),
        ).fetchone()
        if revision_lock and not str(override_reason or "").strip():
            raise HTTPException(
                status_code=409,
                detail=(
                    "Quote decisions are locked while changes are in progress. "
                    "Finish or cancel the revision first."
                ),
            )
        override = _require_reason(override_reason, "Quote decision override") if revision_lock else ""
        ensure_job_allows_new_business(connection, int(quote["job_id"]), "change quote status")
        if target not in QUOTE_TRANSITIONS.get(old, set()):
            extra = (
                " Quote content revisions are intentionally deferred to Lifecycle Batch 2."
                if old in {"REJECTED", "REVISION_REQUIRED"}
                else ""
            )
            raise HTTPException(
                status_code=409,
                detail=f"Quote cannot change from {old} to {target}.{extra}",
            )
        event = {
            "SENT": ("QUOTE_SENT", "📤", "QUOTED", "sent to customer"),
            "APPROVED": ("QUOTE_APPROVED", "✅", "CONFIRMED", "approved"),
            "REJECTED": ("QUOTE_REJECTED", "✕", "QUOTED", "rejected"),
            "REVISION_REQUIRED": (
                "QUOTE_REVISION_REQUIRED", "↺", "QUOTED", "requires changes"
            ),
        }[target]
        event_type, icon, job_status, verb = event
        message = str(notes or "").strip() or f"Quote {quote['quote_number']} {verb}"
        connection.execute("UPDATE quotes SET status=? WHERE id=?", (target, quote_id))
        connection.execute("UPDATE jobs SET status=? WHERE id=?", (job_status, quote["job_id"]))
        connection.execute(
            "INSERT INTO quote_events (quote_id,event_type,from_status,to_status,notes) "
            "VALUES (?,?,?,?,?)",
            (quote_id, event_type, old, target, message),
        )
        write_audit(
            connection, action=event_type, entity_type="QUOTE", entity_id=quote_id,
            summary=message, metadata={"from_status": old, "to_status": target,
                                      "job_id": int(quote["job_id"])}
        )
        if override:
            write_audit(
                connection,
                action="QUOTE_DECISION_WORKFLOW_OVERRIDE",
                entity_type="QUOTE",
                entity_id=quote_id,
                summary="Operator overridden the pending work-revision quote lock",
                metadata={"job_id": int(quote["job_id"]), "reason": override},
            )
        log_job_event(connection, job_id=int(quote["job_id"]), event_type=event_type,
                      icon=icon, message=message)
        connection.commit()
        return dict(connection.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone())


def cancel_job(job_id: int, reason: str):
    reason = _require_reason(reason, "Cancellation")
    with closing(get_connection()) as connection:
        job = _job(connection, job_id)
        if str(job["status"] or "").upper() == "CANCELLED":
            return dict(job)
        blocker = connection.execute(
            "SELECT 1 FROM invoices WHERE job_id=? AND UPPER(status)!='VOID' LIMIT 1",
            (job_id,),
        ).fetchone() or connection.execute(
            "SELECT 1 FROM supplier_orders WHERE job_id=? LIMIT 1", (job_id,)
        ).fetchone() or connection.execute(
            "SELECT 1 FROM deliveries WHERE job_id=? LIMIT 1", (job_id,)
        ).fetchone()
        approved = connection.execute(
            "SELECT 1 FROM quotes WHERE job_id=? AND UPPER(status) IN "
            "('APPROVED','ACCEPTED','CONFIRMED','CONVERTED') LIMIT 1", (job_id,)
        ).fetchone()
        if blocker or approved:
            raise HTTPException(
                status_code=409,
                detail="Resolve or void active approval, invoice, purchasing, and delivery obligations before cancelling this Job.",
            )
        previous = str(job["status"] or "REQUESTED").upper()
        connection.execute(
            "UPDATE jobs SET status='CANCELLED', is_archived=1, status_before_cancel=?, "
            "cancellation_reason=?, cancelled_at=CURRENT_TIMESTAMP WHERE id=?",
            (previous, reason, job_id),
        )
        message = f"Job {job['job_number']} cancelled. Reason: {reason}"
        write_audit(connection, action="JOB_CANCELLED", entity_type="JOB", entity_id=job_id,
                    summary=message, metadata={"previous_status": previous, "reason": reason})
        log_job_event(connection, job_id=job_id, event_type="JOB_CANCELLED", icon="⊘", message=message)
        connection.commit()
        return dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def archive_job(job_id: int):
    return _set_archive(job_id, True)


def restore_job(job_id: int):
    return _set_archive(job_id, False)


def _set_archive(job_id: int, archived: bool):
    with closing(get_connection()) as connection:
        job = _job(connection, job_id)
        value = int(archived)
        if int(job["is_archived"] or 0) == value:
            return dict(job)
        connection.execute("UPDATE jobs SET is_archived=? WHERE id=?", (value, job_id))
        action = "JOB_ARCHIVED" if archived else "JOB_RESTORED"
        message = f"Job {job['job_number']} {'archived' if archived else 'restored to active views'}"
        write_audit(connection, action=action, entity_type="JOB", entity_id=job_id, summary=message)
        log_job_event(connection, job_id=job_id, event_type=action, icon="▣", message=message)
        connection.commit()
        return dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def reopen_job(job_id: int, reason: str):
    reason = _require_reason(reason, "Reopen")
    with closing(get_connection()) as connection:
        job = _job(connection, job_id)
        if str(job["status"] or "").upper() != "CANCELLED":
            raise HTTPException(status_code=409, detail="Only a cancelled Job can be reopened.")
        if job_has_durable_history(connection, job_id):
            raise HTTPException(
                status_code=409,
                detail="This Job has issued business history. Governed work/quote reopening is deferred to Lifecycle Batch 2.",
            )
        target = str(job["status_before_cancel"] or "REQUESTED").upper()
        if target in {"CANCELLED", "VOID", "DELIVERED", "COMPLETED", "COMPLETE", "CLOSED"}:
            target = "REQUESTED"
        connection.execute(
            "UPDATE jobs SET status=?, is_archived=0, cancelled_at=NULL, cancellation_reason='' WHERE id=?",
            (target, job_id),
        )
        message = f"Job {job['job_number']} reopened to {target}. Reason: {reason}"
        write_audit(connection, action="JOB_REOPENED", entity_type="JOB", entity_id=job_id,
                    summary=message, metadata={"target_status": target, "reason": reason})
        log_job_event(connection, job_id=job_id, event_type="JOB_REOPENED", icon="↺", message=message)
        connection.commit()
        return dict(connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def delete_job_safely(
    job_id: int, reason: str, confirmation: str, *, actor: str = "system", request_id: str = ""
):
    reason = _require_reason(reason, "Deletion")
    with closing(get_connection()) as connection:
        job = _job(connection, job_id)
        expected = str(job["job_number"] or "").strip()
        if str(confirmation or "").strip() != expected:
            raise HTTPException(status_code=400, detail=f"Type {expected} to confirm permanent deletion.")
        eligibility = get_job_delete_eligibility(job_id, connection=connection)
        blockers = eligibility["blockers"]
        basket = connection.execute("SELECT id FROM baskets WHERE job_id=?", (job_id,)).fetchone()
        if blockers:
            raise HTTPException(
                status_code=409,
                detail="Permanent deletion is blocked by " + ", ".join(blockers) + ". Cancel the Job instead.",
            )
        metadata = {"customer": job["customer"], "created_date": job["created_date"],
                    "status": job["status"], "former_id": job_id}
        connection.execute(
            "INSERT INTO deletion_tombstones (entity_type,entity_number,former_entity_id,reason,metadata_json) "
            "VALUES ('JOB',?,?,?,?)",
            (expected, job_id, reason, json.dumps(metadata, sort_keys=True)),
        )
        write_audit(connection, action="JOB_DELETED", entity_type="JOB_TOMBSTONE",
                    entity_id=expected, summary=f"Accidental Job {expected} permanently deleted. Reason: {reason}",
                    metadata=metadata, actor=actor, request_id=request_id)
        if basket:
            connection.execute("DELETE FROM baskets WHERE id=?", (basket["id"],))
        # Empty EDITABLE revisions are technical workspace shells created when a
        # Job is first opened; eligibility above rejects every meaningful revision.
        connection.execute(
            "UPDATE jobs SET active_work_revision_id=NULL WHERE id=?", (job_id,)
        )
        connection.execute("DELETE FROM work_revisions WHERE job_id=?", (job_id,))
        connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        connection.commit()
        return {"deleted": True, "job_number": expected}
