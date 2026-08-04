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


MIGRATIONS: list[Migration] = [
    ("0001_basket_foundation", _migration_0001_basket_foundation),
    ("0002_machine_registry", _migration_0002_machine_registry),
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
