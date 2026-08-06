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
                (f"PLG-M{machine_id:05d}", machine_id),
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


MIGRATIONS: list[Migration] = [
    ("0001_basket_foundation", _migration_0001_basket_foundation),
    ("0002_machine_registry", _migration_0002_machine_registry),
    ("0003_universal_registry", _migration_0003_universal_registry),
    ("0004_customer_requests", _migration_0004_customer_requests),
    ("0005_request_job_ready", _migration_0005_request_job_ready),
    ("0006_part_status_timeline", _migration_0006_part_status_timeline),    ("0007_smart_intake_locations", _migration_0007_smart_intake_locations),

    ("0008_job_revenue_adjustments", _migration_0008_job_revenue_adjustments),
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
