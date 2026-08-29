from __future__ import annotations

from contextlib import closing
from collections.abc import Callable
import sqlite3

from legacy_app import get_connection


Migration = tuple[str, Callable[[sqlite3.Connection], None]]


def _migration_0001_basket_foundation(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS baskets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'OPEN',
            currency TEXT NOT NULL DEFAULT 'USD',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            committed_at TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS basket_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT DEFAULT '',
            trust_level TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
            shipping_total REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'USD',
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (basket_id) REFERENCES baskets(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS basket_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_id INTEGER NOT NULL,
            source_id INTEGER,
            requested_description TEXT NOT NULL,
            manufacturer_part_number TEXT DEFAULT '',
            supplier_part_number TEXT DEFAULT '',
            supplier_name TEXT DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'AFTERMARKET',
            brand TEXT DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
            supplier_unit_cost REAL,
            availability TEXT DEFAULT '',
            lead_time TEXT DEFAULT '',
            selected INTEGER NOT NULL DEFAULT 1 CHECK (selected IN (0, 1)),
            confidence REAL,
            source_url TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (basket_id) REFERENCES baskets(id) ON DELETE CASCADE,
            FOREIGN KEY (source_id) REFERENCES basket_sources(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS basket_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_id INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            media_type TEXT DEFAULT '',
            source_name TEXT DEFAULT '',
            uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (basket_id) REFERENCES baskets(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS basket_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_id INTEGER NOT NULL,
            activity_type TEXT NOT NULL,
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (basket_id) REFERENCES baskets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_baskets_job_id
            ON baskets(job_id);

        CREATE INDEX IF NOT EXISTS idx_basket_items_basket_id
            ON basket_items(basket_id);

        CREATE INDEX IF NOT EXISTS idx_basket_items_source_id
            ON basket_items(source_id);

        CREATE INDEX IF NOT EXISTS idx_basket_sources_basket_id
            ON basket_sources(basket_id);

        CREATE INDEX IF NOT EXISTS idx_basket_activity_basket_id
            ON basket_activity(basket_id);
        """
    )


def _migration_0002_machine_registry(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS machines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            machine_number TEXT UNIQUE,
            name TEXT NOT NULL DEFAULT '',
            manufacturer TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            vin_pin_serial TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL DEFAULT '',
            engine_serial TEXT NOT NULL DEFAULT '',
            transmission TEXT NOT NULL DEFAULT '',
            component_details TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_machines_customer_id
            ON machines(customer_id);

        CREATE INDEX IF NOT EXISTS idx_machines_serial
            ON machines(vin_pin_serial);
        """
    )

    job_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "machine_id" not in job_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN machine_id INTEGER")

    existing_jobs = connection.execute(
        """
        SELECT id, customer_id, manufacturer, machine, pin_serial
        FROM jobs
        WHERE customer_id IS NOT NULL AND machine_id IS NULL
          AND (TRIM(COALESCE(manufacturer, '')) != ''
               OR TRIM(COALESCE(machine, '')) != ''
               OR TRIM(COALESCE(pin_serial, '')) != '')
        ORDER BY id
        """
    ).fetchall()

    for job in existing_jobs:
        manufacturer = (job["manufacturer"] or "").strip()
        model = (job["machine"] or "").strip()
        serial = (job["pin_serial"] or "").strip()
        machine = connection.execute(
            """
            SELECT id FROM machines
            WHERE customer_id = ?
              AND LOWER(TRIM(manufacturer)) = LOWER(TRIM(?))
              AND LOWER(TRIM(model)) = LOWER(TRIM(?))
              AND LOWER(TRIM(vin_pin_serial)) = LOWER(TRIM(?))
            ORDER BY id LIMIT 1
            """,
            (job["customer_id"], manufacturer, model, serial),
        ).fetchone()

        if machine is None:
            display_name = " ".join(part for part in (manufacturer, model) if part).strip()
            if not display_name:
                display_name = serial or "Machine"
            cursor = connection.execute(
                """
                INSERT INTO machines (
                    customer_id, name, manufacturer, model, vin_pin_serial
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (job["customer_id"], display_name, manufacturer, model, serial),
            )
            machine_id = cursor.lastrowid
            connection.execute(
                "UPDATE machines SET machine_number = ? WHERE id = ?",
                (f"PPS-M-{machine_id:04d}", machine_id),
            )
        else:
            machine_id = machine["id"]

        connection.execute(
            "UPDATE jobs SET machine_id = ? WHERE id = ?",
            (machine_id, job["id"]),
        )

    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_machine_id ON jobs(machine_id)"
    )


def _migration_0003_universal_registry(connection: sqlite3.Connection) -> None:
    machine_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(machines)").fetchall()
    }
    if "registry_type" not in machine_columns:
        connection.execute(
            "ALTER TABLE machines ADD COLUMN registry_type TEXT NOT NULL DEFAULT 'machine'"
        )

    # Preserve existing records while applying practical defaults where the type is obvious.
    connection.execute(
        """
        UPDATE machines
        SET registry_type = CASE
            WHEN LENGTH(REPLACE(TRIM(vin_pin_serial), ' ', '')) = 17 THEN 'vehicle'
            WHEN LOWER(name || ' ' || manufacturer || ' ' || model) LIKE '%hummer%' THEN 'vehicle'
            WHEN LOWER(name || ' ' || manufacturer || ' ' || model) LIKE '%trailer%' THEN 'trailer'
            WHEN LOWER(name || ' ' || manufacturer || ' ' || model) LIKE '%generator%' THEN 'generator'
            WHEN LOWER(name || ' ' || manufacturer || ' ' || model) LIKE '%engine%' THEN 'engine'
            WHEN LOWER(name || ' ' || manufacturer || ' ' || model) LIKE '%outboard%' THEN 'marine'
            WHEN LOWER(name || ' ' || manufacturer || ' ' || model) LIKE '%marine%' THEN 'marine'
            ELSE COALESCE(NULLIF(registry_type, ''), 'machine')
        END
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_machines_registry_type ON machines(registry_type)"
    )


def _migration_0004_customer_requests(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS customer_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_number TEXT UNIQUE,
            request_text TEXT NOT NULL DEFAULT '',
            individual_name TEXT NOT NULL DEFAULT '',
            company_name TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            reminder_date TEXT,
            status TEXT NOT NULL DEFAULT 'NEW'
                CHECK (status IN ('NEW', 'WAITING', 'READY', 'COMPLETED')),
            customer_id INTEGER,
            machine_id INTEGER,
            job_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL,
            FOREIGN KEY (machine_id) REFERENCES machines(id) ON DELETE SET NULL,
            FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS customer_request_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT '',
            uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (request_id) REFERENCES customer_requests(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_customer_requests_status
            ON customer_requests(status);
        CREATE INDEX IF NOT EXISTS idx_customer_requests_reminder
            ON customer_requests(reminder_date);
        CREATE INDEX IF NOT EXISTS idx_customer_request_attachments_request
            ON customer_request_attachments(request_id);
        """
    )


def _migration_0005_request_job_ready(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(customer_requests)").fetchall()
    }
    additions = {
        "location": "TEXT NOT NULL DEFAULT ''",
        "registry_type": "TEXT NOT NULL DEFAULT 'other'",
        "manufacturer": "TEXT NOT NULL DEFAULT ''",
        "model": "TEXT NOT NULL DEFAULT ''",
        "year": "TEXT NOT NULL DEFAULT ''",
        "identifier": "TEXT NOT NULL DEFAULT ''",
        "requested_parts": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE customer_requests ADD COLUMN {name} {definition}")

    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_customer_requests_customer_id ON customer_requests(customer_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_customer_requests_machine_id ON customer_requests(machine_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_customer_requests_job_id ON customer_requests(job_id)"
    )


def _migration_0006_part_status_timeline(
    connection: sqlite3.Connection,
) -> None:
    basket_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(basket_items)"
        ).fetchall()
    }

    if "markup_percent" not in basket_columns:
        connection.execute(
            """
            ALTER TABLE basket_items
            ADD COLUMN markup_percent REAL
            """
        )

    if "part_status" not in basket_columns:
        connection.execute(
            """
            ALTER TABLE basket_items
            ADD COLUMN part_status TEXT NOT NULL DEFAULT 'RESEARCH'
            """
        )

    connection.execute(
        """
        UPDATE basket_items
        SET part_status = 'RESEARCH'
        WHERE part_status IS NULL OR TRIM(part_status) = ''
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_timeline_job
        ON job_timeline(job_id, created_at DESC)
        """
    )


def _migration_0007_smart_intake_locations(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            location_name TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_locations_customer
        ON customer_locations(customer_id)
        """
    )




def _migration_0008_job_revenue_adjustments(
    connection: sqlite3.Connection,
) -> None:
    """Add optional job-level Service Charge and Sourcing Fee fields."""

    job_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(jobs)"
        ).fetchall()
    }

    additions = {
        "service_charge": "REAL NOT NULL DEFAULT 0",
        "service_charge_description": "TEXT NOT NULL DEFAULT ''",
        "sourcing_fee": "REAL NOT NULL DEFAULT 0",
        "sourcing_fee_description": "TEXT NOT NULL DEFAULT ''",
    }

    for name, definition in additions.items():
        if name not in job_columns:
            connection.execute(
                f"ALTER TABLE jobs ADD COLUMN {name} {definition}"
            )



def _migration_0009_smart_intake_location_links(
    connection: sqlite3.Connection,
) -> None:
    """Ensure Smart Intake location relationships exist."""

    request_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(customer_requests)"
        ).fetchall()
    }

    if "customer_location_id" not in request_columns:
        connection.execute(
            """
            ALTER TABLE customer_requests
            ADD COLUMN customer_location_id INTEGER
            """
        )

    machine_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(machines)"
        ).fetchall()
    }

    if "customer_location_id" not in machine_columns:
        connection.execute(
            """
            ALTER TABLE machines
            ADD COLUMN customer_location_id INTEGER
            """
        )


MIGRATIONS: list[Migration] = [
    ("0001_basket_foundation", _migration_0001_basket_foundation),
    ("0002_machine_registry", _migration_0002_machine_registry),
    ("0003_universal_registry", _migration_0003_universal_registry),
    ("0004_customer_requests", _migration_0004_customer_requests),
    ("0005_request_job_ready", _migration_0005_request_job_ready),
    ("0006_part_status_timeline", _migration_0006_part_status_timeline),    ("0007_smart_intake_locations", _migration_0007_smart_intake_locations),

    ("0008_job_revenue_adjustments", _migration_0008_job_revenue_adjustments),
    ("0009_smart_intake_location_links", _migration_0009_smart_intake_location_links),
]


def run_migrations() -> None:
    # The modular delivery/receiving integrity migrations build on the
    # supply-chain tables introduced by the roadmap migration set.
    from plg_core.roadmap.migrations import run_roadmap_migrations

    run_roadmap_migrations()
    with closing(get_connection()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        applied = {
            row["migration_id"]
            for row in connection.execute(
                "SELECT migration_id FROM schema_migrations"
            ).fetchall()
        }

        for migration_id, migration in MIGRATIONS:
            if migration_id in applied:
                continue

            migration(connection)
            connection.execute(
                """
                INSERT INTO schema_migrations (migration_id)
                VALUES (?)
                """,
                (migration_id,),
            )

        connection.commit()
def _migration_0009_opportunities(
    connection: sqlite3.Connection,
) -> None:
    """Add Opportunity tracking, machines, research, and follow-up data."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_number TEXT UNIQUE,
            customer_id INTEGER,
            title TEXT NOT NULL DEFAULT '',
            request_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'OPEN',
            follow_up_date TEXT,
            estimated_value REAL NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT '',
            converted_job_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (converted_job_id) REFERENCES jobs(id)
        );

        CREATE TABLE IF NOT EXISTS opportunity_machines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL,
            machine_id INTEGER,
            manufacturer TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            vin_pin_serial TEXT NOT NULL DEFAULT '',
            engine TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (opportunity_id)
                REFERENCES opportunities(id) ON DELETE CASCADE,
            FOREIGN KEY (machine_id)
                REFERENCES machines(id)
        );

        CREATE TABLE IF NOT EXISTS opportunity_research (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL,
            opportunity_machine_id INTEGER,
            part_description TEXT NOT NULL DEFAULT '',
            oem_part_number TEXT NOT NULL DEFAULT '',
            alternate_part_number TEXT NOT NULL DEFAULT '',
            supplier_name TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT '',
            confidence REAL,
            research_status TEXT NOT NULL DEFAULT 'CANDIDATE',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (opportunity_id)
                REFERENCES opportunities(id) ON DELETE CASCADE,
            FOREIGN KEY (opportunity_machine_id)
                REFERENCES opportunity_machines(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_opportunities_customer
            ON opportunities(customer_id);

        CREATE INDEX IF NOT EXISTS idx_opportunities_follow_up
            ON opportunities(follow_up_date);

        CREATE INDEX IF NOT EXISTS idx_opportunity_machines_opportunity
            ON opportunity_machines(opportunity_id);

        CREATE INDEX IF NOT EXISTS idx_opportunity_research_opportunity
            ON opportunity_research(opportunity_id);
        """
    )
MIGRATIONS.append(("0009_opportunities", _migration_0009_opportunities))

def _migration_0010_custom_invoices(
    connection: sqlite3.Connection,
) -> None:
    """Add editable Custom Invoices linked to paid invoices."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS custom_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL UNIQUE,
            custom_invoice_number TEXT NOT NULL UNIQUE,
            adjustment_mode TEXT NOT NULL DEFAULT 'MANUAL',
            adjustment_value REAL,
            custom_total REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (invoice_id)
                REFERENCES invoices(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS custom_invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            custom_invoice_id INTEGER NOT NULL,
            invoice_item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            custom_unit_price REAL NOT NULL DEFAULT 0,
            custom_line_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (custom_invoice_id)
                REFERENCES custom_invoices(id) ON DELETE CASCADE,
            FOREIGN KEY (invoice_item_id)
                REFERENCES invoice_items(id)
        );

        CREATE INDEX IF NOT EXISTS idx_custom_invoice_items_invoice
            ON custom_invoice_items(custom_invoice_id);
        """
    )


MIGRATIONS.append(
    ("0010_custom_invoices", _migration_0010_custom_invoices)
)


def _migration_0033_operator_quote_revisions(
    connection: sqlite3.Connection,
) -> None:
    """Add the minimal operator-workflow metadata for Batch 2B."""
    _add_columns(
        connection,
        "work_revisions",
        {
            "purpose": (
                "TEXT NOT NULL DEFAULT 'INITIAL' "
                "CHECK (purpose IN ('INITIAL','DRAFT_CORRECTION','QUOTE_REVISION','REOPEN_REVISION'))"
            ),
            "source_quote_status": "TEXT NOT NULL DEFAULT ''",
        },
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_quotes_one_per_work_revision
        ON quotes(work_revision_id)
        WHERE work_revision_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_quotes_supersedes
        ON quotes(supersedes_quote_id)
        """
    )


def _migration_0034_workflow_followups_and_internal_parts(
    connection: sqlite3.Connection,
) -> None:
    """Add Batch 3A operator follow-ups and stable internal part references."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_follow_ups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            category TEXT NOT NULL
                CHECK (category IN ('CUSTOMER_INFORMATION','OPERATOR_ATTENTION')),
            summary TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'OPEN'
                CHECK (status IN ('OPEN','RECEIVED','RESOLVED','CANCELLED')),
            requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            received_at TEXT,
            resolved_at TEXT,
            resolution TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_job_follow_ups_queue
            ON job_follow_ups(status, category, requested_at);
        CREATE INDEX IF NOT EXISTS idx_job_follow_ups_job
            ON job_follow_ups(job_id, status);

        CREATE TABLE IF NOT EXISTS internal_part_number_sequence (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            last_number INTEGER NOT NULL DEFAULT 0 CHECK (last_number >= 0)
        );
        INSERT OR IGNORE INTO internal_part_number_sequence(singleton,last_number)
        VALUES (1,0);
        """
    )
    for table in (
        "basket_items",
        "work_revision_items",
        "job_parts",
        "quote_items",
        "invoice_items",
    ):
        _add_columns(
            connection,
            table,
            {"internal_part_number": "TEXT NOT NULL DEFAULT ''"},
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_basket_items_internal_part "
        "ON basket_items(internal_part_number)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_parts_internal_part "
        "ON job_parts(internal_part_number)"
    )


def _migration_0032_quote_identity_snapshot_foundation(
    connection: sqlite3.Connection,
) -> None:
    """Add immutable identity slots for newly-created quote snapshots."""
    _add_columns(
        connection,
        "quotes",
        {
            "customer_name_snapshot": "TEXT",
            "company_snapshot": "TEXT",
            "phone_snapshot": "TEXT",
            "email_snapshot": "TEXT",
            "address_snapshot": "TEXT",
            "manufacturer_snapshot": "TEXT",
            "machine_snapshot": "TEXT",
            "pin_serial_snapshot": "TEXT",
        },
    )


def _migration_0030_legacy_revision_snapshots(
    connection: sqlite3.Connection,
) -> None:
    """Complete synthetic snapshots created by the already-applied 0029 migration."""
    connection.execute(
        """
        INSERT INTO work_revision_sources (
            work_revision_id, source_key, source_name, source_url,
            trust_level, shipping_total, currency, original_basket_source_id
        )
        SELECT wr.id, bs.source_key, bs.source_name, COALESCE(bs.source_url, ''),
               bs.trust_level, COALESCE(bs.shipping_total, 0), bs.currency, bs.id
        FROM work_revisions wr
        JOIN baskets b ON b.job_id=wr.job_id
        JOIN basket_sources bs ON bs.basket_id=b.id
        WHERE wr.is_synthetic=1
          AND NOT EXISTS (
              SELECT 1 FROM work_revision_sources existing
              WHERE existing.work_revision_id=wr.id
                AND existing.original_basket_source_id=bs.id
          )
        """
    )
    connection.execute(
        """
        INSERT INTO work_revision_items (
            work_revision_id, revision_source_id,
            requested_description, manufacturer_part_number,
            alternate_part_number, supplier_part_number, supplier_name,
            source_type, brand, quantity, supplier_unit_cost, markup_percent,
            pricing_mode, customer_unit_price_override,
            effective_customer_unit_price, recommended_markup_percent,
            part_status, verification_status, verification_note,
            availability, lead_time, selected, confidence, source_url
        )
        SELECT wr.id, wrs.id,
               bi.requested_description, COALESCE(bi.manufacturer_part_number, ''),
               COALESCE(bi.alternate_part_number, ''),
               COALESCE(bi.supplier_part_number, ''), COALESCE(bi.supplier_name, ''),
               COALESCE(bi.source_type, 'AFTERMARKET'), COALESCE(bi.brand, ''),
               bi.quantity, bi.supplier_unit_cost, bi.markup_percent,
               CASE WHEN bi.customer_unit_price_override IS NULL
                    THEN 'AUTO' ELSE 'OVERRIDE' END,
               bi.customer_unit_price_override,
               CASE WHEN bi.customer_unit_price_override IS NOT NULL
                    THEN bi.customer_unit_price_override
                    ELSE ROUND(COALESCE(bi.supplier_unit_cost, 0) *
                         (1 + COALESCE(bi.markup_percent, 30) / 100.0), 2) END,
               bi.markup_percent,
               COALESCE(bi.part_status, 'RESEARCH'),
               COALESCE(bi.verification_status, 'UNVERIFIED'),
               COALESCE(bi.verification_note, ''), COALESCE(bi.availability, ''),
               COALESCE(bi.lead_time, ''), bi.selected, bi.confidence,
               COALESCE(bi.source_url, '')
        FROM work_revisions wr
        JOIN baskets b ON b.job_id=wr.job_id
        JOIN basket_items bi ON bi.basket_id=b.id
        LEFT JOIN work_revision_sources wrs
          ON wrs.work_revision_id=wr.id
         AND wrs.original_basket_source_id=bi.source_id
        WHERE wr.is_synthetic=1
          AND NOT EXISTS (
              SELECT 1 FROM work_revision_items existing
              WHERE existing.work_revision_id=wr.id
          )
        """
    )
    connection.execute(
        """
        INSERT INTO work_revision_attachments (
            work_revision_id, original_basket_attachment_id,
            original_filename, stored_filename, file_path, media_type, source_name
        )
        SELECT wr.id, ba.id, ba.original_filename, ba.stored_filename,
               ba.file_path, COALESCE(ba.media_type, ''), COALESCE(ba.source_name, '')
        FROM work_revisions wr
        JOIN baskets b ON b.job_id=wr.job_id
        JOIN basket_attachments ba ON ba.basket_id=b.id
        WHERE wr.is_synthetic=1
          AND NOT EXISTS (
              SELECT 1 FROM work_revision_attachments existing
              WHERE existing.work_revision_id=wr.id
                AND existing.original_basket_attachment_id=ba.id
          )
        """
    )


def _migration_0011_machine_ownership_history(
    connection: sqlite3.Connection,
) -> None:
    """Track machine ownership changes without rewriting historical Jobs."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS machine_ownership_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id INTEGER NOT NULL,
            from_customer_id INTEGER,
            to_customer_id INTEGER NOT NULL,
            transfer_note TEXT NOT NULL DEFAULT '',
            transferred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (machine_id) REFERENCES machines(id),
            FOREIGN KEY (from_customer_id) REFERENCES customers(id),
            FOREIGN KEY (to_customer_id) REFERENCES customers(id)
        );

        CREATE INDEX IF NOT EXISTS idx_machine_ownership_history_machine
            ON machine_ownership_history(machine_id);
        """
    )


MIGRATIONS.append(
    ("0011_machine_ownership_history", _migration_0011_machine_ownership_history)
)

def _migration_0012_part_verification_status(
    connection: sqlite3.Connection,
) -> None:
    """Add independent verification state for basket candidates."""

    basket_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(basket_items)"
        ).fetchall()
    }

    if "verification_status" not in basket_columns:
        connection.execute(
            """
            ALTER TABLE basket_items
            ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED'
            """
        )

    connection.execute(
        """
        UPDATE basket_items
        SET verification_status = 'UNVERIFIED'
        WHERE verification_status IS NULL
           OR TRIM(verification_status) = ''
        """
    )


MIGRATIONS.append(
    ("0012_part_verification_status", _migration_0012_part_verification_status)
)

def _migration_0013_basket_verification_note(
    connection: sqlite3.Connection,
) -> None:
    """Store the current verification or override note on basket candidates."""

    basket_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(basket_items)"
        ).fetchall()
    }

    if "verification_note" not in basket_columns:
        connection.execute(
            """
            ALTER TABLE basket_items
            ADD COLUMN verification_note TEXT NOT NULL DEFAULT ''
            """
        )


MIGRATIONS.append(
    ("0013_basket_verification_note", _migration_0013_basket_verification_note)
)

def _migration_0014_part_source_verification(
    connection: sqlite3.Connection,
) -> None:
    """Add verification state to supplier and OEM part candidates."""

    source_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(part_sources)"
        ).fetchall()
    }

    if "verification_status" not in source_columns:
        connection.execute(
            """
            ALTER TABLE part_sources
            ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED'
            """
        )

    if "verification_note" not in source_columns:
        connection.execute(
            """
            ALTER TABLE part_sources
            ADD COLUMN verification_note TEXT NOT NULL DEFAULT ''
            """
        )

    connection.execute(
        """
        UPDATE part_sources
        SET verification_status = 'UNVERIFIED'
        WHERE verification_status IS NULL
           OR TRIM(verification_status) = ''
        """
    )


MIGRATIONS.append(
    ("0014_part_source_verification", _migration_0014_part_source_verification)
)

def _migration_0015_alternate_part_numbers(
    connection: sqlite3.Connection,
) -> None:
    """Preserve alternate and superseded part numbers through the workflow."""

    basket_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(basket_items)"
        ).fetchall()
    }
    if "alternate_part_number" not in basket_columns:
        connection.execute(
            """
            ALTER TABLE basket_items
            ADD COLUMN alternate_part_number TEXT NOT NULL DEFAULT ''
            """
        )

    part_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(job_parts)"
        ).fetchall()
    }
    if "alternate_part_number" not in part_columns:
        connection.execute(
            """
            ALTER TABLE job_parts
            ADD COLUMN alternate_part_number TEXT NOT NULL DEFAULT ''
            """
        )


MIGRATIONS.append(
    ("0015_alternate_part_numbers", _migration_0015_alternate_part_numbers)
)

def _migration_0016_part_source_confidence(
    connection: sqlite3.Connection,
) -> None:
    """Preserve sourcing candidate confidence on permanent part sources."""

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(part_sources)"
        ).fetchall()
    }

    if "confidence" not in columns:
        connection.execute(
            """
            ALTER TABLE part_sources
            ADD COLUMN confidence REAL
            """
        )


MIGRATIONS.append(
    ("0016_part_source_confidence", _migration_0016_part_source_confidence)
)

def _migration_0017_part_source_compatibility(
    connection: sqlite3.Connection,
) -> None:
    """Track candidate compatibility with the linked machine identity."""

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(part_sources)"
        ).fetchall()
    }

    if "compatibility_status" not in columns:
        connection.execute(
            """
            ALTER TABLE part_sources
            ADD COLUMN compatibility_status TEXT NOT NULL DEFAULT 'UNCHECKED'
            """
        )

    if "compatibility_note" not in columns:
        connection.execute(
            """
            ALTER TABLE part_sources
            ADD COLUMN compatibility_note TEXT NOT NULL DEFAULT ''
            """
        )


MIGRATIONS.append(
    ("0017_part_source_compatibility", _migration_0017_part_source_compatibility)
)

def _migration_0022_request_opportunity_link(
    connection: sqlite3.Connection,
) -> None:
    """Link Opportunities back to their originating Customer Request."""

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(opportunities)"
        ).fetchall()
    }

    if "customer_request_id" not in columns:
        connection.execute(
            """
            ALTER TABLE opportunities
            ADD COLUMN customer_request_id INTEGER
            """
        )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS
            idx_opportunities_customer_request
        ON opportunities(customer_request_id)
        """
    )


MIGRATIONS.append(
    (
        "0022_request_opportunity_link",
        _migration_0022_request_opportunity_link,
    )
)


def _migration_0050_structured_receiving_exceptions(
    connection: sqlite3.Connection,
) -> None:
    """Add structured receiving exceptions without changing accepted inventory."""
    _add_columns(
        connection,
        "receiving_events",
        {
            "event_kind": "TEXT NOT NULL DEFAULT 'RECEIPT'",
            "request_fingerprint": "TEXT NOT NULL DEFAULT ''",
        },
    )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS receiving_exception_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            order_item_id INTEGER NOT NULL,
            disposition TEXT NOT NULL
                CHECK(disposition IN (
                    'DAMAGED','WRONG_ITEM','QUARANTINED','REJECTED','SHORT'
                )),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            reason TEXT NOT NULL CHECK(TRIM(reason) != ''),
            notes TEXT NOT NULL DEFAULT '',
            supplier_reference TEXT NOT NULL DEFAULT '',
            evidence_reference TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(receipt_id) REFERENCES receiving_events(id),
            FOREIGN KEY(order_item_id) REFERENCES supplier_order_items(id),
            UNIQUE(receipt_id, order_item_id, disposition)
        );

        CREATE INDEX IF NOT EXISTS idx_receiving_exception_receipt
        ON receiving_exception_items(receipt_id);

        CREATE INDEX IF NOT EXISTS idx_receiving_exception_item_disposition
        ON receiving_exception_items(order_item_id, disposition);

        CREATE TABLE IF NOT EXISTS receiving_exception_resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exception_item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            resolution TEXT NOT NULL CHECK(resolution IN (
                'RETURNED','DISPOSED','REPLACEMENT_EXPECTED',
                'REPLACED_BY_RECEIPT','CLEARED_TO_ACCEPTED',
                'RECLASSIFIED_REJECTED','BACKORDER_CONFIRMED','CLOSED'
            )),
            related_receipt_id INTEGER NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            reason TEXT NOT NULL CHECK(TRIM(reason) != ''),
            notes TEXT NOT NULL DEFAULT '',
            supplier_reference TEXT NOT NULL DEFAULT '',
            evidence_reference TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(exception_item_id) REFERENCES receiving_exception_items(id),
            FOREIGN KEY(related_receipt_id) REFERENCES receiving_events(id)
        );

        CREATE INDEX IF NOT EXISTS idx_receiving_resolution_exception_created
        ON receiving_exception_resolutions(exception_item_id, created_at, id);

        CREATE UNIQUE INDEX IF NOT EXISTS uq_receiving_resolution_idempotency
        ON receiving_exception_resolutions(exception_item_id, idempotency_key)
        WHERE idempotency_key != '';

        CREATE TABLE IF NOT EXISTS supplier_order_item_backorder_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_item_id INTEGER NOT NULL,
            event_kind TEXT NOT NULL
                CHECK(event_kind IN ('DECLARED','UPDATED','RESOLVED')),
            backordered_quantity INTEGER NOT NULL
                CHECK(backordered_quantity >= 0),
            reason TEXT NOT NULL CHECK(TRIM(reason) != ''),
            notes TEXT NOT NULL DEFAULT '',
            supplier_reference TEXT NOT NULL DEFAULT '',
            actor TEXT NOT NULL DEFAULT 'system',
            request_id TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL,
            related_receipt_id INTEGER NULL,
            fulfilled_quantity INTEGER NOT NULL DEFAULT 0
                CHECK(fulfilled_quantity >= 0),
            receipt_id_cutoff INTEGER NOT NULL DEFAULT 0
                CHECK(receipt_id_cutoff >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(event_kind != 'RESOLVED' OR backordered_quantity = 0),
            CHECK(event_kind = 'RESOLVED' OR backordered_quantity > 0),
            CHECK(event_kind = 'RESOLVED' OR fulfilled_quantity = 0),
            CHECK(related_receipt_id IS NOT NULL OR fulfilled_quantity = 0),
            FOREIGN KEY(order_item_id) REFERENCES supplier_order_items(id),
            FOREIGN KEY(related_receipt_id) REFERENCES receiving_events(id)
        );

        CREATE INDEX IF NOT EXISTS idx_backorder_item_created
        ON supplier_order_item_backorder_events(order_item_id, created_at, id);

        CREATE INDEX IF NOT EXISTS idx_backorder_receipt_item
        ON supplier_order_item_backorder_events(related_receipt_id, order_item_id)
        WHERE related_receipt_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_receiving_resolution_related_receipt
        ON receiving_exception_resolutions(related_receipt_id, resolution)
        WHERE related_receipt_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_backorder_event_idempotency
        ON supplier_order_item_backorder_events(order_item_id, idempotency_key)
        WHERE idempotency_key != '';
        """
    )


def _migration_0047_delivery_2_integrity(
    connection: sqlite3.Connection,
) -> None:
    """Add safe Delivery reservations and immutable Delivery Notes."""
    delivery_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(deliveries)"
        ).fetchall()
    }
    expected_deliveries = {
        "id", "job_id", "invoice_id", "status", "recipient",
        "delivery_date", "notes", "created_at", "updated_at",
    }
    item_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(delivery_items)"
        ).fetchall()
    }
    expected_items = {
        "id", "delivery_id", "order_item_id", "quantity_delivered",
    }
    if not expected_deliveries.issubset(delivery_columns):
        raise RuntimeError(
            "deliveries does not match the expected pre-0047 schema"
        )
    if not expected_items.issubset(item_columns):
        raise RuntimeError(
            "delivery_items does not match the expected pre-0047 schema"
        )
    for name, definition in (
        ("request_id", "TEXT NOT NULL DEFAULT ''"),
        ("source_path", "TEXT NOT NULL DEFAULT ''"),
        ("idempotency_key", "TEXT"),
    ):
        if name not in delivery_columns:
            connection.execute(
                f"ALTER TABLE deliveries ADD COLUMN {name} {definition}"
            )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_deliveries_idempotency
        ON deliveries(idempotency_key)
        WHERE idempotency_key IS NOT NULL AND idempotency_key != ''
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_deliveries_one_ready_per_job
        ON deliveries(job_id)
        WHERE status = 'READY'
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS delivery_documents_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_id INTEGER NOT NULL,
            document_kind TEXT NOT NULL DEFAULT 'DELIVERY_NOTE',
            audience TEXT NOT NULL DEFAULT 'CUSTOMER',
            version INTEGER NOT NULL DEFAULT 1,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_current INTEGER NOT NULL DEFAULT 1
                CHECK(is_current IN (0,1)),
            FOREIGN KEY(delivery_id) REFERENCES deliveries(id),
            UNIQUE(delivery_id, document_kind, audience, version)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_delivery_documents_manifest_delivery
        ON delivery_documents_manifest(delivery_id)
        """
    )


MIGRATIONS.append(
    ("0047_delivery_2_integrity", _migration_0047_delivery_2_integrity)
)


def _migration_0046_receiving_2_integrity(
    connection: sqlite3.Connection,
) -> None:
    """Add attributed, idempotent receiving and immutable receipt manifests."""
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(receiving_events)"
        ).fetchall()
    }
    expected = {
        "id", "receipt_number", "order_id", "notes", "received_at",
    }
    if not expected.issubset(columns):
        raise RuntimeError(
            "receiving_events does not match the expected pre-0046 schema"
        )
    additions = (
        ("receiver", "TEXT NOT NULL DEFAULT ''"),
        ("request_id", "TEXT NOT NULL DEFAULT ''"),
        ("source_path", "TEXT NOT NULL DEFAULT ''"),
        ("idempotency_key", "TEXT"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.execute(
                f"ALTER TABLE receiving_events ADD COLUMN {name} {definition}"
            )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_receiving_events_idempotency
        ON receiving_events(idempotency_key)
        WHERE idempotency_key IS NOT NULL AND idempotency_key != ''
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS receiving_documents_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL,
            document_kind TEXT NOT NULL DEFAULT 'RECEIVING_SUMMARY',
            audience TEXT NOT NULL DEFAULT 'INTERNAL',
            version INTEGER NOT NULL DEFAULT 1,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_current INTEGER NOT NULL DEFAULT 1
                CHECK(is_current IN (0,1)),
            FOREIGN KEY(receipt_id) REFERENCES receiving_events(id),
            UNIQUE(receipt_id, document_kind, audience, version)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_receiving_documents_receipt
        ON receiving_documents_manifest(receipt_id)
        """
    )


MIGRATIONS.append(
    ("0046_receiving_2_integrity", _migration_0046_receiving_2_integrity)
)


def _migration_0026_lifecycle_safety(
    connection: sqlite3.Connection,
) -> None:
    """Add durable lifecycle metadata and deletion tombstones."""

    job_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    job_additions = {
        "is_archived": "INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1))",
        "cancelled_at": "TEXT",
        "cancellation_reason": "TEXT NOT NULL DEFAULT ''",
        "status_before_cancel": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in job_additions.items():
        if name not in job_columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    request_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(customer_requests)"
        ).fetchall()
    }
    request_additions = {
        "is_archived": "INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1))",
        "is_cancelled": "INTEGER NOT NULL DEFAULT 0 CHECK (is_cancelled IN (0, 1))",
        "cancelled_at": "TEXT",
        "cancellation_reason": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in request_additions.items():
        if name not in request_columns:
            connection.execute(
                f"ALTER TABLE customer_requests ADD COLUMN {name} {definition}"
            )

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS deletion_tombstones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_number TEXT NOT NULL,
            former_entity_id INTEGER,
            reason TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_deletion_tombstones_entity
            ON deletion_tombstones(entity_type, entity_number);
        CREATE INDEX IF NOT EXISTS idx_jobs_archived
            ON jobs(is_archived, status);
        CREATE INDEX IF NOT EXISTS idx_requests_archived
            ON customer_requests(is_archived, status);
        """
    )


MIGRATIONS.append(("0026_lifecycle_safety", _migration_0026_lifecycle_safety))

def _migration_0023_machine_parts_history(
    connection: sqlite3.Connection,
) -> None:
    """Store permanent verified part fitments for registry machines."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS machine_parts_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id INTEGER NOT NULL,

            part_description TEXT NOT NULL DEFAULT '',
            oem_part_number TEXT NOT NULL DEFAULT '',
            alternate_part_number TEXT NOT NULL DEFAULT '',

            source_type TEXT NOT NULL DEFAULT '',
            brand TEXT NOT NULL DEFAULT '',
            supplier_part_number TEXT NOT NULL DEFAULT '',
            supplier_name TEXT NOT NULL DEFAULT '',

            fitment_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            fitment_source TEXT NOT NULL DEFAULT '',
            fitment_note TEXT NOT NULL DEFAULT '',

            source_verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            source_url TEXT NOT NULL DEFAULT '',

            supplier_cost REAL,
            customer_unit_price REAL,
            quantity INTEGER NOT NULL DEFAULT 1,

            history_status TEXT NOT NULL DEFAULT 'REFERENCE',
            original_job_number TEXT NOT NULL DEFAULT '',
            original_job_status TEXT NOT NULL DEFAULT '',
            original_captured_at TEXT,

            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (machine_id)
                REFERENCES machines(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS
            idx_machine_parts_history_machine
        ON machine_parts_history(machine_id);

        CREATE INDEX IF NOT EXISTS
            idx_machine_parts_history_oem
        ON machine_parts_history(oem_part_number);

        CREATE INDEX IF NOT EXISTS
            idx_machine_parts_history_supplier_part
        ON machine_parts_history(supplier_part_number);
        """
    )


MIGRATIONS.append(
    (
        "0023_machine_parts_history",
        _migration_0023_machine_parts_history,
    )
)

def _migration_0024_business_number_sequences(
    connection: sqlite3.Connection,
) -> None:
    """Persist PPS business-number sequences independently of row IDs."""

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pps_number_sequences (
            entity_type TEXT PRIMARY KEY,
            prefix TEXT NOT NULL UNIQUE,
            last_number INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    sources = (
        ("CUSTOMER", "customers", "customer_number", "PPS-C-"),
        ("MACHINE", "machines", "machine_number", "PPS-M-"),
        ("REQUEST", "customer_requests", "request_number", "PPS-R-"),
        ("JOB", "jobs", "job_number", "PPS-J-"),
        ("QUOTE", "quotes", "quote_number", "PPS-Q-"),
    )

    for entity_type, table, column, prefix in sources:
        highest = 0

        rows = connection.execute(
            f"""
            SELECT {column}
            FROM {table}
            WHERE {column} LIKE ?
            """,
            (f"{prefix}%",),
        ).fetchall()

        for row in rows:
            value = str(row[0] or "").strip()

            if not value.startswith(prefix):
                continue

            sequence = value[len(prefix):]

            if len(sequence) != 4 or not sequence.isdigit():
                continue

            highest = max(highest, int(sequence))

        connection.execute(
            """
            INSERT INTO pps_number_sequences (
                entity_type,
                prefix,
                last_number
            )
            VALUES (?, ?, ?)
            ON CONFLICT(entity_type) DO UPDATE SET
                prefix = excluded.prefix,
                last_number = MAX(
                    pps_number_sequences.last_number,
                    excluded.last_number
                ),
                updated_at = CURRENT_TIMESTAMP
            """,
            (entity_type, prefix, highest),
        )


MIGRATIONS.append(
    (
        "0024_business_number_sequences",
        _migration_0024_business_number_sequences,
    )
)

def _migration_0025_customer_price_override(
    connection: sqlite3.Connection,
) -> None:
    """Allow an explicit customer selling price, including zero."""

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(basket_items)"
        ).fetchall()
    }

    if "customer_unit_price_override" not in columns:
        connection.execute(
            """
            ALTER TABLE basket_items
            ADD COLUMN customer_unit_price_override REAL
            CHECK (
                customer_unit_price_override IS NULL
                OR customer_unit_price_override >= 0
            )
            """
        )


MIGRATIONS.append(
    (
        "0025_customer_price_override",
        _migration_0025_customer_price_override,
    )
)


def _migration_0027_request_cancellation_flag(
    connection: sqlite3.Connection,
) -> None:
    """Ensure request cancellation is independent of the legacy status CHECK."""
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(customer_requests)"
        ).fetchall()
    }
    if "is_cancelled" not in columns:
        connection.execute(
            "ALTER TABLE customer_requests ADD COLUMN is_cancelled "
            "INTEGER NOT NULL DEFAULT 0 CHECK (is_cancelled IN (0, 1))"
        )


MIGRATIONS.append(
    ("0027_request_cancellation_flag", _migration_0027_request_cancellation_flag)
)


def _migration_0028_one_active_quote_per_job(
    connection: sqlite3.Connection,
) -> None:
    """Enforce the current PPS rule that a Job has at most one active quote."""
    duplicates = connection.execute(
        """
        SELECT job_id
        FROM quotes
        WHERE COALESCE(is_archived, 0) = 0
        GROUP BY job_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicates is not None:
        raise RuntimeError(
            "Cannot enforce one active quote per Job: existing duplicate active quotes require review."
        )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_quotes_one_active_per_job
        ON quotes(job_id)
        WHERE is_archived = 0
        """
    )


MIGRATIONS.append(
    ("0028_one_active_quote_per_job", _migration_0028_one_active_quote_per_job)
)


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _add_columns(
    connection: sqlite3.Connection,
    table: str,
    additions: dict[str, str],
) -> None:
    existing = _table_columns(connection, table)
    for name, definition in additions.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )


def _preflight_0029_work_revisions(
    connection: sqlite3.Connection,
) -> None:
    """Refuse ambiguous legacy quote lineage rather than inventing history."""
    duplicate = connection.execute(
        """
        SELECT job_id, COUNT(*) AS quote_count
        FROM quotes
        WHERE COALESCE(is_archived, 0) = 0
        GROUP BY job_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate is not None:
        raise RuntimeError(
            "Batch 2A migration cannot determine the current quote for Job "
            f"{duplicate['job_id']}: {duplicate['quote_count']} unarchived quotes "
            "require manual review. No lineage changes were applied."
        )

    duplicate_basket = connection.execute(
        """
        SELECT job_id, COUNT(*) AS basket_count
        FROM baskets
        GROUP BY job_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate_basket is not None:
        raise RuntimeError(
            "Batch 2A migration found multiple active basket projections for Job "
            f"{duplicate_basket['job_id']}; manual review is required."
        )


def _migration_0029_work_quote_revision_foundation(
    connection: sqlite3.Connection,
) -> None:
    """Add the immutable Work Revision foundation without rewriting history."""
    _preflight_0029_work_revisions(connection)

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS work_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            revision_number INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('EDITABLE', 'COMMITTED', 'CANCELLED')),
            parent_revision_id INTEGER,
            based_on_quote_id INTEGER,
            reason TEXT NOT NULL DEFAULT '',
            lock_version INTEGER NOT NULL DEFAULT 1 CHECK (lock_version >= 1),
            is_synthetic INTEGER NOT NULL DEFAULT 0 CHECK (is_synthetic IN (0, 1)),
            service_charge REAL NOT NULL DEFAULT 0,
            service_charge_description TEXT NOT NULL DEFAULT '',
            sourcing_fee REAL NOT NULL DEFAULT 0,
            sourcing_fee_description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            committed_at TEXT,
            cancelled_at TEXT,
            UNIQUE(job_id, revision_number),
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (parent_revision_id) REFERENCES work_revisions(id),
            FOREIGN KEY (based_on_quote_id) REFERENCES quotes(id)
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_work_revisions_one_editable
            ON work_revisions(job_id) WHERE state = 'EDITABLE';
        CREATE INDEX IF NOT EXISTS idx_work_revisions_job_state
            ON work_revisions(job_id, state, revision_number DESC);

        CREATE TABLE IF NOT EXISTS work_revision_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_revision_id INTEGER NOT NULL,
            source_key TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            trust_level TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
            shipping_total REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'USD',
            original_basket_source_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (work_revision_id) REFERENCES work_revisions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_work_revision_sources_revision
            ON work_revision_sources(work_revision_id);

        CREATE TABLE IF NOT EXISTS work_revision_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_revision_id INTEGER NOT NULL,
            revision_source_id INTEGER,
            source_revision_item_id INTEGER,
            origin_quote_item_id INTEGER,
            generated_job_part_id INTEGER,
            disposition TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (disposition IN ('ACTIVE', 'REMOVED', 'REPLACED')),
            replacement_for_item_id INTEGER,
            requested_description TEXT NOT NULL,
            manufacturer_part_number TEXT NOT NULL DEFAULT '',
            alternate_part_number TEXT NOT NULL DEFAULT '',
            supplier_part_number TEXT NOT NULL DEFAULT '',
            supplier_name TEXT NOT NULL DEFAULT '',
            source_type TEXT NOT NULL DEFAULT 'AFTERMARKET',
            brand TEXT NOT NULL DEFAULT '',
            quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
            supplier_unit_cost REAL,
            markup_percent REAL,
            pricing_mode TEXT NOT NULL DEFAULT 'AUTO'
                CHECK (pricing_mode IN ('AUTO', 'OVERRIDE', 'LEGACY_FIXED')),
            customer_unit_price_override REAL,
            effective_customer_unit_price REAL NOT NULL DEFAULT 0,
            recommended_markup_percent REAL,
            part_status TEXT NOT NULL DEFAULT 'RESEARCH',
            verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED',
            verification_note TEXT NOT NULL DEFAULT '',
            availability TEXT NOT NULL DEFAULT '',
            lead_time TEXT NOT NULL DEFAULT '',
            selected INTEGER NOT NULL DEFAULT 1 CHECK (selected IN (0, 1)),
            confidence REAL,
            source_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (work_revision_id) REFERENCES work_revisions(id),
            FOREIGN KEY (revision_source_id) REFERENCES work_revision_sources(id),
            FOREIGN KEY (source_revision_item_id) REFERENCES work_revision_items(id),
            FOREIGN KEY (generated_job_part_id) REFERENCES job_parts(id),
            FOREIGN KEY (replacement_for_item_id) REFERENCES work_revision_items(id)
        );
        CREATE INDEX IF NOT EXISTS idx_work_revision_items_revision
            ON work_revision_items(work_revision_id);

        CREATE TABLE IF NOT EXISTS work_revision_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_revision_id INTEGER NOT NULL,
            original_basket_attachment_id INTEGER,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (work_revision_id) REFERENCES work_revisions(id)
        );

        CREATE TABLE IF NOT EXISTS quote_documents_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id INTEGER NOT NULL,
            audience TEXT NOT NULL CHECK (audience IN ('CUSTOMER', 'INTERNAL')),
            document_kind TEXT NOT NULL DEFAULT 'QUOTE',
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL DEFAULT '',
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_issued INTEGER NOT NULL DEFAULT 0 CHECK (is_issued IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(quote_id, audience, document_kind, is_issued),
            FOREIGN KEY (quote_id) REFERENCES quotes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_quote_documents_manifest_quote
            ON quote_documents_manifest(quote_id);
        """
    )

    _add_columns(
        connection,
        "jobs",
        {"active_work_revision_id": "INTEGER REFERENCES work_revisions(id)"},
    )
    _add_columns(
        connection,
        "job_parts",
        {
            "work_revision_id": "INTEGER REFERENCES work_revisions(id)",
            "work_revision_item_id": "INTEGER REFERENCES work_revision_items(id)",
        },
    )
    _add_columns(
        connection,
        "part_sources",
        {
            "work_revision_source_id": (
                "INTEGER REFERENCES work_revision_sources(id)"
            )
        },
    )
    _add_columns(
        connection,
        "quotes",
        {
            "work_revision_id": "INTEGER REFERENCES work_revisions(id)",
            "supersedes_quote_id": "INTEGER REFERENCES quotes(id)",
            "is_current": "INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1))",
            "issued_at": "TEXT",
            "superseded_at": "TEXT",
            "supersession_reason": "TEXT NOT NULL DEFAULT ''",
            "content_version": "INTEGER NOT NULL DEFAULT 1",
        },
    )
    _add_columns(
        connection,
        "quote_items",
        {
            "pricing_mode": (
                "TEXT NOT NULL DEFAULT 'LEGACY_FIXED' "
                "CHECK (pricing_mode IN ('AUTO', 'OVERRIDE', 'LEGACY_FIXED'))"
            ),
            "customer_unit_price_override": "REAL",
            "recommended_markup_percent": "REAL",
        },
    )

    # Archive visibility is not quote lineage. Existing archived quotes are
    # historical; the one unarchived quote (preflighted above) is current.
    connection.execute(
        "UPDATE quotes SET is_current = CASE WHEN is_archived = 0 THEN 1 ELSE 0 END"
    )
    connection.execute("DROP INDEX IF EXISTS uq_quotes_one_active_per_job")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_quotes_one_current_per_job "
        "ON quotes(job_id) WHERE is_current = 1"
    )

    # Synthetic Revision 1 records let legacy and initial work participate in
    # the new invariants without changing quote/invoice/item values.
    connection.execute(
        """
        INSERT INTO work_revisions (
            job_id, revision_number, state, reason, is_synthetic,
            service_charge, service_charge_description,
            sourcing_fee, sourcing_fee_description, committed_at
        )
        SELECT b.job_id, 1,
               CASE WHEN b.status = 'COMMITTED' THEN 'COMMITTED' ELSE 'EDITABLE' END,
               'Legacy work compatibility record', 1,
               COALESCE(j.service_charge, 0),
               COALESCE(j.service_charge_description, ''),
               COALESCE(j.sourcing_fee, 0),
               COALESCE(j.sourcing_fee_description, ''),
               CASE WHEN b.status = 'COMMITTED'
                    THEN COALESCE(b.committed_at, CURRENT_TIMESTAMP) END
        FROM baskets b
        JOIN jobs j ON j.id = b.job_id
        WHERE NOT EXISTS (
            SELECT 1 FROM work_revisions wr WHERE wr.job_id = b.job_id
        )
        """
    )
    connection.execute(
        """
        UPDATE jobs
        SET active_work_revision_id = (
            SELECT wr.id FROM work_revisions wr
            WHERE wr.job_id = jobs.id
            ORDER BY wr.revision_number DESC LIMIT 1
        )
        WHERE active_work_revision_id IS NULL
          AND EXISTS (SELECT 1 FROM work_revisions wr WHERE wr.job_id = jobs.id)
        """
    )


MIGRATIONS.append(
    (
        "0029_work_quote_revision_foundation",
        _migration_0029_work_quote_revision_foundation,
    )
)

MIGRATIONS.append(
    ("0030_legacy_revision_snapshots", _migration_0030_legacy_revision_snapshots)
)


def _migration_0031_active_work_pricing_provenance(
    connection: sqlite3.Connection,
) -> None:
    """Retain AUTO/OVERRIDE/LEGACY_FIXED intent in the active projection."""
    _add_columns(
        connection,
        "basket_items",
        {
            "pricing_mode": (
                "TEXT NOT NULL DEFAULT 'AUTO' "
                "CHECK (pricing_mode IN ('AUTO', 'OVERRIDE', 'LEGACY_FIXED'))"
            )
        },
    )
    connection.execute(
        """
        UPDATE basket_items
        SET pricing_mode = CASE
            WHEN customer_unit_price_override IS NULL THEN 'AUTO'
            ELSE 'OVERRIDE'
        END
        WHERE pricing_mode IS NULL OR pricing_mode NOT IN (
            'AUTO', 'OVERRIDE', 'LEGACY_FIXED'
        )
        """
    )


MIGRATIONS.append(
    (
        "0031_active_work_pricing_provenance",
        _migration_0031_active_work_pricing_provenance,
    )
)

MIGRATIONS.append(
    (
        "0032_quote_identity_snapshot_foundation",
        _migration_0032_quote_identity_snapshot_foundation,
    )
)

MIGRATIONS.append(
    ("0033_operator_quote_revisions", _migration_0033_operator_quote_revisions)
)

MIGRATIONS.append(
    (
        "0034_workflow_followups_and_internal_parts",
        _migration_0034_workflow_followups_and_internal_parts,
    )
)


def _migration_0035_multi_asset_commercial_foundation(
    connection: sqlite3.Connection,
) -> None:
    """Add multi-asset Jobs and independent commercial quote lineages."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            machine_id INTEGER,
            customer_id INTEGER,
            asset_type TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            manufacturer TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            vin_pin_serial TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
            state TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (state IN ('ACTIVE','ARCHIVED')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (machine_id) REFERENCES machines(id),
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );
        CREATE INDEX IF NOT EXISTS idx_job_assets_job
            ON job_assets(job_id,state,id);
        CREATE INDEX IF NOT EXISTS idx_job_assets_machine
            ON job_assets(machine_id);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_job_assets_machine_membership
            ON job_assets(job_id,machine_id) WHERE machine_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_job_assets_primary
            ON job_assets(job_id) WHERE is_primary=1 AND state='ACTIVE';

        CREATE TABLE IF NOT EXISTS quote_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            root_quote_id INTEGER,
            purpose TEXT NOT NULL DEFAULT 'INDEPENDENT'
                CHECK (purpose IN ('INDEPENDENT','REVISION','SPLIT_SUCCESSOR')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (root_quote_id) REFERENCES quotes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_quote_tracks_job ON quote_tracks(job_id,id);

        CREATE TABLE IF NOT EXISTS quote_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_quote_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_quote_id) REFERENCES quotes(id)
        );
        CREATE TABLE IF NOT EXISTS quote_split_successors (
            split_id INTEGER NOT NULL,
            successor_quote_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (split_id,successor_quote_id),
            FOREIGN KEY (split_id) REFERENCES quote_splits(id),
            FOREIGN KEY (successor_quote_id) REFERENCES quotes(id)
        );
        CREATE TABLE IF NOT EXISTS quote_item_lineage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            split_id INTEGER,
            predecessor_quote_item_id INTEGER NOT NULL,
            successor_quote_item_id INTEGER,
            successor_quote_id INTEGER,
            disposition TEXT NOT NULL DEFAULT 'MOVED'
                CHECK (disposition IN ('MOVED','ACCEPTED','DECLINED','DEFERRED')),
            quantity REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(split_id,predecessor_quote_item_id,successor_quote_id,disposition),
            FOREIGN KEY (split_id) REFERENCES quote_splits(id),
            FOREIGN KEY (predecessor_quote_item_id) REFERENCES quote_items(id),
            FOREIGN KEY (successor_quote_item_id) REFERENCES quote_items(id),
            FOREIGN KEY (successor_quote_id) REFERENCES quotes(id)
        );
        CREATE INDEX IF NOT EXISTS idx_quote_item_lineage_predecessor
            ON quote_item_lineage(predecessor_quote_item_id);

        CREATE TABLE IF NOT EXISTS quote_item_decisions (
            quote_item_id INTEGER PRIMARY KEY,
            decision TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (decision IN ('PENDING','ACCEPTED','DECLINED','DEFERRED')),
            accepted_quantity REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            decided_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (quote_item_id) REFERENCES quote_items(id)
        );

        CREATE TABLE IF NOT EXISTS verification_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            job_asset_id INTEGER,
            basket_item_id INTEGER,
            job_part_id INTEGER,
            connector_profile_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','COMPLETED','CANCELLED')),
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (job_asset_id) REFERENCES job_assets(id),
            FOREIGN KEY (basket_item_id) REFERENCES basket_items(id),
            FOREIGN KEY (job_part_id) REFERENCES job_parts(id),
            FOREIGN KEY (connector_profile_id) REFERENCES connector_profiles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_verification_sessions_context
            ON verification_sessions(job_id,job_asset_id,basket_item_id,status);
        """
    )

    # Preserve legacy identity verbatim while exposing it as the default asset.
    connection.execute(
        """
        INSERT INTO job_assets (
            job_id,machine_id,customer_id,asset_type,name,manufacturer,model,
            vin_pin_serial,is_primary
        )
        SELECT j.id,j.machine_id,j.customer_id,
               COALESCE(m.registry_type,''),
               COALESCE(NULLIF(m.name,''),TRIM(COALESCE(j.manufacturer,'') || ' ' || COALESCE(j.machine,''))),
               COALESCE(NULLIF(j.manufacturer,''),m.manufacturer,''),
               COALESCE(NULLIF(j.machine,''),m.model,m.name,''),
               COALESCE(NULLIF(j.pin_serial,''),m.vin_pin_serial,''),1
        FROM jobs j
        LEFT JOIN machines m ON m.id=j.machine_id
        WHERE (j.machine_id IS NOT NULL
               OR TRIM(COALESCE(j.manufacturer,''))!=''
               OR TRIM(COALESCE(j.machine,''))!=''
               OR TRIM(COALESCE(j.pin_serial,''))!='')
          AND NOT EXISTS (SELECT 1 FROM job_assets a WHERE a.job_id=j.id)
        """
    )

    asset_columns = {
        "job_asset_id": "INTEGER REFERENCES job_assets(id)",
    }
    for table in (
        "basket_items", "work_revision_items", "job_parts", "quote_items",
        "invoice_items", "supplier_order_items",
    ):
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            _add_columns(connection, table, asset_columns)
    _add_columns(
        connection,
        "basket_items",
        {"origin_work_revision_item_id": "INTEGER REFERENCES work_revision_items(id)"},
    )

    _add_columns(
        connection,
        "quote_items",
        {
            "origin_work_revision_item_id": "INTEGER REFERENCES work_revision_items(id)",
            "asset_name_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_type_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_manufacturer_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_model_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_year_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_serial_snapshot": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        connection,
        "invoice_items",
        {
            "asset_name_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_type_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_manufacturer_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_model_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_year_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_serial_snapshot": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        connection,
        "quotes",
        {
            "quote_track_id": "INTEGER REFERENCES quote_tracks(id)",
            "commercial_kind": (
                "TEXT NOT NULL DEFAULT 'INDEPENDENT' "
                "CHECK (commercial_kind IN ('INDEPENDENT','REVISION','SPLIT_SUCCESSOR'))"
            ),
            "split_from_quote_id": "INTEGER REFERENCES quotes(id)",
            "bill_to_kind": (
                "TEXT NOT NULL DEFAULT 'CONTACT' "
                "CHECK (bill_to_kind IN ('CONTACT','COMPANY','CUSTOM'))"
            ),
            "bill_to_name_snapshot": "TEXT NOT NULL DEFAULT ''",
            "bill_to_company_snapshot": "TEXT NOT NULL DEFAULT ''",
            "bill_to_address_snapshot": "TEXT NOT NULL DEFAULT ''",
            "bill_to_phone_snapshot": "TEXT NOT NULL DEFAULT ''",
            "bill_to_email_snapshot": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        connection,
        "invoices",
        {
            "bill_to_kind": "TEXT NOT NULL DEFAULT 'CONTACT'",
            "bill_to_name_snapshot": "TEXT NOT NULL DEFAULT ''",
            "bill_to_company_snapshot": "TEXT NOT NULL DEFAULT ''",
            "bill_to_address_snapshot": "TEXT NOT NULL DEFAULT ''",
            "bill_to_phone_snapshot": "TEXT NOT NULL DEFAULT ''",
            "bill_to_email_snapshot": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        connection,
        "job_follow_ups",
        {
            "job_asset_id": "INTEGER REFERENCES job_assets(id)",
            "basket_item_id": "INTEGER REFERENCES basket_items(id)",
            "job_part_id": "INTEGER REFERENCES job_parts(id)",
        },
    )
    _add_columns(
        connection,
        "connector_profiles",
        {
            "manufacturer_applicability": "TEXT NOT NULL DEFAULT ''",
            "notes": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        connection,
        "active_source_import",
        {
            "job_asset_id": "INTEGER REFERENCES job_assets(id)",
            "basket_item_id": "INTEGER REFERENCES basket_items(id)",
            "verification_session_id": "INTEGER REFERENCES verification_sessions(id)",
        },
    )

    # Legacy line records inherit the Job's primary compatibility asset where
    # no issued line-level snapshot existed. Existing quote values are untouched.
    for table, job_expression in (
        ("basket_items", "(SELECT b.job_id FROM baskets b WHERE b.id=basket_items.basket_id)"),
        ("work_revision_items", "(SELECT wr.job_id FROM work_revisions wr WHERE wr.id=work_revision_items.work_revision_id)"),
        ("job_parts", "job_parts.job_id"),
        ("quote_items", "(SELECT q.job_id FROM quotes q WHERE q.id=quote_items.quote_id)"),
        ("invoice_items", "(SELECT i.job_id FROM invoices i WHERE i.id=invoice_items.invoice_id)"),
    ):
        connection.execute(
            f"""
            UPDATE {table} SET job_asset_id=(
                SELECT a.id FROM job_assets a
                WHERE a.job_id={job_expression} AND a.is_primary=1 AND a.state='ACTIVE'
                ORDER BY a.id LIMIT 1
            ) WHERE job_asset_id IS NULL
            """
        )

    # Create one lineage track for each legacy root and attach descendants.
    quotes = connection.execute(
        "SELECT id,job_id,supersedes_quote_id,quote_track_id FROM quotes ORDER BY id"
    ).fetchall()
    tracks: dict[int, int] = {}
    for quote in quotes:
        if quote["quote_track_id"] is not None:
            tracks[int(quote["id"])] = int(quote["quote_track_id"])
            continue
        predecessor = quote["supersedes_quote_id"]
        track_id = tracks.get(int(predecessor)) if predecessor else None
        if track_id is None:
            cursor = connection.execute(
                "INSERT INTO quote_tracks(job_id,root_quote_id,purpose) VALUES (?,?,?)",
                (
                    quote["job_id"], quote["id"],
                    "REVISION" if predecessor else "INDEPENDENT",
                ),
            )
            track_id = int(cursor.lastrowid)
        tracks[int(quote["id"])] = track_id
        connection.execute(
            "UPDATE quotes SET quote_track_id=? WHERE id=? AND quote_track_id IS NULL",
            (track_id, quote["id"]),
        )

    connection.execute(
        """
        UPDATE quotes SET
            bill_to_name_snapshot=COALESCE(NULLIF(customer_name_snapshot,''),''),
            bill_to_company_snapshot=COALESCE(NULLIF(company_snapshot,''),''),
            bill_to_address_snapshot=COALESCE(NULLIF(address_snapshot,''),''),
            bill_to_phone_snapshot=COALESCE(NULLIF(phone_snapshot,''),''),
            bill_to_email_snapshot=COALESCE(NULLIF(email_snapshot,''),'')
        WHERE bill_to_name_snapshot=''
        """
    )
    connection.execute(
        """
        UPDATE invoices SET
            bill_to_kind=COALESCE((SELECT q.bill_to_kind FROM quotes q WHERE q.id=invoices.quote_id),'CONTACT'),
            bill_to_name_snapshot=COALESCE((SELECT q.bill_to_name_snapshot FROM quotes q WHERE q.id=invoices.quote_id),''),
            bill_to_company_snapshot=COALESCE((SELECT q.bill_to_company_snapshot FROM quotes q WHERE q.id=invoices.quote_id),''),
            bill_to_address_snapshot=COALESCE((SELECT q.bill_to_address_snapshot FROM quotes q WHERE q.id=invoices.quote_id),''),
            bill_to_phone_snapshot=COALESCE((SELECT q.bill_to_phone_snapshot FROM quotes q WHERE q.id=invoices.quote_id),''),
            bill_to_email_snapshot=COALESCE((SELECT q.bill_to_email_snapshot FROM quotes q WHERE q.id=invoices.quote_id),'')
        WHERE bill_to_name_snapshot=''
        """
    )
    connection.execute("DROP INDEX IF EXISTS uq_quotes_one_current_per_job")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_quotes_one_current_per_track "
        "ON quotes(quote_track_id) WHERE is_current=1 AND quote_track_id IS NOT NULL"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_job_current ON quotes(job_id,is_current,status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_quote_items_asset ON quote_items(quote_id,job_asset_id,id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_basket_items_asset ON basket_items(basket_id,job_asset_id,id)"
    )


MIGRATIONS.append(
    (
        "0035_multi_asset_commercial_foundation",
        _migration_0035_multi_asset_commercial_foundation,
    )
)


def _migration_0036_multi_asset_completion(connection: sqlite3.Connection) -> None:
    """Complete columns if 0035 was applied during an earlier development rehearsal."""
    additions = {
        "basket_items": {
            "job_asset_id": "INTEGER REFERENCES job_assets(id)",
            "origin_work_revision_item_id": "INTEGER REFERENCES work_revision_items(id)",
        },
        "work_revision_items": {"job_asset_id": "INTEGER REFERENCES job_assets(id)"},
        "job_parts": {"job_asset_id": "INTEGER REFERENCES job_assets(id)"},
        "quote_items": {"job_asset_id": "INTEGER REFERENCES job_assets(id)"},
        "invoice_items": {"job_asset_id": "INTEGER REFERENCES job_assets(id)"},
        "supplier_order_items": {"job_asset_id": "INTEGER REFERENCES job_assets(id)"},
    }
    for table, columns in additions.items():
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            _add_columns(connection, table, columns)


MIGRATIONS.append(("0036_multi_asset_completion", _migration_0036_multi_asset_completion))


def _migration_0037_machine_first_research(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS requested_needs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            job_asset_id INTEGER,
            customer_request_id INTEGER,
            wording TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'OPEN'
                CHECK (state IN ('OPEN','SATISFIED','ARCHIVED')),
            resolution TEXT NOT NULL DEFAULT '',
            lock_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (job_asset_id) REFERENCES job_assets(id),
            FOREIGN KEY (customer_request_id) REFERENCES customer_requests(id)
        );
        CREATE INDEX IF NOT EXISTS idx_requested_needs_job_asset
            ON requested_needs(job_id,job_asset_id,state,id);

        CREATE TABLE IF NOT EXISTS basket_item_need_links (
            basket_item_id INTEGER NOT NULL,
            requested_need_id INTEGER NOT NULL,
            relationship TEXT NOT NULL DEFAULT 'ADDRESSES'
                CHECK (relationship IN ('ADDRESSES','SATISFIES','POSSIBLE_MATCH')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (basket_item_id,requested_need_id),
            FOREIGN KEY (basket_item_id) REFERENCES basket_items(id) ON DELETE CASCADE,
            FOREIGN KEY (requested_need_id) REFERENCES requested_needs(id)
        );
        CREATE TABLE IF NOT EXISTS work_revision_item_need_links (
            work_revision_item_id INTEGER NOT NULL,
            requested_need_id INTEGER NOT NULL,
            relationship TEXT NOT NULL DEFAULT 'ADDRESSES',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (work_revision_item_id,requested_need_id),
            FOREIGN KEY (work_revision_item_id) REFERENCES work_revision_items(id),
            FOREIGN KEY (requested_need_id) REFERENCES requested_needs(id)
        );

        CREATE TABLE IF NOT EXISTS manufacturer_brands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL UNIQUE,
            normalized_key TEXT NOT NULL UNIQUE,
            aliases TEXT NOT NULL DEFAULT '',
            local_logo_path TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS part_shipping_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            basket_item_id INTEGER,
            work_revision_item_id INTEGER,
            job_part_id INTEGER,
            manufacturer_part_number TEXT NOT NULL DEFAULT '',
            unit_weight REAL,
            weight_unit TEXT NOT NULL DEFAULT 'lb'
                CHECK (weight_unit IN ('lb','kg','oz','g')),
            length REAL,
            width REAL,
            height REAL,
            dimension_unit TEXT NOT NULL DEFAULT 'in'
                CHECK (dimension_unit IN ('in','cm','mm','m')),
            quality TEXT NOT NULL DEFAULT 'MANUAL'
                CHECK (quality IN ('ACTUAL','VERIFIED','ESTIMATED_HIGH','ESTIMATED_MEDIUM','ESTIMATED_LOW','MANUAL')),
            provenance TEXT NOT NULL DEFAULT '',
            confidence REAL CHECK (confidence IS NULL OR (confidence>=0 AND confidence<=1)),
            measured_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            is_current INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (basket_item_id IS NOT NULL OR work_revision_item_id IS NOT NULL OR job_part_id IS NOT NULL OR manufacturer_part_number!=''),
            CHECK (unit_weight IS NULL OR unit_weight>=0),
            CHECK (length IS NULL OR length>=0),
            CHECK (width IS NULL OR width>=0),
            CHECK (height IS NULL OR height>=0),
            FOREIGN KEY (basket_item_id) REFERENCES basket_items(id) ON DELETE CASCADE,
            FOREIGN KEY (work_revision_item_id) REFERENCES work_revision_items(id),
            FOREIGN KEY (job_part_id) REFERENCES job_parts(id)
        );
        CREATE INDEX IF NOT EXISTS idx_part_shipping_basket
            ON part_shipping_data(basket_item_id,is_current,id);
        CREATE INDEX IF NOT EXISTS idx_part_shipping_part_number
            ON part_shipping_data(manufacturer_part_number,is_current,quality);

        CREATE TABLE IF NOT EXISTS consolidated_shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            invoice_id INTEGER,
            status TEXT NOT NULL DEFAULT 'ESTIMATE'
                CHECK (status IN ('ESTIMATE','PACKED','SHIPPED','VOID')),
            package_count INTEGER NOT NULL DEFAULT 1,
            total_weight REAL,
            weight_unit TEXT NOT NULL DEFAULT 'lb',
            length REAL,
            width REAL,
            height REAL,
            dimension_unit TEXT NOT NULL DEFAULT 'in',
            quality TEXT NOT NULL DEFAULT 'MANUAL',
            provenance TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            measured_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        );
        """
    )
    additions = {
        "basket_items": {
            "research_state": "TEXT NOT NULL DEFAULT 'LEGACY_CANDIDATE' CHECK (research_state IN ('RESEARCH_RESULT','QUOTE_CANDIDATE','LEGACY_CANDIDATE'))",
            "primary_requested_need_id": "INTEGER REFERENCES requested_needs(id)",
        },
        "work_revision_items": {
            "research_state": "TEXT NOT NULL DEFAULT 'LEGACY_CANDIDATE'",
            "primary_requested_need_id": "INTEGER REFERENCES requested_needs(id)",
        },
        "job_parts": {"primary_requested_need_id": "INTEGER REFERENCES requested_needs(id)"},
        "quote_items": {"primary_requested_need_id": "INTEGER REFERENCES requested_needs(id)"},
        "invoice_items": {"primary_requested_need_id": "INTEGER REFERENCES requested_needs(id)"},
        "verification_sessions": {"requested_need_id": "INTEGER REFERENCES requested_needs(id)"},
        "active_source_import": {
            "requested_need_id": "INTEGER REFERENCES requested_needs(id)",
            "job_part_id": "INTEGER REFERENCES job_parts(id)",
        },
        "job_follow_ups": {"requested_need_id": "INTEGER REFERENCES requested_needs(id)"},
    }
    for table, columns in additions.items():
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            _add_columns(connection, table, columns)

    # Existing selectable work remains quote-compatible; only new research results
    # begin outside the Quote Candidate state.
    connection.execute(
        "UPDATE basket_items SET research_state='LEGACY_CANDIDATE' "
        "WHERE research_state IS NULL OR research_state=''"
    )
    for canonical, key, aliases in (
        ("John Deere", "john_deere", "deere,john deere"),
        ("JCB", "jcb", "jcb"),
        ("Caterpillar", "caterpillar", "cat,caterpillar"),
        ("Cummins", "cummins", "cummins"),
        ("Komatsu", "komatsu", "komatsu"),
        ("BOMAG", "bomag", "bomag"),
        ("HAMM", "hamm", "hamm"),
        ("Toyota", "toyota", "toyota"),
        ("Honda", "honda", "honda"),
        ("BMW", "bmw", "bmw"),
        ("Mack", "mack", "mack"),
        ("Volvo", "volvo", "volvo"),
        ("Isuzu", "isuzu", "isuzu"),
    ):
        connection.execute(
            "INSERT INTO manufacturer_brands (canonical_name,normalized_key,aliases) "
            "SELECT ?,?,? WHERE NOT EXISTS ("
            "SELECT 1 FROM manufacturer_brands WHERE normalized_key=? OR canonical_name=?"
            ")",
            (canonical, key, aliases, key, canonical),
        )


MIGRATIONS.append(("0037_machine_first_research", _migration_0037_machine_first_research))


def _migration_0038_machine_research_context_completion(connection: sqlite3.Connection) -> None:
    """Complete the legacy active-import context without rewriting migration 0037."""
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='active_source_import'"
    ).fetchone():
        _add_columns(
            connection,
            "active_source_import",
            {"job_part_id": "INTEGER REFERENCES job_parts(id)"},
        )


MIGRATIONS.append(("0038_machine_research_context_completion", _migration_0038_machine_research_context_completion))


def _migration_0039_smart_intake_proposals(connection: sqlite3.Connection) -> None:
    """Persistent, reviewable Smart Intake proposals; no business records are backfilled."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS intake_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_input TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'DRAFT'
                CHECK (status IN ('DRAFT','CONFIRMED','CANCELLED')),
            contact_name TEXT NOT NULL DEFAULT '',
            company_name TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            review_state TEXT NOT NULL DEFAULT 'REVIEW'
                CHECK (review_state IN ('CONFIDENT','REVIEW','UNASSIGNED')),
            matched_customer_id INTEGER,
            created_request_id INTEGER,
            created_job_id INTEGER,
            lock_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            confirmed_at TEXT,
            FOREIGN KEY (matched_customer_id) REFERENCES customers(id),
            FOREIGN KEY (created_request_id) REFERENCES customer_requests(id),
            FOREIGN KEY (created_job_id) REFERENCES jobs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_intake_proposals_status
            ON intake_proposals(status,id);

        CREATE TABLE IF NOT EXISTS intake_proposal_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL DEFAULT 1,
            asset_category TEXT NOT NULL DEFAULT 'other',
            manufacturer TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            year TEXT NOT NULL DEFAULT '',
            market_region TEXT NOT NULL DEFAULT 'UNKNOWN'
                CHECK (market_region IN ('JDM','USDM','EDM','UK','GLOBAL','UNKNOWN')),
            model_code TEXT NOT NULL DEFAULT '',
            review_state TEXT NOT NULL DEFAULT 'REVIEW'
                CHECK (review_state IN ('CONFIDENT','REVIEW','UNASSIGNED')),
            matched_machine_id INTEGER,
            included INTEGER NOT NULL DEFAULT 1 CHECK (included IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (proposal_id) REFERENCES intake_proposals(id) ON DELETE CASCADE,
            FOREIGN KEY (matched_machine_id) REFERENCES machines(id),
            UNIQUE(proposal_id,sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_intake_assets_proposal
            ON intake_proposal_assets(proposal_id,included,sequence,id);

        CREATE TABLE IF NOT EXISTS intake_proposal_identifiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            proposal_asset_id INTEGER,
            identifier_type TEXT NOT NULL DEFAULT 'UNKNOWN'
                CHECK (identifier_type IN ('AUTOMOTIVE_VIN','JDM_FRAME','JDM_CHASSIS','MODEL_CODE','PIN','MACHINE_SERIAL','ENGINE_SERIAL','COMPONENT_SERIAL','OTHER_IDENTIFIER','UNKNOWN')),
            identifier_value TEXT NOT NULL,
            component_label TEXT NOT NULL DEFAULT '',
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
            review_state TEXT NOT NULL DEFAULT 'REVIEW'
                CHECK (review_state IN ('CONFIDENT','REVIEW','UNASSIGNED')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (proposal_id) REFERENCES intake_proposals(id) ON DELETE CASCADE,
            FOREIGN KEY (proposal_asset_id) REFERENCES intake_proposal_assets(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_intake_identifiers_asset
            ON intake_proposal_identifiers(proposal_id,proposal_asset_id,id);

        CREATE TABLE IF NOT EXISTS intake_proposal_needs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            proposal_asset_id INTEGER,
            sequence INTEGER NOT NULL DEFAULT 1,
            wording TEXT NOT NULL,
            original_wording TEXT NOT NULL DEFAULT '',
            quantity REAL,
            position TEXT NOT NULL DEFAULT '',
            review_state TEXT NOT NULL DEFAULT 'REVIEW'
                CHECK (review_state IN ('CONFIDENT','REVIEW','UNASSIGNED')),
            included INTEGER NOT NULL DEFAULT 1 CHECK (included IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (proposal_id) REFERENCES intake_proposals(id) ON DELETE CASCADE,
            FOREIGN KEY (proposal_asset_id) REFERENCES intake_proposal_assets(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_intake_needs_proposal
            ON intake_proposal_needs(proposal_id,included,proposal_asset_id,sequence,id);

        CREATE TABLE IF NOT EXISTS intake_proposal_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            original_filename TEXT NOT NULL,
            stored_path TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT '',
            proposal_asset_id INTEGER,
            proposal_need_id INTEGER,
            extraction_status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (extraction_status IN ('PENDING','PROPOSED','REVIEWED','UNAVAILABLE')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (proposal_id) REFERENCES intake_proposals(id) ON DELETE CASCADE,
            FOREIGN KEY (proposal_asset_id) REFERENCES intake_proposal_assets(id) ON DELETE SET NULL,
            FOREIGN KEY (proposal_need_id) REFERENCES intake_proposal_needs(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS intake_proposal_contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            contributor_type TEXT NOT NULL
                CHECK (contributor_type IN ('DETERMINISTIC','AI','IMAGE','DECODER','OPERATOR')),
            payload_json TEXT NOT NULL DEFAULT '{}',
            evidence TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (proposal_id) REFERENCES intake_proposals(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS machine_identifiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id INTEGER NOT NULL,
            identifier_type TEXT NOT NULL,
            identifier_value TEXT NOT NULL,
            component_label TEXT NOT NULL DEFAULT '',
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
            source TEXT NOT NULL DEFAULT 'OPERATOR_CONFIRMED',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (machine_id) REFERENCES machines(id),
            UNIQUE(machine_id,identifier_type,identifier_value,component_label)
        );
        CREATE INDEX IF NOT EXISTS idx_machine_identifiers_value
            ON machine_identifiers(identifier_value,identifier_type);
        """
    )
    _add_columns(
        connection,
        "machines",
        {
            "market_region": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "model_code": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        connection,
        "job_assets",
        {
            "market_region": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "model_code": "TEXT NOT NULL DEFAULT ''",
        },
    )


MIGRATIONS.append(("0039_smart_intake_proposals", _migration_0039_smart_intake_proposals))


def _migration_0040_smart_intake_identifier_completion(connection: sqlite3.Connection) -> None:
    """Complete 0039 safely if an auto-reload applied it before source editing finished."""
    _add_columns(
        connection,
        "machines",
        {
            "market_region": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "model_code": "TEXT NOT NULL DEFAULT ''",
        },
    )
    _add_columns(
        connection,
        "job_assets",
        {
            "market_region": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "model_code": "TEXT NOT NULL DEFAULT ''",
        },
    )


MIGRATIONS.append(("0040_smart_intake_identifier_completion", _migration_0040_smart_intake_identifier_completion))


def _migration_0041_need_aware_research_sources(connection: sqlite3.Connection) -> None:
    """Strengthen research context/source evidence without rewriting existing work."""
    _add_columns(
        connection,
        "connector_profiles",
        {
            "source_type": "TEXT NOT NULL DEFAULT 'OTHER'",
            "asset_category_applicability": "TEXT NOT NULL DEFAULT ''",
            "market_applicability": "TEXT NOT NULL DEFAULT ''",
            "source_priority": "INTEGER NOT NULL DEFAULT 100",
            "is_default": "INTEGER NOT NULL DEFAULT 0",
            "provenance": "TEXT NOT NULL DEFAULT 'LEGACY'",
        },
    )
    _add_columns(
        connection,
        "verification_sessions",
        {
            "customer_request_id": "INTEGER REFERENCES customer_requests(id)",
            "identifier_type_snapshot": "TEXT NOT NULL DEFAULT ''",
            "identifier_value_snapshot": "TEXT NOT NULL DEFAULT ''",
            "asset_snapshot": "TEXT NOT NULL DEFAULT ''",
            "need_wording_snapshot": "TEXT NOT NULL DEFAULT ''",
            "source_name_snapshot": "TEXT NOT NULL DEFAULT ''",
            "source_url_snapshot": "TEXT NOT NULL DEFAULT ''",
            "source_type_snapshot": "TEXT NOT NULL DEFAULT ''",
        },
    )
    lineage_columns = {
        "research_session_id": "INTEGER REFERENCES verification_sessions(id)",
        "research_evidence": "TEXT NOT NULL DEFAULT ''",
        "research_notes": "TEXT NOT NULL DEFAULT ''",
        "identified_at": "TEXT",
    }
    for table in ("basket_items", "work_revision_items", "job_parts", "quote_items"):
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            _add_columns(connection, table, lineage_columns)

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_capture_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_type TEXT NOT NULL
                CHECK (proposal_type IN ('PART_RESULT','CART_RESULTS','SOURCE')),
            status TEXT NOT NULL DEFAULT 'REVIEW'
                CHECK (status IN ('REVIEW','CONFIRMED','REJECTED')),
            verification_session_id INTEGER,
            job_id INTEGER NOT NULL,
            job_asset_id INTEGER,
            requested_need_id INTEGER,
            connector_profile_id INTEGER,
            page_url TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            submitted_by TEXT NOT NULL DEFAULT 'FUTURE_EXTENSION',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT,
            FOREIGN KEY (verification_session_id) REFERENCES verification_sessions(id),
            FOREIGN KEY (job_id) REFERENCES jobs(id),
            FOREIGN KEY (job_asset_id) REFERENCES job_assets(id),
            FOREIGN KEY (requested_need_id) REFERENCES requested_needs(id),
            FOREIGN KEY (connector_profile_id) REFERENCES connector_profiles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_research_capture_context
            ON research_capture_proposals(job_id,job_asset_id,requested_need_id,status,id);
        CREATE INDEX IF NOT EXISTS idx_verification_sessions_need
            ON verification_sessions(job_id,job_asset_id,requested_need_id,status,id);
        """
    )

    # A URL-less general option is honest fallback context, not a fabricated site.
    connection.execute(
        """INSERT INTO connector_profiles (
            connector_key,display_name,category,trust_level,launch_url,
            connector_type,parser_key,is_enabled,sort_order,
            manufacturer_applicability,notes,source_type,
            asset_category_applicability,market_applicability,
            source_priority,is_default,provenance
        )
        SELECT 'general_research','General Research','Research','NEEDS_REVIEW','',
               'CATALOG','',1,999,'',
               'No saved external destination. Add a confirmed source when one is known.',
               'GENERAL_RESEARCH','','',999,0,'PPS_DEFAULT'
        WHERE NOT EXISTS (
            SELECT 1 FROM connector_profiles WHERE connector_key='general_research'
        )"""
    )


MIGRATIONS.append(("0041_need_aware_research_sources", _migration_0041_need_aware_research_sources))


def _migration_0042_source_routing_corrections(connection: sqlite3.Connection) -> None:
    """Scope existing legacy catalogs without creating replacement records."""
    connection.execute(
        """UPDATE connector_profiles
           SET source_type='OEM_CATALOG',
               manufacturer_applicability='Caterpillar,CAT',
               asset_category_applicability='machine,equipment,heavy equipment',
               market_applicability='GLOBAL',
               source_priority=50,
               is_default=1,
               updated_at=CURRENT_TIMESTAMP
           WHERE lower(display_name) IN ('cat sis','caterpillar sis')"""
    )
    connection.execute(
        """UPDATE connector_profiles
           SET source_type='AFTERMARKET_CATALOG',
               manufacturer_applicability='',
               asset_category_applicability='vehicle,automotive',
               market_applicability='GLOBAL',
               source_priority=50,
               is_default=1,
               updated_at=CURRENT_TIMESTAMP
           WHERE lower(display_name)='worldpac'"""
    )


MIGRATIONS.append(("0042_source_routing_corrections", _migration_0042_source_routing_corrections))


def _migration_0043_unified_source_directory(connection: sqlite3.Connection) -> None:
    """Use connector_profiles as the single Admin and Job research source directory."""
    requested_sources = (
        ("amazon", "Amazon", "https://www.amazon.com", "Supplier", "SUPPLIER", "CART", "", "", "GLOBAL", 50, 1,
         "General parts marketplace source."),
        ("ebay", "eBay", "https://www.ebay.com", "Supplier", "SUPPLIER", "CART", "", "", "GLOBAL", 60, 1,
         "General parts marketplace source."),
        ("miami_star", "Miami Star", "https://miamistar.com", "Supplier", "SUPPLIER", "CART", "", "machine", "GLOBAL", 70, 1,
         "Heavy-duty truck and equipment parts supplier."),
        ("oem_parts_online", "OEM Parts Online", "https://oempartsonline.com", "OEM Catalog", "OEM_CATALOG", "CATALOG", "", "vehicle", "GLOBAL", 70, 1,
         "OEM automotive parts catalog source."),
        ("fcp_euro", "FCP Euro", "https://www.fcpeuro.com", "Aftermarket Catalog", "AFTERMARKET_CATALOG", "CATALOG", "", "vehicle", "GLOBAL", 80, 1,
         "European automotive parts source."),
    )
    for (key, name, url, category, source_type, connector_type, manufacturer,
         asset_category, market, priority, recommended, notes) in requested_sources:
        existing = connection.execute(
            "SELECT id FROM connector_profiles WHERE lower(trim(display_name))=lower(trim(?)) ORDER BY id LIMIT 1",
            (name,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO connector_profiles (
                       connector_key,display_name,category,trust_level,launch_url,connector_type,
                       parser_key,is_enabled,is_archived,sort_order,manufacturer_applicability,notes,
                       source_type,asset_category_applicability,market_applicability,source_priority,
                       is_default,provenance
                   ) VALUES (?,?,?,'NEEDS_REVIEW',?,?,'',1,0,100,?,?,?,?,?,?,?,'PPS_DIRECTORY')""",
                (key, name, category, url, connector_type, manufacturer, notes, source_type,
                 asset_category, market, priority, recommended),
            )
        else:
            connection.execute(
                """UPDATE connector_profiles SET category=?,source_type=?,connector_type=?,
                          manufacturer_applicability=?,asset_category_applicability=?,
                          market_applicability=?,source_priority=?,is_default=?,notes=?,
                          is_enabled=1,is_archived=0,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (category, source_type, connector_type, manufacturer, asset_category, market,
                 priority, recommended, notes, int(existing["id"])),
            )

    # Existing catalogs retain their IDs and URLs; only directory metadata is normalized.
    connection.execute(
        """UPDATE connector_profiles SET source_type='OEM_CATALOG',category='OEM Catalog',
                  manufacturer_applicability='John Deere',asset_category_applicability='machine',
                  market_applicability='GLOBAL',source_priority=100,is_default=1,
                  is_enabled=1,is_archived=0,updated_at=CURRENT_TIMESTAMP
           WHERE lower(trim(display_name))='john deere parts catalog'"""
    )
    connection.execute(
        """UPDATE connector_profiles SET source_type='OEM_CATALOG',category='OEM Catalog',
                  manufacturer_applicability='Caterpillar,CAT',asset_category_applicability='machine',
                  market_applicability='GLOBAL',source_priority=100,is_default=1,
                  is_enabled=1,is_archived=0,updated_at=CURRENT_TIMESTAMP
           WHERE lower(trim(display_name)) IN ('cat sis','caterpillar sis')"""
    )
    connection.execute(
        """UPDATE connector_profiles SET source_type='AFTERMARKET_CATALOG',
                  asset_category_applicability='vehicle',market_applicability='GLOBAL',
                  source_priority=70,is_default=1,is_enabled=1,is_archived=0,
                  updated_at=CURRENT_TIMESTAMP
           WHERE lower(trim(display_name))='worldpac'"""
    )
    connection.execute(
        """UPDATE connector_profiles SET source_type='VIN_OR_ASSET_DECODER',
                  asset_category_applicability='vehicle',market_applicability='GLOBAL',
                  source_priority=60,is_default=0,is_enabled=1,is_archived=0,
                  updated_at=CURRENT_TIMESTAMP
           WHERE lower(trim(display_name))='7zap'"""
    )
    connection.execute(
        """UPDATE connector_profiles SET source_type='GENERAL_RESEARCH',
                  manufacturer_applicability='',asset_category_applicability='',
                  market_applicability='',source_priority=0,is_default=0,
                  is_enabled=1,is_archived=0,updated_at=CURRENT_TIMESTAMP
           WHERE connector_key='general_research' OR lower(trim(display_name))='general research'"""
    )


MIGRATIONS.append(("0043_unified_source_directory", _migration_0043_unified_source_directory))


def _migration_0044_document_integrity(connection: sqlite3.Connection) -> None:
    """Version issued quote documents and add immutable invoice manifests."""
    quote_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(quote_documents_manifest)"
        ).fetchall()
    }

    if "version" not in quote_columns:
        connection.execute("ALTER TABLE quote_documents_manifest RENAME TO quote_documents_manifest_legacy")
        connection.execute(
            """
            CREATE TABLE quote_documents_manifest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id INTEGER NOT NULL,
                audience TEXT NOT NULL CHECK (audience IN ('CUSTOMER', 'INTERNAL')),
                document_kind TEXT NOT NULL DEFAULT 'QUOTE',
                file_path TEXT NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_issued INTEGER NOT NULL DEFAULT 0 CHECK (is_issued IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                quote_status TEXT NOT NULL DEFAULT '',
                is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
                UNIQUE (quote_id, audience, document_kind, version),
                FOREIGN KEY (quote_id) REFERENCES quotes(id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO quote_documents_manifest (
                id, quote_id, audience, document_kind, file_path, sha256,
                generated_at, is_issued, created_at, version, quote_status,
                is_current
            )
            SELECT legacy.id, legacy.quote_id, legacy.audience,
                   legacy.document_kind, legacy.file_path, legacy.sha256,
                   legacy.generated_at, legacy.is_issued, legacy.created_at,
                   1, '', 1
            FROM quote_documents_manifest_legacy legacy
            """
        )
        connection.execute("DROP TABLE quote_documents_manifest_legacy")

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_quote_documents_manifest_quote
        ON quote_documents_manifest(quote_id)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_quote_document_current
        ON quote_documents_manifest(quote_id, document_kind, audience)
        WHERE is_current = 1
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS invoice_documents_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            document_kind TEXT NOT NULL,
            audience TEXT NOT NULL CHECK (audience IN ('CUSTOMER', 'INTERNAL')),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
            invoice_status TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
            UNIQUE (invoice_id, document_kind, audience, version),
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_invoice_documents_manifest_invoice
        ON invoice_documents_manifest(invoice_id)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_document_current
        ON invoice_documents_manifest(invoice_id, document_kind, audience)
        WHERE is_current = 1
        """
    )


MIGRATIONS.append(("0044_document_integrity", _migration_0044_document_integrity))


def _migration_0045_supplier_order_document_integrity(
    connection: sqlite3.Connection,
) -> None:
    """Add immutable, versioned Supplier Purchase Order manifests."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS supplier_order_documents_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_order_id INTEGER NOT NULL,
            document_kind TEXT NOT NULL
                CHECK(document_kind IN ('SUPPLIER_PURCHASE_ORDER')),
            audience TEXT NOT NULL
                CHECK(audience IN ('SUPPLIER','INTERNAL')),
            version INTEGER NOT NULL DEFAULT 1
                CHECK(version >= 1),
            supplier_order_status TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_current INTEGER NOT NULL DEFAULT 1
                CHECK(is_current IN (0,1)),
            UNIQUE(
                supplier_order_id,
                document_kind,
                audience,
                version
            ),
            FOREIGN KEY(supplier_order_id)
                REFERENCES supplier_orders(id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_supplier_order_documents_order
        ON supplier_order_documents_manifest(supplier_order_id)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_order_document_current
        ON supplier_order_documents_manifest(
            supplier_order_id,
            document_kind,
            audience
        )
        WHERE is_current = 1
        """
    )


MIGRATIONS.append(
    (
        "0045_supplier_order_document_integrity",
        _migration_0045_supplier_order_document_integrity,
    )
)


def _migration_0048_actual_supplier_costs(
    connection: sqlite3.Connection,
) -> None:
    """Separate confirmed actual costs from booked and placed snapshots."""
    item_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(supplier_order_items)"
        ).fetchall()
    }
    if "actual_unit_cost" not in item_columns:
        connection.execute(
            """
            ALTER TABLE supplier_order_items
            ADD COLUMN actual_unit_cost REAL NULL
                CHECK(actual_unit_cost >= 0)
            """
        )

    order_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(supplier_orders)"
        ).fetchall()
    }
    if "actual_shipping_total" not in order_columns:
        connection.execute(
            """
            ALTER TABLE supplier_orders
            ADD COLUMN actual_shipping_total REAL NULL
                CHECK(actual_shipping_total >= 0)
            """
        )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS supplier_cost_adjustments (
            id INTEGER PRIMARY KEY,
            supplier_order_id INTEGER NOT NULL,
            supplier_order_item_id INTEGER NULL,
            cost_kind TEXT NOT NULL,
            old_amount REAL NULL,
            new_amount REAL NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            supplier_reference TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(cost_kind IN ('ITEM','SHIPPING')),
            FOREIGN KEY(supplier_order_id) REFERENCES supplier_orders(id),
            FOREIGN KEY(supplier_order_item_id) REFERENCES supplier_order_items(id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_supplier_cost_adjustments_order_created
        ON supplier_cost_adjustments(supplier_order_id, created_at)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_cost_adjustment_request
        ON supplier_cost_adjustments(supplier_order_id, request_id)
        WHERE request_id != ''
        """
    )


MIGRATIONS.append(
    ("0048_actual_supplier_costs", _migration_0048_actual_supplier_costs)
)


def _migration_0049_custom_invoice_presentation_adjustments(
    connection: sqlite3.Connection,
) -> None:
    """Persist custom-invoice visibility without changing source invoices."""
    _add_columns(
        connection,
        "custom_invoices",
        {
            "include_freight": (
                "INTEGER NOT NULL DEFAULT 1 CHECK(include_freight IN (0,1))"
            ),
        },
    )
    _add_columns(
        connection,
        "custom_invoice_items",
        {
            "is_visible": (
                "INTEGER NOT NULL DEFAULT 1 CHECK(is_visible IN (0,1))"
            ),
        },
    )


MIGRATIONS.append(
    (
        "0049_custom_invoice_presentation_adjustments",
        _migration_0049_custom_invoice_presentation_adjustments,
    )
)

MIGRATIONS.append(
    (
        "0050_structured_receiving_exceptions",
        _migration_0050_structured_receiving_exceptions,
    )
)


def _migration_0051_invoice_jmd_presentation(
    connection: sqlite3.Connection,
) -> None:
    """Persist an optional invoice-specific JMD display snapshot."""
    _add_columns(
        connection,
        "invoices",
        {
            "show_jmd_total": (
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK(show_jmd_total IN (0,1))"
            ),
            "jmd_exchange_rate": "TEXT NULL",
        },
    )


MIGRATIONS.append(
    (
        "0051_invoice_jmd_presentation",
        _migration_0051_invoice_jmd_presentation,
    )
)
