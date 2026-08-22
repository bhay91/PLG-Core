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
    from plg_core.lifecycle import transition_quote
    transition_quote(quote_id, status, notes)
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
    *,
    actor: str = "system",
    request_id: str = "",
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
            actor=str(actor or "system"),
            request_id=str(request_id or ""),
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
            from plg_core.documents.integrity import issue_invoice_documents

            updated_invoice, updated_items = load_invoice(
                connection,
                invoice_id,
            )

            issue_invoice_documents(
                connection, updated_invoice, updated_items, variant="PAID"
            )
            connection.commit()

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

        # The originally issued invoice and its paid variant remain immutable.
        # A reversal changes accounting state, not either historical PDF.

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

        from legacy_app import load_invoice
        from plg_core.documents.integrity import issue_invoice_documents

        updated_invoice, updated_items = load_invoice(
            connection,
            invoice_id,
        )

        issue_invoice_documents(
            connection, updated_invoice, updated_items, variant="VOID"
        )
        connection.commit()

    return get_invoice(invoice_id)
