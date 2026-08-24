from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException

from plg_core.documents.paths import resolve_manifest_path


DOCUMENT_FAMILIES = {
    "quote": {
        "table": "quote_documents_manifest",
        "parent_table": "quotes",
        "parent_key": "quote_id",
    },
    "customer-invoice": {
        "table": "invoice_documents_manifest",
        "parent_table": "invoices",
        "parent_key": "invoice_id",
    },
    "internal-invoice": {
        "table": "invoice_documents_manifest",
        "parent_table": "invoices",
        "parent_key": "invoice_id",
    },
    "supplier-po": {
        "table": "supplier_order_documents_manifest",
        "parent_table": "supplier_orders",
        "parent_key": "supplier_order_id",
    },
    "receiving-summary": {
        "table": "receiving_documents_manifest",
        "parent_table": "receiving_events",
        "parent_key": "receipt_id",
    },
    "delivery-note": {
        "table": "delivery_documents_manifest",
        "parent_table": "deliveries",
        "parent_key": "delivery_id",
    },
}

DOCUMENT_TYPES = (
    "QUOTE",
    "CUSTOMER INVOICE",
    "INTERNAL INVOICE",
    "SUPPLIER PO",
    "RECEIVING SUMMARY",
    "DELIVERY NOTE",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _contained_document_path(root: Path, supplied: str) -> Path | None:
    try:
        return resolve_manifest_path(supplied, root=root)
    except ValueError:
        return None


def _integrity(root: Path, item: dict) -> tuple[str, str]:
    if not item.get("parent_exists"):
        return "MISSING PARENT", "The authoritative parent record is missing."
    path = _contained_document_path(root, item.get("file_path", ""))
    if path is None:
        return "MISSING FILE", "The manifest path is outside the PPS document root."
    if not path.is_file():
        return "MISSING FILE", "The manifested PDF is missing."
    if _sha256(path) != str(item.get("sha256") or ""):
        return "HASH MISMATCH", "The PDF does not match its manifest SHA-256."
    return "VALID", "Manifest, parent, file, and SHA-256 verified."


def _current_url(item: dict, *, download: bool = False) -> str:
    parent_id = int(item["parent_id"])
    kind = str(item["document_kind"])
    audience = str(item["audience"])
    family = item["family"]
    if family == "quote":
        base = f"/quotes/{parent_id}/{audience.lower()}/pdf"
    elif family in {"customer-invoice", "internal-invoice"}:
        if kind == "CUSTOM_INVOICE":
            base = f"/invoices/{parent_id}/custom/pdf"
        else:
            segment = "customer" if audience == "CUSTOMER" else "internal"
            paid = "/paid-pdf" if kind.endswith("_PAID") else "/pdf"
            base = f"/invoices/{parent_id}/{segment}{paid}"
    elif family == "supplier-po":
        base = f"/purchasing/orders/{parent_id}/purchase-order/pdf"
    elif family == "receiving-summary":
        base = f"/purchasing/receipts/{parent_id}/summary/pdf"
    else:
        base = f"/deliveries/{parent_id}/delivery-note/pdf"
    params = {"v": int(item["version"])}
    if download:
        params = {"download": 1, **params}
    return f"{base}?{urlencode(params)}"


def _history_url(item: dict, *, download: bool = False) -> str:
    base = f"/documents/manifest/{item['family']}/{int(item['manifest_id'])}"
    return f"{base}?download=1" if download else base


def _finish_item(root: Path, item: dict) -> dict:
    item["version"] = int(item.get("version") or 1)
    item["is_current"] = bool(item.get("is_current"))
    item["parent_exists"] = bool(item.get("parent_exists"))
    item["integrity"], item["integrity_detail"] = _integrity(root, item)
    item["is_valid"] = item["integrity"] == "VALID"
    item["version_label"] = "CURRENT" if item["is_current"] else "HISTORICAL"
    item["open_url"] = ""
    item["download_url"] = ""
    if item["is_valid"]:
        resolver = _current_url if item["is_current"] else _history_url
        item["open_url"] = resolver(item)
        item["download_url"] = resolver(item, download=True)
    return item


def _manifest_rows(connection: sqlite3.Connection, root: Path) -> list[dict]:
    rows: list[dict] = []

    for row in connection.execute(
        """
        SELECT m.id manifest_id,m.quote_id parent_id,m.document_kind,m.audience,
               m.version,m.is_current,m.file_path,m.sha256,m.generated_at date,
               COALESCE(NULLIF(m.quote_status,''),q.status,'') status,
               q.quote_number document_number,j.job_number,
               COALESCE(NULLIF(q.bill_to_name_snapshot,''),NULLIF(q.customer_name_snapshot,''),
                        NULLIF(j.customer,''),'—') customer,
               i.invoice_number related_invoice_number,q.quote_number related_quote_number,
               '' related_po_number,q.id IS NOT NULL parent_exists,q.id quote_id,
               q.job_id job_id,'quote' family,'QUOTE' document_type,
               CASE WHEN q.id IS NOT NULL THEN '/quotes/' || q.id || '/documents' ELSE '' END parent_url
        FROM quote_documents_manifest m
        LEFT JOIN quotes q ON q.id=m.quote_id
        LEFT JOIN jobs j ON j.id=q.job_id
        LEFT JOIN invoices i ON i.quote_id=q.id
        WHERE COALESCE(m.is_issued,0)=1
        """
    ):
        rows.append(_finish_item(root, dict(row)))

    for row in connection.execute(
        """
        SELECT m.id manifest_id,m.invoice_id parent_id,m.document_kind,m.audience,
               m.version,m.is_current,m.file_path,m.sha256,
               COALESCE(NULLIF(m.issued_at,''),m.generated_at) date,
               COALESCE(NULLIF(m.invoice_status,''),i.status,'') status,
               i.invoice_number document_number,j.job_number,
               COALESCE(NULLIF(i.bill_to_name_snapshot,''),NULLIF(j.customer,''),'—') customer,
               i.invoice_number related_invoice_number,q.quote_number related_quote_number,
               '' related_po_number,i.id IS NOT NULL parent_exists,i.id invoice_id,
               i.job_id job_id,
               CASE WHEN m.audience='INTERNAL' THEN 'internal-invoice' ELSE 'customer-invoice' END family,
               CASE WHEN m.audience='INTERNAL' THEN 'INTERNAL INVOICE' ELSE 'CUSTOMER INVOICE' END document_type,
               CASE WHEN i.id IS NOT NULL THEN '/invoices/' || i.id || '/documents' ELSE '' END parent_url
        FROM invoice_documents_manifest m
        LEFT JOIN invoices i ON i.id=m.invoice_id
        LEFT JOIN jobs j ON j.id=i.job_id
        LEFT JOIN quotes q ON q.id=i.quote_id
        WHERE m.audience IN ('CUSTOMER','INTERNAL')
          AND (m.document_kind LIKE '%INVOICE%')
        """
    ):
        rows.append(_finish_item(root, dict(row)))

    for row in connection.execute(
        """
        SELECT m.id manifest_id,m.supplier_order_id parent_id,m.document_kind,m.audience,
               m.version,m.is_current,m.file_path,m.sha256,
               COALESCE(NULLIF(m.issued_at,''),m.generated_at) date,
               COALESCE(NULLIF(m.supplier_order_status,''),so.status,'') status,
               so.po_number document_number,j.job_number,
               COALESCE(NULLIF(i.bill_to_name_snapshot,''),NULLIF(j.customer,''),'—') customer,
               i.invoice_number related_invoice_number,q.quote_number related_quote_number,
               so.po_number related_po_number,so.id IS NOT NULL parent_exists,
               so.invoice_id invoice_id,so.job_id job_id,'supplier-po' family,
               'SUPPLIER PO' document_type,
               CASE WHEN so.id IS NOT NULL THEN '/purchasing/orders/' || so.id ELSE '' END parent_url
        FROM supplier_order_documents_manifest m
        LEFT JOIN supplier_orders so ON so.id=m.supplier_order_id
        LEFT JOIN invoices i ON i.id=so.invoice_id
        LEFT JOIN quotes q ON q.id=i.quote_id
        LEFT JOIN jobs j ON j.id=so.job_id
        """
    ):
        rows.append(_finish_item(root, dict(row)))

    for row in connection.execute(
        """
        SELECT m.id manifest_id,m.receipt_id parent_id,m.document_kind,m.audience,
               m.version,m.is_current,m.file_path,m.sha256,
               COALESCE(NULLIF(m.issued_at,''),m.generated_at) date,
               COALESCE(so.status,'') status,re.receipt_number document_number,j.job_number,
               COALESCE(NULLIF(i.bill_to_name_snapshot,''),NULLIF(j.customer,''),'—') customer,
               i.invoice_number related_invoice_number,q.quote_number related_quote_number,
               so.po_number related_po_number,re.id IS NOT NULL parent_exists,
               so.invoice_id invoice_id,so.job_id job_id,'receiving-summary' family,
               'RECEIVING SUMMARY' document_type,
               CASE WHEN re.id IS NOT NULL AND so.id IS NOT NULL
                    THEN '/purchasing/orders/' || so.id || '?receipt_id=' || re.id ELSE '' END parent_url
        FROM receiving_documents_manifest m
        LEFT JOIN receiving_events re ON re.id=m.receipt_id
        LEFT JOIN supplier_orders so ON so.id=re.order_id
        LEFT JOIN invoices i ON i.id=so.invoice_id
        LEFT JOIN quotes q ON q.id=i.quote_id
        LEFT JOIN jobs j ON j.id=so.job_id
        """
    ):
        rows.append(_finish_item(root, dict(row)))

    for row in connection.execute(
        """
        SELECT m.id manifest_id,m.delivery_id parent_id,m.document_kind,m.audience,
               m.version,m.is_current,m.file_path,m.sha256,
               COALESCE(NULLIF(m.issued_at,''),m.generated_at) date,
               COALESCE(d.status,'') status,
               CASE WHEN d.id IS NOT NULL THEN printf('PPS-DEL-%04d',d.id) ELSE '' END document_number,
               j.job_number,COALESCE(NULLIF(i.bill_to_name_snapshot,''),NULLIF(j.customer,''),'—') customer,
               i.invoice_number related_invoice_number,q.quote_number related_quote_number,
               '' related_po_number,d.id IS NOT NULL parent_exists,d.invoice_id invoice_id,
               d.job_id job_id,'delivery-note' family,'DELIVERY NOTE' document_type,
               CASE WHEN d.id IS NOT NULL THEN '/jobs/' || d.job_id || '/delivery?delivery_id=' || d.id ELSE '' END parent_url
        FROM delivery_documents_manifest m
        LEFT JOIN deliveries d ON d.id=m.delivery_id
        LEFT JOIN invoices i ON i.id=d.invoice_id
        LEFT JOIN quotes q ON q.id=i.quote_id
        LEFT JOIN jobs j ON j.id=d.job_id
        """
    ):
        rows.append(_finish_item(root, dict(row)))

    return rows


def query_authoritative_documents(
    connection: sqlite3.Connection,
    root: Path,
    *,
    q: str = "",
    document_type: str = "",
    audience: str = "",
    version_scope: str = "current",
    status: str = "",
    customer: str = "",
) -> dict:
    all_items = _manifest_rows(connection, Path(root))
    normalized_scope = str(version_scope or "current").strip().lower()
    if normalized_scope not in {"current", "historical", "all"}:
        normalized_scope = "current"

    selected = []
    needle = str(q or "").strip().lower()
    for item in all_items:
        if normalized_scope == "current" and not item["is_current"]:
            continue
        if normalized_scope == "historical" and item["is_current"]:
            continue
        if document_type and item["document_type"] != document_type:
            continue
        if audience and item["audience"] != audience:
            continue
        if status and item["status"] != status:
            continue
        if customer and item["customer"] != customer:
            continue
        searchable = " ".join(str(item.get(key) or "") for key in (
            "document_number", "customer", "job_number", "related_invoice_number",
            "related_quote_number", "related_po_number", "document_kind", "document_type",
        )).lower()
        if needle and needle not in searchable:
            continue
        selected.append(item)

    selected.sort(key=lambda item: (
        str(item.get("date") or ""), int(item.get("manifest_id") or 0)
    ), reverse=True)

    series = {}
    for item in all_items:
        key = (item["family"], item["parent_id"], item["document_kind"], item["audience"])
        series.setdefault(key, []).append(item)
    for item in selected:
        key = (item["family"], item["parent_id"], item["document_kind"], item["audience"])
        item["history"] = sorted(
            (other for other in series[key] if other["manifest_id"] != item["manifest_id"]),
            key=lambda other: other["version"], reverse=True,
        )

    types = list(DOCUMENT_TYPES)
    audiences = ["CUSTOMER", "INTERNAL", "SUPPLIER"]
    statuses = sorted({item["status"] for item in all_items if item["status"]})
    customers = sorted({item["customer"] for item in all_items if item["customer"] != "—"})
    counts = Counter(item["integrity"] for item in selected)
    return {
        "items": selected,
        "summary": {
            "total": len(selected),
            "valid": counts["VALID"],
            "issues": len(selected) - counts["VALID"],
            "current": sum(1 for item in selected if item["is_current"]),
        },
        "filters": {
            "q": str(q or "").strip(), "document_type": document_type,
            "audience": audience, "version_scope": normalized_scope,
            "status": status, "customer": customer,
        },
        "options": {
            "document_types": types, "audiences": audiences,
            "statuses": statuses, "customers": customers,
        },
    }


def resolve_manifest_document(
    connection: sqlite3.Connection,
    root: Path,
    family: str,
    manifest_id: int,
) -> tuple[Path, sqlite3.Row]:
    normalized_family = str(family or "").strip().lower()
    spec = DOCUMENT_FAMILIES.get(normalized_family)
    if spec is None:
        raise HTTPException(status_code=404, detail="Document family not found.")
    audience_clause = ""
    parameters: list[object] = [int(manifest_id)]
    if normalized_family == "customer-invoice":
        audience_clause = " AND m.audience='CUSTOMER'"
    elif normalized_family == "internal-invoice":
        audience_clause = " AND m.audience='INTERNAL'"
    row = connection.execute(
        f"""
        SELECT m.*,p.id parent_exists
        FROM {spec['table']} m
        LEFT JOIN {spec['parent_table']} p ON p.id=m.{spec['parent_key']}
        WHERE m.id=? {audience_clause}
        """,
        parameters,
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document manifest not found.")
    if row["parent_exists"] is None:
        raise HTTPException(status_code=409, detail="Document parent record is missing.")
    path = _contained_document_path(Path(root), row["file_path"])
    if path is None:
        raise HTTPException(status_code=403, detail="Document path is outside the PPS document root.")
    if not path.is_file():
        raise HTTPException(status_code=409, detail="Manifested document file is missing.")
    if _sha256(path) != str(row["sha256"] or ""):
        raise HTTPException(status_code=409, detail="Document failed SHA-256 verification.")
    return path, row
