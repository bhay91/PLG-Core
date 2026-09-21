from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from legacy_app import BASE_DIR, get_connection
from plg_core.audit import write_audit
from .service import _delete_files, _delete_request_chain, _marks, _request_plan, _safe_paths


PURGE_PHRASE = "PERMANENTLY DELETE TEST CHAIN"
DOCUMENT_ROOT = (BASE_DIR / "documents").resolve()
_TEST_MARKER = re.compile(r"(?:^|[^A-Z])(TEST|DUMMY|BATCH|DISP(?:OSABLE)?|SYNTHETIC)(?:[^A-Z]|$)", re.I)


def _rows(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _token(plan: dict[str, Any]) -> str:
    state = {key: plan[key] for key in (
        "request_id", "request_updated_at", "job_id", "job_number", "job_state",
        "invoice_id", "invoice_number", "invoice_status", "quote_ids", "order_ids",
        "delivery_ids", "transaction_ids", "revision_versions", "counts", "blockers",
    )}
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_document_paths(paths: list[str]) -> list[Path]:
    safe: list[Path] = []
    for raw in paths:
        path = Path(raw).resolve()
        try:
            path.relative_to(DOCUMENT_ROOT)
        except ValueError:
            raise HTTPException(status_code=409, detail="A test document is outside the approved PPS document root.")
        safe.append(path)
    return safe


def build_test_chain_purge_plan(job_number: str, invoice_number: str,
                                connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    if connection is None:
        with closing(get_connection()) as owned:
            return build_test_chain_purge_plan(job_number, invoice_number, owned)
    job = connection.execute("SELECT * FROM jobs WHERE job_number=?", (job_number,)).fetchone()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    job_id = int(job["id"])
    requests = _rows(connection, "SELECT * FROM customer_requests WHERE job_id=? ORDER BY id", (job_id,))
    invoices = _rows(connection, "SELECT * FROM invoices WHERE job_id=? ORDER BY id", (job_id,))
    blockers: list[str] = []
    if len(requests) != 1:
        blockers.append(f"expected exactly one linked Request, found {len(requests)}")
    if len(invoices) != 1:
        blockers.append(f"expected exactly one linked Invoice, found {len(invoices)}")
    invoice = next((row for row in invoices if row["invoice_number"] == invoice_number), None)
    if invoice is None:
        raise HTTPException(status_code=409, detail="The Job and Invoice confirmation do not identify the same chain.")
    if not requests:
        raise HTTPException(status_code=409, detail="The test chain has no authoritative Request root.")
    base = _request_plan(connection, int(requests[0]["id"]))
    quote_rows = _rows(connection, "SELECT * FROM quotes WHERE job_id=? ORDER BY id", (job_id,))
    quote_ids = [int(row["id"]) for row in quote_rows]
    invoice_ids = [int(row["id"]) for row in invoices]
    order_rows = _rows(connection, "SELECT * FROM supplier_orders WHERE job_id=? ORDER BY id", (job_id,))
    order_ids = [int(row["id"]) for row in order_rows]
    delivery_rows = _rows(connection, "SELECT * FROM deliveries WHERE job_id=? ORDER BY id", (job_id,))
    delivery_ids = [int(row["id"]) for row in delivery_rows]
    transaction_rows = _rows(connection, "SELECT * FROM customer_transactions WHERE job_id=? ORDER BY id", (job_id,))
    transaction_ids = [int(row["id"]) for row in transaction_rows]
    if len(quote_rows) != 1 or int(invoice["quote_id"]) not in quote_ids:
        blockers.append("expected exactly one Quote paired to the Invoice")
    if str(job["status"]).upper() != "DELIVERED":
        blockers.append("Job is not a completed DELIVERED test chain")
    if str(invoice["status"]).upper() != "PAID":
        blockers.append("Invoice is not PAID")
    if not order_rows or any(str(row["status"]).upper() != "RECEIVED" for row in order_rows):
        blockers.append("supplier orders are not fully RECEIVED")
    if not delivery_rows or any(str(row["status"]).upper() != "DELIVERED" for row in delivery_rows):
        blockers.append("delivery is not DELIVERED")
    if any(row.get("invoice_id") not in invoice_ids for row in transaction_rows):
        blockers.append("a Job transaction is not exclusively owned by the confirmed Invoice")
    markers = [str(job[key] or "") for key in ("customer", "company", "manufacturer", "machine", "pin_serial", "notes")]
    markers.extend([str(requests[0].get(key) or "") for key in ("request_text", "individual_name", "company_name", "identifier")])
    marker_hits = [value for value in markers if _TEST_MARKER.search(value)]
    if len(marker_hits) < 2:
        blockers.append("fewer than two independent disposable/test markers were found")
    if connection.execute("SELECT COUNT(*) FROM opportunities WHERE converted_job_id=?", (job_id,)).fetchone()[0]:
        blockers.append("external opportunity/reference exists")

    quote_item_ids = [int(row[0]) for row in connection.execute(
        f"SELECT id FROM quote_items WHERE quote_id IN ({_marks(quote_ids)})", tuple(quote_ids)
    )] if quote_ids else []
    invoice_item_ids = [int(row[0]) for row in connection.execute(
        f"SELECT id FROM invoice_items WHERE invoice_id IN ({_marks(invoice_ids)})", tuple(invoice_ids)
    )]
    order_item_ids = [int(row[0]) for row in connection.execute(
        f"SELECT id FROM supplier_order_items WHERE order_id IN ({_marks(order_ids)})", tuple(order_ids)
    )] if order_ids else []
    receipt_ids = [int(row[0]) for row in connection.execute(
        f"SELECT id FROM receiving_events WHERE order_id IN ({_marks(order_ids)})", tuple(order_ids)
    )] if order_ids else []
    document_rows = _rows(connection,
        f"SELECT id,file_path FROM quote_documents_manifest WHERE quote_id IN ({_marks(quote_ids)})",
        tuple(quote_ids)) if quote_ids else []
    invoice_document_rows = _rows(
        connection,
        f"SELECT id,file_path FROM invoice_documents_manifest WHERE invoice_id IN ({_marks(invoice_ids)})",
        tuple(invoice_ids),
    ) if invoice_ids else []
    exact_document_names = {
        f"{invoice_number}.pdf", f"{invoice_number}-PAID.pdf",
        f"{invoice_number}-Internal.pdf", f"{invoice_number}-Internal-PAID.pdf",
        *(f"{number}{suffix}" for number in [str(row["quote_number"]) for row in quote_rows]
          for suffix in (".pdf", "-Internal.pdf")),
    }
    generated_files = [str(path) for path in DOCUMENT_ROOT.rglob("*")
                       if path.is_file() and path.name in exact_document_names]
    audit_ids = [int(row[0]) for row in connection.execute(
        "SELECT id FROM audit_logs WHERE "
        "(entity_type='JOB' AND entity_id=?) OR "
        f"(entity_type='QUOTE' AND entity_id IN ({_marks(quote_ids)})) OR "
        f"(entity_type='INVOICE' AND entity_id IN ({_marks(invoice_ids)})) OR "
        "metadata_json LIKE ?",
        (str(job_id), *[str(value) for value in quote_ids], *[str(value) for value in invoice_ids],
         f'%\"job_id\": {job_id}%'),
    )]
    plan = dict(base)
    plan.update({
        "job_number": str(job_number), "job_state": str(job["status"]),
        "invoice_id": int(invoice["id"]), "invoice_number": str(invoice_number),
        "invoice_status": str(invoice["status"]), "invoice_ids": invoice_ids,
        "quote_ids": quote_ids, "quote_numbers": [str(row["quote_number"]) for row in quote_rows],
        "quote_item_ids": quote_item_ids, "invoice_item_ids": invoice_item_ids,
        "order_ids": order_ids, "order_numbers": [str(row["po_number"]) for row in order_rows],
        "order_item_ids": order_item_ids, "receipt_ids": receipt_ids,
        "delivery_ids": delivery_ids, "transaction_ids": transaction_ids,
        "document_manifest_ids": [int(row["id"]) for row in document_rows],
        "invoice_document_manifest_ids": [int(row["id"]) for row in invoice_document_rows],
        "document_files": list(dict.fromkeys(
            [str(row["file_path"]) for row in document_rows if row.get("file_path")]
            + [str(row["file_path"]) for row in invoice_document_rows if row.get("file_path")]
            + generated_files)),
        "audit_ids": audit_ids,
        "disposable_markers": marker_hits, "blockers": blockers,
    })
    plan["counts"].update({
        "Quotes": len(quote_ids), "Quote Items": len(quote_item_ids), "Invoices": len(invoice_ids),
        "Invoice Items": len(invoice_item_ids), "Customer Transactions": len(transaction_ids),
        "Supplier Orders": len(order_ids), "Supplier Order Items": len(order_item_ids),
        "Receiving Events": len(receipt_ids),
        "Receiving Items": connection.execute(
            f"SELECT COUNT(*) FROM receiving_event_items WHERE receipt_id IN ({_marks(receipt_ids)})",
            tuple(receipt_ids)).fetchone()[0] if receipt_ids else 0,
        "Deliveries": len(delivery_ids),
        "Delivery Items": connection.execute(
            f"SELECT COUNT(*) FROM delivery_items WHERE delivery_id IN ({_marks(delivery_ids)})",
            tuple(delivery_ids)).fetchone()[0] if delivery_ids else 0,
        "Audit Events": len(audit_ids),
    })
    plan["token"] = _token(plan)
    return plan


def purge_test_chain(*, job_number: str, invoice_number: str, reason: str,
                     confirmation_job: str, confirmation_invoice: str,
                     confirmation_phrase: str, expected_token: str) -> dict[str, Any]:
    connection = get_connection()
    files: list[Path] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = build_test_chain_purge_plan(job_number, invoice_number, connection)
        if not str(reason or "").strip():
            raise HTTPException(status_code=400, detail="Purge reason is required.")
        if confirmation_job != plan["job_number"] or confirmation_invoice != plan["invoice_number"]:
            raise HTTPException(status_code=400, detail="Type the exact Job and Invoice numbers to confirm purge.")
        if confirmation_phrase != PURGE_PHRASE:
            raise HTTPException(status_code=400, detail=f"Type {PURGE_PHRASE} exactly to confirm purge.")
        if expected_token != plan["token"]:
            raise HTTPException(status_code=409, detail="The test chain changed after review. Reload the purge review.")
        if plan["blockers"]:
            raise HTTPException(status_code=409, detail="Test-chain purge is blocked by " + ", ".join(plan["blockers"]) + ".")
        files = _safe_paths(plan["files"]) + _safe_document_paths(plan["document_files"])
        _purge(connection, plan, str(reason).strip())
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Foreign-key violations after test-chain purge: {violations}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _delete_files(files)
    return {"purged": True, "job_number": job_number, "invoice_number": invoice_number,
            "counts": plan["counts"]}


def _purge(connection: sqlite3.Connection, plan: dict[str, Any], reason: str) -> None:
    job_id = int(plan["job_id"]); quote_ids = plan["quote_ids"]; invoice_ids = plan["invoice_ids"]
    order_ids = plan["order_ids"]; delivery_ids = plan["delivery_ids"]; receipt_ids = plan["receipt_ids"]
    qi = plan["quote_item_ids"]
    metadata = {"root": "DISPOSABLE_TEST_CHAIN", "job_number": plan["job_number"],
                "invoice_number": plan["invoice_number"], "counts": plan["counts"],
                "reason": reason, "operator_confirmation": PURGE_PHRASE}
    # Remove earlier chain audit rows, then preserve a tombstone and purge audit outside the operational graph.
    if plan["audit_ids"]:
        connection.execute(f"DELETE FROM audit_logs WHERE id IN ({_marks(plan['audit_ids'])})", tuple(plan["audit_ids"]))
    if delivery_ids:
        connection.execute(f"DELETE FROM delivery_items WHERE delivery_id IN ({_marks(delivery_ids)})", tuple(delivery_ids))
        connection.execute(f"DELETE FROM deliveries WHERE id IN ({_marks(delivery_ids)})", tuple(delivery_ids))
    if receipt_ids:
        connection.execute(f"DELETE FROM receiving_event_items WHERE receipt_id IN ({_marks(receipt_ids)})", tuple(receipt_ids))
        connection.execute(f"DELETE FROM receiving_events WHERE id IN ({_marks(receipt_ids)})", tuple(receipt_ids))
    if order_ids:
        connection.execute(f"DELETE FROM supplier_order_items WHERE order_id IN ({_marks(order_ids)})", tuple(order_ids))
        connection.execute(f"DELETE FROM supplier_orders WHERE id IN ({_marks(order_ids)})", tuple(order_ids))
    connection.execute(f"DELETE FROM consolidated_shipments WHERE invoice_id IN ({_marks(invoice_ids)}) OR job_id=?", (*invoice_ids, job_id))
    connection.execute(f"DELETE FROM customer_transactions WHERE id IN ({_marks(plan['transaction_ids'])})", tuple(plan["transaction_ids"]))
    connection.execute(f"DELETE FROM custom_invoices WHERE invoice_id IN ({_marks(invoice_ids)})", tuple(invoice_ids))
    connection.execute(f"DELETE FROM invoice_documents_manifest WHERE invoice_id IN ({_marks(invoice_ids)})", tuple(invoice_ids))
    connection.execute(f"DELETE FROM invoice_events WHERE invoice_id IN ({_marks(invoice_ids)})", tuple(invoice_ids))
    connection.execute(f"DELETE FROM invoice_items WHERE invoice_id IN ({_marks(invoice_ids)})", tuple(invoice_ids))
    connection.execute(f"DELETE FROM invoices WHERE id IN ({_marks(invoice_ids)})", tuple(invoice_ids))
    if qi:
        connection.execute(f"DELETE FROM quote_item_lineage WHERE predecessor_quote_item_id IN ({_marks(qi)}) OR successor_quote_item_id IN ({_marks(qi)})", (*qi, *qi))
        connection.execute(f"DELETE FROM quote_item_decisions WHERE quote_item_id IN ({_marks(qi)})", tuple(qi))
    connection.execute(f"DELETE FROM quote_documents_manifest WHERE quote_id IN ({_marks(quote_ids)})", tuple(quote_ids))
    connection.execute(f"DELETE FROM quote_events WHERE quote_id IN ({_marks(quote_ids)})", tuple(quote_ids))
    connection.execute(f"DELETE FROM quote_split_successors WHERE successor_quote_id IN ({_marks(quote_ids)})", tuple(quote_ids))
    connection.execute(f"DELETE FROM quote_splits WHERE source_quote_id IN ({_marks(quote_ids)})", tuple(quote_ids))
    connection.execute("UPDATE jobs SET active_work_revision_id=NULL WHERE id=?", (job_id,))
    connection.execute(f"UPDATE work_revisions SET based_on_quote_id=NULL WHERE job_id=?", (job_id,))
    connection.execute(f"UPDATE quotes SET quote_track_id=NULL,work_revision_id=NULL,split_from_quote_id=NULL,supersedes_quote_id=NULL WHERE id IN ({_marks(quote_ids)})", tuple(quote_ids))
    connection.execute(f"DELETE FROM quote_items WHERE quote_id IN ({_marks(quote_ids)})", tuple(quote_ids))
    connection.execute("DELETE FROM quote_tracks WHERE job_id=?", (job_id,))
    connection.execute(f"DELETE FROM quotes WHERE id IN ({_marks(quote_ids)})", tuple(quote_ids))
    # The established research-chain remover now has no commercial dependencies to encounter.
    revision_item_ids = plan["ids"]["revision_items"]
    revision_ids = plan["ids"]["revisions"]
    if revision_item_ids:
        connection.execute(f"UPDATE basket_items SET origin_work_revision_item_id=NULL WHERE origin_work_revision_item_id IN ({_marks(revision_item_ids)})", tuple(revision_item_ids))
        connection.execute(f"UPDATE job_parts SET work_revision_item_id=NULL WHERE work_revision_item_id IN ({_marks(revision_item_ids)})", tuple(revision_item_ids))
        connection.execute(f"UPDATE work_revision_items SET replacement_for_item_id=NULL,source_revision_item_id=NULL WHERE id IN ({_marks(revision_item_ids)})", tuple(revision_item_ids))
    if revision_ids:
        connection.execute(f"UPDATE job_parts SET work_revision_id=NULL WHERE work_revision_id IN ({_marks(revision_ids)})", tuple(revision_ids))
        connection.execute("UPDATE part_sources SET work_revision_source_id=NULL WHERE part_id IN (SELECT id FROM job_parts WHERE job_id=?)", (job_id,))
    plan["blockers"] = []
    plan["exclusive_machine_ids"] = []
    plan["exclusive_customer_id"] = None
    _delete_request_chain(connection, plan, reason, plan["job_number"])
    connection.execute("DELETE FROM audit_logs WHERE action='DISPOSABLE_RECORD_DELETED' AND entity_type='DELETION_TOMBSTONE' AND entity_id=?", (plan["job_number"],))
    connection.execute("DELETE FROM deletion_tombstones WHERE entity_type='DISPOSABLE_JOB' AND entity_number=? AND former_entity_id=?", (plan["job_number"], job_id))
    connection.execute(
        "INSERT INTO deletion_tombstones(entity_type,entity_number,former_entity_id,reason,metadata_json) VALUES ('DISPOSABLE_TEST_CHAIN',?,?,?,?)",
        (plan["job_number"], job_id, reason, json.dumps(metadata, sort_keys=True)),
    )
    write_audit(connection, action="DISPOSABLE_TEST_CHAIN_PURGED", entity_type="DELETION_TOMBSTONE",
                entity_id=plan["job_number"], summary=f"Test chain {plan['job_number']} / {plan['invoice_number']} permanently purged.", metadata=metadata)
