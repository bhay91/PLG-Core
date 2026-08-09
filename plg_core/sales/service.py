from __future__ import annotations
from contextlib import closing
from fastapi import HTTPException
from legacy_app import get_connection
from plg_core.audit import write_audit

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
    with closing(get_connection()) as connection:
        quote = connection.execute(
            "SELECT id, quote_number, status FROM quotes WHERE id=?",
            (quote_id,),
        ).fetchone()
        if quote is None:
            raise HTTPException(status_code=404, detail="Quote not found.")
        old = str(quote["status"] or "").upper()
        connection.execute("UPDATE quotes SET status=? WHERE id=?", (status, quote_id))
        connection.execute("""
            INSERT INTO quote_events (quote_id,event_type,from_status,to_status,notes)
            VALUES (?, 'STATUS_CHANGED', ?, ?, ?)
        """, (quote_id, old, status, notes.strip()))
        write_audit(
            connection,
            action="QUOTE_STATUS_CHANGED",
            entity_type="QUOTE",
            entity_id=quote_id,
            summary=f"{quote['quote_number']} changed from {old or 'UNKNOWN'} to {status}",
            metadata={"notes": notes},
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
    result = dict(invoice)
    result["items"] = [dict(row) for row in items]
    result["payments"] = [dict(row) for row in payments]
    result["payments_received"] = round(sum(float(row["amount"] or 0) for row in payments), 2)
    return result
