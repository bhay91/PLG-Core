from __future__ import annotations

from collections.abc import Iterable, Mapping
import sqlite3


def _value(record, key: str, default=None):
    if isinstance(record, Mapping):
        return record.get(key, default)
    try:
        return record[key]
    except (KeyError, TypeError, IndexError):
        return default


def is_freight_item(item) -> bool:
    description = str(_value(item, "description", "") or "").strip().lower()
    return any(
        marker in description
        for marker in ("freight", "freidgt", "inbound shipping")
    )


def custom_invoice_presentation(invoice, custom_invoice, items: Iterable) -> dict:
    """Return the custom-only visible rows and derived customer totals."""
    rows = list(items)
    operator_visible_items = [
        row for row in rows
        if int(_value(row, "is_visible", 1) or 0) == 1
    ]
    include_freight = int(
        _value(custom_invoice, "include_freight", 1) or 0
    ) == 1
    visible_items = [
        row for row in operator_visible_items
        if include_freight or not is_freight_item(row)
    ]
    excluded_items = [row for row in rows if row not in visible_items]
    freight = float(_value(invoice, "shipping_total", 0) or 0)
    freight_item_total = sum(
        float(_value(row, "custom_line_total", 0) or 0)
        for row in rows
        if is_freight_item(row)
    )
    excluded_item_total = sum(
        float(_value(row, "custom_line_total", 0) or 0)
        for row in excluded_items
    )
    baseline_total = float(_value(custom_invoice, "custom_total", 0) or 0)
    invoice_total = max(
        0.0,
        round(
            baseline_total
            - excluded_item_total
            - (0.0 if include_freight else freight),
            2,
        ),
    )
    displayed_freight = freight if include_freight else 0.0
    subtotal = max(0.0, round(invoice_total - displayed_freight, 2))

    return {
        "visible_items": visible_items,
        "excluded_items": excluded_items,
        "include_freight": include_freight,
        "freight": displayed_freight,
        "freight_adjustment_total": round(freight + freight_item_total, 2),
        "subtotal": subtotal,
        "invoice_total": invoice_total,
        # Custom invoices are only available after the source invoice is paid.
        "balance_due": 0.0,
    }


def save_custom_invoice_visibility(
    connection: sqlite3.Connection,
    custom_invoice_id: int,
    items: Iterable,
    visible_item_ids: set[int],
    include_freight: bool,
) -> None:
    """Persist custom-only visibility settings."""
    for item in items:
        connection.execute(
            "UPDATE custom_invoice_items SET is_visible=? WHERE id=?",
            (1 if int(item["id"]) in visible_item_ids else 0, item["id"]),
        )
    connection.execute(
        "UPDATE custom_invoices SET include_freight=? WHERE id=?",
        (1 if include_freight else 0, custom_invoice_id),
    )


def revert_custom_invoice_presentation(
    connection: sqlite3.Connection,
    invoice,
    custom_invoice,
    custom_items: Iterable,
) -> None:
    """Restore the presentation copy from the source invoice snapshots."""
    original_items = {
        row["id"]: row
        for row in connection.execute(
            "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id",
            (invoice["id"],),
        ).fetchall()
    }
    for item in custom_items:
        original = original_items[item["invoice_item_id"]]
        connection.execute(
            """
            UPDATE custom_invoice_items
            SET quantity=?, custom_unit_price=?, custom_line_total=?,
                is_visible=1
            WHERE id=?
            """,
            (
                int(original["quantity"] or 1),
                float(original["customer_unit_price"] or 0),
                float(original["customer_line_total"] or 0),
                item["id"],
            ),
        )
    connection.execute(
        """
        UPDATE custom_invoices
        SET adjustment_mode='MANUAL', adjustment_value=NULL,
            custom_total=?, include_freight=1,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (float(invoice["customer_total"] or 0), custom_invoice["id"]),
    )
