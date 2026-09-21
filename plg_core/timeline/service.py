from __future__ import annotations

import sqlite3


def log_job_event(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    event_type: str,
    message: str,
    icon: str = "",
) -> int:
    """Write one event to the Job Timeline."""

    normalized_event_type = event_type.strip().upper()
    normalized_message = message.strip()
    normalized_icon = icon.strip()

    if job_id <= 0:
        raise ValueError("job_id must be greater than zero.")

    if not normalized_event_type:
        raise ValueError("event_type is required.")

    if not normalized_message:
        raise ValueError("message is required.")

    cursor = connection.execute(
        """
        INSERT INTO job_timeline (
            job_id,
            event_type,
            icon,
            message
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            job_id,
            normalized_event_type,
            normalized_icon,
            normalized_message,
        ),
    )

    return int(cursor.lastrowid)
