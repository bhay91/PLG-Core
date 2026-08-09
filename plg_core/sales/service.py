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
                       WHERE t.invoice_id=i.id AND t.transaction_type='PAYMENT'
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
            SELECT * FROM customer_transactions
            WHERE invoice_id=? AND transaction_type='PAYMENT'
            ORDER BY transaction_date DESC,id DESC
        """, (invoice_id,)).fetchall()
        events = connection.execute(
            "SELECT * FROM invoice_events WHERE invoice_id=? ORDER BY id DESC",
            (invoice_id,),
        ).fetchall()
    result = dict(invoice)
    result["items"] = [dict(row) for row in items]
    result["payments"] = [dict(row) for row in payments]
    result["events"] = [dict(row) for row in events]
    result["payments_received"] = round(sum(float(row["amount"] or 0) for row in payments), 2)
    return result

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
            SELECT 1
            FROM customer_transactions
            WHERE invoice_id = ?
              AND transaction_type = 'PAYMENT'
            LIMIT 1
            """,
            (invoice_id,),
        ).fetchone()

        if payment is not None:
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
