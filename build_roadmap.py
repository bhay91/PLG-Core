#!/usr/bin/env python3
"""Generate PPS Core Alpha 12-19 starter modules against the current codebase.

Run from the PLG-Core repository root after placing this file there:
    python3 build_roadmap.py --dry-run
    python3 build_roadmap.py

Safety:
- Existing generated files are skipped unless --force is supplied.
- Existing business tables/data are never deleted.
- Current quote/invoice/customer/machine systems are extended, not replaced.
- plg_core/application.py is patched only inside one marked block.
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_FILE = ROOT / "plg_core" / "application.py"
BEGIN = "# BEGIN PPS ROADMAP ALPHA 12-19"
END = "# END PPS ROADMAP ALPHA 12-19"


def block(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n").rstrip() + "\n"


FILES = {
    "plg_core/roadmap/__init__.py": block('''
        """PPS Alpha 12-19 roadmap extensions."""
    '''),
    "plg_core/roadmap/migrations.py": block('''
        from __future__ import annotations
        from contextlib import closing
        from legacy_app import get_connection

        MIGRATIONS = (
            ("0018_sales_tracking", """
                CREATE TABLE IF NOT EXISTS quote_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quote_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (quote_id) REFERENCES quotes(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS invoice_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_quote_events_quote
                    ON quote_events(quote_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_invoice_events_invoice
                    ON invoice_events(invoice_id, created_at DESC);
            """),
            ("0019_supply_chain", """
                CREATE TABLE IF NOT EXISTS supplier_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    po_number TEXT UNIQUE,
                    job_id INTEGER NOT NULL,
                    invoice_id INTEGER,
                    supplier_id INTEGER,
                    supplier_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'DRAFT',
                    currency TEXT NOT NULL DEFAULT 'USD',
                    parts_total REAL NOT NULL DEFAULT 0,
                    shipping_total REAL NOT NULL DEFAULT 0,
                    order_total REAL NOT NULL DEFAULT 0,
                    ordered_at TEXT,
                    expected_at TEXT,
                    received_at TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id),
                    FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
                );
                CREATE TABLE IF NOT EXISTS supplier_order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    invoice_item_id INTEGER,
                    description TEXT NOT NULL,
                    supplier_part_number TEXT NOT NULL DEFAULT '',
                    quantity_ordered INTEGER NOT NULL DEFAULT 1,
                    quantity_received INTEGER NOT NULL DEFAULT 0,
                    unit_cost REAL NOT NULL DEFAULT 0,
                    line_cost REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (order_id) REFERENCES supplier_orders(id) ON DELETE CASCADE,
                    FOREIGN KEY (invoice_item_id) REFERENCES invoice_items(id)
                );
                CREATE TABLE IF NOT EXISTS receiving_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_number TEXT UNIQUE,
                    order_id INTEGER NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (order_id) REFERENCES supplier_orders(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS receiving_event_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER NOT NULL,
                    order_item_id INTEGER NOT NULL,
                    quantity_received INTEGER NOT NULL,
                    FOREIGN KEY (receipt_id) REFERENCES receiving_events(id) ON DELETE CASCADE,
                    FOREIGN KEY (order_item_id) REFERENCES supplier_order_items(id)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    invoice_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'READY',
                    recipient TEXT NOT NULL DEFAULT '',
                    delivery_date TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
                );
                CREATE TABLE IF NOT EXISTS delivery_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id INTEGER NOT NULL,
                    order_item_id INTEGER NOT NULL,
                    quantity_delivered INTEGER NOT NULL,
                    FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE,
                    FOREIGN KEY (order_item_id) REFERENCES supplier_order_items(id)
                );
                CREATE INDEX IF NOT EXISTS idx_supplier_orders_job ON supplier_orders(job_id);
                CREATE INDEX IF NOT EXISTS idx_supplier_orders_invoice ON supplier_orders(invoice_id);
                CREATE INDEX IF NOT EXISTS idx_supplier_order_items_order ON supplier_order_items(order_id);
                CREATE INDEX IF NOT EXISTS idx_receiving_events_order ON receiving_events(order_id);
                CREATE INDEX IF NOT EXISTS idx_deliveries_job ON deliveries(job_id);
            """),
            ("0020_audit_logs", """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL DEFAULT 'system',
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    request_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_audit_logs_entity
                    ON audit_logs(entity_type, entity_id, created_at DESC);
            """),
            ("0021_api_hardening", """
                CREATE TABLE IF NOT EXISTS api_idempotency_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    operation TEXT NOT NULL DEFAULT '',
                    response_code INTEGER,
                    response_body TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TEXT
                );
            """),
        )

        def run_roadmap_migrations() -> None:
            with closing(get_connection()) as connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        migration_id TEXT PRIMARY KEY,
                        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                applied = {
                    row["migration_id"]
                    for row in connection.execute(
                        "SELECT migration_id FROM schema_migrations"
                    ).fetchall()
                }
                for migration_id, sql in MIGRATIONS:
                    if migration_id in applied:
                        continue
                    connection.executescript(sql)
                    connection.execute(
                        "INSERT INTO schema_migrations (migration_id) VALUES (?)",
                        (migration_id,),
                    )
                connection.commit()
    '''),
    "plg_core/audit/__init__.py": block('''
        from plg_core.audit.service import write_audit
        __all__ = ["write_audit"]
    '''),
    "plg_core/audit/service.py": block('''
        from __future__ import annotations
        import json

        def write_audit(
            connection,
            *,
            action: str,
            entity_type: str,
            entity_id=None,
            summary: str = "",
            metadata: dict | None = None,
            actor: str = "system",
            request_id: str = "",
        ) -> None:
            connection.execute("""
                INSERT INTO audit_logs (
                    actor, action, entity_type, entity_id,
                    summary, metadata_json, request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                actor.strip() or "system",
                action.strip().upper(),
                entity_type.strip().upper(),
                str(entity_id or ""),
                summary.strip(),
                json.dumps(metadata or {}, default=str, sort_keys=True),
                request_id.strip(),
            ))
    '''),
    "plg_core/sales/__init__.py": block('''
        """Alpha 12-13 sales and invoice starter layer."""
    '''),
    "plg_core/sales/models.py": block('''
        from typing import Literal
        from pydantic import BaseModel, Field

        QuoteStatus = Literal[
            "DRAFT", "SENT", "APPROVED", "REJECTED",
            "REVISION_REQUIRED", "CONVERTED"
        ]

        class QuoteStatusUpdate(BaseModel):
            status: QuoteStatus
            notes: str = Field(default="", max_length=1000)

        class ConversionRequest(BaseModel):
            force: bool = False

        class InvoiceVoidRequest(BaseModel):
            reason: str = Field(min_length=1, max_length=1000)

        class InvoicePaymentRequest(BaseModel):
            amount: float = Field(gt=0)
            payment_method: str = Field(min_length=1, max_length=50)
            reference: str = Field(default="", max_length=200)
            payment_date: str = Field(default="", max_length=10)

        class InvoicePaymentReversalRequest(BaseModel):
            amount: float = Field(gt=0)
            reason: str = Field(min_length=1, max_length=1000)
            reversal_date: str = Field(default="", max_length=10)
    '''),
    "plg_core/sales/service.py": block('''
        from __future__ import annotations
        from contextlib import closing
        from fastapi import HTTPException
        from legacy_app import get_connection
        from plg_core.audit import write_audit
        from plg_core.timeline import log_job_event

        def list_quotes(limit: int = 100):
            limit = max(1, min(int(limit), 500))
            with closing(get_connection()) as connection:
                rows = connection.execute("""
                    SELECT q.*, j.job_number, j.customer_id, j.customer, j.company,
                           i.id AS invoice_id, i.invoice_number,
                           i.status AS invoice_status, i.balance_due
                    FROM quotes q
                    JOIN jobs j ON j.id = q.job_id
                    LEFT JOIN invoices i ON i.quote_id = q.id
                    ORDER BY q.id DESC LIMIT ?
                """, (limit,)).fetchall()
            return [dict(row) for row in rows]

        def get_quote(quote_id: int):
            with closing(get_connection()) as connection:
                quote = connection.execute("""
                    SELECT q.*, j.job_number, j.customer_id, j.customer, j.company,
                           i.id AS invoice_id, i.invoice_number,
                           i.status AS invoice_status, i.balance_due
                    FROM quotes q
                    JOIN jobs j ON j.id = q.job_id
                    LEFT JOIN invoices i ON i.quote_id = q.id
                    WHERE q.id = ?
                """, (quote_id,)).fetchone()
                if quote is None:
                    raise HTTPException(status_code=404, detail="Quote not found.")
                items = connection.execute(
                    "SELECT * FROM quote_items WHERE quote_id=? ORDER BY id",
                    (quote_id,),
                ).fetchall()
                events = connection.execute(
                    "SELECT * FROM quote_events WHERE quote_id=? ORDER BY id DESC",
                    (quote_id,),
                ).fetchall()
            result = dict(quote)
            result["items"] = [dict(row) for row in items]
            result["events"] = [dict(row) for row in events]
            return result

        def update_quote_status(quote_id: int, status: str, notes: str = ""):
            allowed = {"DRAFT", "SENT", "APPROVED", "REJECTED", "REVISION_REQUIRED", "CONVERTED"}
            status = status.strip().upper()
            if status not in allowed:
                raise HTTPException(status_code=400, detail="Invalid quote status.")

            decision_rules = {
                "SENT": ("QUOTE_SENT", "📤", "QUOTED", "sent to customer"),
                "APPROVED": ("QUOTE_APPROVED", "✅", "CONFIRMED", "approved"),
                "REVISION_REQUIRED": ("QUOTE_REVISION_REQUIRED", "↺", "QUOTED", "requires revision"),
                "REJECTED": ("QUOTE_REJECTED", "✕", "QUOTED", "rejected"),
            }

            with closing(get_connection()) as connection:
                quote = connection.execute(
                    "SELECT id, quote_number, job_id, status FROM quotes WHERE id=?",
                    (quote_id,),
                ).fetchone()
                if quote is None:
                    raise HTTPException(status_code=404, detail="Quote not found.")

                old = str(quote["status"] or "").strip().upper()
                rule = decision_rules.get(status)

                if rule:
                    event_type, icon, job_status, verb = rule
                    message = f"Quote {quote['quote_number']} {verb}"
                    connection.execute(
                        "UPDATE jobs SET status=? WHERE id=?",
                        (job_status, quote["job_id"]),
                    )
                else:
                    event_type = "STATUS_CHANGED"
                    icon = ""
                    message = (
                        f"Quote {quote['quote_number']} changed "
                        f"from {old or 'UNKNOWN'} to {status}"
                    )

                if old != status:
                    connection.execute(
                        "UPDATE quotes SET status=? WHERE id=?",
                        (status, quote_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO quote_events (
                            quote_id,event_type,from_status,to_status,notes
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (quote_id, event_type, old, status, notes.strip() or message),
                    )
                    write_audit(
                        connection,
                        action=event_type if rule else "QUOTE_STATUS_CHANGED",
                        entity_type="QUOTE",
                        entity_id=quote_id,
                        summary=message,
                        metadata={
                            "notes": notes,
                            "from_status": old,
                            "to_status": status,
                            "job_id": int(quote["job_id"]),
                        },
                    )
                    if rule:
                        log_job_event(
                            connection,
                            job_id=int(quote["job_id"]),
                            event_type=event_type,
                            icon=icon,
                            message=message,
                        )

                connection.commit()

            return get_quote(quote_id)

        def list_invoices(limit: int = 100):
            limit = max(1, min(int(limit), 500))
            with closing(get_connection()) as connection:
                rows = connection.execute("""
                    SELECT i.*, q.quote_number, j.job_number, j.customer_id,
                           j.customer, j.company,
                           COALESCE((
                               SELECT SUM(t.amount)
                               FROM customer_transactions t
                               WHERE t.invoice_id=i.id
                                 AND t.transaction_type IN ('PAYMENT','PAYMENT_REVERSAL')
                           ), 0) AS payments_received
                    FROM invoices i
                    JOIN quotes q ON q.id=i.quote_id
                    JOIN jobs j ON j.id=i.job_id
                    ORDER BY i.id DESC LIMIT ?
                """, (limit,)).fetchall()
            return [dict(row) for row in rows]

        def get_invoice(invoice_id: int):
            with closing(get_connection()) as connection:
                invoice = connection.execute("""
                    SELECT i.*, q.quote_number, j.job_number, j.customer_id,
                           j.customer, j.company
                    FROM invoices i
                    JOIN quotes q ON q.id=i.quote_id
                    JOIN jobs j ON j.id=i.job_id
                    WHERE i.id=?
                """, (invoice_id,)).fetchone()
                if invoice is None:
                    raise HTTPException(status_code=404, detail="Invoice not found.")
                items = connection.execute(
                    "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id",
                    (invoice_id,),
                ).fetchall()
                payments = connection.execute("""
                    SELECT
                        p.*,
                        COALESCE((
                            SELECT -SUM(r.amount)
                            FROM customer_transactions r
                            WHERE r.invoice_id=p.invoice_id
                              AND r.transaction_type='PAYMENT_REVERSAL'
                              AND r.reference='PAYMENT_REVERSAL:' || p.id
                        ), 0) AS reversed_amount,
                        ROUND(
                            p.amount - COALESCE((
                                SELECT -SUM(r.amount)
                                FROM customer_transactions r
                                WHERE r.invoice_id=p.invoice_id
                                  AND r.transaction_type='PAYMENT_REVERSAL'
                                  AND r.reference='PAYMENT_REVERSAL:' || p.id
                            ), 0),
                            2
                        ) AS reversible_amount
                    FROM customer_transactions p
                    WHERE p.invoice_id=?
                      AND p.transaction_type='PAYMENT'
                    ORDER BY p.transaction_date DESC,p.id DESC
                """, (invoice_id,)).fetchall()
                reversals = connection.execute("""
                    SELECT *
                    FROM customer_transactions
                    WHERE invoice_id=?
                      AND transaction_type='PAYMENT_REVERSAL'
                    ORDER BY transaction_date DESC,id DESC
                """, (invoice_id,)).fetchall()
                events = connection.execute(
                    "SELECT * FROM invoice_events WHERE invoice_id=? ORDER BY id DESC",
                    (invoice_id,),
                ).fetchall()
            result = dict(invoice)
            result["items"] = [dict(row) for row in items]
            result["payments"] = [dict(row) for row in payments]
            result["payment_reversals"] = [
                dict(row) for row in reversals
            ]
            result["events"] = [dict(row) for row in events]
            result["payments_received"] = round(
                sum(float(row["amount"] or 0) for row in payments)
                + sum(float(row["amount"] or 0) for row in reversals),
                2,
            )
            return result

        def record_invoice_payment(
            invoice_id: int,
            amount: float,
            payment_method: str,
            reference: str = "",
            payment_date: str = "",
        ):
            amount = round(float(amount or 0), 2)
            payment_method = str(payment_method or "").strip().upper()
            reference = str(reference or "").strip()
            payment_date = (
                str(payment_date or "").strip()
                or __import__("datetime").date.today().isoformat()
            )

            if amount <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Payment amount must be greater than zero.",
                )

            allowed_methods = {
                "CASH",
                "BANK TRANSFER",
                "ZELLE",
                "CARD",
                "CHEQUE",
                "OTHER",
            }

            if payment_method not in allowed_methods:
                raise HTTPException(
                    status_code=400,
                    detail="Select a valid payment method.",
                )

            with closing(get_connection()) as connection:
                invoice = connection.execute(
                    """
                    SELECT i.*, j.customer_id
                    FROM invoices i
                    JOIN jobs j ON j.id = i.job_id
                    WHERE i.id = ?
                    """,
                    (invoice_id,),
                ).fetchone()

                if invoice is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Invoice not found.",
                    )

                previous_status = str(
                    invoice["status"] or ""
                ).strip().upper()

                if previous_status == "VOID":
                    raise HTTPException(
                        status_code=400,
                        detail="A void invoice cannot receive payment.",
                    )

                current_balance = round(
                    float(invoice["balance_due"] or 0),
                    2,
                )

                if current_balance <= 0:
                    return get_invoice(invoice_id)

                if amount > current_balance:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Payment cannot be greater than the "
                            f"current balance of ${current_balance:.2f}."
                        ),
                    )

                new_balance = round(current_balance - amount, 2)
                new_status = "PAID" if new_balance <= 0 else "PARTIAL"

                connection.execute(
                    """
                    INSERT INTO customer_transactions (
                        customer_id,
                        transaction_date,
                        transaction_type,
                        amount,
                        payment_method,
                        reference,
                        reason,
                        job_id,
                        quote_id,
                        invoice_id
                    )
                    VALUES (?, ?, 'PAYMENT', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invoice["customer_id"],
                        payment_date,
                        amount,
                        payment_method,
                        reference,
                        f"Payment received for {invoice['invoice_number']}",
                        invoice["job_id"],
                        invoice["quote_id"],
                        invoice_id,
                    ),
                )

                connection.execute(
                    """
                    UPDATE invoices
                    SET balance_due = ?,
                        status = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        max(new_balance, 0),
                        new_status,
                        invoice_id,
                    ),
                )

                payment_message = (
                    f"${amount:.2f} payment received for "
                    f"{invoice['invoice_number']} via "
                    f"{payment_method.title()}. "
                    f"Balance due ${max(new_balance, 0):.2f}."
                )

                connection.execute(
                    """
                    INSERT INTO invoice_events (
                        invoice_id,
                        event_type,
                        from_status,
                        to_status,
                        notes
                    )
                    VALUES (?, 'PAYMENT_RECEIVED', ?, ?, ?)
                    """,
                    (
                        invoice_id,
                        previous_status,
                        new_status,
                        payment_message,
                    ),
                )

                write_audit(
                    connection,
                    action="PAYMENT_RECEIVED",
                    entity_type="INVOICE",
                    entity_id=invoice_id,
                    summary=payment_message,
                    metadata={
                        "amount": amount,
                        "payment_method": payment_method,
                        "reference": reference,
                        "payment_date": payment_date,
                        "from_status": previous_status,
                        "to_status": new_status,
                        "balance_due": max(new_balance, 0),
                        "job_id": int(invoice["job_id"]),
                    },
                )

                if new_status == "PAID":
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'CONFIRMED'
                        WHERE id = ?
                        """,
                        (invoice["job_id"],),
                    )

                try:
                    log_job_event(
                        connection,
                        job_id=int(invoice["job_id"]),
                        event_type="PAYMENT_RECEIVED",
                        icon="💳",
                        message=(
                            f"${amount:.2f} payment received "
                            f"for {invoice['invoice_number']} "
                            f"via {payment_method.title()}"
                        ),
                    )
                except Exception:
                    pass

                connection.commit()

                if new_status == "PAID":
                    from legacy_app import load_invoice
                    from plg_core.documents.invoice_pdf import (
                        generate_paid_invoice_pdfs,
                    )

                    updated_invoice, updated_items = load_invoice(
                        connection,
                        invoice_id,
                    )

                    generate_paid_invoice_pdfs(
                        updated_invoice,
                        updated_items,
                    )

            return get_invoice(invoice_id)


        def reverse_invoice_payment(
            invoice_id: int,
            payment_id: int,
            amount: float,
            reason: str,
            reversal_date: str = "",
        ):
            amount = round(float(amount or 0), 2)
            reason = str(reason or "").strip()
            reversal_date = (
                str(reversal_date or "").strip()
                or __import__("datetime").date.today().isoformat()
            )

            if amount <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Reversal amount must be greater than zero.",
                )

            if not reason:
                raise HTTPException(
                    status_code=400,
                    detail="Reversal reason is required.",
                )

            with closing(get_connection()) as connection:
                invoice = connection.execute(
                    """
                    SELECT i.*, j.customer_id
                    FROM invoices i
                    JOIN jobs j ON j.id=i.job_id
                    WHERE i.id=?
                    """,
                    (invoice_id,),
                ).fetchone()

                if invoice is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Invoice not found.",
                    )

                previous_status = str(
                    invoice["status"] or ""
                ).strip().upper()

                if previous_status == "VOID":
                    raise HTTPException(
                        status_code=409,
                        detail="A void invoice cannot have payments reversed.",
                    )

                payment = connection.execute(
                    """
                    SELECT *
                    FROM customer_transactions
                    WHERE id=?
                      AND invoice_id=?
                      AND transaction_type='PAYMENT'
                    """,
                    (payment_id, invoice_id),
                ).fetchone()

                if payment is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Payment not found for this invoice.",
                    )

                reversal_reference = (
                    f"PAYMENT_REVERSAL:{payment_id}"
                )

                reversed_row = connection.execute(
                    """
                    SELECT COALESCE(-SUM(amount), 0) AS amount
                    FROM customer_transactions
                    WHERE invoice_id=?
                      AND transaction_type='PAYMENT_REVERSAL'
                      AND reference=?
                    """,
                    (invoice_id, reversal_reference),
                ).fetchone()

                already_reversed = round(
                    float(reversed_row["amount"] or 0),
                    2,
                )

                original_amount = round(
                    float(payment["amount"] or 0),
                    2,
                )

                remaining = round(
                    original_amount - already_reversed,
                    2,
                )

                if remaining <= 0:
                    raise HTTPException(
                        status_code=409,
                        detail="This payment has already been fully reversed.",
                    )

                if amount > remaining:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Reversal cannot exceed the remaining "
                            f"reversible amount of ${remaining:.2f}."
                        ),
                    )

                connection.execute(
                    """
                    INSERT INTO customer_transactions (
                        customer_id,
                        transaction_date,
                        transaction_type,
                        amount,
                        payment_method,
                        reference,
                        reason,
                        job_id,
                        quote_id,
                        invoice_id
                    )
                    VALUES (
                        ?, ?, 'PAYMENT_REVERSAL', ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        invoice["customer_id"],
                        reversal_date,
                        -amount,
                        payment["payment_method"],
                        reversal_reference,
                        (
                            f"Payment reversal/refund for "
                            f"{invoice['invoice_number']}: {reason}"
                        ),
                        invoice["job_id"],
                        invoice["quote_id"],
                        invoice_id,
                    ),
                )

                net_row = connection.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) AS amount
                    FROM customer_transactions
                    WHERE invoice_id=?
                      AND transaction_type IN (
                          'PAYMENT',
                          'PAYMENT_REVERSAL'
                      )
                    """,
                    (invoice_id,),
                ).fetchone()

                net_payments = round(
                    float(net_row["amount"] or 0),
                    2,
                )

                base_due = round(
                    max(
                        float(invoice["customer_total"] or 0)
                        - float(invoice["credit_applied"] or 0),
                        0,
                    ),
                    2,
                )

                new_balance = round(
                    max(base_due - net_payments, 0),
                    2,
                )

                if new_balance <= 0:
                    new_status = "PAID"
                elif (
                    net_payments > 0
                    or float(invoice["credit_applied"] or 0) > 0
                ):
                    new_status = "PARTIAL"
                else:
                    new_status = "UNPAID"

                connection.execute(
                    """
                    UPDATE invoices
                    SET balance_due=?,
                        status=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        new_balance,
                        new_status,
                        invoice_id,
                    ),
                )

                message = (
                    f"${amount:.2f} reversed/refunded from payment "
                    f"#{payment_id} for {invoice['invoice_number']}. "
                    f"Reason: {reason}. "
                    f"Balance due ${new_balance:.2f}."
                )

                connection.execute(
                    """
                    INSERT INTO invoice_events (
                        invoice_id,
                        event_type,
                        from_status,
                        to_status,
                        notes
                    )
                    VALUES (
                        ?, 'PAYMENT_REVERSED', ?, ?, ?
                    )
                    """,
                    (
                        invoice_id,
                        previous_status,
                        new_status,
                        message,
                    ),
                )

                write_audit(
                    connection,
                    action="PAYMENT_REVERSED",
                    entity_type="INVOICE",
                    entity_id=invoice_id,
                    summary=message,
                    metadata={
                        "payment_id": int(payment_id),
                        "amount": amount,
                        "reason": reason,
                        "reversal_date": reversal_date,
                        "from_status": previous_status,
                        "to_status": new_status,
                        "balance_due": new_balance,
                        "job_id": int(invoice["job_id"]),
                    },
                )

                try:
                    log_job_event(
                        connection,
                        job_id=int(invoice["job_id"]),
                        event_type="PAYMENT_REVERSED",
                        icon="↩",
                        message=message,
                    )
                except Exception:
                    pass

                connection.commit()

                from legacy_app import (
                    generate_invoice_pdfs,
                    load_invoice,
                )

                updated_invoice, updated_items = load_invoice(
                    connection,
                    invoice_id,
                )

                generate_invoice_pdfs(
                    updated_invoice,
                    updated_items,
                )

            return get_invoice(invoice_id)


        def void_invoice(invoice_id: int, reason: str):
            reason = str(reason or "").strip()

            if not reason:
                raise HTTPException(
                    status_code=400,
                    detail="Void reason is required.",
                )

            with closing(get_connection()) as connection:
                invoice = connection.execute(
                    """
                    SELECT i.*, j.customer_id
                    FROM invoices i
                    JOIN jobs j
                      ON j.id = i.job_id
                    WHERE i.id = ?
                    """,
                    (invoice_id,),
                ).fetchone()

                if invoice is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Invoice not found.",
                    )

                previous_status = str(
                    invoice["status"] or ""
                ).strip().upper()

                if previous_status == "VOID":
                    return get_invoice(invoice_id)

                payment = connection.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) AS amount
                    FROM customer_transactions
                    WHERE invoice_id = ?
                      AND transaction_type IN (
                          'PAYMENT',
                          'PAYMENT_REVERSAL'
                      )
                    """,
                    (invoice_id,),
                ).fetchone()

                if float(payment["amount"] or 0) > 0.005:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Invoices with recorded customer payments cannot "
                            "be voided. Refund or reverse the payment first."
                        ),
                    )

                supplier_order = connection.execute(
                    """
                    SELECT 1
                    FROM supplier_orders
                    WHERE invoice_id = ?
                    LIMIT 1
                    """,
                    (invoice_id,),
                ).fetchone()

                purchased_part = connection.execute(
                    """
                    SELECT 1
                    FROM basket_items
                    JOIN baskets
                      ON baskets.id = basket_items.basket_id
                    WHERE baskets.job_id = ?
                      AND UPPER(COALESCE(basket_items.part_status, ''))
                          IN ('ORDERED', 'RECEIVED')
                    LIMIT 1
                    """,
                    (invoice["job_id"],),
                ).fetchone()

                if supplier_order is not None or purchased_part is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Invoice cannot be voided because purchasing "
                            "has already started."
                        ),
                    )

                invoice_charge = connection.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) AS amount
                    FROM customer_transactions
                    WHERE invoice_id = ?
                      AND transaction_type = 'INVOICE'
                    """,
                    (invoice_id,),
                ).fetchone()

                reversal_amount = max(
                    -float(invoice_charge["amount"] or 0),
                    0.0,
                )

                if invoice["customer_id"] and reversal_amount > 0:
                    connection.execute(
                        """
                        INSERT INTO customer_transactions (
                            customer_id,
                            transaction_date,
                            transaction_type,
                            amount,
                            reference,
                            reason,
                            job_id,
                            quote_id,
                            invoice_id
                        )
                        VALUES (?, ?, 'INVOICE_VOID', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            invoice["customer_id"],
                            __import__("datetime").date.today().isoformat(),
                            round(reversal_amount, 2),
                            invoice["invoice_number"],
                            f"Void reversal: {reason}",
                            invoice["job_id"],
                            invoice["quote_id"],
                            invoice_id,
                        ),
                    )

                connection.execute(
                    """
                    UPDATE invoices
                    SET status = 'VOID',
                        balance_due = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (invoice_id,),
                )

                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'QUOTED'
                    WHERE id = ?
                    """,
                    (invoice["job_id"],),
                )

                message = (
                    f"Invoice {invoice['invoice_number']} voided. "
                    f"Reason: {reason}"
                )

                connection.execute(
                    """
                    INSERT INTO invoice_events (
                        invoice_id,
                        event_type,
                        from_status,
                        to_status,
                        notes
                    )
                    VALUES (?, 'INVOICE_VOIDED', ?, 'VOID', ?)
                    """,
                    (
                        invoice_id,
                        previous_status,
                        message,
                    ),
                )

                write_audit(
                    connection,
                    action="INVOICE_VOIDED",
                    entity_type="INVOICE",
                    entity_id=invoice_id,
                    summary=message,
                    metadata={
                        "reason": reason,
                        "from_status": previous_status,
                        "to_status": "VOID",
                        "balance_due_before": float(
                            invoice["balance_due"] or 0
                        ),
                        "ledger_reversal": round(
                            reversal_amount,
                            2,
                        ),
                        "job_id": int(invoice["job_id"]),
                        "quote_id": int(invoice["quote_id"]),
                    },
                )

                log_job_event(
                    connection,
                    job_id=int(invoice["job_id"]),
                    event_type="INVOICE_VOIDED",
                    icon="⊘",
                    message=message,
                )

                connection.commit()

                from legacy_app import (
                    generate_invoice_pdfs,
                    load_invoice,
                )

                updated_invoice, updated_items = load_invoice(
                    connection,
                    invoice_id,
                )

                generate_invoice_pdfs(
                    updated_invoice,
                    updated_items,
                )

            return get_invoice(invoice_id)
    '''),
    "plg_core/sales/routes.py": block('''
        from contextlib import closing
        from fastapi import APIRouter, HTTPException
        from legacy_app import get_connection
        from plg_core.sales.models import ConversionRequest, InvoicePaymentRequest, InvoicePaymentReversalRequest, InvoiceVoidRequest, QuoteStatusUpdate
        from plg_core.sales.service import get_invoice, get_quote, list_invoices, list_quotes, record_invoice_payment, reverse_invoice_payment, update_quote_status, void_invoice

        router = APIRouter(prefix="/api/v1/sales", tags=["alpha12-13-sales"])

        @router.get("/quotes")
        def quotes(limit: int = 100):
            return {"items": list_quotes(limit)}

        @router.get("/quotes/{quote_id}")
        def quote_detail(quote_id: int):
            return get_quote(quote_id)

        @router.post("/quotes/{quote_id}/status")
        def quote_status(quote_id: int, payload: QuoteStatusUpdate):
            return update_quote_status(quote_id, payload.status, payload.notes)

        @router.post("/quotes/{quote_id}/convert")
        def convert_quote(quote_id: int, payload: ConversionRequest | None = None):
            quote = get_quote(quote_id)
            if quote.get("invoice_id"):
                return {"created": False, "invoice": get_invoice(int(quote["invoice_id"]))}
            status = str(quote.get("status") or "").upper()
            force = bool(payload.force) if payload else False
            if status not in {"APPROVED", "ACCEPTED", "CONFIRMED"} and not force:
                raise HTTPException(status_code=409, detail="Quote must be approved before API conversion.")
            from legacy_app import convert_quote_to_invoice
            convert_quote_to_invoice(quote_id, force=force)
            with closing(get_connection()) as connection:
                invoice = connection.execute(
                    "SELECT id FROM invoices WHERE quote_id=?",
                    (quote_id,),
                ).fetchone()
                if invoice is None:
                    raise HTTPException(status_code=500, detail="Conversion did not create an invoice.")
            return {"created": True, "invoice": get_invoice(int(invoice["id"]))}

        @router.get("/invoices")
        def invoices(limit: int = 100):
            return {"items": list_invoices(limit)}

        @router.get("/invoices/{invoice_id}")
        def invoice_detail(invoice_id: int):
            return get_invoice(invoice_id)

        @router.post("/invoices/{invoice_id}/payments")
        def invoice_payment(
            invoice_id: int,
            payload: InvoicePaymentRequest,
        ):
            return record_invoice_payment(
                invoice_id=invoice_id,
                amount=payload.amount,
                payment_method=payload.payment_method,
                reference=payload.reference,
                payment_date=payload.payment_date,
            )

        @router.post(
            "/invoices/{invoice_id}/payments/{payment_id}/reverse"
        )
        def invoice_payment_reversal(
            invoice_id: int,
            payment_id: int,
            payload: InvoicePaymentReversalRequest,
        ):
            return reverse_invoice_payment(
                invoice_id=invoice_id,
                payment_id=payment_id,
                amount=payload.amount,
                reason=payload.reason,
                reversal_date=payload.reversal_date,
            )

        @router.post("/invoices/{invoice_id}/void")
        def invoice_void(invoice_id: int, payload: InvoiceVoidRequest):
            return void_invoice(invoice_id, payload.reason)
    '''),
    "plg_core/supply/__init__.py": block('''
        """Alpha 14-15 purchasing, receiving, and delivery."""
    '''),
    "plg_core/supply/models.py": block('''
        from pydantic import BaseModel, Field

        class ReceiptItem(BaseModel):
            order_item_id: int
            quantity_received: int = Field(ge=1)

        class ReceiptCreate(BaseModel):
            items: list[ReceiptItem]
            notes: str = Field(default="", max_length=1000)

        class DeliveryCreate(BaseModel):
            recipient: str = Field(default="", max_length=200)
            notes: str = Field(default="", max_length=1000)

        class SupplierOrderUpdate(BaseModel):
            shipping_total: float = Field(default=0, ge=0)
            expected_at: str = Field(default="", max_length=40)
            notes: str = Field(default="", max_length=1000)

        class SupplierOrderItemCostUpdate(BaseModel):
            unit_cost: float = Field(ge=0)
    '''),
    "plg_core/supply/service.py": block('''
        from contextlib import closing
        from fastapi import HTTPException
        from legacy_app import get_connection
        from plg_core.audit import write_audit
        from plg_core.timeline import log_job_event
        from plg_core.supply.models import ReceiptCreate, DeliveryCreate

        def create_orders_from_paid_invoice(invoice_id: int):
            with closing(get_connection()) as connection:
                invoice = connection.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
                if invoice is None:
                    raise HTTPException(status_code=404, detail="Invoice not found.")
                if str(invoice["status"] or "").upper() != "PAID" or float(invoice["balance_due"] or 0) > 0:
                    raise HTTPException(status_code=409, detail="Supplier orders require a fully paid invoice.")
                existing = connection.execute(
                    "SELECT * FROM supplier_orders WHERE invoice_id=? ORDER BY id",
                    (invoice_id,),
                ).fetchall()
                if existing:
                    return [dict(row) for row in existing]
                items = connection.execute(
                    "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY supplier_name,id",
                    (invoice_id,),
                ).fetchall()
                if not items:
                    raise HTTPException(status_code=409, detail="Invoice has no items to purchase.")
                grouped = {}
                for item in items:
                    name = str(item["supplier_name"] or "").strip() or "Unassigned Supplier"
                    grouped.setdefault(name, []).append(item)
                ids = []
                for supplier_name, supplier_items in grouped.items():
                    supplier = connection.execute(
                        "SELECT id FROM suppliers WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) LIMIT 1",
                        (supplier_name,),
                    ).fetchone()
                    total = round(sum(float(item["supplier_line_total"] or 0) for item in supplier_items), 2)
                    cur = connection.execute("""
                        INSERT INTO supplier_orders (
                            job_id,invoice_id,supplier_id,supplier_name,status,
                            parts_total,shipping_total,order_total,notes
                        ) VALUES (?, ?, ?, ?, 'DRAFT', ?, 0, ?, ?)
                    """, (
                        invoice["job_id"], invoice_id,
                        supplier["id"] if supplier else None,
                        supplier_name, total, total,
                        "Generated from paid invoice; invoice-level shipping is not allocated to supplier POs.",
                    ))
                    order_id = int(cur.lastrowid)
                    ids.append(order_id)
                    po_number = f"PPS-PO-{order_id:04d}"
                    connection.execute("UPDATE supplier_orders SET po_number=? WHERE id=?", (po_number, order_id))
                    for item in supplier_items:
                        qty = max(1, int(item["quantity"] or 1))
                        unit = float(item["supplier_unit_cost"] or 0)
                        connection.execute("""
                            INSERT INTO supplier_order_items (
                                order_id,invoice_item_id,description,supplier_part_number,
                                quantity_ordered,unit_cost,line_cost
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (
                            order_id, item["id"], item["description"],
                            item["supplier_part_number"] or "", qty, unit,
                            round(unit * qty, 2),
                        ))
                    write_audit(
                        connection,
                        action="SUPPLIER_ORDER_CREATED",
                        entity_type="SUPPLIER_ORDER",
                        entity_id=order_id,
                        summary=f"{po_number} created for {supplier_name}",
                        metadata={"invoice_id": invoice_id},
                    )
                    log_job_event(
                        connection,
                        job_id=int(invoice["job_id"]),
                        event_type="SUPPLIER_ORDER_CREATED",
                        icon="📋",
                        message=(
                            f"{po_number} created for {supplier_name}"
                        ),
                    )
                connection.commit()
                rows = connection.execute(
                    f"SELECT * FROM supplier_orders WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id",
                    ids,
                ).fetchall()
            return [dict(row) for row in rows]

        def list_orders(limit: int = 100):
            limit = max(1, min(int(limit), 500))
            with closing(get_connection()) as connection:
                rows = connection.execute("""
                    SELECT po.*, j.job_number, i.invoice_number,
                           (SELECT COUNT(*) FROM supplier_order_items oi WHERE oi.order_id=po.id) AS item_count
                    FROM supplier_orders po
                    JOIN jobs j ON j.id=po.job_id
                    LEFT JOIN invoices i ON i.id=po.invoice_id
                    ORDER BY po.id DESC LIMIT ?
                """, (limit,)).fetchall()
            return [dict(row) for row in rows]

        def get_order(order_id: int):
            with closing(get_connection()) as connection:
                order = connection.execute(
                    """
                    SELECT
                        po.*,
                        j.job_number,
                        j.customer,
                        j.company,
                        i.invoice_number
                    FROM supplier_orders po
                    JOIN jobs j
                      ON j.id=po.job_id
                    LEFT JOIN invoices i
                      ON i.id=po.invoice_id
                    WHERE po.id=?
                    """,
                    (order_id,),
                ).fetchone()
                if order is None:
                    raise HTTPException(status_code=404, detail="Supplier order not found.")
                items = connection.execute(
                    """
                    SELECT
                        oi.*,
                        ii.supplier_unit_cost AS quoted_unit_cost
                    FROM supplier_order_items oi
                    LEFT JOIN invoice_items ii
                      ON ii.id=oi.invoice_item_id
                    WHERE oi.order_id=?
                    ORDER BY oi.id
                    """,
                    (order_id,),
                ).fetchall()
                receipts = connection.execute(
                    """
                    SELECT
                        r.*,
                        COALESCE(
                            SUM(ri.quantity_received),
                            0
                        ) AS quantity_received
                    FROM receiving_events r
                    LEFT JOIN receiving_event_items ri
                      ON ri.receipt_id=r.id
                    WHERE r.order_id=?
                    GROUP BY r.id
                    ORDER BY r.id DESC
                    """,
                    (order_id,),
                ).fetchall()
            result = dict(order)
            result["items"] = [dict(row) for row in items]
            result["receipts"] = [dict(row) for row in receipts]
            return result

        def update_order_item_cost(
            order_id: int,
            item_id: int,
            unit_cost: float,
        ):
            unit_cost = round(
                float(unit_cost or 0),
                2,
            )

            if unit_cost < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Supplier unit cost cannot be negative.",
                )

            with closing(get_connection()) as connection:
                order = connection.execute(
                    """
                    SELECT *
                    FROM supplier_orders
                    WHERE id=?
                    """,
                    (order_id,),
                ).fetchone()

                if order is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Supplier order not found.",
                    )

                status = str(
                    order["status"] or ""
                ).strip().upper()

                if status != "DRAFT":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Supplier costs are locked once "
                            "the purchase order is placed."
                        ),
                    )

                item = connection.execute(
                    """
                    SELECT *
                    FROM supplier_order_items
                    WHERE id=?
                      AND order_id=?
                    """,
                    (item_id, order_id),
                ).fetchone()

                if item is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Supplier order item not found.",
                    )

                old_unit_cost = round(
                    float(item["unit_cost"] or 0),
                    2,
                )

                quantity = max(
                    1,
                    int(item["quantity_ordered"] or 1),
                )

                line_cost = round(
                    unit_cost * quantity,
                    2,
                )

                connection.execute(
                    """
                    UPDATE supplier_order_items
                    SET unit_cost=?,
                        line_cost=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        unit_cost,
                        line_cost,
                        item_id,
                    ),
                )

                totals = connection.execute(
                    """
                    SELECT COALESCE(SUM(line_cost), 0) AS parts_total
                    FROM supplier_order_items
                    WHERE order_id=?
                    """,
                    (order_id,),
                ).fetchone()

                parts_total = round(
                    float(totals["parts_total"] or 0),
                    2,
                )

                shipping_total = round(
                    float(order["shipping_total"] or 0),
                    2,
                )

                order_total = round(
                    parts_total + shipping_total,
                    2,
                )

                connection.execute(
                    """
                    UPDATE supplier_orders
                    SET parts_total=?,
                        order_total=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        parts_total,
                        order_total,
                        order_id,
                    ),
                )

                difference = round(
                    unit_cost - old_unit_cost,
                    2,
                )

                message = (
                    f"{order['po_number']} supplier cost updated: "
                    f"{item['description']} "
                    f"${old_unit_cost:.2f} → ${unit_cost:.2f} "
                    f"per unit. PO total ${order_total:.2f}."
                )

                write_audit(
                    connection,
                    action="SUPPLIER_ORDER_COST_UPDATED",
                    entity_type="SUPPLIER_ORDER",
                    entity_id=order_id,
                    summary=message,
                    metadata={
                        "job_id": int(order["job_id"]),
                        "invoice_id": order["invoice_id"],
                        "order_item_id": int(item_id),
                        "invoice_item_id": item["invoice_item_id"],
                        "quantity": quantity,
                        "old_unit_cost": old_unit_cost,
                        "new_unit_cost": unit_cost,
                        "unit_cost_variance": difference,
                        "line_cost": line_cost,
                        "parts_total": parts_total,
                        "order_total": order_total,
                    },
                )

                log_job_event(
                    connection,
                    job_id=int(order["job_id"]),
                    event_type="SUPPLIER_ORDER_COST_UPDATED",
                    icon="💲",
                    message=message,
                )

                connection.commit()

            return get_order(order_id)


        def update_order(
            order_id: int,
            shipping_total: float = 0,
            expected_at: str = "",
            notes: str = "",
        ):
            shipping_total = round(
                float(shipping_total or 0),
                2,
            )
            expected_at = str(expected_at or "").strip()
            notes = str(notes or "").strip()

            if shipping_total < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Shipping total cannot be negative.",
                )

            with closing(get_connection()) as connection:
                order = connection.execute(
                    """
                    SELECT *
                    FROM supplier_orders
                    WHERE id=?
                    """,
                    (order_id,),
                ).fetchone()

                if order is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Supplier order not found.",
                    )

                status = str(
                    order["status"] or ""
                ).strip().upper()

                if status != "DRAFT":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Only a draft supplier order can be edited."
                        ),
                    )

                parts_total = round(
                    float(order["parts_total"] or 0),
                    2,
                )

                order_total = round(
                    parts_total + shipping_total,
                    2,
                )

                connection.execute(
                    """
                    UPDATE supplier_orders
                    SET shipping_total=?,
                        order_total=?,
                        expected_at=?,
                        notes=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        shipping_total,
                        order_total,
                        expected_at or None,
                        notes,
                        order_id,
                    ),
                )

                message = (
                    f"{order['po_number']} updated for "
                    f"{order['supplier_name']}. "
                    f"Order total ${order_total:.2f}."
                )

                write_audit(
                    connection,
                    action="SUPPLIER_ORDER_UPDATED",
                    entity_type="SUPPLIER_ORDER",
                    entity_id=order_id,
                    summary=message,
                    metadata={
                        "job_id": int(order["job_id"]),
                        "invoice_id": order["invoice_id"],
                        "shipping_total": shipping_total,
                        "expected_at": expected_at,
                        "order_total": order_total,
                    },
                )

                log_job_event(
                    connection,
                    job_id=int(order["job_id"]),
                    event_type="SUPPLIER_ORDER_UPDATED",
                    icon="✎",
                    message=message,
                )

                connection.commit()

            return get_order(order_id)


        def place_order(order_id: int):
            with closing(get_connection()) as connection:
                order = connection.execute(
                    """
                    SELECT po.*, i.invoice_number
                    FROM supplier_orders po
                    LEFT JOIN invoices i
                      ON i.id=po.invoice_id
                    WHERE po.id=?
                    """,
                    (order_id,),
                ).fetchone()

                if order is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Supplier order not found.",
                    )

                current_status = str(
                    order["status"] or ""
                ).strip().upper()

                if current_status == "ORDERED":
                    return get_order(order_id)

                if current_status != "DRAFT":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Only a draft supplier order can be "
                            "marked ordered."
                        ),
                    )

                connection.execute(
                    """
                    UPDATE supplier_orders
                    SET status='ORDERED',
                        ordered_at=COALESCE(
                            ordered_at,
                            CURRENT_TIMESTAMP
                        ),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (order_id,),
                )

                write_audit(
                    connection,
                    action="SUPPLIER_ORDER_PLACED",
                    entity_type="SUPPLIER_ORDER",
                    entity_id=order_id,
                    summary=f"{order['po_number']} marked ordered",
                    metadata={
                        "invoice_id": order["invoice_id"],
                        "job_id": int(order["job_id"]),
                    },
                )

                log_job_event(
                    connection,
                    job_id=int(order["job_id"]),
                    event_type="SUPPLIER_ORDER_PLACED",
                    icon="🛒",
                    message=(
                        f"{order['po_number']} placed with "
                        f"{order['supplier_name']}"
                    ),
                )

                if order["invoice_id"]:
                    remaining = connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM supplier_orders
                        WHERE invoice_id=?
                          AND UPPER(
                              COALESCE(status, 'DRAFT')
                          )='DRAFT'
                        """,
                        (order["invoice_id"],),
                    ).fetchone()[0]

                    if int(remaining or 0) == 0:
                        connection.execute(
                            """
                            UPDATE jobs
                            SET status='ORDERED'
                            WHERE id=?
                            """,
                            (order["job_id"],),
                        )

                        log_job_event(
                            connection,
                            job_id=int(order["job_id"]),
                            event_type="PURCHASING_STARTED",
                            icon="✅",
                            message=(
                                "All supplier orders placed for "
                                f"{order['invoice_number'] or 'invoice'}"
                            ),
                        )

                connection.commit()

            return get_order(order_id)

        def record_receipt(order_id: int, payload: ReceiptCreate):
            if not payload.items:
                raise HTTPException(
                    status_code=400,
                    detail="Receipt requires at least one item.",
                )

            incoming_ids = [
                int(item.order_item_id)
                for item in payload.items
            ]

            if len(set(incoming_ids)) != len(incoming_ids):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Each supplier order item may appear only "
                        "once on a receipt."
                    ),
                )

            with closing(get_connection()) as connection:
                order = connection.execute(
                    """
                    SELECT *
                    FROM supplier_orders
                    WHERE id=?
                    """,
                    (order_id,),
                ).fetchone()

                if order is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Supplier order not found.",
                    )

                current_status = str(
                    order["status"] or ""
                ).strip().upper()

                if current_status == "DRAFT":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Parts cannot be received until the "
                            "supplier order has been placed."
                        ),
                    )

                if current_status == "RECEIVED":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This supplier order is already fully received."
                        ),
                    )

                if current_status not in {
                    "ORDERED",
                    "PARTIAL",
                }:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Supplier order is not in a receivable status."
                        ),
                    )

                validated = []

                for incoming in payload.items:
                    item = connection.execute(
                        """
                        SELECT *
                        FROM supplier_order_items
                        WHERE id=?
                          AND order_id=?
                        """,
                        (
                            incoming.order_item_id,
                            order_id,
                        ),
                    ).fetchone()

                    if item is None:
                        raise HTTPException(
                            status_code=404,
                            detail=(
                                f"Order item "
                                f"{incoming.order_item_id} not found."
                            ),
                        )

                    ordered = int(
                        item["quantity_ordered"] or 0
                    )
                    already_received = int(
                        item["quantity_received"] or 0
                    )
                    remaining = (
                        ordered - already_received
                    )

                    if remaining <= 0:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"{item['description']} is already "
                                "fully received."
                            ),
                        )

                    quantity = int(
                        incoming.quantity_received
                    )

                    if quantity > remaining:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"Only {remaining} remain for "
                                f"{item['description']}."
                            ),
                        )

                    validated.append(
                        (
                            int(item["id"]),
                            quantity,
                            str(item["description"] or "Part"),
                        )
                    )

                cur = connection.execute(
                    """
                    INSERT INTO receiving_events (
                        order_id,
                        notes
                    )
                    VALUES (?, ?)
                    """,
                    (
                        order_id,
                        payload.notes.strip(),
                    ),
                )

                receipt_id = int(cur.lastrowid)
                receipt_number = (
                    f"PPS-RCPT-{receipt_id:04d}"
                )

                connection.execute(
                    """
                    UPDATE receiving_events
                    SET receipt_number=?
                    WHERE id=?
                    """,
                    (
                        receipt_number,
                        receipt_id,
                    ),
                )

                total_received = 0

                for item_id, quantity, _description in validated:
                    connection.execute(
                        """
                        INSERT INTO receiving_event_items (
                            receipt_id,
                            order_item_id,
                            quantity_received
                        )
                        VALUES (?, ?, ?)
                        """,
                        (
                            receipt_id,
                            item_id,
                            quantity,
                        ),
                    )

                    connection.execute(
                        """
                        UPDATE supplier_order_items
                        SET quantity_received=
                                quantity_received+?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (
                            quantity,
                            item_id,
                        ),
                    )

                    total_received += quantity

                remaining_rows = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM supplier_order_items
                    WHERE order_id=?
                      AND quantity_received < quantity_ordered
                    """,
                    (order_id,),
                ).fetchone()[0]

                new_status = (
                    "RECEIVED"
                    if int(remaining_rows or 0) == 0
                    else "PARTIAL"
                )

                connection.execute(
                    """
                    UPDATE supplier_orders
                    SET status=?,
                        received_at=
                            CASE
                                WHEN ?='RECEIVED'
                                THEN COALESCE(
                                    received_at,
                                    CURRENT_TIMESTAMP
                                )
                                ELSE received_at
                            END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        new_status,
                        new_status,
                        order_id,
                    ),
                )

                summary = (
                    f"{receipt_number}: "
                    f"{total_received} "
                    f"part{'s' if total_received != 1 else ''} "
                    f"received from {order['supplier_name']}. "
                    f"PO status {new_status}."
                )

                write_audit(
                    connection,
                    action="PARTS_RECEIVED",
                    entity_type="SUPPLIER_ORDER",
                    entity_id=order_id,
                    summary=summary,
                    metadata={
                        "receipt_id": receipt_id,
                        "receipt_number": receipt_number,
                        "quantity_received": total_received,
                        "status": new_status,
                        "job_id": int(order["job_id"]),
                        "invoice_id": order["invoice_id"],
                    },
                )

                log_job_event(
                    connection,
                    job_id=int(order["job_id"]),
                    event_type="PARTS_RECEIVED",
                    icon="📦",
                    message=summary,
                )

                outstanding_orders = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM supplier_orders
                    WHERE job_id=?
                      AND UPPER(
                            COALESCE(status, 'DRAFT')
                          ) != 'RECEIVED'
                    """,
                    (order["job_id"],),
                ).fetchone()[0]

                if int(outstanding_orders or 0) == 0:
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status='RECEIVED'
                        WHERE id=?
                        """,
                        (order["job_id"],),
                    )

                    log_job_event(
                        connection,
                        job_id=int(order["job_id"]),
                        event_type="RECEIVING_COMPLETE",
                        icon="✅",
                        message=(
                            "All supplier purchase orders have "
                            "been received."
                        ),
                    )

                connection.commit()

            return get_order(order_id)


        def get_delivery(delivery_id: int):
            with closing(get_connection()) as connection:
                delivery = connection.execute(
                    """
                    SELECT
                        d.*,
                        j.job_number,
                        j.customer,
                        j.company,
                        i.invoice_number
                    FROM deliveries d
                    JOIN jobs j
                      ON j.id=d.job_id
                    LEFT JOIN invoices i
                      ON i.id=d.invoice_id
                    WHERE d.id=?
                    """,
                    (delivery_id,),
                ).fetchone()

                if delivery is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Delivery not found.",
                    )

                items = connection.execute(
                    """
                    SELECT
                        di.*,
                        oi.description,
                        oi.supplier_part_number,
                        po.po_number,
                        po.supplier_name
                    FROM delivery_items di
                    JOIN supplier_order_items oi
                      ON oi.id=di.order_item_id
                    JOIN supplier_orders po
                      ON po.id=oi.order_id
                    WHERE di.delivery_id=?
                    ORDER BY di.id
                    """,
                    (delivery_id,),
                ).fetchall()

            result = dict(delivery)
            result["items"] = [dict(row) for row in items]
            result["quantity_total"] = sum(
                int(row["quantity_delivered"] or 0)
                for row in items
            )
            return result


        def get_delivery_workspace(job_id: int):
            with closing(get_connection()) as connection:
                job = connection.execute(
                    """
                    SELECT *
                    FROM jobs
                    WHERE id=?
                    """,
                    (job_id,),
                ).fetchone()

                if job is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Job not found.",
                    )

                items = connection.execute(
                    """
                    SELECT
                        oi.id,
                        oi.description,
                        oi.supplier_part_number,
                        oi.quantity_ordered,
                        oi.quantity_received,
                        po.po_number,
                        po.supplier_name,
                        COALESCE((
                            SELECT SUM(di.quantity_delivered)
                            FROM delivery_items di
                            JOIN deliveries d
                              ON d.id=di.delivery_id
                            WHERE di.order_item_id=oi.id
                              AND d.status='DELIVERED'
                        ),0) AS quantity_delivered,
                        COALESCE((
                            SELECT SUM(di.quantity_delivered)
                            FROM delivery_items di
                            JOIN deliveries d
                              ON d.id=di.delivery_id
                            WHERE di.order_item_id=oi.id
                              AND d.status='READY'
                        ),0) AS quantity_reserved
                    FROM supplier_order_items oi
                    JOIN supplier_orders po
                      ON po.id=oi.order_id
                    WHERE po.job_id=?
                    ORDER BY po.id, oi.id
                    """,
                    (job_id,),
                ).fetchall()

                deliveries = connection.execute(
                    """
                    SELECT
                        d.*,
                        COALESCE(
                            SUM(di.quantity_delivered),
                            0
                        ) AS quantity_total
                    FROM deliveries d
                    LEFT JOIN delivery_items di
                      ON di.delivery_id=d.id
                    WHERE d.job_id=?
                    GROUP BY d.id
                    ORDER BY d.id DESC
                    """,
                    (job_id,),
                ).fetchall()

                ready = connection.execute(
                    """
                    SELECT id
                    FROM deliveries
                    WHERE job_id=?
                      AND status='READY'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()

            item_rows = []

            for row in items:
                item = dict(row)

                item["available_to_deliver"] = max(
                    int(item["quantity_received"] or 0)
                    - int(item["quantity_delivered"] or 0)
                    - int(item["quantity_reserved"] or 0),
                    0,
                )

                item_rows.append(item)

            return {
                "job": dict(job),
                "items": item_rows,
                "deliveries": [
                    dict(row)
                    for row in deliveries
                ],
                "ready_delivery": (
                    get_delivery(int(ready["id"]))
                    if ready
                    else None
                ),
                "has_available": any(
                    int(item["available_to_deliver"]) > 0
                    for item in item_rows
                ),
            }


        def create_delivery(job_id: int, payload: DeliveryCreate):
            with closing(get_connection()) as connection:
                job = connection.execute(
                    """
                    SELECT id, job_number, customer, status
                    FROM jobs
                    WHERE id=?
                    """,
                    (job_id,),
                ).fetchone()

                if job is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Job not found.",
                    )

                existing = connection.execute(
                    """
                    SELECT id
                    FROM deliveries
                    WHERE job_id=?
                      AND status='READY'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()

                if existing:
                    result = get_delivery(
                        int(existing["id"])
                    )
                    result["existing"] = True
                    return result

                available = connection.execute(
                    """
                    SELECT
                        oi.id,
                        oi.description,
                        oi.quantity_received,
                        COALESCE((
                            SELECT SUM(di.quantity_delivered)
                            FROM delivery_items di
                            JOIN deliveries d
                              ON d.id=di.delivery_id
                            WHERE di.order_item_id=oi.id
                              AND d.status!='CANCELLED'
                        ),0) AS committed
                    FROM supplier_order_items oi
                    JOIN supplier_orders po
                      ON po.id=oi.order_id
                    WHERE po.job_id=?
                      AND oi.quantity_received>0
                    ORDER BY po.id, oi.id
                    """,
                    (job_id,),
                ).fetchall()

                deliverable = []

                for row in available:
                    quantity = (
                        int(row["quantity_received"] or 0)
                        - int(row["committed"] or 0)
                    )

                    if quantity > 0:
                        deliverable.append(
                            (
                                int(row["id"]),
                                quantity,
                            )
                        )

                if not deliverable:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "No received parts are ready "
                            "for customer delivery."
                        ),
                    )

                invoice = connection.execute(
                    """
                    SELECT id
                    FROM invoices
                    WHERE job_id=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (job_id,),
                ).fetchone()

                recipient = (
                    payload.recipient.strip()
                    or str(job["customer"] or "").strip()
                    or "Customer"
                )

                cursor = connection.execute(
                    """
                    INSERT INTO deliveries (
                        job_id,
                        invoice_id,
                        recipient,
                        notes
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        invoice["id"] if invoice else None,
                        recipient,
                        payload.notes.strip(),
                    ),
                )

                delivery_id = int(cursor.lastrowid)

                quantity_total = 0

                for order_item_id, quantity in deliverable:
                    connection.execute(
                        """
                        INSERT INTO delivery_items (
                            delivery_id,
                            order_item_id,
                            quantity_delivered
                        )
                        VALUES (?, ?, ?)
                        """,
                        (
                            delivery_id,
                            order_item_id,
                            quantity,
                        ),
                    )

                    quantity_total += quantity

                message = (
                    f"Delivery #{delivery_id} prepared for "
                    f"{recipient}: {quantity_total} "
                    f"part{'s' if quantity_total != 1 else ''}."
                )

                write_audit(
                    connection,
                    action="DELIVERY_CREATED",
                    entity_type="DELIVERY",
                    entity_id=delivery_id,
                    summary=message,
                    metadata={
                        "job_id": job_id,
                        "quantity": quantity_total,
                        "recipient": recipient,
                    },
                )

                log_job_event(
                    connection,
                    job_id=job_id,
                    event_type="DELIVERY_CREATED",
                    icon="🚚",
                    message=message,
                )

                connection.commit()

            result = get_delivery(delivery_id)
            result["existing"] = False
            return result


        def complete_delivery(delivery_id: int):
            with closing(get_connection()) as connection:
                delivery = connection.execute(
                    """
                    SELECT *
                    FROM deliveries
                    WHERE id=?
                    """,
                    (delivery_id,),
                ).fetchone()

                if delivery is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Delivery not found.",
                    )

                status = str(
                    delivery["status"] or ""
                ).strip().upper()

                if status == "DELIVERED":
                    result = get_delivery(delivery_id)
                    result["job_complete"] = (
                        str(
                            connection.execute(
                                "SELECT status FROM jobs WHERE id=?",
                                (delivery["job_id"],),
                            ).fetchone()["status"]
                            or ""
                        ).upper()
                        == "DELIVERED"
                    )
                    return result

                if status != "READY":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Only a ready delivery can be "
                            "marked delivered."
                        ),
                    )

                quantity_total = connection.execute(
                    """
                    SELECT COALESCE(
                        SUM(quantity_delivered),
                        0
                    )
                    FROM delivery_items
                    WHERE delivery_id=?
                    """,
                    (delivery_id,),
                ).fetchone()[0]

                if int(quantity_total or 0) <= 0:
                    raise HTTPException(
                        status_code=409,
                        detail="Delivery has no parts.",
                    )

                connection.execute(
                    """
                    UPDATE deliveries
                    SET status='DELIVERED',
                        delivery_date=COALESCE(
                            delivery_date,
                            CURRENT_TIMESTAMP
                        ),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (delivery_id,),
                )

                remaining = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM supplier_order_items oi
                    JOIN supplier_orders po
                      ON po.id=oi.order_id
                    WHERE po.job_id=?
                      AND (
                            oi.quantity_received
                                < oi.quantity_ordered
                            OR
                            COALESCE((
                                SELECT SUM(
                                    di.quantity_delivered
                                )
                                FROM delivery_items di
                                JOIN deliveries d
                                  ON d.id=di.delivery_id
                                WHERE di.order_item_id=oi.id
                                  AND d.status='DELIVERED'
                            ),0) < oi.quantity_ordered
                      )
                    """,
                    (delivery["job_id"],),
                ).fetchone()[0]

                receiving_remaining = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM supplier_order_items oi
                    JOIN supplier_orders po
                      ON po.id=oi.order_id
                    WHERE po.job_id=?
                      AND oi.quantity_received
                            < oi.quantity_ordered
                    """,
                    (delivery["job_id"],),
                ).fetchone()[0]

                job_complete = (
                    int(remaining or 0) == 0
                )

                if job_complete:
                    new_job_status = "DELIVERED"
                elif int(receiving_remaining or 0) > 0:
                    new_job_status = "ORDERED"
                else:
                    new_job_status = "RECEIVED"

                connection.execute(
                    """
                    UPDATE jobs
                    SET status=?
                    WHERE id=?
                    """,
                    (
                        new_job_status,
                        delivery["job_id"],
                    ),
                )

                message = (
                    f"Delivery #{delivery_id} delivered to "
                    f"{delivery['recipient'] or 'customer'}: "
                    f"{int(quantity_total)} "
                    f"part{'s' if int(quantity_total) != 1 else ''}."
                )

                write_audit(
                    connection,
                    action="DELIVERY_COMPLETED",
                    entity_type="DELIVERY",
                    entity_id=delivery_id,
                    summary=message,
                    metadata={
                        "job_id": int(delivery["job_id"]),
                        "quantity": int(quantity_total),
                        "job_complete": job_complete,
                    },
                )

                log_job_event(
                    connection,
                    job_id=int(delivery["job_id"]),
                    event_type="DELIVERY_COMPLETED",
                    icon="📬",
                    message=message,
                )

                if job_complete:
                    log_job_event(
                        connection,
                        job_id=int(delivery["job_id"]),
                        event_type="JOB_DELIVERED",
                        icon="✅",
                        message=(
                            "All purchased parts have been "
                            "delivered to the customer."
                        ),
                    )

                connection.commit()

            result = get_delivery(delivery_id)
            result["job_complete"] = job_complete
            return result
    '''),
    "plg_core/supply/routes.py": block('''
        from fastapi import APIRouter
        from plg_core.supply.models import DeliveryCreate, ReceiptCreate, SupplierOrderItemCostUpdate, SupplierOrderUpdate
        from plg_core.supply.service import (
            complete_delivery, create_delivery, create_orders_from_paid_invoice,
            get_order, list_orders, place_order, record_receipt, update_order, update_order_item_cost,
        )

        router = APIRouter(prefix="/api/v1/supply", tags=["alpha14-15-supply"])

        @router.get("/orders")
        def orders(limit: int = 100):
            return {"items": list_orders(limit)}

        @router.get("/orders/{order_id}")
        def order_detail(order_id: int):
            return get_order(order_id)

        @router.post("/orders/from-invoice/{invoice_id}")
        def orders_from_invoice(invoice_id: int):
            return {"items": create_orders_from_paid_invoice(invoice_id)}

        @router.patch(
            "/orders/{order_id}/items/{item_id}"
        )
        def order_item_cost_update(
            order_id: int,
            item_id: int,
            payload: SupplierOrderItemCostUpdate,
        ):
            return update_order_item_cost(
                order_id=order_id,
                item_id=item_id,
                unit_cost=payload.unit_cost,
            )

        @router.patch("/orders/{order_id}")
        def order_update(
            order_id: int,
            payload: SupplierOrderUpdate,
        ):
            return update_order(
                order_id=order_id,
                shipping_total=payload.shipping_total,
                expected_at=payload.expected_at,
                notes=payload.notes,
            )

        @router.post("/orders/{order_id}/place")
        def order_place(order_id: int):
            return place_order(order_id)

        @router.post("/orders/{order_id}/receipts")
        def receive(order_id: int, payload: ReceiptCreate):
            return record_receipt(order_id, payload)

        @router.post("/deliveries/from-job/{job_id}")
        def delivery_prepare(job_id: int, payload: DeliveryCreate):
            return create_delivery(job_id, payload)

        @router.post("/deliveries/{delivery_id}/complete")
        def delivery_complete(delivery_id: int):
            return complete_delivery(delivery_id)
    '''),
    "plg_core/crm/__init__.py": block('''
        """Alpha 16 CRM API over existing PPS records."""
    '''),
    "plg_core/crm/routes.py": block('''
        from contextlib import closing
        from fastapi import APIRouter, HTTPException
        from legacy_app import get_connection

        router = APIRouter(prefix="/api/v1/crm", tags=["alpha16-crm"])

        @router.get("/customers")
        def customers(limit: int = 100):
            limit = max(1, min(int(limit), 500))
            with closing(get_connection()) as connection:
                rows = connection.execute("""
                    SELECT c.*, COUNT(DISTINCT j.id) AS jobs_count,
                           COUNT(DISTINCT m.id) AS machines_count
                    FROM customers c
                    LEFT JOIN jobs j ON j.customer_id=c.id
                    LEFT JOIN machines m ON m.customer_id=c.id
                    WHERE c.active=1
                    GROUP BY c.id ORDER BY c.name COLLATE NOCASE LIMIT ?
                """, (limit,)).fetchall()
            return {"items": [dict(row) for row in rows]}

        @router.get("/customers/{customer_id}")
        def customer_detail(customer_id: int):
            with closing(get_connection()) as connection:
                customer = connection.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
                if customer is None:
                    raise HTTPException(status_code=404, detail="Customer not found.")
                machines = connection.execute("SELECT * FROM machines WHERE customer_id=? ORDER BY id DESC", (customer_id,)).fetchall()
                jobs = connection.execute("SELECT * FROM jobs WHERE customer_id=? ORDER BY id DESC", (customer_id,)).fetchall()
            result = dict(customer)
            result["machines"] = [dict(row) for row in machines]
            result["jobs"] = [dict(row) for row in jobs]
            return result

        @router.get("/machines")
        def machines(limit: int = 100):
            limit = max(1, min(int(limit), 500))
            with closing(get_connection()) as connection:
                rows = connection.execute("""
                    SELECT m.*, c.customer_number, c.name AS customer_name,
                           COUNT(DISTINCT j.id) AS jobs_count
                    FROM machines m
                    JOIN customers c ON c.id=m.customer_id
                    LEFT JOIN jobs j ON j.machine_id=m.id
                    WHERE m.active=1
                    GROUP BY m.id ORDER BY m.id DESC LIMIT ?
                """, (limit,)).fetchall()
            return {"items": [dict(row) for row in rows]}

        @router.get("/machines/{machine_id}")
        def machine_detail(machine_id: int):
            with closing(get_connection()) as connection:
                machine = connection.execute("""
                    SELECT m.*, c.customer_number, c.name AS customer_name
                    FROM machines m JOIN customers c ON c.id=m.customer_id
                    WHERE m.id=?
                """, (machine_id,)).fetchone()
                if machine is None:
                    raise HTTPException(status_code=404, detail="Machine not found.")
                jobs = connection.execute("SELECT * FROM jobs WHERE machine_id=? ORDER BY id DESC", (machine_id,)).fetchall()
            result = dict(machine)
            result["jobs"] = [dict(row) for row in jobs]
            return result
    '''),
    "plg_core/admin/__init__.py": block('''
        """Alpha 17-18 ERP admin starter layer."""
    '''),
    "plg_core/admin/service.py": block('''
        from contextlib import closing
        from legacy_app import get_connection

        def dashboard_snapshot():
            with closing(get_connection()) as connection:
                counts = {
                    "customers": connection.execute("SELECT COUNT(*) FROM customers WHERE active=1").fetchone()[0],
                    "machines": connection.execute("SELECT COUNT(*) FROM machines WHERE active=1").fetchone()[0],
                    "open_jobs": connection.execute("SELECT COUNT(*) FROM jobs WHERE UPPER(COALESCE(status,'')) NOT IN ('DELIVERED','COMPLETED','COMPLETE','CLOSED')").fetchone()[0],
                    "active_quotes": connection.execute("SELECT COUNT(*) FROM quotes WHERE COALESCE(is_archived,0)=0").fetchone()[0],
                    "open_invoices": connection.execute("SELECT COUNT(*) FROM invoices WHERE status IN ('UNPAID','PARTIAL')").fetchone()[0],
                    "supplier_orders": connection.execute("SELECT COUNT(*) FROM supplier_orders WHERE status NOT IN ('RECEIVED','CANCELLED')").fetchone()[0],
                    "ready_deliveries": connection.execute("SELECT COUNT(*) FROM deliveries WHERE status='READY'").fetchone()[0],
                }
                money = connection.execute("""
                    SELECT COALESCE(SUM(customer_total),0) AS revenue,
                           COALESCE(SUM(profit_total),0) AS profit,
                           COALESCE(SUM(balance_due),0) AS receivables
                    FROM invoices WHERE status!='VOID'
                """).fetchone()
                audit = connection.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 20").fetchall()
            return {
                "counts": {key: int(value or 0) for key, value in counts.items()},
                "financials": {
                    "invoiced_revenue": round(float(money["revenue"] or 0), 2),
                    "invoiced_profit": round(float(money["profit"] or 0), 2),
                    "outstanding_receivables": round(float(money["receivables"] or 0), 2),
                },
                "recent_audit": [dict(row) for row in audit],
            }
    '''),
    "plg_core/admin/routes.py": block('''
        from contextlib import closing
        from fastapi import APIRouter
        from legacy_app import get_connection
        from plg_core.admin.service import dashboard_snapshot

        router = APIRouter(prefix="/api/v1/admin", tags=["alpha17-18-admin"])

        @router.get("/dashboard")
        def dashboard():
            return dashboard_snapshot()

        @router.get("/audit")
        def audit(limit: int = 100, entity_type: str = "", entity_id: str = ""):
            limit = max(1, min(int(limit), 500))
            conditions = []
            params = []
            if entity_type.strip():
                conditions.append("entity_type=?")
                params.append(entity_type.strip().upper())
            if entity_id.strip():
                conditions.append("entity_id=?")
                params.append(entity_id.strip())
            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            with closing(get_connection()) as connection:
                rows = connection.execute(
                    f"SELECT * FROM audit_logs {where} ORDER BY id DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
            return {"items": [dict(row) for row in rows]}
    '''),
    "plg_core/core_api/__init__.py": block('''
        """Alpha 19 API hardening starter layer."""
    '''),
    "plg_core/core_api/security.py": block('''
        import hmac
        import os
        from fastapi import Header, HTTPException

        def require_api_key(x_pps_api_key: str | None = Header(default=None)) -> None:
            """Opt-in API-key dependency. It is not globally enabled by the generator."""
            expected = os.getenv("PPS_API_KEY", "").strip()
            if not expected:
                raise HTTPException(status_code=503, detail="PPS_API_KEY is not configured.")
            supplied = (x_pps_api_key or "").strip()
            if not supplied or not hmac.compare_digest(supplied, expected):
                raise HTTPException(status_code=401, detail="Invalid PPS API key.")
    '''),
    "plg_core/core_api/middleware.py": block('''
        import os
        import uuid

        def install_optional_api_hardening(app) -> None:
            enabled = os.getenv("PPS_ENABLE_API_HARDENING", "").strip().lower() in {"1","true","yes","on"}
            if not enabled:
                return

            @app.middleware("http")
            async def pps_api_hardening(request, call_next):
                request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
                response = await call_next(request)
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Content-Type-Options"] = "nosniff"
                response.headers["Referrer-Policy"] = "same-origin"
                return response
    '''),
    "plg_core/core_api/routes.py": block('''
        from contextlib import closing
        from fastapi import APIRouter
        from legacy_app import get_connection

        router = APIRouter(prefix="/api/v1/core", tags=["alpha19-core"])

        @router.get("/ready")
        def ready():
            required = {
                "customers", "machines", "jobs", "quotes", "invoices",
                "supplier_orders", "supplier_order_items",
                "receiving_events", "deliveries", "audit_logs",
            }
            with closing(get_connection()) as connection:
                existing = {row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
            missing = sorted(required - existing)
            return {"ok": not missing, "missing_tables": missing, "roadmap": "alpha-12-19-starter"}

        @router.get("/capabilities")
        def capabilities():
            return {
                "alpha_12_13": ["quote tracker", "quote status", "quote conversion adapter", "invoice tracker"],
                "alpha_14_15": ["supplier orders", "receiving", "delivery"],
                "alpha_16_18": ["customer API", "machine API", "admin dashboard", "audit logs"],
                "alpha_19": ["versioned API", "readiness", "API-key template", "optional hardening middleware"],
            }
    '''),
}

APP_BLOCK = block('''
    # BEGIN PPS ROADMAP ALPHA 12-19
    from plg_core.admin.routes import router as roadmap_admin_router
    from plg_core.core_api.middleware import install_optional_api_hardening
    from plg_core.core_api.routes import router as roadmap_core_router
    from plg_core.crm.routes import router as roadmap_crm_router
    from plg_core.roadmap.migrations import run_roadmap_migrations
    from plg_core.sales.routes import router as roadmap_sales_router
    from plg_core.supply.routes import router as roadmap_supply_router

    app.include_router(roadmap_sales_router)
    app.include_router(roadmap_supply_router)
    app.include_router(roadmap_crm_router)
    app.include_router(roadmap_admin_router)
    app.include_router(roadmap_core_router)

    install_optional_api_hardening(app)

    @app.on_event("startup")
    def run_alpha_12_19_migrations() -> None:
        run_roadmap_migrations()
    # END PPS ROADMAP ALPHA 12-19
''')


def ensure_repo() -> None:
    required = [ROOT / "plg_core", APP_FILE, ROOT / "legacy_app.py"]
    missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
    if missing:
        print("ERROR: place build_roadmap.py in the PLG-Core repo root before running it.")
        print("Missing: " + ", ".join(missing))
        raise SystemExit(2)


def write_one(relative: str, content: str, *, force: bool, dry_run: bool) -> str:
    path = ROOT / relative
    if path.exists():
        if path.read_text() == content:
            return "unchanged"
        if not force:
            return "skipped-existing"
    if dry_run:
        return "would-write"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return "written"


def patch_application(*, dry_run: bool) -> str:
    text = APP_FILE.read_text()
    if BEGIN in text and END in text:
        before, rest = text.split(BEGIN, 1)
        _, after = rest.split(END, 1)
        new_text = before.rstrip() + "\n\n" + APP_BLOCK + after.lstrip("\n")
        status = "would-refresh" if dry_run else "refreshed"
    else:
        new_text = text.rstrip() + "\n\n" + APP_BLOCK
        status = "would-patch" if dry_run else "patched"
    if not dry_run and new_text != text:
        backup = APP_FILE.with_suffix(APP_FILE.suffix + ".roadmap-backup")
        if not backup.exists():
            shutil.copy2(APP_FILE, backup)
        APP_FILE.write_text(new_text)
    return status


def compile_check() -> list[str]:
    failures = []
    for relative in FILES:
        path = ROOT / relative
        if path.suffix != ".py" or not path.exists():
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"{relative}: {exc.msg}")
    try:
        py_compile.compile(str(APP_FILE), doraise=True)
    except py_compile.PyCompileError as exc:
        failures.append(f"plg_core/application.py: {exc.msg}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    ensure_repo()

    print("PPS Alpha 12-19 starter generator")
    print("Existing quotes, invoices, customers, machines and payments are preserved.")
    print()

    for relative, content in FILES.items():
        status = write_one(relative, content, force=args.force, dry_run=args.dry_run)
        print(f"{status:16} {relative}")
    print(f"{patch_application(dry_run=args.dry_run):16} plg_core/application.py")

    if args.dry_run:
        print("\nDry run complete. No files changed.")
        return 0

    failures = compile_check()
    if failures:
        print("\nSyntax checks FAILED:")
        for failure in failures:
            print(" -", failure)
        return 1

    print("\nSyntax checks: PASS")
    print("Roadmap migrations will run automatically the next time PPS starts.")
    print("Core check after startup: GET /api/v1/core/ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
