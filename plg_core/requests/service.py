from __future__ import annotations

import sqlite3

from plg_core.audit import write_audit


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
