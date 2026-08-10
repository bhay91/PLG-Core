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
