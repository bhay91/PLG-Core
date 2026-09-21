from __future__ import annotations

from contextlib import closing
from pathlib import Path

from fastapi import HTTPException


def _document_kind(invoice_status: str) -> str:
    return (
        "INTERNAL_INVOICE_PAID"
        if str(invoice_status or "").strip().upper() == "PAID"
        else "INTERNAL_INVOICE"
    )


def internal_invoice_reconciliation_status(connection, invoice_id: int) -> dict:
    """Describe whether actual-cost history is newer than its internal PDF."""
    invoice = connection.execute(
        "SELECT id,invoice_number,status FROM invoices WHERE id=?",
        (invoice_id,),
    ).fetchone()
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found.")
    kind = _document_kind(invoice["status"])
    latest_change = connection.execute(
        """
        SELECT MAX(changed_at) FROM (
          SELECT a.created_at AS changed_at
          FROM supplier_cost_adjustments a
          JOIN supplier_orders po ON po.id=a.supplier_order_id
          WHERE po.invoice_id=?
          UNION ALL
          SELECT COALESCE(po.ordered_at,po.created_at) AS changed_at
          FROM supplier_orders po
          WHERE po.invoice_id=?
            AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
        )
        """,
        (invoice_id, invoice_id),
    ).fetchone()[0]
    current = connection.execute(
        """
        SELECT * FROM invoice_documents_manifest
        WHERE invoice_id=? AND document_kind=? AND audience='INTERNAL'
          AND is_current=1
        ORDER BY version DESC LIMIT 1
        """,
        (invoice_id, kind),
    ).fetchone()
    document_time = (
        str(current["generated_at"] or current["issued_at"] or "")
        if current is not None else ""
    )
    older = None
    if current is not None and document_time and latest_change:
        older = connection.execute(
            "SELECT julianday(?) < julianday(?)",
            (document_time, latest_change),
        ).fetchone()[0]
    stale = bool(latest_change and (current is None or not document_time or older is None or older))
    return {
        "invoice_id": int(invoice["id"]),
        "invoice_number": invoice["invoice_number"],
        "invoice_status": invoice["status"],
        "document_kind": kind,
        "latest_financial_change_at": latest_change,
        "current_manifest_id": int(current["id"]) if current is not None else None,
        "current_version": int(current["version"]) if current is not None else None,
        "current_document_at": document_time or None,
        "stale": stale,
    }


def find_stale_internal_invoice_documents(connection, invoice_id: int | None = None) -> list[dict]:
    parameters = () if invoice_id is None else (invoice_id,)
    where = "" if invoice_id is None else "AND i.id=?"
    rows = connection.execute(
        f"""
        SELECT DISTINCT i.id
        FROM invoices i
        JOIN supplier_orders po ON po.invoice_id=i.id
        WHERE UPPER(COALESCE(i.status,'')) != 'VOID'
          AND UPPER(COALESCE(po.status,'')) IN ('ORDERED','PARTIAL','RECEIVED')
          {where}
        ORDER BY i.id
        """,
        parameters,
    ).fetchall()
    statuses = [
        internal_invoice_reconciliation_status(connection, int(row["id"]))
        for row in rows
    ]
    return [status for status in statuses if status["stale"]]


def reconcile_stale_internal_invoice_documents(
    *, invoice_id: int | None = None, dry_run: bool = True, connection_factory=None,
) -> list[dict]:
    """Reconcile stale internal PDFs, one invoice per atomic transaction."""
    if connection_factory is None:
        from legacy_app import get_connection
        connection_factory = get_connection

    with closing(connection_factory()) as connection:
        candidates = find_stale_internal_invoice_documents(connection, invoice_id)
    if dry_run:
        return candidates

    results = []
    for candidate in candidates:
        generated_path = None
        with closing(connection_factory()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = internal_invoice_reconciliation_status(
                    connection, candidate["invoice_id"]
                )
                if not current["stale"]:
                    connection.rollback()
                    continue
                from legacy_app import load_invoice
                from plg_core.admin.service import invoice_financial_state
                from plg_core.documents.integrity import issue_current_internal_invoice_document

                invoice, items = load_invoice(connection, current["invoice_id"])
                financial_state = invoice_financial_state(connection, current["invoice_id"])
                generated_path = issue_current_internal_invoice_document(
                    connection, invoice, items, financial_state
                )
                connection.commit()
                results.append({
                    **current,
                    "reconciled": True,
                    "new_file_path": generated_path,
                })
            except Exception as exc:
                connection.rollback()
                if generated_path:
                    path = Path(generated_path)
                    if path.exists():
                        path.unlink()
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Internal invoice reconciliation failed for "
                        f"{candidate['invoice_number']}: {exc}"
                    ),
                ) from exc
    return results
