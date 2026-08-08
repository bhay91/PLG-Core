from __future__ import annotations

import re


def normalize_machine_identifier(value: str) -> str:
    """Normalize VIN/PIN/serial values for reliable duplicate matching."""
    return re.sub(
        r"[^a-z0-9]+",
        "",
        str(value or "").strip().lower(),
    )


def find_machine_by_identifier(connection, identifier: str, exclude_machine_id: int | None = None):
    """Find an active machine globally by normalized VIN/PIN/serial."""
    wanted = normalize_machine_identifier(identifier)
    if not wanted:
        return None

    rows = connection.execute(
        """
        SELECT machines.*,
               customers.name AS customer_name,
               customers.customer_number
        FROM machines
        JOIN customers ON customers.id = machines.customer_id
        WHERE machines.active = 1
          AND TRIM(COALESCE(machines.vin_pin_serial, '')) != ''
        ORDER BY machines.id
        """
    ).fetchall()

    for row in rows:
        if exclude_machine_id is not None and row["id"] == exclude_machine_id:
            continue
        if normalize_machine_identifier(row["vin_pin_serial"]) == wanted:
            return row

    return None
