from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from plg_core.documents.paths import portable_manifest_path, resolve_manifest_path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _versioned_path(path: Path, version: int) -> Path:
    if version <= 1:
        return path
    return path.with_name(f"{path.stem}-v{version}{path.suffix}")


def _verify_manifest_path(row: sqlite3.Row) -> Path:
    try:
        path = resolve_manifest_path(row["file_path"])
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail="Issued document is missing and cannot be regenerated silently.",
        ) from None
    if not path.is_file():
        raise HTTPException(
            status_code=409,
            detail="Issued document is missing and cannot be regenerated silently.",
        )
    if _digest(path) != str(row["sha256"] or ""):
        raise HTTPException(
            status_code=409,
            detail="Issued document failed integrity verification.",
        )
    return path


def current_quote_document(
    connection: sqlite3.Connection,
    quote_id: int,
    audience: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM quote_documents_manifest
        WHERE quote_id=? AND document_kind='QUOTE' AND audience=?
          AND is_current=1
        ORDER BY version DESC LIMIT 1
        """,
        (quote_id, audience.upper()),
    ).fetchone()


def verified_quote_document(
    connection: sqlite3.Connection,
    quote_id: int,
    audience: str,
) -> Path:
    row = current_quote_document(connection, quote_id, audience)
    if row is None or not int(row["is_issued"] or 0):
        raise HTTPException(
            status_code=409,
            detail="Issued quote document manifest is missing.",
        )
    return _verify_manifest_path(row)


def issue_quote_documents(
    connection: sqlite3.Connection,
    quote,
    items,
    *,
    force_new: bool = False,
) -> dict[str, str]:
    from plg_core.documents.quote_pdf import (
        DOCUMENT_ROOT as quote_customer_root,
        build_quote_pdf,
        quote_paths,
    )

    quote_id = int(quote["id"])
    status = str(quote["status"] or "").strip().upper()
    if status != "SENT":
        raise HTTPException(
            status_code=409,
            detail="Quote documents may be issued only after status is SENT.",
        )

    existing = {
        audience: current_quote_document(connection, quote_id, audience)
        for audience in ("CUSTOMER", "INTERNAL")
    }
    if not force_new and all(
        row is not None and int(row["is_issued"] or 0)
        for row in existing.values()
    ):
        return {
            audience.lower(): str(_verify_manifest_path(row))
            for audience, row in existing.items()
        }

    version = int(
        connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM quote_documents_manifest WHERE quote_id=?",
            (quote_id,),
        ).fetchone()[0]
    )
    bases = quote_paths(quote["customer"], quote["quote_number"])
    paths = {
        "CUSTOMER": _versioned_path(bases["customer"], version),
        "INTERNAL": _versioned_path(bases["internal"], version),
    }
    build_quote_pdf(quote, items, paths["CUSTOMER"], internal=False)
    build_quote_pdf(quote, items, paths["INTERNAL"], internal=True)

    connection.execute(
        "UPDATE quote_documents_manifest SET is_current=0 WHERE quote_id=? AND is_current=1",
        (quote_id,),
    )
    for audience, path in paths.items():
        connection.execute(
            """
            INSERT INTO quote_documents_manifest (
                quote_id, audience, document_kind, file_path, sha256,
                is_issued, version, quote_status, is_current
            ) VALUES (?,?,'QUOTE',?,?,1,?,?,1)
            """,
            (
                quote_id, audience,
                portable_manifest_path(path, root=quote_customer_root.parent),
                _digest(path), version, status,
            ),
        )
    return {audience.lower(): str(path) for audience, path in paths.items()}


def current_invoice_document(
    connection: sqlite3.Connection,
    invoice_id: int,
    document_kind: str,
    audience: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM invoice_documents_manifest
        WHERE invoice_id=? AND document_kind=? AND audience=? AND is_current=1
        ORDER BY version DESC LIMIT 1
        """,
        (invoice_id, document_kind, audience.upper()),
    ).fetchone()


def verified_invoice_document(
    connection: sqlite3.Connection,
    invoice_id: int,
    document_kind: str,
    audience: str,
) -> Path:
    row = current_invoice_document(connection, invoice_id, document_kind, audience)
    if row is None:
        raise HTTPException(status_code=409, detail="Issued invoice document manifest is missing.")
    return _verify_manifest_path(row)


def current_invoice_document_versions(
    connection: sqlite3.Connection,
    invoice_id: int,
    invoice_status: str,
) -> dict[str, int]:
    """Return presentation cache keys for the active issued/paid document family."""
    paid = str(invoice_status or "").strip().upper() == "PAID"
    kinds = {
        "customer_document_version": (
            "CUSTOMER_INVOICE_PAID" if paid else "CUSTOMER_INVOICE"
        ),
        "internal_document_version": (
            "INTERNAL_INVOICE_PAID" if paid else "INTERNAL_INVOICE"
        ),
    }
    versions = {}
    for key, kind in kinds.items():
        audience = "CUSTOMER" if key.startswith("customer") else "INTERNAL"
        row = current_invoice_document(
            connection, invoice_id, kind, audience
        )
        versions[key] = int(row["version"]) if row is not None else 0
    return versions


def issue_invoice_documents(
    connection: sqlite3.Connection,
    invoice,
    items,
    *,
    variant: str = "ISSUED",
) -> dict[str, str]:
    from plg_core.documents.invoice_pdf import (
        DOCUMENT_ROOT as invoice_customer_root,
        build_invoice_pdf,
        invoice_paths,
        paid_invoice_paths,
    )

    invoice_id = int(invoice["id"])
    status = str(invoice["status"] or "").strip().upper()
    variants = {
        "ISSUED": ("CUSTOMER_INVOICE", "INTERNAL_INVOICE"),
        "PAID": ("CUSTOMER_INVOICE_PAID", "INTERNAL_INVOICE_PAID"),
        "VOID": ("CUSTOMER_INVOICE_VOID", "INTERNAL_INVOICE_VOID"),
    }
    if variant not in variants:
        raise ValueError(f"Unsupported invoice document variant: {variant}")
    if variant == "PAID" and status != "PAID":
        raise HTTPException(status_code=409, detail="Paid invoice documents require PAID status.")
    if variant == "VOID" and status != "VOID":
        raise HTTPException(status_code=409, detail="Void invoice documents require VOID status.")

    customer_kind, internal_kind = variants[variant]
    kinds = {"CUSTOMER": customer_kind, "INTERNAL": internal_kind}
    existing = {
        audience: current_invoice_document(connection, invoice_id, kind, audience)
        for audience, kind in kinds.items()
    }
    if all(row is not None for row in existing.values()):
        return {
            audience.lower(): str(_verify_manifest_path(row))
            for audience, row in existing.items()
        }

    version = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(version),0)+1
            FROM invoice_documents_manifest
            WHERE invoice_id=? AND document_kind IN (?,?)
            """,
            (invoice_id, customer_kind, internal_kind),
        ).fetchone()[0]
    )
    if variant == "PAID":
        bases = paid_invoice_paths(invoice["customer"], invoice["invoice_number"])
    else:
        bases = invoice_paths(invoice["customer"], invoice["invoice_number"])
        if variant == "VOID":
            bases = {
                key: path.with_name(f"{path.stem}-VOID{path.suffix}")
                for key, path in bases.items()
            }
    paths = {
        "CUSTOMER": _versioned_path(bases["customer"], version),
        "INTERNAL": _versioned_path(bases["internal"], version),
    }
    from plg_core.admin.service import derive_invoice_financial_state
    internal_invoice = dict(invoice)
    internal_invoice.update(derive_invoice_financial_state(
        customer_total=invoice["customer_total"],
        estimated_cost=invoice["supplier_total"],
        placed_cost=0,
        confirmed_actual_cost=0,
        actual_component_count=0,
        confirmed_actual_component_count=0,
    ))
    build_invoice_pdf(invoice, items, paths["CUSTOMER"], internal=False)
    build_invoice_pdf(internal_invoice, items, paths["INTERNAL"], internal=True)

    for audience, kind in kinds.items():
        connection.execute(
            """
            UPDATE invoice_documents_manifest SET is_current=0
            WHERE invoice_id=? AND document_kind=? AND audience=? AND is_current=1
            """,
            (invoice_id, kind, audience),
        )
        path = paths[audience]
        connection.execute(
            """
            INSERT INTO invoice_documents_manifest (
                invoice_id, document_kind, audience, version, invoice_status,
                file_path, sha256, is_current
            ) VALUES (?,?,?,?,?,?,?,1)
            """,
            (
                invoice_id, kind, audience, version, status,
                portable_manifest_path(path, root=invoice_customer_root.parent),
                _digest(path),
            ),
        )
    return {audience.lower(): str(path) for audience, path in paths.items()}


def issue_current_internal_invoice_document(
    connection: sqlite3.Connection,
    invoice,
    items,
    financial_state,
) -> str:
    """Create and register a new immutable internal financial document version."""
    from plg_core.documents.invoice_pdf import (
        DOCUMENT_ROOT as invoice_customer_root,
        build_invoice_pdf,
        invoice_paths,
        paid_invoice_paths,
    )
    from plg_core.documents.pdf_fit import page_count

    invoice_id = int(invoice["id"])
    status = str(invoice["status"] or "").strip().upper()
    kind = "INTERNAL_INVOICE_PAID" if status == "PAID" else "INTERNAL_INVOICE"
    version = int(connection.execute(
        """SELECT COALESCE(MAX(version),0)+1 FROM invoice_documents_manifest
           WHERE invoice_id=? AND document_kind=? AND audience='INTERNAL'""",
        (invoice_id, kind),
    ).fetchone()[0])
    bases = paid_invoice_paths(invoice["customer"], invoice["invoice_number"]) if status == "PAID" else invoice_paths(invoice["customer"], invoice["invoice_number"])
    path = _versioned_path(bases["internal"], version)
    if path.exists():
        raise HTTPException(status_code=409, detail="Internal invoice version path already exists without a manifest.")

    current_invoice = dict(invoice)
    current_invoice.update(financial_state)
    try:
        build_invoice_pdf(current_invoice, items, path, internal=True)
        data = path.read_bytes()
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:] or page_count(path) < 1:
            raise HTTPException(status_code=500, detail="Generated internal invoice PDF failed verification.")
        digest = _digest(path)
        connection.execute(
            """UPDATE invoice_documents_manifest SET is_current=0
               WHERE invoice_id=? AND document_kind=? AND audience='INTERNAL' AND is_current=1""",
            (invoice_id, kind),
        )
        connection.execute(
            """INSERT INTO invoice_documents_manifest (
                 invoice_id,document_kind,audience,version,invoice_status,file_path,sha256,is_current
               ) VALUES (?,?,'INTERNAL',?,?,?,?,1)""",
            (
                invoice_id, kind, version, status,
                portable_manifest_path(path, root=invoice_customer_root.parent), digest,
            ),
        )
        manifest = current_invoice_document(connection, invoice_id, kind, "INTERNAL")
        if manifest is None or _verify_manifest_path(manifest) != path:
            raise HTTPException(status_code=500, detail="Internal invoice manifest verification failed.")
    except Exception:
        if path.exists():
            path.unlink()
        raise
    return str(path)


def issue_current_customer_invoice_document(
    connection: sqlite3.Connection,
    invoice,
    items,
) -> str:
    """Create a new immutable customer copy after a presentation change."""
    from plg_core.documents.invoice_pdf import (
        DOCUMENT_ROOT as invoice_customer_root,
        build_invoice_pdf,
        invoice_paths,
        paid_invoice_paths,
    )
    from plg_core.documents.pdf_fit import page_count

    invoice_id = int(invoice["id"])
    status = str(invoice["status"] or "").strip().upper()
    if status == "PAID":
        kind = "CUSTOMER_INVOICE_PAID"
        base = paid_invoice_paths(
            invoice["customer"], invoice["invoice_number"]
        )["customer"]
    elif status == "VOID":
        kind = "CUSTOMER_INVOICE_VOID"
        ordinary = invoice_paths(
            invoice["customer"], invoice["invoice_number"]
        )["customer"]
        base = ordinary.with_name(f"{ordinary.stem}-VOID{ordinary.suffix}")
    else:
        kind = "CUSTOMER_INVOICE"
        base = invoice_paths(
            invoice["customer"], invoice["invoice_number"]
        )["customer"]

    version = int(connection.execute(
        """SELECT COALESCE(MAX(version),0)+1
           FROM invoice_documents_manifest
           WHERE invoice_id=? AND document_kind=? AND audience='CUSTOMER'""",
        (invoice_id, kind),
    ).fetchone()[0])
    path = _versioned_path(base, version)
    if path.exists():
        raise HTTPException(
            status_code=409,
            detail="Customer invoice version path already exists without a manifest.",
        )

    try:
        build_invoice_pdf(invoice, items, path, internal=False)
        data = path.read_bytes()
        if (
            not data.startswith(b"%PDF-")
            or b"%%EOF" not in data[-1024:]
            or page_count(path) < 1
        ):
            raise HTTPException(
                status_code=500,
                detail="Generated customer invoice PDF failed verification.",
            )
        digest = _digest(path)
        connection.execute(
            """UPDATE invoice_documents_manifest SET is_current=0
               WHERE invoice_id=? AND document_kind=?
                 AND audience='CUSTOMER' AND is_current=1""",
            (invoice_id, kind),
        )
        connection.execute(
            """INSERT INTO invoice_documents_manifest (
                 invoice_id,document_kind,audience,version,invoice_status,
                 file_path,sha256,is_current
               ) VALUES (?,?,'CUSTOMER',?,?,?,?,1)""",
            (
                invoice_id,
                kind,
                version,
                status,
                portable_manifest_path(path, root=invoice_customer_root.parent),
                digest,
            ),
        )
        manifest = current_invoice_document(
            connection, invoice_id, kind, "CUSTOMER"
        )
        if manifest is None or _verify_manifest_path(manifest) != path:
            raise HTTPException(
                status_code=500,
                detail="Customer invoice manifest verification failed.",
            )
    except Exception:
        if path.exists():
            path.unlink()
        raise
    return str(path)


def issue_custom_invoice_document(
    connection: sqlite3.Connection,
    invoice,
    custom_invoice,
    custom_items,
) -> str:
    from plg_core.documents.invoice_pdf import (
        custom_invoice_path,
        generate_custom_invoice_pdf,
    )

    invoice_id = int(invoice["id"])
    current = current_invoice_document(
        connection, invoice_id, "CUSTOM_INVOICE", "CUSTOMER"
    )
    version = int(current["version"] or 0) + 1 if current is not None else 1
    path = _versioned_path(
        custom_invoice_path(invoice, custom_invoice), version
    )
    generate_custom_invoice_pdf(
        invoice, custom_invoice, custom_items, output_path=path
    )
    connection.execute(
        """
        UPDATE invoice_documents_manifest SET is_current=0
        WHERE invoice_id=? AND document_kind='CUSTOM_INVOICE'
          AND audience='CUSTOMER' AND is_current=1
        """,
        (invoice_id,),
    )
    connection.execute(
        """
        INSERT INTO invoice_documents_manifest (
            invoice_id,document_kind,audience,version,invoice_status,
            file_path,sha256,is_current
        ) VALUES (?,'CUSTOM_INVOICE','CUSTOMER',?,'PAID',?,?,1)
        """,
        (invoice_id, version, portable_manifest_path(path), _digest(path)),
    )
    return str(path)


def current_supplier_order_document(
    connection: sqlite3.Connection,
    supplier_order_id: int,
    audience: str = "SUPPLIER",
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM supplier_order_documents_manifest
        WHERE supplier_order_id=?
          AND document_kind='SUPPLIER_PURCHASE_ORDER'
          AND audience=? AND is_current=1
        ORDER BY version DESC LIMIT 1
        """,
        (supplier_order_id, audience.upper()),
    ).fetchone()


def verified_supplier_order_document(
    connection: sqlite3.Connection,
    supplier_order_id: int,
    audience: str = "SUPPLIER",
) -> Path:
    row = current_supplier_order_document(
        connection, supplier_order_id, audience
    )
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="Issued Supplier Purchase Order manifest is missing.",
        )
    return _verify_manifest_path(row)


def issue_supplier_order_document(
    connection: sqlite3.Connection,
    supplier_order_id: int,
) -> str:
    from plg_core.documents.supplier_order_pdf import (
        build_supplier_order_pdf,
        load_supplier_order_document_data,
        supplier_order_path,
    )

    order = load_supplier_order_document_data(connection, supplier_order_id)
    status = str(order["status"] or "").strip().upper()
    if status != "ORDERED":
        raise HTTPException(
            status_code=409,
            detail="A Supplier Purchase Order may be issued only after the order is ORDERED.",
        )
    current = current_supplier_order_document(connection, supplier_order_id)
    if current is not None:
        return str(_verify_manifest_path(current))

    version = int(
        connection.execute(
            """
            SELECT COALESCE(MAX(version),0)+1
            FROM supplier_order_documents_manifest
            WHERE supplier_order_id=?
            """,
            (supplier_order_id,),
        ).fetchone()[0]
    )
    path = supplier_order_path(order, version=version)
    if path.exists():
        raise HTTPException(
            status_code=409,
            detail="Supplier Purchase Order path already exists without an issued manifest.",
        )
    build_supplier_order_pdf(order, path, draft=False)
    connection.execute(
        """
        UPDATE supplier_order_documents_manifest SET is_current=0
        WHERE supplier_order_id=?
          AND document_kind='SUPPLIER_PURCHASE_ORDER'
          AND audience='SUPPLIER' AND is_current=1
        """,
        (supplier_order_id,),
    )
    connection.execute(
        """
        INSERT INTO supplier_order_documents_manifest (
            supplier_order_id,document_kind,audience,version,
            supplier_order_status,file_path,sha256,is_current
        ) VALUES (?,'SUPPLIER_PURCHASE_ORDER','SUPPLIER',?,?,?,?,1)
        """,
        (
            supplier_order_id,
            version,
            status,
            portable_manifest_path(path),
            _digest(path),
        ),
    )
    return str(path)


def preview_supplier_order_document(
    connection: sqlite3.Connection,
    supplier_order_id: int,
) -> str:
    from plg_core.documents.supplier_order_pdf import (
        build_supplier_order_pdf,
        load_supplier_order_document_data,
        supplier_order_path,
    )

    order = load_supplier_order_document_data(connection, supplier_order_id)
    if str(order["status"] or "").strip().upper() != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail="Only DRAFT Supplier Orders use a regenerable preview.",
        )
    path = supplier_order_path(order, draft=True)
    build_supplier_order_pdf(order, path, draft=True)
    return str(path)


def current_receiving_document(
    connection: sqlite3.Connection,
    receipt_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM receiving_documents_manifest
        WHERE receipt_id=? AND document_kind='RECEIVING_SUMMARY'
          AND audience='INTERNAL' AND is_current=1
        ORDER BY version DESC LIMIT 1
        """,
        (receipt_id,),
    ).fetchone()


def verified_receiving_document(
    connection: sqlite3.Connection,
    receipt_id: int,
) -> Path:
    row = current_receiving_document(connection, receipt_id)
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="Issued Receiving Summary manifest is missing.",
        )
    return _verify_manifest_path(row)


def issue_receiving_document(
    connection: sqlite3.Connection,
    receipt_id: int,
) -> str:
    from plg_core.documents.receiving_pdf import (
        build_receiving_pdf,
        receiving_summary_path,
    )
    from plg_core.supply.service import get_receipt

    current = current_receiving_document(connection, receipt_id)
    if current is not None:
        return str(_verify_manifest_path(current))
    receipt = get_receipt(receipt_id)
    path = receiving_summary_path(receipt)
    if path.exists():
        raise HTTPException(
            status_code=409,
            detail="Receiving Summary path exists without an issued manifest.",
        )
    build_receiving_pdf(receipt, path)
    connection.execute(
        """
        INSERT INTO receiving_documents_manifest (
            receipt_id,document_kind,audience,version,file_path,sha256,is_current
        ) VALUES (?,'RECEIVING_SUMMARY','INTERNAL',1,?,?,1)
        """,
        (receipt_id, portable_manifest_path(path), _digest(path)),
    )
    return str(path)


def current_delivery_document(
    connection: sqlite3.Connection,
    delivery_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM delivery_documents_manifest
        WHERE delivery_id=? AND document_kind='DELIVERY_NOTE'
          AND audience='CUSTOMER' AND is_current=1
        ORDER BY version DESC LIMIT 1
        """,
        (delivery_id,),
    ).fetchone()


def verified_delivery_document(
    connection: sqlite3.Connection,
    delivery_id: int,
) -> Path:
    row = current_delivery_document(connection, delivery_id)
    if row is None:
        raise HTTPException(status_code=409, detail="Issued Delivery Note manifest is missing.")
    return _verify_manifest_path(row)


def issue_delivery_document(
    connection: sqlite3.Connection,
    delivery_id: int,
) -> str:
    from plg_core.documents.delivery_pdf import build_delivery_pdf, delivery_note_path
    from plg_core.supply.service import get_delivery

    current = current_delivery_document(connection, delivery_id)
    if current is not None:
        return str(_verify_manifest_path(current))
    delivery = get_delivery(delivery_id)
    if str(delivery["status"] or "").upper() != "DELIVERED":
        raise HTTPException(status_code=409, detail="Delivery Notes require a DELIVERED delivery.")
    path = delivery_note_path(delivery)
    if path.exists():
        raise HTTPException(status_code=409, detail="Delivery Note path exists without an issued manifest.")
    build_delivery_pdf(delivery, path)
    connection.execute(
        """
        INSERT INTO delivery_documents_manifest (
            delivery_id,document_kind,audience,version,file_path,sha256,is_current
        ) VALUES (?,'DELIVERY_NOTE','CUSTOMER',1,?,?,1)
        """,
        (delivery_id, portable_manifest_path(path), _digest(path)),
    )
    return str(path)
