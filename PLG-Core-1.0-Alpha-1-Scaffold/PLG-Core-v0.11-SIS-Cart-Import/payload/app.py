from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "plg_core.db"

app = FastAPI(title="PartsLink Global Core")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
    allow_origin_regex=r"^moz-extension://.*$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def initialize_database() -> None:
    with closing(get_connection()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_number TEXT UNIQUE,
                created_date TEXT NOT NULL,
                customer TEXT NOT NULL,
                company TEXT,
                phone TEXT,
                email TEXT,
                manufacturer TEXT,
                machine TEXT,
                pin_serial TEXT,
                status TEXT NOT NULL DEFAULT 'REQUESTED',
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS job_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                requested_description TEXT NOT NULL,
                oem_part_number TEXT,
                oem_description TEXT,
                supplier TEXT,
                supplier_cost REAL,
                quantity INTEGER NOT NULL DEFAULT 1,
                verification_status TEXT NOT NULL DEFAULT 'PENDING',
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS part_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id INTEGER NOT NULL,
                supplier_name TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'AFTERMARKET',
                brand TEXT,
                supplier_part_number TEXT,
                supplier_cost REAL,
                availability TEXT,
                lead_time TEXT,
                quote_reference TEXT,
                trust_level TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
                selected_for_quote INTEGER NOT NULL DEFAULT 0,
                source_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (part_id) REFERENCES job_parts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS active_verification (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                job_id INTEGER,
                part_id INTEGER,
                started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
                FOREIGN KEY (part_id) REFERENCES job_parts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS active_job_import (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                job_id INTEGER,
                started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
            );
            """
        )

        existing = column_names(connection, "job_parts")
        migrations = {
            "diagram_name": "ALTER TABLE job_parts ADD COLUMN diagram_name TEXT",
            "callout_number": "ALTER TABLE job_parts ADD COLUMN callout_number TEXT",
            "source_url": "ALTER TABLE job_parts ADD COLUMN source_url TEXT",
            "verification_notes": "ALTER TABLE job_parts ADD COLUMN verification_notes TEXT",
            "availability": "ALTER TABLE job_parts ADD COLUMN availability TEXT",
            "customer_unit_price": "ALTER TABLE job_parts ADD COLUMN customer_unit_price REAL",
            "diagram_url": "ALTER TABLE job_parts ADD COLUMN diagram_url TEXT",
            "product_url": "ALTER TABLE job_parts ADD COLUMN product_url TEXT",
            "captured_at": "ALTER TABLE job_parts ADD COLUMN captured_at TEXT",
            "oem_dealer_name": "ALTER TABLE job_parts ADD COLUMN oem_dealer_name TEXT",
            "oem_dealer_price": "ALTER TABLE job_parts ADD COLUMN oem_dealer_price REAL",
            "oem_dealer_availability": "ALTER TABLE job_parts ADD COLUMN oem_dealer_availability TEXT",
            "oem_dealer_lead_time": "ALTER TABLE job_parts ADD COLUMN oem_dealer_lead_time TEXT",
            "verification_source": "ALTER TABLE job_parts ADD COLUMN verification_source TEXT",
        }

        for column, sql in migrations.items():
            if column not in existing:
                connection.execute(sql)

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_part_sources_part_id ON part_sources(part_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_part_sources_selected ON part_sources(part_id, selected_for_quote)"
        )

        # Convert existing OEM pricing into selectable source options.
        connection.execute(
            """
            INSERT INTO part_sources (
                part_id, supplier_name, source_type, brand, supplier_part_number,
                supplier_cost, availability, lead_time, trust_level, source_url
            )
            SELECT
                job_parts.id,
                COALESCE(NULLIF(job_parts.oem_dealer_name, ''), NULLIF(job_parts.verification_source, ''), 'OEM Source'),
                'OEM',
                '',
                COALESCE(job_parts.oem_part_number, ''),
                job_parts.oem_dealer_price,
                COALESCE(job_parts.oem_dealer_availability, ''),
                COALESCE(job_parts.oem_dealer_lead_time, ''),
                'OEM_VERIFIED',
                COALESCE(NULLIF(job_parts.product_url, ''), NULLIF(job_parts.source_url, ''), '')
            FROM job_parts
            WHERE job_parts.verification_status = 'VERIFIED'
              AND (job_parts.oem_dealer_price IS NOT NULL OR COALESCE(job_parts.oem_dealer_name, '') != '')
              AND NOT EXISTS (
                  SELECT 1 FROM part_sources
                  WHERE part_sources.part_id = job_parts.id
                    AND part_sources.source_type = 'OEM'
                    AND COALESCE(part_sources.supplier_part_number, '') = COALESCE(job_parts.oem_part_number, '')
              )
            """
        )

        # Preserve any supplier information entered in older versions.
        connection.execute(
            """
            INSERT INTO part_sources (
                part_id, supplier_name, source_type, supplier_part_number,
                supplier_cost, availability, trust_level
            )
            SELECT
                job_parts.id, job_parts.supplier, 'AFTERMARKET', '',
                job_parts.supplier_cost, COALESCE(job_parts.availability, ''), 'MANUAL'
            FROM job_parts
            WHERE COALESCE(job_parts.supplier, '') != ''
              AND NOT EXISTS (
                  SELECT 1 FROM part_sources
                  WHERE part_sources.part_id = job_parts.id
                    AND part_sources.supplier_name = job_parts.supplier
                    AND COALESCE(part_sources.supplier_cost, -1) = COALESCE(job_parts.supplier_cost, -1)
              )
            """
        )

        connection.execute(
            """
            INSERT OR IGNORE INTO suppliers (name)
            SELECT DISTINCT TRIM(supplier_name)
            FROM part_sources
            WHERE TRIM(COALESCE(supplier_name, '')) != ''
            """
        )

        connection.commit()


def next_job_number(connection: sqlite3.Connection) -> str:
    current_year = date.today().year
    prefix = f"PLG-J-{current_year}-"

    row = connection.execute(
        """
        SELECT job_number
        FROM jobs
        WHERE job_number LIKE ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"{prefix}%",),
    ).fetchone()

    if row is None:
        sequence = 1
    else:
        try:
            sequence = int(row["job_number"].split("-")[-1]) + 1
        except (ValueError, IndexError):
            sequence = 1

    return f"{prefix}{sequence:03d}"


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with closing(get_connection()) as connection:
        recent_jobs = connection.execute(
            """
            SELECT
                jobs.*,
                COUNT(job_parts.id) AS parts_count,
                SUM(CASE WHEN job_parts.verification_status = 'VERIFIED' THEN 1 ELSE 0 END)
                    AS verified_parts
            FROM jobs
            LEFT JOIN job_parts ON job_parts.job_id = jobs.id
            GROUP BY jobs.id
            ORDER BY jobs.id DESC
            LIMIT 10
            """
        ).fetchall()

        totals = connection.execute(
            """
            SELECT
                COUNT(*) AS jobs_total,
                SUM(CASE WHEN status = 'REQUESTED' THEN 1 ELSE 0 END) AS requested,
                SUM(CASE WHEN status = 'RESEARCHING' THEN 1 ELSE 0 END) AS researching,
                SUM(CASE WHEN status = 'VERIFIED' THEN 1 ELSE 0 END) AS verified
            FROM jobs
            """
        ).fetchone()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "recent_jobs": recent_jobs,
            "totals": totals,
            "active_page": "dashboard",
        },
    )


@app.get("/jobs/new", response_class=HTMLResponse)
def new_job_form(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="new_job.html",
        context={"active_page": "jobs"},
    )


@app.post("/jobs")
def create_job(
    customer: Annotated[str, Form()],
    company: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    manufacturer: Annotated[str, Form()] = "",
    machine: Annotated[str, Form()] = "",
    pin_serial: Annotated[str, Form()] = "",
    requested_parts: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    customer = customer.strip()
    if not customer:
        raise HTTPException(status_code=400, detail="Customer is required.")

    part_lines = [
        line.strip(" -•\t")
        for line in requested_parts.splitlines()
        if line.strip(" -•\t")
    ]

    with closing(get_connection()) as connection:
        job_number = next_job_number(connection)
        cursor = connection.execute(
            """
            INSERT INTO jobs (
                job_number, created_date, customer, company, phone, email,
                manufacturer, machine, pin_serial, status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'REQUESTED', ?)
            """,
            (
                job_number,
                date.today().isoformat(),
                customer,
                company.strip(),
                phone.strip(),
                email.strip(),
                manufacturer.strip(),
                machine.strip(),
                pin_serial.strip(),
                notes.strip(),
            ),
        )
        job_id = cursor.lastrowid

        for part in part_lines:
            connection.execute(
                """
                INSERT INTO job_parts (
                    job_id, requested_description, quantity, verification_status
                )
                VALUES (?, ?, 1, 'PENDING')
                """,
                (job_id, part),
            )

        connection.commit()

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def list_jobs(request: Request):
    with closing(get_connection()) as connection:
        jobs = connection.execute(
            """
            SELECT
                jobs.*,
                COUNT(job_parts.id) AS parts_count,
                SUM(CASE WHEN job_parts.verification_status = 'VERIFIED' THEN 1 ELSE 0 END)
                    AS verified_parts
            FROM jobs
            LEFT JOIN job_parts ON job_parts.job_id = jobs.id
            GROUP BY jobs.id
            ORDER BY jobs.id DESC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={"jobs": jobs, "active_page": "jobs"},
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        parts = connection.execute(
            "SELECT * FROM job_parts WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()

        source_rows = connection.execute(
            """
            SELECT part_sources.*
            FROM part_sources
            JOIN job_parts ON job_parts.id = part_sources.part_id
            WHERE job_parts.job_id = ?
            ORDER BY part_sources.part_id, part_sources.selected_for_quote DESC, part_sources.id
            """,
            (job_id,),
        ).fetchall()

        supplier_rows = connection.execute(
            "SELECT name FROM suppliers ORDER BY name COLLATE NOCASE"
        ).fetchall()
        supplier_names = [row["name"] for row in supplier_rows]

        brand_rows = connection.execute(
            """
            SELECT DISTINCT TRIM(brand) AS brand
            FROM part_sources
            WHERE TRIM(COALESCE(brand, '')) != ''
            ORDER BY brand COLLATE NOCASE
            """
        ).fetchall()
        brand_names = [row["brand"] for row in brand_rows]

        sources_by_part: dict[int, list[sqlite3.Row]] = {}
        for source in source_rows:
            sources_by_part.setdefault(source["part_id"], []).append(source)

        selected_count = sum(
            1 for part in parts
            if any(source["selected_for_quote"] for source in sources_by_part.get(part["id"], []))
        )
        ready_for_quote = bool(parts) and selected_count == len(parts)

        active = connection.execute(
            """
            SELECT part_id
            FROM active_verification
            WHERE id = 1
            """
        ).fetchone()

    return templates.TemplateResponse(
        request=request,
        name="job_detail.html",
        context={
            "job": job,
            "parts": parts,
            "sources_by_part": sources_by_part,
            "supplier_names": supplier_names,
            "brand_names": brand_names,
            "selected_count": selected_count,
            "ready_for_quote": ready_for_quote,
            "active_part_id": active["part_id"] if active else None,
            "active_page": "jobs",
        },
    )


@app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
def edit_job_form(request: Request, job_id: int):
    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    return templates.TemplateResponse(
        request=request,
        name="edit_job.html",
        context={"job": job, "active_page": "jobs"},
    )


@app.post("/jobs/{job_id}/edit")
def update_job(
    job_id: int,
    customer: Annotated[str, Form()],
    company: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    manufacturer: Annotated[str, Form()] = "",
    machine: Annotated[str, Form()] = "",
    pin_serial: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    customer = customer.strip()
    if not customer:
        raise HTTPException(status_code=400, detail="Customer is required.")

    with closing(get_connection()) as connection:
        result = connection.execute(
            """
            UPDATE jobs
            SET
                customer = ?,
                company = ?,
                phone = ?,
                email = ?,
                manufacturer = ?,
                machine = ?,
                pin_serial = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                customer,
                company.strip(),
                phone.strip(),
                email.strip(),
                manufacturer.strip(),
                machine.strip(),
                pin_serial.strip(),
                notes.strip(),
                job_id,
            ),
        )

        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Job not found.")

        connection.commit()

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/parts")
def add_job_part(
    job_id: int,
    requested_description: Annotated[str, Form()],
    quantity: Annotated[int, Form()] = 1,
):
    description = requested_description.strip()
    if not description:
        raise HTTPException(status_code=400, detail="Part description is required.")

    quantity = max(1, quantity)

    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT id FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        connection.execute(
            """
            INSERT INTO job_parts (
                job_id, requested_description, quantity, verification_status
            )
            VALUES (?, ?, ?, 'PENDING')
            """,
            (job_id, description, quantity),
        )
        connection.commit()

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/status")
def update_job_status(
    job_id: int,
    status: Annotated[str, Form()],
):
    allowed = {
        "REQUESTED", "RESEARCHING", "VERIFIED", "QUOTED", "CONFIRMED",
        "ORDERED", "RECEIVED", "DELIVERED", "VOID",
    }

    if status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid status.")

    with closing(get_connection()) as connection:
        connection.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            (status, job_id),
        )
        connection.commit()

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/parts/{part_id}/verify")
def verify_part(
    part_id: int,
    oem_part_number: Annotated[str, Form()] = "",
    oem_description: Annotated[str, Form()] = "",
    diagram_name: Annotated[str, Form()] = "",
    callout_number: Annotated[str, Form()] = "",
    source_url: Annotated[str, Form()] = "",
    verification_notes: Annotated[str, Form()] = "",
):
    with closing(get_connection()) as connection:
        part = connection.execute(
            "SELECT id, job_id FROM job_parts WHERE id = ?",
            (part_id,),
        ).fetchone()

        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        status = "VERIFIED" if oem_part_number.strip() else "PENDING"

        connection.execute(
            """
            UPDATE job_parts
            SET
                oem_part_number = ?,
                oem_description = ?,
                diagram_name = ?,
                callout_number = ?,
                source_url = ?,
                verification_notes = ?,
                verification_status = ?
            WHERE id = ?
            """,
            (
                oem_part_number.strip(),
                oem_description.strip(),
                diagram_name.strip(),
                callout_number.strip(),
                source_url.strip(),
                verification_notes.strip(),
                status,
                part_id,
            ),
        )

        job_id = part["job_id"]
        remaining = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_parts
            WHERE job_id = ? AND verification_status != 'VERIFIED'
            """,
            (job_id,),
        ).fetchone()["count"]

        if remaining == 0:
            connection.execute(
                "UPDATE jobs SET status = 'VERIFIED' WHERE id = ?",
                (job_id,),
            )
        else:
            connection.execute(
                """
                UPDATE jobs
                SET status = CASE
                    WHEN status = 'REQUESTED' THEN 'RESEARCHING'
                    ELSE status
                END
                WHERE id = ?
                """,
                (job_id,),
            )

        connection.commit()

    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/parts/{part_id}/supplier")
def update_supplier(
    part_id: int,
    supplier: Annotated[str, Form()] = "",
    supplier_cost: Annotated[float | None, Form()] = None,
    availability: Annotated[str, Form()] = "",
):
    with closing(get_connection()) as connection:
        part = connection.execute(
            "SELECT job_id, oem_part_number FROM job_parts WHERE id = ?",
            (part_id,),
        ).fetchone()

        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        connection.execute(
            """
            UPDATE job_parts
            SET supplier = ?, supplier_cost = ?, availability = ?
            WHERE id = ?
            """,
            (supplier.strip(), supplier_cost, availability.strip(), part_id),
        )
        connection.commit()

    return RedirectResponse(url=f"/jobs/{part['job_id']}", status_code=303)


@app.post("/parts/{part_id}/sources")
def add_part_source(
    part_id: int,
    supplier_name: Annotated[str, Form()],
    source_type: Annotated[str, Form()] = "AFTERMARKET",
    brand: Annotated[str, Form()] = "",
    supplier_part_number: Annotated[str, Form()] = "",
    supplier_cost: Annotated[float | None, Form()] = None,
    availability: Annotated[str, Form()] = "",
    lead_time: Annotated[str, Form()] = "",
    quote_reference: Annotated[str, Form()] = "",
):
    supplier_name = supplier_name.strip()
    if not supplier_name:
        raise HTTPException(status_code=400, detail="Supplier name is required.")

    source_type = source_type.strip().upper()
    if source_type not in {"OEM", "AFTERMARKET", "USED", "REMAN"}:
        raise HTTPException(status_code=400, detail="Invalid source type.")

    with closing(get_connection()) as connection:
        part = connection.execute(
            "SELECT job_id, oem_part_number FROM job_parts WHERE id = ?",
            (part_id,),
        ).fetchone()
        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        final_supplier_part_number = supplier_part_number.strip() or (part["oem_part_number"] or "")

        connection.execute(
            "INSERT OR IGNORE INTO suppliers (name) VALUES (?)",
            (supplier_name,),
        )

        connection.execute(
            """
            INSERT INTO part_sources (
                part_id, supplier_name, source_type, brand,
                supplier_part_number, supplier_cost, availability,
                lead_time, quote_reference, trust_level
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL')
            """,
            (
                part_id,
                supplier_name,
                source_type,
                brand.strip(),
                final_supplier_part_number,
                supplier_cost,
                availability.strip(),
                lead_time.strip(),
                quote_reference.strip(),
            ),
        )
        connection.commit()

    return RedirectResponse(url=f"/jobs/{part['job_id']}#part-{part_id}", status_code=303)


@app.post("/parts/{part_id}/sources/{source_id}/select")
def select_part_source(part_id: int, source_id: int):
    with closing(get_connection()) as connection:
        part = connection.execute(
            "SELECT job_id FROM job_parts WHERE id = ?",
            (part_id,),
        ).fetchone()
        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        source = connection.execute(
            "SELECT * FROM part_sources WHERE id = ? AND part_id = ?",
            (source_id, part_id),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="Supplier source not found.")

        connection.execute(
            "UPDATE part_sources SET selected_for_quote = 0, updated_at = CURRENT_TIMESTAMP WHERE part_id = ?",
            (part_id,),
        )
        connection.execute(
            "UPDATE part_sources SET selected_for_quote = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (source_id,),
        )
        connection.execute(
            """
            UPDATE job_parts
            SET supplier = ?, supplier_cost = ?, availability = ?
            WHERE id = ?
            """,
            (
                source["supplier_name"],
                source["supplier_cost"],
                source["availability"],
                part_id,
            ),
        )
        connection.commit()

    return RedirectResponse(url=f"/jobs/{part['job_id']}#part-{part_id}", status_code=303)


@app.post("/parts/{part_id}/sources/{source_id}/delete")
def delete_part_source(part_id: int, source_id: int):
    with closing(get_connection()) as connection:
        part = connection.execute(
            "SELECT job_id FROM job_parts WHERE id = ?",
            (part_id,),
        ).fetchone()
        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        source = connection.execute(
            "SELECT selected_for_quote FROM part_sources WHERE id = ? AND part_id = ?",
            (source_id, part_id),
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="Supplier source not found.")

        connection.execute("DELETE FROM part_sources WHERE id = ?", (source_id,))
        if source["selected_for_quote"]:
            connection.execute(
                "UPDATE job_parts SET supplier = '', supplier_cost = NULL, availability = '' WHERE id = ?",
                (part_id,),
            )
        connection.commit()

    return RedirectResponse(url=f"/jobs/{part['job_id']}#part-{part_id}", status_code=303)


@app.post("/parts/{part_id}/quick-capture")
def quick_capture_part(
    part_id: int,
    raw_oem_text: Annotated[str, Form()],
):
    raw = raw_oem_text.strip()

    if not raw:
        raise HTTPException(
            status_code=400,
            detail="Paste the OEM part number and description.",
        )

    part_number = ""
    description = ""

    if ":" in raw:
        left, right = raw.split(":", 1)
        part_number = left.strip()
        description = right.strip()
    elif " - " in raw:
        left, right = raw.split(" - ", 1)
        part_number = left.strip()
        description = right.strip()
    else:
        pieces = raw.split(maxsplit=1)
        part_number = pieces[0].strip()
        description = pieces[1].strip() if len(pieces) > 1 else ""

    if not part_number:
        raise HTTPException(
            status_code=400,
            detail="Could not identify the OEM part number.",
        )

    with closing(get_connection()) as connection:
        part = connection.execute(
            "SELECT id, job_id FROM job_parts WHERE id = ?",
            (part_id,),
        ).fetchone()

        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        connection.execute(
            """
            UPDATE job_parts
            SET
                oem_part_number = ?,
                oem_description = ?,
                verification_status = 'VERIFIED'
            WHERE id = ?
            """,
            (part_number, description, part_id),
        )

        remaining = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_parts
            WHERE job_id = ?
              AND verification_status != 'VERIFIED'
            """,
            (part["job_id"],),
        ).fetchone()["count"]

        new_status = "VERIFIED" if remaining == 0 else "RESEARCHING"

        connection.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            (new_status, part["job_id"]),
        )
        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{part['job_id']}#part-{part_id}",
        status_code=303,
    )



@app.post("/jobs/{job_id}/start-sis-cart-import")
def start_sis_cart_import(job_id: int):
    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT id, manufacturer FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if (job["manufacturer"] or "").strip().upper() not in {"CAT", "CATERPILLAR"}:
            raise HTTPException(status_code=400, detail="SIS cart import is available only for CAT jobs.")
        connection.execute(
            """
            INSERT INTO active_job_import (id, job_id, started_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                job_id = excluded.job_id,
                started_at = CURRENT_TIMESTAMP
            """,
            (job_id,),
        )
        connection.commit()
    return RedirectResponse(url="https://sis2.cat.com/#/cart", status_code=303)


@app.get("/api/active-import-job")
def api_active_import_job():
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT jobs.id AS job_id, jobs.job_number, jobs.customer,
                   jobs.manufacturer, jobs.machine, jobs.pin_serial
            FROM active_job_import
            JOIN jobs ON jobs.id = active_job_import.job_id
            WHERE active_job_import.id = 1
            """
        ).fetchone()
    if row is None:
        return JSONResponse(status_code=404, content={"ok": False, "message": "No active cart import job."})
    return {"ok": True, **dict(row)}


@app.post("/api/import-sis-cart")
async def api_import_sis_cart(request: Request):
    payload = await request.json()
    job_id = payload.get("job_id")
    items = payload.get("items") or []
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required.")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="At least one SIS cart item is required.")

    imported = []
    with closing(get_connection()) as connection:
        job = connection.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        for raw in items:
            part_number = str(raw.get("oem_part_number", "")).strip()
            description = str(raw.get("oem_description", "")).strip() or "CAT SIS Part"
            quantity = raw.get("quantity", 1)
            try:
                quantity = max(1, int(quantity))
            except (TypeError, ValueError):
                quantity = 1
            raw_price = raw.get("oem_price")
            try:
                price = float(raw_price) if raw_price not in (None, "") else None
            except (TypeError, ValueError):
                price = None
            availability = str(raw.get("availability", "")).strip()
            source_url = str(raw.get("source_url", "")).strip() or "https://sis2.cat.com/#/cart"

            if not part_number:
                continue

            part = connection.execute(
                """
                SELECT * FROM job_parts
                WHERE job_id = ? AND COALESCE(oem_part_number, '') = ?
                ORDER BY id LIMIT 1
                """,
                (job_id, part_number),
            ).fetchone()

            if part is None:
                part = connection.execute(
                    """
                    SELECT * FROM job_parts
                    WHERE job_id = ? AND verification_status != 'VERIFIED'
                      AND LOWER(TRIM(requested_description)) = LOWER(TRIM(?))
                    ORDER BY id LIMIT 1
                    """,
                    (job_id, description),
                ).fetchone()

            if part is None:
                cursor = connection.execute(
                    """
                    INSERT INTO job_parts (
                        job_id, requested_description, quantity,
                        oem_part_number, oem_description, verification_status,
                        verification_source, oem_dealer_name, oem_dealer_price,
                        oem_dealer_availability, source_url, product_url, captured_at
                    ) VALUES (?, ?, ?, ?, ?, 'VERIFIED', 'CAT SIS CART',
                              'CAT SIS', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (job_id, description, quantity, part_number, description,
                     price, availability, source_url, source_url),
                )
                part_id = cursor.lastrowid
            else:
                part_id = part["id"]
                connection.execute(
                    """
                    UPDATE job_parts
                    SET quantity = ?, oem_part_number = ?, oem_description = ?,
                        verification_status = 'VERIFIED', verification_source = 'CAT SIS CART',
                        oem_dealer_name = 'CAT SIS', oem_dealer_price = ?,
                        oem_dealer_availability = ?, source_url = ?, product_url = ?,
                        captured_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (quantity, part_number, description, price, availability,
                     source_url, source_url, part_id),
                )

            source = connection.execute(
                """
                SELECT id FROM part_sources
                WHERE part_id = ? AND source_type = 'OEM'
                  AND COALESCE(supplier_part_number, '') = ?
                ORDER BY id LIMIT 1
                """,
                (part_id, part_number),
            ).fetchone()
            if source:
                connection.execute(
                    """
                    UPDATE part_sources
                    SET supplier_name = 'CAT SIS', brand = 'CAT', supplier_cost = ?,
                        availability = ?, trust_level = 'OEM_VERIFIED', source_url = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (price, availability, source_url, source["id"]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO part_sources (
                        part_id, supplier_name, source_type, brand,
                        supplier_part_number, supplier_cost, availability,
                        trust_level, source_url
                    ) VALUES (?, 'CAT SIS', 'OEM', 'CAT', ?, ?, ?, 'OEM_VERIFIED', ?)
                    """,
                    (part_id, part_number, price, availability, source_url),
                )
            connection.execute("INSERT OR IGNORE INTO suppliers (name) VALUES ('CAT SIS')")
            imported.append({"part_id": part_id, "oem_part_number": part_number, "description": description})

        remaining = connection.execute(
            "SELECT COUNT(*) AS count FROM job_parts WHERE job_id = ? AND verification_status != 'VERIFIED'",
            (job_id,),
        ).fetchone()["count"]
        connection.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            ("VERIFIED" if remaining == 0 else "RESEARCHING", job_id),
        )
        connection.execute("DELETE FROM active_job_import WHERE id = 1")
        connection.commit()

    return {"ok": True, "job_id": job_id, "imported_count": len(imported), "items": imported}


@app.post("/parts/{part_id}/start-cat-verification")
def start_cat_verification(part_id: int):
    with closing(get_connection()) as connection:
        part = connection.execute(
            """
            SELECT
                job_parts.id AS part_id,
                job_parts.job_id,
                jobs.manufacturer,
                jobs.pin_serial
            FROM job_parts
            JOIN jobs ON jobs.id = job_parts.job_id
            WHERE job_parts.id = ?
            """,
            (part_id,),
        ).fetchone()

        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        if (part["manufacturer"] or "").strip().upper() not in {"CAT", "CATERPILLAR"}:
            raise HTTPException(
                status_code=400,
                detail="CAT verification is available only for Caterpillar jobs.",
            )

        connection.execute(
            """
            INSERT INTO active_verification (id, job_id, part_id, started_at)
            VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                job_id = excluded.job_id,
                part_id = excluded.part_id,
                started_at = CURRENT_TIMESTAMP
            """,
            (part["job_id"], part_id),
        )
        connection.execute(
            """
            UPDATE jobs
            SET status = CASE
                WHEN status = 'REQUESTED' THEN 'RESEARCHING'
                ELSE status
            END
            WHERE id = ?
            """,
            (part["job_id"],),
        )
        connection.commit()

    return RedirectResponse(
        url=f"https://sis2.cat.com/#/detail?serialNumber={part['pin_serial']}&tab=parts",
        status_code=303,
    )


@app.get("/api/active-verification")
def api_active_verification():
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT
                jobs.id AS job_id,
                jobs.job_number,
                jobs.customer,
                jobs.manufacturer,
                jobs.machine,
                jobs.pin_serial,
                job_parts.id AS part_id,
                job_parts.requested_description
            FROM active_verification
            JOIN jobs ON jobs.id = active_verification.job_id
            JOIN job_parts ON job_parts.id = active_verification.part_id
            WHERE active_verification.id = 1
            """
        ).fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "message": "No active verification."},
        )

    return {"ok": True, **dict(row)}


@app.post("/api/capture-part")
async def api_capture_part(request: Request):
    payload = await request.json()

    part_id = payload.get("part_id")
    oem_part_number = str(payload.get("oem_part_number", "")).strip()
    oem_description = str(payload.get("oem_description", "")).strip()
    diagram_name = str(payload.get("diagram_name", "")).strip()
    callout_number = str(payload.get("callout_number", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    diagram_url = str(payload.get("diagram_url", "")).strip()
    product_url = str(payload.get("product_url", "")).strip()
    oem_dealer_name = str(payload.get("oem_dealer_name", "")).strip()
    oem_dealer_availability = str(payload.get("oem_dealer_availability", "")).strip()
    oem_dealer_lead_time = str(payload.get("oem_dealer_lead_time", "")).strip()
    raw_dealer_price = payload.get("oem_dealer_price", None)
    try:
        oem_dealer_price = float(raw_dealer_price) if raw_dealer_price not in (None, "") else None
    except (TypeError, ValueError):
        oem_dealer_price = None
    verification_source = str(payload.get("verification_source", "")).strip()
    verification_notes = str(payload.get("verification_notes", "")).strip()

    if not part_id:
        raise HTTPException(status_code=400, detail="part_id is required.")

    if not oem_part_number:
        raise HTTPException(status_code=400, detail="OEM part number is required.")

    with closing(get_connection()) as connection:
        part = connection.execute(
            "SELECT id, job_id FROM job_parts WHERE id = ?",
            (part_id,),
        ).fetchone()

        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        connection.execute(
            """
            UPDATE job_parts
            SET
                oem_part_number = ?,
                oem_description = ?,
                diagram_name = ?,
                callout_number = ?,
                source_url = ?,
                diagram_url = ?,
                product_url = ?,
                captured_at = CURRENT_TIMESTAMP,
                oem_dealer_name = ?,
                oem_dealer_price = ?,
                oem_dealer_availability = ?,
                oem_dealer_lead_time = ?,
                verification_source = ?,
                verification_notes = ?,
                verification_status = 'VERIFIED'
            WHERE id = ?
            """,
            (
                oem_part_number,
                oem_description,
                diagram_name,
                callout_number,
                source_url,
                diagram_url,
                product_url,
                oem_dealer_name,
                oem_dealer_price,
                oem_dealer_availability,
                oem_dealer_lead_time,
                verification_source,
                verification_notes,
                part_id,
            ),
        )

        # Keep the verified OEM option available for supplier selection.
        existing_oem = connection.execute(
            """
            SELECT id FROM part_sources
            WHERE part_id = ? AND source_type = 'OEM'
            ORDER BY id LIMIT 1
            """,
            (part_id,),
        ).fetchone()

        oem_supplier_name = oem_dealer_name or verification_source or "OEM Source"
        oem_source_url = product_url or source_url

        if existing_oem:
            connection.execute(
                """
                UPDATE part_sources
                SET supplier_name = ?, supplier_part_number = ?, supplier_cost = ?,
                    availability = ?, lead_time = ?, trust_level = 'OEM_VERIFIED',
                    source_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    oem_supplier_name, oem_part_number, oem_dealer_price,
                    oem_dealer_availability, oem_dealer_lead_time,
                    oem_source_url, existing_oem["id"],
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO part_sources (
                    part_id, supplier_name, source_type, supplier_part_number,
                    supplier_cost, availability, lead_time, trust_level, source_url
                )
                VALUES (?, ?, 'OEM', ?, ?, ?, ?, 'OEM_VERIFIED', ?)
                """,
                (
                    part_id, oem_supplier_name, oem_part_number,
                    oem_dealer_price, oem_dealer_availability,
                    oem_dealer_lead_time, oem_source_url,
                ),
            )

        remaining = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_parts
            WHERE job_id = ? AND verification_status != 'VERIFIED'
            """,
            (part["job_id"],),
        ).fetchone()["count"]

        new_status = "VERIFIED" if remaining == 0 else "RESEARCHING"

        connection.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            (new_status, part["job_id"]),
        )

        connection.execute(
            "DELETE FROM active_verification WHERE id = 1"
        )
        connection.commit()

    return {
        "ok": True,
        "message": "OEM part captured and saved.",
        "job_id": part["job_id"],
        "part_id": part_id,
    }
