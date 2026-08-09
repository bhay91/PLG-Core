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
    '''),
    "plg_core/sales/service.py": block('''
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

            decision_rules = {
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
            result = dict(invoice)
            result["items"] = [dict(row) for row in items]
            result["payments"] = [dict(row) for row in payments]
            result["payments_received"] = round(sum(float(row["amount"] or 0) for row in payments), 2)
            return result
    '''),
    "plg_core/sales/routes.py": block('''
        from contextlib import closing
        from fastapi import APIRouter, HTTPException
        from legacy_app import get_connection
        from plg_core.sales.models import ConversionRequest, QuoteStatusUpdate
        from plg_core.sales.service import get_invoice, get_quote, list_invoices, list_quotes, update_quote_status

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
    '''),
    "plg_core/supply/service.py": block('''
        from contextlib import closing
        from fastapi import HTTPException
        from legacy_app import get_connection
        from plg_core.audit import write_audit
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
                order = connection.execute("SELECT * FROM supplier_orders WHERE id=?", (order_id,)).fetchone()
                if order is None:
                    raise HTTPException(status_code=404, detail="Supplier order not found.")
                items = connection.execute(
                    "SELECT * FROM supplier_order_items WHERE order_id=? ORDER BY id",
                    (order_id,),
                ).fetchall()
                receipts = connection.execute(
                    "SELECT * FROM receiving_events WHERE order_id=? ORDER BY id DESC",
                    (order_id,),
                ).fetchall()
            result = dict(order)
            result["items"] = [dict(row) for row in items]
            result["receipts"] = [dict(row) for row in receipts]
            return result

        def place_order(order_id: int):
            with closing(get_connection()) as connection:
                order = connection.execute("SELECT * FROM supplier_orders WHERE id=?", (order_id,)).fetchone()
                if order is None:
                    raise HTTPException(status_code=404, detail="Supplier order not found.")
                connection.execute("""
                    UPDATE supplier_orders
                    SET status='ORDERED', ordered_at=COALESCE(ordered_at,CURRENT_TIMESTAMP),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (order_id,))
                write_audit(connection, action="SUPPLIER_ORDER_PLACED", entity_type="SUPPLIER_ORDER",
                            entity_id=order_id, summary=f"{order['po_number']} marked ordered")
                connection.commit()
            return get_order(order_id)

        def record_receipt(order_id: int, payload: ReceiptCreate):
            if not payload.items:
                raise HTTPException(status_code=400, detail="Receipt requires at least one item.")
            with closing(get_connection()) as connection:
                order = connection.execute("SELECT * FROM supplier_orders WHERE id=?", (order_id,)).fetchone()
                if order is None:
                    raise HTTPException(status_code=404, detail="Supplier order not found.")
                cur = connection.execute(
                    "INSERT INTO receiving_events (order_id,notes) VALUES (?,?)",
                    (order_id, payload.notes.strip()),
                )
                receipt_id = int(cur.lastrowid)
                receipt_number = f"PPS-RCPT-{receipt_id:04d}"
                connection.execute("UPDATE receiving_events SET receipt_number=? WHERE id=?", (receipt_number, receipt_id))
                for incoming in payload.items:
                    item = connection.execute(
                        "SELECT * FROM supplier_order_items WHERE id=? AND order_id=?",
                        (incoming.order_item_id, order_id),
                    ).fetchone()
                    if item is None:
                        raise HTTPException(status_code=404, detail=f"Order item {incoming.order_item_id} not found.")
                    remaining = int(item["quantity_ordered"] or 0) - int(item["quantity_received"] or 0)
                    if incoming.quantity_received > remaining:
                        raise HTTPException(status_code=409, detail=f"Only {remaining} remain for order item {item['id']}.")
                    connection.execute("""
                        INSERT INTO receiving_event_items (receipt_id,order_item_id,quantity_received)
                        VALUES (?, ?, ?)
                    """, (receipt_id, item["id"], incoming.quantity_received))
                    connection.execute("""
                        UPDATE supplier_order_items
                        SET quantity_received=quantity_received+?, updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                    """, (incoming.quantity_received, item["id"]))
                remaining_rows = connection.execute("""
                    SELECT COUNT(*) FROM supplier_order_items
                    WHERE order_id=? AND quantity_received < quantity_ordered
                """, (order_id,)).fetchone()[0]
                status = "RECEIVED" if remaining_rows == 0 else "PARTIAL"
                connection.execute("""
                    UPDATE supplier_orders
                    SET status=?, received_at=CASE WHEN ?='RECEIVED' THEN CURRENT_TIMESTAMP ELSE received_at END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (status, status, order_id))
                write_audit(connection, action="PARTS_RECEIVED", entity_type="SUPPLIER_ORDER",
                            entity_id=order_id, summary=f"{receipt_number} recorded for {order['po_number']}")
                connection.commit()
            return get_order(order_id)

        def create_delivery(job_id: int, payload: DeliveryCreate):
            with closing(get_connection()) as connection:
                job = connection.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job is None:
                    raise HTTPException(status_code=404, detail="Job not found.")
                existing = connection.execute(
                    "SELECT * FROM deliveries WHERE job_id=? AND status='READY' ORDER BY id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                if existing:
                    return {"delivery_id": existing["id"], "status": existing["status"], "existing": True}
                available = connection.execute("""
                    SELECT oi.id, oi.quantity_received,
                           COALESCE((
                               SELECT SUM(di.quantity_delivered)
                               FROM delivery_items di
                               JOIN deliveries d ON d.id=di.delivery_id
                               WHERE di.order_item_id=oi.id AND d.status!='CANCELLED'
                           ),0) AS delivered
                    FROM supplier_order_items oi
                    JOIN supplier_orders po ON po.id=oi.order_id
                    WHERE po.job_id=? AND oi.quantity_received>0
                """, (job_id,)).fetchall()
                deliverable = [(int(row["id"]), int(row["quantity_received"])-int(row["delivered"])) for row in available
                               if int(row["quantity_received"])-int(row["delivered"]) > 0]
                if not deliverable:
                    raise HTTPException(status_code=409, detail="No received parts are ready for delivery.")
                invoice = connection.execute(
                    "SELECT id FROM invoices WHERE job_id=? ORDER BY id DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                cur = connection.execute("""
                    INSERT INTO deliveries (job_id,invoice_id,recipient,notes)
                    VALUES (?, ?, ?, ?)
                """, (job_id, invoice["id"] if invoice else None, payload.recipient.strip(), payload.notes.strip()))
                delivery_id = int(cur.lastrowid)
                for order_item_id, qty in deliverable:
                    connection.execute("""
                        INSERT INTO delivery_items (delivery_id,order_item_id,quantity_delivered)
                        VALUES (?, ?, ?)
                    """, (delivery_id, order_item_id, qty))
                write_audit(connection, action="DELIVERY_CREATED", entity_type="DELIVERY",
                            entity_id=delivery_id, summary=f"Delivery prepared for job {job_id}")
                connection.commit()
            return {"delivery_id": delivery_id, "status": "READY", "existing": False}

        def complete_delivery(delivery_id: int):
            with closing(get_connection()) as connection:
                delivery = connection.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                if delivery is None:
                    raise HTTPException(status_code=404, detail="Delivery not found.")
                connection.execute("""
                    UPDATE deliveries SET status='DELIVERED',
                        delivery_date=COALESCE(delivery_date,CURRENT_TIMESTAMP),
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                """, (delivery_id,))
                connection.execute("UPDATE jobs SET status='DELIVERED' WHERE id=?", (delivery["job_id"],))
                write_audit(connection, action="DELIVERY_COMPLETED", entity_type="DELIVERY",
                            entity_id=delivery_id, summary=f"Job {delivery['job_id']} delivered")
                connection.commit()
            return {"delivery_id": delivery_id, "status": "DELIVERED", "job_id": delivery["job_id"]}
    '''),
    "plg_core/supply/routes.py": block('''
        from fastapi import APIRouter
        from plg_core.supply.models import DeliveryCreate, ReceiptCreate
        from plg_core.supply.service import (
            complete_delivery, create_delivery, create_orders_from_paid_invoice,
            get_order, list_orders, place_order, record_receipt,
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
