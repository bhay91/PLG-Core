from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from fastapi import HTTPException
from legacy_app import get_connection
from plg_core.audit import write_audit


def _request_row(connection: sqlite3.Connection, request_id: int):
    row = connection.execute("SELECT * FROM customer_requests WHERE id=?", (request_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Customer request not found.")
    return row


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _count(connection: sqlite3.Connection, table: str, where: str, params: tuple) -> int:
    if not _table_exists(connection, table):
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])


def get_request_delete_eligibility(
    request_id: int, *, connection: sqlite3.Connection | None = None
) -> dict:
    """Return a fail-closed permanent-delete preflight for an Inbox record."""
    owned = connection is None
    database = connection or get_connection()
    try:
        record = _request_row(database, request_id)
        blockers: list[str] = []
        field_checks = (
            ("Original customer wording", "request_text"),
            ("Requested part / need wording", "requested_parts"),
            ("Reminder", "reminder_date"),
            ("Customer Registry link", "customer_id"),
            ("Machine / asset Registry link", "machine_id"),
            ("Linked Job", "job_id"),
        )
        for label, column in field_checks:
            if str(record[column] or "").strip():
                blockers.append(label)

        entered_context = any(
            str(record[column] or "").strip()
            for column in (
                "individual_name", "company_name", "phone", "email", "location",
                "manufacturer", "model", "year", "identifier",
            )
        )
        if entered_context:
            blockers.append("Customer or machine intake context")

        relationship_checks = (
            ("Attachment", "customer_request_attachments", "request_id=?"),
            ("Requested Need", "requested_needs", "customer_request_id=?"),
            ("Smart Intake proposal", "intake_proposals", "created_request_id=?"),
            ("Verification / intake evidence", "verification_sessions", "customer_request_id=?"),
            ("External opportunity reference", "opportunities", "customer_request_id=?"),
        )
        for label, table, where in relationship_checks:
            count = _count(database, table, where, (request_id,))
            if count:
                blockers.append(f"{label} ({count})")

        return {
            "can_permanently_delete": not blockers,
            "blockers": blockers,
            "safe_alternative": "Remove this request from the Inbox or cancel it to preserve its history.",
            "request": dict(record),
        }
    finally:
        if owned:
            database.close()


def delete_request_safely(
    request_id: int, reason: str, confirmation: str, *, actor: str = "system",
    audit_request_id: str = "",
) -> dict:
    reason = str(reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Deletion reason is required.")
    with closing(get_connection()) as connection:
        record = _request_row(connection, request_id)
        expected = str(record["request_number"] or "").strip()
        if str(confirmation or "").strip() != expected:
            raise HTTPException(status_code=400, detail=f"Type {expected} to confirm permanent deletion.")
        eligibility = get_request_delete_eligibility(request_id, connection=connection)
        if eligibility["blockers"]:
            raise HTTPException(
                status_code=409,
                detail="Permanent deletion is blocked by " + ", ".join(eligibility["blockers"]) + ".",
            )
        metadata = {
            "former_id": request_id,
            "individual_name": record["individual_name"],
            "company_name": record["company_name"],
            "created_at": record["created_at"],
            "status": record["status"],
        }
        connection.execute(
            "INSERT INTO deletion_tombstones "
            "(entity_type,entity_number,former_entity_id,reason,metadata_json) "
            "VALUES ('REQUEST',?,?,?,?)",
            (expected, request_id, reason, json.dumps(metadata, sort_keys=True)),
        )
        write_audit(
            connection, action="REQUEST_DELETED", entity_type="REQUEST_TOMBSTONE",
            entity_id=expected,
            summary=f"Accidental request {expected} permanently deleted. Reason: {reason}",
            metadata=metadata, actor=actor, request_id=audit_request_id,
        )
        connection.execute("DELETE FROM customer_requests WHERE id=?", (request_id,))
        connection.commit()
        return {"deleted": True, "request_number": expected}


def archive_originating_requests_for_quote(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    quote_id: int,
) -> list[int]:
    """Archive, never delete, Requests represented by a generated quote."""
    rows = connection.execute(
        "SELECT id,request_number FROM customer_requests "
        "WHERE job_id=? AND COALESCE(is_archived,0)=0",
        (job_id,),
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE customer_requests SET is_archived=1,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (row["id"],),
        )
        write_audit(
            connection,
            action="REQUEST_ARCHIVED_AFTER_QUOTE",
            entity_type="REQUEST",
            entity_id=int(row["id"]),
            summary=f"Request {row['request_number']} archived after quote generation",
            metadata={"job_id": job_id, "quote_id": quote_id},
        )
    return [int(row["id"]) for row in rows]
