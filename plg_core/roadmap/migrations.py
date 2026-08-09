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
