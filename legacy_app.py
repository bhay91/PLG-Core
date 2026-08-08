from __future__ import annotations

from fastapi.responses import FileResponse
from plg_core.documents.quote_pdf import generate_quote_pdfs, quote_paths, sanitize_path_name
from plg_core.documents.invoice_pdf import generate_invoice_pdfs, invoice_paths
from fastapi import File, UploadFile

import sqlite3
import math
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from plg_core.jobs.engine import JobEngine
from plg_core.dashboard.service import get_dashboard_data
from plg_core.machines.identifiers import find_machine_by_identifier

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "plg_core.db"

from plg_core.documents.parts_order_pdf import (
    generate_parts_order_sheet,
    parts_order_sheet_path,
)

app = FastAPI(title="Pinpoint Sourcing Co. Core")
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

        supplier_columns = {row["name"] for row in connection.execute("PRAGMA table_info(suppliers)").fetchall()}
        supplier_additions = {
            "website": "ALTER TABLE suppliers ADD COLUMN website TEXT DEFAULT ''",
            "phone": "ALTER TABLE suppliers ADD COLUMN phone TEXT DEFAULT ''",
            "email": "ALTER TABLE suppliers ADD COLUMN email TEXT DEFAULT ''",
            "contact_person": "ALTER TABLE suppliers ADD COLUMN contact_person TEXT DEFAULT ''",
            "account_number": "ALTER TABLE suppliers ADD COLUMN account_number TEXT DEFAULT ''",
            "rating": "ALTER TABLE suppliers ADD COLUMN rating INTEGER NOT NULL DEFAULT 3",
            "status": "ALTER TABLE suppliers ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'",
            "preferred": "ALTER TABLE suppliers ADD COLUMN preferred INTEGER NOT NULL DEFAULT 0",
        }
        for column, sql in supplier_additions.items():
            if column not in supplier_columns:
                connection.execute(sql)

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

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS active_source_import (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                job_id INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                source_name TEXT NOT NULL,
                activated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS connector_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connector_key TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Supplier',
                trust_level TEXT NOT NULL DEFAULT 'SUPPLIER_VERIFIED',
                launch_url TEXT NOT NULL DEFAULT '',
                connector_type TEXT NOT NULL DEFAULT 'CART',
                parser_key TEXT NOT NULL DEFAULT '',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 100,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connector_columns = {row["name"] for row in connection.execute("PRAGMA table_info(connector_profiles)").fetchall()}
        if "is_archived" not in connector_columns:
            connection.execute("ALTER TABLE connector_profiles ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0")

        default_connectors = [
            ('cat_sis', 'CAT SIS', 'OEM', 'OEM_VERIFIED',
             'https://sis2.cat.com/#/cart', 'CART', 'cat_sis_cart', 1, 10),
            ('worldpac', 'Worldpac', 'Automotive', 'SUPPLIER_VERIFIED',
             'https://speeddial.worldpac.com/#/login', 'CART', 'worldpac_cart', 1, 20),
            ('7zap', '7zap', 'Automotive Catalog', 'NEEDS_REVIEW',
             'https://7zap.com/en/vin-decoder/', 'CATALOG', '7zap_catalog', 1, 25),
            ('ssf', 'SSF', 'Automotive', 'SUPPLIER_VERIFIED',
             'https://www.ssfautoparts.com/', 'CART', 'ssf_cart', 0, 30),
            ('rockauto', 'RockAuto', 'Automotive', 'SUPPLIER_VERIFIED',
             'https://www.rockauto.com/', 'CART', 'rockauto_cart', 0, 40),
            ('upload_quote', 'Upload Quote / Image', 'Document', 'NEEDS_REVIEW',
             '', 'UPLOAD', 'document_import', 1, 90),
        ]

        for row in default_connectors:
            connection.execute(
                """
                INSERT INTO connector_profiles (
                    connector_key, display_name, category, trust_level,
                    launch_url, connector_type, parser_key,
                    is_enabled, sort_order
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    category = excluded.category,
                    trust_level = excluded.trust_level,
                    launch_url = excluded.launch_url,
                    connector_type = excluded.connector_type,
                    parser_key = excluded.parser_key,
                    sort_order = excluded.sort_order
                """,
                row,
            )


        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_cart_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT DEFAULT '',
                shipping_total REAL,
                currency TEXT DEFAULT 'USD',
                imported_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_number TEXT UNIQUE,
                name TEXT NOT NULL,
                company TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                email TEXT DEFAULT '',
                address TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        customer_columns={row["name"] for row in connection.execute("PRAGMA table_info(customers)").fetchall()}
        if "last_viewed_at" not in customer_columns:
            connection.execute("ALTER TABLE customers ADD COLUMN last_viewed_at TEXT")

        job_columns={row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
        if "customer_id" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN customer_id INTEGER")
        if "address" not in job_columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN address TEXT DEFAULT ''")

        for old_job in connection.execute("SELECT id, customer, company, phone, email, address FROM jobs ORDER BY id").fetchall():
            name=str(old_job["customer"] or "").strip()
            if not name:
                continue
            row=connection.execute("""
                SELECT id FROM customers
                WHERE LOWER(TRIM(name))=LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(company,'')))=LOWER(TRIM(COALESCE(?,'')))
                ORDER BY id LIMIT 1
            """,(name,old_job["company"] or "")).fetchone()
            if row is None:
                cur=connection.execute("INSERT INTO customers (name,company,phone,email,address) VALUES (?,?,?,?,?)",(name,old_job["company"] or "",old_job["phone"] or "",old_job["email"] or "",old_job["address"] or ""))
                customer_id=cur.lastrowid
                connection.execute("UPDATE customers SET customer_number=? WHERE id=?",(f"PPS-C-{customer_id:04d}",customer_id))
            else:
                customer_id=row["id"]
            connection.execute("UPDATE jobs SET customer_id=? WHERE id=?",(customer_id,old_job["id"]))

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_number TEXT UNIQUE NOT NULL,
                job_id INTEGER NOT NULL,
                quote_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                parts_subtotal REAL NOT NULL DEFAULT 0,
                shipping_total REAL NOT NULL DEFAULT 0,
                customer_total REAL NOT NULL DEFAULT 0,
                supplier_total REAL NOT NULL DEFAULT 0,
                profit_total REAL NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
            """
        )

        quote_columns = {row["name"] for row in connection.execute("PRAGMA table_info(quotes)").fetchall()}
        if "is_archived" not in quote_columns:
            connection.execute("ALTER TABLE quotes ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS quote_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id INTEGER NOT NULL,
                part_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                description TEXT NOT NULL,
                supplier_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                brand TEXT DEFAULT '',
                supplier_part_number TEXT DEFAULT '',
                supplier_unit_cost REAL NOT NULL DEFAULT 0,
                customer_unit_price REAL NOT NULL DEFAULT 0,
                supplier_line_total REAL NOT NULL DEFAULT 0,
                customer_line_total REAL NOT NULL DEFAULT 0,
                line_profit REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (quote_id) REFERENCES quotes(id)
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS document_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_key TEXT NOT NULL DEFAULT 'master_corporate',
                display_name TEXT NOT NULL,
                version_number INTEGER NOT NULL DEFAULT 1,
                original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0,
                uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                notes TEXT DEFAULT '',
                UNIQUE(template_key, version_number)
            )
            """
        )

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                quote_id INTEGER NOT NULL UNIQUE,
                job_id INTEGER NOT NULL,
                invoice_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'UNPAID',
                parts_subtotal REAL NOT NULL DEFAULT 0,
                shipping_total REAL NOT NULL DEFAULT 0,
                customer_total REAL NOT NULL DEFAULT 0,
                supplier_total REAL NOT NULL DEFAULT 0,
                profit_total REAL NOT NULL DEFAULT 0,
                credit_applied REAL NOT NULL DEFAULT 0,
                balance_due REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (quote_id) REFERENCES quotes(id),
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            );

            CREATE TABLE IF NOT EXISTS invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                quote_item_id INTEGER,
                part_id INTEGER,
                source_id INTEGER,
                quantity INTEGER NOT NULL DEFAULT 1,
                description TEXT NOT NULL,
                supplier_name TEXT DEFAULT '',
                source_type TEXT DEFAULT '',
                brand TEXT DEFAULT '',
                supplier_part_number TEXT DEFAULT '',
                supplier_unit_cost REAL NOT NULL DEFAULT 0,
                customer_unit_price REAL NOT NULL DEFAULT 0,
                supplier_line_total REAL NOT NULL DEFAULT 0,
                customer_line_total REAL NOT NULL DEFAULT 0,
                line_profit REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
                FOREIGN KEY (quote_item_id) REFERENCES quote_items(id)
            );

            CREATE INDEX IF NOT EXISTS idx_invoices_quote_id ON invoices(quote_id);
            CREATE INDEX IF NOT EXISTS idx_invoices_job_id ON invoices(job_id);
            CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice_id ON invoice_items(invoice_id);
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                transaction_date TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                amount REAL NOT NULL,
                payment_method TEXT DEFAULT '',
                reference TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                job_id INTEGER,
                quote_id INTEGER,
                invoice_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            )
            """
        )

        connection.commit()


def next_job_number(connection: sqlite3.Connection) -> str:
    prefix = "PPS-J-"

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

    return f"{prefix}{sequence:04d}"


@app.on_event("startup")
def startup() -> None:
    initialize_database()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with closing(get_connection()) as connection:
        dashboard_data = get_dashboard_data(connection)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            **dashboard_data,
            "active_page": "dashboard",
        },
    )


@app.get("/jobs/new", response_class=HTMLResponse)
def new_job_form(request: Request, customer_id: int | None = None):
    with closing(get_connection()) as connection:
        customers=connection.execute("SELECT * FROM customers WHERE active=1 ORDER BY name COLLATE NOCASE, company COLLATE NOCASE").fetchall()
        machines=connection.execute("SELECT * FROM machines WHERE active=1 ORDER BY customer_id, name COLLATE NOCASE").fetchall()
    return templates.TemplateResponse(request=request,name="new_job.html",context={"customers":customers,"machines":machines,"selected_customer_id":customer_id,"active_page":"jobs"})



@app.post("/jobs")
def create_job(
    customer: Annotated[str, Form()],
    customer_id: Annotated[int | None, Form()] = None,
    machine_id: Annotated[int | None, Form()] = None,
    company: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    address: Annotated[str, Form()] = "",
    manufacturer: Annotated[str, Form()] = "",
    machine: Annotated[str, Form()] = "",
    pin_serial: Annotated[str, Form()] = "",
    requested_parts: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    with closing(get_connection()) as connection:
        if customer_id:
            customer_row=connection.execute("SELECT * FROM customers WHERE id=? AND active=1",(customer_id,)).fetchone()
            if customer_row is None:
                raise HTTPException(status_code=400,detail="Selected customer not found.")
        else:
            name=customer.strip()
            if not name:
                raise HTTPException(status_code=400,detail="Customer is required.")
            cur=connection.execute("INSERT INTO customers (name,company,phone,email,address) VALUES (?,?,?,?,?)",(name,company.strip(),phone.strip(),email.strip(),address.strip()))
            customer_id=cur.lastrowid
            connection.execute("UPDATE customers SET customer_number=? WHERE id=?",(f"PPS-C-{customer_id:04d}",customer_id))
            customer_row=connection.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()
        selected_machine = None
        if machine_id:
            selected_machine = connection.execute(
                "SELECT * FROM machines WHERE id=? AND customer_id=? AND active=1",
                (machine_id, customer_row["id"]),
            ).fetchone()
            if selected_machine is None:
                raise HTTPException(status_code=400, detail="Selected machine not found for this customer.")
            manufacturer = selected_machine["manufacturer"] or ""
            machine = selected_machine["model"] or selected_machine["name"] or ""
            pin_serial = selected_machine["vin_pin_serial"] or ""
        elif any((manufacturer.strip(), machine.strip(), pin_serial.strip())):
            existing_machine = find_machine_by_identifier(
                connection,
                pin_serial.strip(),
            )

            if existing_machine is not None:
                if existing_machine["customer_id"] != customer_row["id"]:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"VIN/PIN/serial already belongs to "
                            f"{existing_machine['machine_number']} "
                            f"({existing_machine['customer_name']}). "
                            "Transfer the machine before creating this Job."
                        ),
                    )
                machine_id = existing_machine["id"]
                manufacturer = existing_machine["manufacturer"] or manufacturer
                machine = existing_machine["model"] or existing_machine["name"] or machine
                pin_serial = existing_machine["vin_pin_serial"] or pin_serial
            else:
                display_name = " ".join(
                    part for part in (manufacturer.strip(), machine.strip()) if part
                ).strip() or pin_serial.strip()
                machine_cursor = connection.execute(
                    "INSERT INTO machines (customer_id,name,manufacturer,model,vin_pin_serial) VALUES (?,?,?,?,?)",
                    (customer_row["id"], display_name, manufacturer.strip(), machine.strip(), pin_serial.strip()),
                )
                machine_id = machine_cursor.lastrowid
                connection.execute(
                    "UPDATE machines SET machine_number=? WHERE id=?",
                    (f"PPS-M-{machine_id:04d}", machine_id),
                )
        job_number=next_job_number(connection)
        cur=connection.execute("""
            INSERT INTO jobs (job_number,created_date,customer_id,machine_id,customer,company,phone,email,address,manufacturer,machine,pin_serial,status,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'REQUESTED',?)
        """,(job_number,date.today().isoformat(),customer_row["id"],machine_id,customer_row["name"],customer_row["company"] or "",customer_row["phone"] or "",customer_row["email"] or "",customer_row["address"] or "",manufacturer.strip(),machine.strip(),pin_serial.strip(),notes.strip()))
        job_id=cur.lastrowid
        connection.commit()
    return RedirectResponse(url=f"/jobs/{job_id}/basket",status_code=303)



@app.get("/customers", response_class=HTMLResponse)
def list_customers(request: Request, view: str = "active"):
    if view not in {"active", "inactive", "all"}: view = "active"
    where = "" if view == "all" else ("WHERE customers.active=1" if view == "active" else "WHERE customers.active=0")
    with closing(get_connection()) as connection:
        rows=connection.execute(f"""
            SELECT customers.*, COUNT(DISTINCT jobs.id) AS jobs_count,
                   COUNT(DISTINCT quotes.id) AS quotes_count,
                   COALESCE(SUM(customer_transactions.amount),0) AS net_balance
            FROM customers
            LEFT JOIN jobs ON jobs.customer_id=customers.id
            LEFT JOIN quotes ON quotes.job_id=jobs.id
            LEFT JOIN customer_transactions ON customer_transactions.customer_id=customers.id
            {where}
            GROUP BY customers.id
            ORDER BY customers.name COLLATE NOCASE
        """).fetchall()
        customers=[]
        for row in rows:
            item=dict(row); net=float(item.get("net_balance") or 0)
            item["available_credit"]=max(net,0); item["outstanding_balance"]=max(-net,0); customers.append(item)
        recent_customers=connection.execute("SELECT * FROM customers WHERE active=1 AND last_viewed_at IS NOT NULL ORDER BY last_viewed_at DESC LIMIT 8").fetchall()
    return templates.TemplateResponse(request=request,name="customers.html",context={"customers":customers,"recent_customers":recent_customers,"view":view,"active_page":"customers"})

@app.get("/customers/new", response_class=HTMLResponse)
def new_customer_form(request: Request):
    return templates.TemplateResponse(request=request,name="customer_form.html",context={"title":"New Customer","subtitle":"Create a customer without creating a job.","form_action":"/customers/new","cancel_url":"/customers","submit_label":"Save Customer","duplicate":None,"form":{"name":"","company":"","phone":"","email":"","address":""},"active_page":"customers"})

@app.post("/customers/new")
def create_customer(request: Request,name: Annotated[str,Form()],company: Annotated[str,Form()]="",phone: Annotated[str,Form()]="",email: Annotated[str,Form()]="",address: Annotated[str,Form()]="",create_anyway: Annotated[int,Form()]=0):
    name,company,phone,email,address=[v.strip() for v in (name,company,phone,email,address)]
    if not name: raise HTTPException(status_code=400,detail="Customer name is required.")
    with closing(get_connection()) as connection:
        duplicate=connection.execute("""SELECT * FROM customers WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) OR (?!='' AND TRIM(phone)=TRIM(?)) OR (?!='' AND LOWER(TRIM(email))=LOWER(TRIM(?))) ORDER BY active DESC,id LIMIT 1""",(name,phone,phone,email,email)).fetchone()
        if duplicate is not None and not create_anyway:
            return templates.TemplateResponse(request=request,name="customer_form.html",context={"title":"New Customer","subtitle":"Review the possible duplicate.","form_action":"/customers/new","cancel_url":"/customers","submit_label":"Save Customer","duplicate":duplicate,"form":{"name":name,"company":company,"phone":phone,"email":email,"address":address},"active_page":"customers"})
        cur=connection.execute("INSERT INTO customers (name,company,phone,email,address,active) VALUES (?,?,?,?,?,1)",(name,company,phone,email,address))
        customer_id=cur.lastrowid
        connection.execute("UPDATE customers SET customer_number=? WHERE id=?",(f"PPS-C-{customer_id:04d}",customer_id)); connection.commit()
    return RedirectResponse(url=f"/customers/{customer_id}",status_code=303)

@app.get("/customers/{customer_id}/edit", response_class=HTMLResponse)
def edit_customer_form(request: Request,customer_id: int):
    with closing(get_connection()) as connection: customer=connection.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()
    if customer is None: raise HTTPException(status_code=404,detail="Customer not found.")
    return templates.TemplateResponse(request=request,name="customer_form.html",context={"title":"Edit Customer","subtitle":customer["customer_number"],"form_action":f"/customers/{customer_id}/edit","cancel_url":f"/customers/{customer_id}","submit_label":"Save Changes","duplicate":None,"form":customer,"active_page":"customers"})

@app.post("/customers/{customer_id}/edit")
def update_customer(customer_id: int,name: Annotated[str,Form()],company: Annotated[str,Form()]="",phone: Annotated[str,Form()]="",email: Annotated[str,Form()]="",address: Annotated[str,Form()]=""):
    name=name.strip()
    if not name: raise HTTPException(status_code=400,detail="Customer name is required.")
    with closing(get_connection()) as connection:
        connection.execute("UPDATE customers SET name=?,company=?,phone=?,email=?,address=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(name,company.strip(),phone.strip(),email.strip(),address.strip(),customer_id))
        connection.execute("UPDATE jobs SET customer=?,company=?,phone=?,email=?,address=? WHERE customer_id=?",(name,company.strip(),phone.strip(),email.strip(),address.strip(),customer_id)); connection.commit()
    return RedirectResponse(url=f"/customers/{customer_id}",status_code=303)

@app.post("/customers/{customer_id}/deactivate")
def deactivate_customer(customer_id: int):
    with closing(get_connection()) as connection: connection.execute("UPDATE customers SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",(customer_id,)); connection.commit()
    return RedirectResponse(url="/customers?view=active",status_code=303)

@app.post("/customers/{customer_id}/reactivate")
def reactivate_customer(customer_id: int):
    with closing(get_connection()) as connection: connection.execute("UPDATE customers SET active=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",(customer_id,)); connection.commit()
    return RedirectResponse(url=f"/customers/{customer_id}",status_code=303)

@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_account(request: Request, customer_id: int):
    with closing(get_connection()) as connection:
        customer=connection.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()
        if customer is None: raise HTTPException(status_code=404,detail="Customer not found.")
        connection.execute("UPDATE customers SET last_viewed_at=CURRENT_TIMESTAMP WHERE id=?",(customer_id,))
        jobs=connection.execute("SELECT * FROM jobs WHERE customer_id=? ORDER BY id DESC",(customer_id,)).fetchall()
        machines=connection.execute("SELECT * FROM machines WHERE customer_id=? ORDER BY active DESC, name COLLATE NOCASE",(customer_id,)).fetchall()
        quotes=connection.execute("SELECT quotes.* FROM quotes JOIN jobs ON jobs.id=quotes.job_id WHERE jobs.customer_id=? ORDER BY quotes.id DESC",(customer_id,)).fetchall()
        transactions=connection.execute("SELECT * FROM customer_transactions WHERE customer_id=? ORDER BY transaction_date DESC,id DESC",(customer_id,)).fetchall()
        summary=connection.execute("SELECT COALESCE(SUM(amount),0) AS net_balance,COALESCE(SUM(CASE WHEN transaction_type='PAYMENT' THEN amount ELSE 0 END),0) AS total_payments FROM customer_transactions WHERE customer_id=?",(customer_id,)).fetchone(); connection.commit()
        net=float(summary["net_balance"] or 0); account={"available_credit":max(net,0),"outstanding_balance":max(-net,0),"total_payments":float(summary["total_payments"] or 0)}
    return templates.TemplateResponse(request=request,name="customer_account.html",context={"customer":customer,"machines":machines,"jobs":jobs,"quotes":quotes,"transactions":transactions,"account":account,"active_page":"customers"})

@app.post("/customers/{customer_id}/transactions/payment")
def record_customer_payment(customer_id: int,amount: Annotated[float,Form()],payment_method: Annotated[str,Form()],reference: Annotated[str,Form()]=""):
    if amount<=0: raise HTTPException(status_code=400,detail="Payment amount must be greater than zero.")
    with closing(get_connection()) as connection: connection.execute("INSERT INTO customer_transactions (customer_id,transaction_date,transaction_type,amount,payment_method,reference) VALUES (?,?,'PAYMENT',?,?,?)",(customer_id,date.today().isoformat(),round(float(amount),2),payment_method.strip().upper(),reference.strip())); connection.commit()
    return RedirectResponse(url=f"/customers/{customer_id}",status_code=303)

@app.post("/customers/{customer_id}/transactions/refund")
def record_customer_refund(customer_id: int,amount: Annotated[float,Form()],reason: Annotated[str,Form()],reference: Annotated[str,Form()]=""):
    if amount<=0: raise HTTPException(status_code=400,detail="Refund amount must be greater than zero.")
    with closing(get_connection()) as connection: connection.execute("INSERT INTO customer_transactions (customer_id,transaction_date,transaction_type,amount,reference,reason) VALUES (?,?,'REFUND',?,?,?)",(customer_id,date.today().isoformat(),-round(float(amount),2),reference.strip(),reason.strip())); connection.commit()
    return RedirectResponse(url=f"/customers/{customer_id}",status_code=303)

@app.post("/customers/{customer_id}/transactions/adjustment")
def record_customer_adjustment(customer_id: int,amount: Annotated[float,Form()],reason: Annotated[str,Form()],reference: Annotated[str,Form()]=""):
    if amount==0: raise HTTPException(status_code=400,detail="Adjustment amount cannot be zero.")
    with closing(get_connection()) as connection: connection.execute("INSERT INTO customer_transactions (customer_id,transaction_date,transaction_type,amount,reference,reason) VALUES (?,?,'ADJUSTMENT',?,?,?)",(customer_id,date.today().isoformat(),round(float(amount),2),reference.strip(),reason.strip())); connection.commit()
    return RedirectResponse(url=f"/customers/{customer_id}",status_code=303)


@app.get("/jobs", response_class=HTMLResponse)
def list_jobs(request: Request):
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT
                jobs.*,

                (
                    SELECT COUNT(*)
                    FROM job_parts
                    WHERE job_parts.job_id = jobs.id
                ) AS parts_count,

                (
                    SELECT COUNT(*)
                    FROM job_parts
                    WHERE job_parts.job_id = jobs.id
                      AND job_parts.verification_status = 'VERIFIED'
                ) AS verified_parts,

                (
                    SELECT baskets.status
                    FROM baskets
                    WHERE baskets.job_id = jobs.id
                    ORDER BY baskets.id DESC
                    LIMIT 1
                ) AS basket_status,

                (
                    SELECT COUNT(*)
                    FROM basket_items
                    JOIN baskets
                      ON baskets.id = basket_items.basket_id
                    WHERE baskets.job_id = jobs.id
                      AND basket_items.selected = 1
                ) AS selected_items,

                (
                    SELECT COUNT(*)
                    FROM basket_items
                    JOIN baskets
                      ON baskets.id = basket_items.basket_id
                    WHERE baskets.job_id = jobs.id
                      AND basket_items.selected = 1
                      AND COALESCE(
                            basket_items.part_status,
                            'RESEARCH'
                          ) = 'RESEARCH'
                ) AS research_items,

                (
                    SELECT COUNT(*)
                    FROM basket_items
                    JOIN baskets
                      ON baskets.id = basket_items.basket_id
                    WHERE baskets.job_id = jobs.id
                      AND basket_items.selected = 1
                      AND basket_items.part_status = 'QUOTED'
                ) AS quoted_items,

                (
                    SELECT COUNT(*)
                    FROM basket_items
                    JOIN baskets
                      ON baskets.id = basket_items.basket_id
                    WHERE baskets.job_id = jobs.id
                      AND basket_items.selected = 1
                      AND basket_items.part_status = 'ORDERED'
                ) AS ordered_items,

                (
                    SELECT COUNT(*)
                    FROM basket_items
                    JOIN baskets
                      ON baskets.id = basket_items.basket_id
                    WHERE baskets.job_id = jobs.id
                      AND basket_items.selected = 1
                      AND basket_items.part_status = 'RECEIVED'
                ) AS received_items,

                (
                    SELECT customer_requests.id
                    FROM customer_requests
                    WHERE customer_requests.job_id = jobs.id
                    ORDER BY customer_requests.id DESC
                    LIMIT 1
                ) AS customer_request_id,

                (
                    SELECT COALESCE(
                        NULLIF(TRIM(customer_requests.requested_parts), ''),
                        NULLIF(TRIM(customer_requests.request_text), '')
                    )
                    FROM customer_requests
                    WHERE customer_requests.job_id = jobs.id
                    ORDER BY customer_requests.id DESC
                    LIMIT 1
                ) AS request_description,

                (
                    SELECT GROUP_CONCAT(
                        basket_items.requested_description,
                        ', '
                    )
                    FROM basket_items
                    JOIN baskets
                      ON baskets.id = basket_items.basket_id
                    WHERE baskets.job_id = jobs.id
                      AND basket_items.selected = 1
                ) AS selected_part_descriptions,

                (
                    SELECT quotes.id
                    FROM quotes
                    WHERE quotes.job_id = jobs.id
                      AND COALESCE(quotes.is_archived, 0) = 0
                    ORDER BY quotes.id DESC
                    LIMIT 1
                ) AS quote_id,

                (
                    SELECT quotes.status
                    FROM quotes
                    WHERE quotes.job_id = jobs.id
                      AND COALESCE(quotes.is_archived, 0) = 0
                    ORDER BY quotes.id DESC
                    LIMIT 1
                ) AS quote_status,

                (
                    SELECT invoices.id
                    FROM invoices
                    WHERE invoices.job_id = jobs.id
                    ORDER BY invoices.id DESC
                    LIMIT 1
                ) AS invoice_id,

                (
                    SELECT invoices.status
                    FROM invoices
                    WHERE invoices.job_id = jobs.id
                    ORDER BY invoices.id DESC
                    LIMIT 1
                ) AS invoice_status,

                (
                    SELECT MAX(job_timeline.created_at)
                    FROM job_timeline
                    WHERE job_timeline.job_id = jobs.id
                ) AS last_activity,

                (
                    SELECT GROUP_CONCAT(
                        basket_items.requested_description,
                        '||'
                    )
                    FROM basket_items
                    JOIN baskets
                      ON baskets.id = basket_items.basket_id
                    WHERE baskets.job_id = jobs.id
                      AND basket_items.selected = 1
                      AND COALESCE(
                            basket_items.part_status,
                            'RESEARCH'
                          ) != 'RECEIVED'
                ) AS outstanding_part_descriptions

            FROM jobs
            ORDER BY jobs.id DESC
            """
        ).fetchall()

        jobs = []

        manufacturer_codes = {
            "CATERPILLAR": "CAT",
            "CAT": "CAT",
            "CUMMINS": "CUM",
            "DETROIT DIESEL": "DD",
            "DETROIT": "DD",
            "JOHN DEERE": "JD",
            "JCB": "JCB",
            "TOYOTA": "TOY",
            "HONDA": "HON",
            "BMW": "BMW",
            "AUDI": "AUD",
            "FREIGHTLINER": "FTL",
            "MACK": "MACK",
            "INTERNATIONAL": "INT",
            "ISUZU": "ISU",
            "YANMAR": "YAN",
        }

        paid_statuses = {
            "PAID",
            "PAYMENT RECEIVED",
            "PAYMENT_RECEIVED",
        }

        completed_statuses = {
            "COMPLETE",
            "COMPLETED",
            "DELIVERED",
            "CLOSED",
        }

        approved_statuses = {
            "APPROVED",
            "ACCEPTED",
            "CONFIRMED",
        }

        for row in rows:
            item = dict(row)

            outstanding_parts = [
                part.strip()
                for part in str(
                    item.get("outstanding_part_descriptions") or ""
                ).split("||")
                if part.strip()
            ]

            intelligence = JobEngine.evaluate(
                item,
                selected_items=item.get("selected_items", 0),
                research_items=item.get("research_items", 0),
                quoted_items=item.get("quoted_items", 0),
                ordered_items=item.get("ordered_items", 0),
                received_items=item.get("received_items", 0),
                outstanding_parts=outstanding_parts,
                basket_status=item.get("basket_status") or "OPEN",
            ).to_dict()

            item["intelligence"] = intelligence

            description = (
                item.get("request_description")
                or item.get("selected_part_descriptions")
                or "No job description entered"
            )

            item["job_description"] = " ".join(
                str(description).split()
            )

            manufacturer = (
                item.get("manufacturer") or ""
            ).strip()

            item["manufacturer_code"] = manufacturer_codes.get(
                manufacturer.upper(),
                manufacturer[:3].upper() or "PLG",
            )

            job_status = (
                item.get("status") or ""
            ).strip().upper()

            quote_status = (
                item.get("quote_status") or ""
            ).strip().upper()

            invoice_status = (
                item.get("invoice_status") or ""
            ).strip().upper()

            health_label = (
                intelligence.get("health_label") or ""
            ).strip().lower()

            selected_items = int(
                item.get("selected_items") or 0
            )

            research_items = int(
                item.get("research_items") or 0
            )

            received_items = int(
                item.get("received_items") or 0
            )

            if job_status in completed_statuses:
                stage_key = "COMPLETED"
                stage_label = "Completed"
                stage_icon = "✅"
                progress = 100
                priority_rank = 70

            elif invoice_status in paid_statuses:
                stage_key = "PAID"
                stage_label = "Paid"
                stage_icon = "💰"
                progress = 88
                priority_rank = 60

            elif quote_status in approved_statuses:
                stage_key = "APPROVED"
                stage_label = "Customer Approved"
                stage_icon = "👍"
                progress = 75
                priority_rank = 50

            elif item.get("quote_id"):
                stage_key = "WAITING"
                stage_label = "Waiting for Customer"
                stage_icon = "⏳"
                progress = 63
                priority_rank = 40

            elif selected_items > 0 and research_items == 0:
                stage_key = "READY"
                stage_label = "Ready to Quote"
                stage_icon = "📝"
                progress = 50
                priority_rank = 30

            elif selected_items > 0:
                stage_key = "RESEARCH"
                stage_label = "Parts Research"
                stage_icon = "🔍"
                progress = 42
                priority_rank = 20

            else:
                stage_key = "RESEARCH"
                stage_label = "Research Required"
                stage_icon = "🔍"
                progress = 30
                priority_rank = 20

            if (
                "attention" in health_label
                or "action required" in health_label
                or "overdue" in health_label
            ):
                stage_key = "URGENT"
                stage_label = "Needs Attention"
                stage_icon = "🔥"
                priority_rank = 10

            item["stage_key"] = stage_key
            item["stage_label"] = stage_label
            item["stage_icon"] = stage_icon
            item["progress_percent"] = progress
            item["priority_rank"] = priority_rank

            item["parts_progress"] = {
                "research": research_items,
                "quoted": int(item.get("quoted_items") or 0),
                "ordered": int(item.get("ordered_items") or 0),
                "received": received_items,
                "total": selected_items,
            }

            item["last_activity_display"] = (
                item.get("last_activity")
                or item.get("created_date")
                or "No activity recorded"
            )

            jobs.append(item)

        jobs.sort(
            key=lambda job: (
                job["priority_rank"],
                job.get("last_activity_display") or "",
                job.get("id") or 0,
            )
        )

    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "jobs": jobs,
            "active_page": "jobs",
        },
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

        connectors = connection.execute(
            """
            SELECT *
            FROM connector_profiles
            WHERE is_enabled = 1
            ORDER BY sort_order, display_name
            """
        ).fetchall()


        source_imports = connection.execute(
            """
            SELECT
                source_cart_imports.id,
                source_cart_imports.source_name,
                source_cart_imports.shipping_total,
                source_cart_imports.currency,
                source_cart_imports.imported_at,
                COALESCE(SUM(part_sources.supplier_cost * job_parts.quantity), 0) AS parts_total
            FROM source_cart_imports
            LEFT JOIN job_parts
                ON job_parts.job_id = source_cart_imports.job_id
            LEFT JOIN part_sources
                ON part_sources.part_id = job_parts.id
               AND LOWER(TRIM(part_sources.supplier_name)) = LOWER(TRIM(source_cart_imports.source_name))
            WHERE source_cart_imports.job_id = ?
            GROUP BY source_cart_imports.id
            ORDER BY source_cart_imports.id DESC
            """,
            (job_id,),
        ).fetchall()

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
            "connectors": connectors,
            "source_imports": source_imports,
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



@app.post("/parts/{part_id}/delete")
def delete_job_part(part_id: int):
    with closing(get_connection()) as connection:
        part = connection.execute(
            """
            SELECT id, job_id, requested_description
            FROM job_parts
            WHERE id = ?
            """,
            (part_id,),
        ).fetchone()

        if part is None:
            raise HTTPException(status_code=404, detail="Part not found.")

        # Clear any active verification connected to this part.
        connection.execute(
            "DELETE FROM active_verification WHERE part_id = ?",
            (part_id,),
        )

        # Newer PLG versions store supplier choices separately.
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        if "part_sources" in tables:
            connection.execute(
                "DELETE FROM part_sources WHERE part_id = ?",
                (part_id,),
            )

        if "supplier_options" in tables:
            connection.execute(
                "DELETE FROM supplier_options WHERE part_id = ?",
                (part_id,),
            )

        connection.execute(
            "DELETE FROM job_parts WHERE id = ?",
            (part_id,),
        )

        remaining = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN verification_status = 'VERIFIED' THEN 1
                        ELSE 0
                    END
                ) AS verified
            FROM job_parts
            WHERE job_id = ?
            """,
            (part["job_id"],),
        ).fetchone()

        total = remaining["total"] or 0
        verified = remaining["verified"] or 0

        if total == 0:
            new_status = "REQUESTED"
        elif verified == total:
            new_status = "VERIFIED"
        else:
            new_status = "RESEARCHING"

        connection.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            (new_status, part["job_id"]),
        )

        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{part['job_id']}",
        status_code=303,
    )



def next_quote_number(connection: sqlite3.Connection) -> str:
    prefix = "PPS-Q-"

    row = connection.execute(
        """
        SELECT quote_number
        FROM quotes
        WHERE quote_number LIKE ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (f"{prefix}%",),
    ).fetchone()

    if row is None:
        sequence = 1
    else:
        try:
            sequence = int(row["quote_number"].split("-")[-1]) + 1
        except (ValueError, IndexError):
            sequence = 1

    return f"{prefix}{sequence:04d}"


def calculate_customer_unit_price(cost: float) -> float:
    if cost <= 50:
        markup = 0.40
    elif cost <= 200:
        markup = 0.30
    elif cost <= 500:
        markup = 0.25
    else:
        markup = 0.20

    return float(math.ceil(cost * (1 + markup)))


@app.post("/jobs/{job_id}/generate-quote")
def generate_quote(job_id: int):
    # The basket commit is an internal step. The user should not
    # have to click Review Quote before generating the quote.
    from plg_core.basket.service import commit_basket

    commit_basket(job_id)

    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

        if job is None:
            raise HTTPException(
                status_code=404,
                detail="Job not found.",
            )

        selected = connection.execute(
            """
            SELECT
                job_parts.id AS part_id,
                job_parts.requested_description,
                job_parts.oem_description,
                job_parts.quantity,
                job_parts.customer_unit_price,
                part_sources.id AS source_id,
                part_sources.supplier_name,
                part_sources.source_type,
                part_sources.brand,
                part_sources.supplier_part_number,
                part_sources.supplier_cost
            FROM job_parts
            JOIN part_sources
              ON part_sources.part_id = job_parts.id
             AND part_sources.selected_for_quote = 1
            WHERE job_parts.job_id = ?
            ORDER BY job_parts.id
            """,
            (job_id,),
        ).fetchall()

        # Count only quote-ready parts that have supplier-source
        # records. Smart Intake request placeholders are excluded.
        total_parts = connection.execute(
            """
            SELECT COUNT(DISTINCT job_parts.id) AS count
            FROM job_parts
            JOIN part_sources
              ON part_sources.part_id = job_parts.id
            WHERE job_parts.job_id = ?
            """,
            (job_id,),
        ).fetchone()["count"]

        if total_parts == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Select at least one priced supplier part "
                    "before generating the quote."
                ),
            )

        if len(selected) != total_parts:
            missing = connection.execute(
                """
                SELECT DISTINCT
                    job_parts.requested_description
                FROM job_parts
                JOIN part_sources
                  ON part_sources.part_id = job_parts.id
                WHERE job_parts.job_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM part_sources selected_source
                      WHERE selected_source.part_id = job_parts.id
                        AND selected_source.selected_for_quote = 1
                  )
                ORDER BY job_parts.id
                """,
                (job_id,),
            ).fetchall()

            missing_names = [
                str(row["requested_description"] or "Part").strip()
                for row in missing
            ]

            detail = "Choose one supplier for every quoted part."

            if missing_names:
                detail += " Missing: " + ", ".join(missing_names)

            raise HTTPException(
                status_code=400,
                detail=detail,
            )

        quote_number = next_quote_number(connection)
        quote_date = date.today().isoformat()

        shipping_rows = connection.execute(
            """
            SELECT source_name, shipping_total
            FROM source_cart_imports
            WHERE id IN (
                SELECT MAX(id)
                FROM source_cart_imports
                WHERE job_id = ?
                GROUP BY LOWER(TRIM(source_name))
            )
            """,
            (job_id,),
        ).fetchall()

        selected_suppliers = {
            str(row["supplier_name"] or "").strip().lower()
            for row in selected
        }

        shipping_total = sum(
            float(row["shipping_total"] or 0)
            for row in shipping_rows
            if str(row["source_name"] or "").strip().lower()
            in selected_suppliers
        )

        parts_subtotal = 0.0
        supplier_parts_total = 0.0
        item_rows = []

        for row in selected:
            quantity = max(1, int(row["quantity"] or 1))
            supplier_unit_cost = float(
                row["supplier_cost"] or 0
            )

            stored_customer_unit_price = row["customer_unit_price"]
            customer_unit_price = (
                float(stored_customer_unit_price)
                if stored_customer_unit_price is not None
                else calculate_customer_unit_price(
                    supplier_unit_cost
                )
            )

            supplier_line_total = (
                supplier_unit_cost * quantity
            )
            customer_line_total = (
                customer_unit_price * quantity
            )
            line_profit = (
                customer_line_total - supplier_line_total
            )

            description = (
                str(
                    row["requested_description"] or ""
                ).strip()
                or str(
                    row["oem_description"] or ""
                ).strip()
                or "Part"
            )

            supplier_parts_total += supplier_line_total
            parts_subtotal += customer_line_total

            item_rows.append(
                (
                    row["part_id"],
                    row["source_id"],
                    quantity,
                    description,
                    row["supplier_name"],
                    row["source_type"],
                    row["brand"] or "",
                    row["supplier_part_number"] or "",
                    supplier_unit_cost,
                    customer_unit_price,
                    supplier_line_total,
                    customer_line_total,
                    line_profit,
                )
            )

        customer_total = parts_subtotal + shipping_total
        supplier_total = (
            supplier_parts_total + shipping_total
        )
        profit_total = customer_total - supplier_total

        cursor = connection.execute(
            """
            INSERT INTO quotes (
                quote_number,
                job_id,
                quote_date,
                status,
                parts_subtotal,
                shipping_total,
                customer_total,
                supplier_total,
                profit_total
            )
            VALUES (
                ?, ?, ?, 'DRAFT', ?, ?, ?, ?, ?
            )
            """,
            (
                quote_number,
                job_id,
                quote_date,
                parts_subtotal,
                shipping_total,
                customer_total,
                supplier_total,
                profit_total,
            ),
        )

        quote_id = cursor.lastrowid

        for item in item_rows:
            connection.execute(
                """
                INSERT INTO quote_items (
                    quote_id,
                    part_id,
                    source_id,
                    quantity,
                    description,
                    supplier_name,
                    source_type,
                    brand,
                    supplier_part_number,
                    supplier_unit_cost,
                    customer_unit_price,
                    supplier_line_total,
                    customer_line_total,
                    line_profit
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (quote_id, *item),
            )

        connection.execute(
            """
            UPDATE jobs
            SET status = 'QUOTED'
            WHERE id = ?
            """,
            (job_id,),
        )

        from plg_core.timeline import log_job_event

        log_job_event(
            connection,
            job_id=job_id,
            event_type="QUOTE_GENERATED",
            icon="📝",
            message=f"Quote {quote_number} generated",
        )

        connection.commit()

        quote, pdf_items = load_quote(
            connection,
            quote_id,
        )
        generate_quote_pdfs(quote, pdf_items)

    return RedirectResponse(
        url=f"/quotes/{quote_id}/documents",
        status_code=303,
    )


def load_quote(connection: sqlite3.Connection, quote_id: int):
    quote = connection.execute(
        """
        SELECT
            quotes.*,
            jobs.job_number,
            jobs.customer,
            jobs.company,
            jobs.phone,
            jobs.email,
            jobs.address,
            jobs.manufacturer,
            jobs.machine,
            jobs.pin_serial
        FROM quotes
        JOIN jobs ON jobs.id = quotes.job_id
        WHERE quotes.id = ?
        """,
        (quote_id,),
    ).fetchone()

    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found.")

    items = connection.execute(
        """
        SELECT *
        FROM quote_items
        WHERE quote_id = ?
        ORDER BY id
        """,
        (quote_id,),
    ).fetchall()

    return quote, items



def next_invoice_number(connection: sqlite3.Connection) -> str:
    prefix = "PPS-INV-"
    row = connection.execute("SELECT invoice_number FROM invoices WHERE invoice_number LIKE ? ORDER BY id DESC LIMIT 1", (f"{prefix}%",)).fetchone()
    if row is None:
        sequence = 1
    else:
        try:
            sequence = int(row["invoice_number"].split("-")[-1]) + 1
        except (ValueError, IndexError):
            sequence = 1
    return f"{prefix}{sequence:04d}"


def load_invoice(
    connection: sqlite3.Connection,
    invoice_id: int,
):
    invoice = connection.execute(
        """
        SELECT
            invoices.*,
            jobs.customer_id,
            jobs.job_number,
            jobs.customer,
            jobs.company,
            jobs.phone,
            jobs.email,
            jobs.address,
            jobs.manufacturer,
            jobs.machine,
            jobs.pin_serial,

            (
                SELECT customer_transactions.transaction_date
                FROM customer_transactions
                WHERE customer_transactions.invoice_id = invoices.id
                  AND customer_transactions.transaction_type = 'PAYMENT'
                ORDER BY customer_transactions.transaction_date DESC,
                         customer_transactions.id DESC
                LIMIT 1
            ) AS paid_date,

            (
                SELECT customer_transactions.payment_method
                FROM customer_transactions
                WHERE customer_transactions.invoice_id = invoices.id
                  AND customer_transactions.transaction_type = 'PAYMENT'
                ORDER BY customer_transactions.transaction_date DESC,
                         customer_transactions.id DESC
                LIMIT 1
            ) AS payment_method,

            (
                SELECT customer_transactions.reference
                FROM customer_transactions
                WHERE customer_transactions.invoice_id = invoices.id
                  AND customer_transactions.transaction_type = 'PAYMENT'
                ORDER BY customer_transactions.transaction_date DESC,
                         customer_transactions.id DESC
                LIMIT 1
            ) AS payment_reference

        FROM invoices
        JOIN jobs
          ON jobs.id = invoices.job_id
        WHERE invoices.id = ?
        """,
        (invoice_id,),
    ).fetchone()

    if invoice is None:
        raise HTTPException(
            status_code=404,
            detail="Invoice not found.",
        )

    items = connection.execute(
        """
        SELECT *
        FROM invoice_items
        WHERE invoice_id = ?
        ORDER BY id
        """,
        (invoice_id,),
    ).fetchall()

    return invoice, items


def get_or_create_custom_invoice(
    connection: sqlite3.Connection,
    invoice_id: int,
):
    invoice, items = load_invoice(connection, invoice_id)

    if str(invoice["status"] or "").upper() != "PAID":
        raise HTTPException(
            status_code=400,
            detail="Custom Invoice is only available for paid invoices.",
        )

    custom_invoice = connection.execute(
        """
        SELECT *
        FROM custom_invoices
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    ).fetchone()

    if custom_invoice is None:
        custom_number = f"{invoice['invoice_number']}C"

        cursor = connection.execute(
            """
            INSERT INTO custom_invoices (
                invoice_id,
                custom_invoice_number,
                adjustment_mode,
                custom_total
            )
            VALUES (?, ?, 'MANUAL', ?)
            """,
            (
                invoice_id,
                custom_number,
                float(invoice["customer_total"] or 0),
            ),
        )
        custom_invoice_id = cursor.lastrowid

        for item in items:
            quantity = int(item["quantity"] or 1)
            unit_price = float(item["customer_unit_price"] or 0)
            line_total = round(quantity * unit_price, 2)

            connection.execute(
                """
                INSERT INTO custom_invoice_items (
                    custom_invoice_id,
                    invoice_item_id,
                    quantity,
                    custom_unit_price,
                    custom_line_total
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    custom_invoice_id,
                    item["id"],
                    quantity,
                    unit_price,
                    line_total,
                ),
            )

        connection.commit()

        custom_invoice = connection.execute(
            """
            SELECT *
            FROM custom_invoices
            WHERE id = ?
            """,
            (custom_invoice_id,),
        ).fetchone()

    custom_items = connection.execute(
        """
        SELECT
            custom_invoice_items.*,
            invoice_items.description,
            invoice_items.brand,
            invoice_items.supplier_part_number
        FROM custom_invoice_items
        JOIN invoice_items
          ON invoice_items.id = custom_invoice_items.invoice_item_id
        WHERE custom_invoice_items.custom_invoice_id = ?
        ORDER BY custom_invoice_items.id
        """,
        (custom_invoice["id"],),
    ).fetchall()

    return invoice, custom_invoice, custom_items



@app.get(
    "/invoices/{invoice_id}/custom",
    response_class=HTMLResponse,
)
def custom_invoice_editor(
    request: Request,
    invoice_id: int,
):
    with closing(get_connection()) as connection:
        invoice, custom_invoice, custom_items = (
            get_or_create_custom_invoice(
                connection,
                invoice_id,
            )
        )

    return templates.TemplateResponse(
        request=request,
        name="custom_invoice.html",
        context={
            "invoice": invoice,
            "custom_invoice": custom_invoice,
            "items": custom_items,
            "active_page": "invoices",
        },
    )


@app.post("/invoices/{invoice_id}/custom/save")
async def save_custom_invoice(
    request: Request,
    invoice_id: int,
):
    form = await request.form()

    with closing(get_connection()) as connection:
        invoice, custom_invoice, custom_items = (
            get_or_create_custom_invoice(
                connection,
                invoice_id,
            )
        )

        mode = str(
            form.get("adjustment_mode") or "MANUAL"
        ).upper()

        try:
            adjustment_value = float(
                form.get("adjustment_value") or 0
            )
        except (TypeError, ValueError):
            adjustment_value = 0.0

        original_items = connection.execute(
            """
            SELECT *
            FROM invoice_items
            WHERE invoice_id = ?
            ORDER BY id
            """,
            (invoice_id,),
        ).fetchall()

        original_by_id = {
            row["id"]: row
            for row in original_items
        }

        updates = []

        if mode == "PERCENTAGE":
            factor = max(0.0, 1.0 - adjustment_value / 100.0)

            for item in custom_items:
                original = original_by_id[item["invoice_item_id"]]
                qty = int(item["quantity"] or 1)
                unit_price = round(
                    float(original["customer_unit_price"] or 0) * factor,
                    2,
                )
                line_total = round(qty * unit_price, 2)

                updates.append(
                    (item["id"], unit_price, line_total)
                )

        elif mode == "TARGET_TOTAL":
            target_total = max(0.0, round(adjustment_value, 2))

            base_total = sum(
                float(row["customer_line_total"] or 0)
                for row in original_items
            )

            running_total = 0.0

            for index, item in enumerate(custom_items):
                original = original_by_id[item["invoice_item_id"]]
                qty = int(item["quantity"] or 1)

                if index == len(custom_items) - 1:
                    line_total = round(
                        target_total - running_total,
                        2,
                    )
                elif base_total > 0:
                    share = (
                        float(original["customer_line_total"] or 0)
                        / base_total
                    )
                    line_total = round(target_total * share, 2)
                    running_total += line_total
                else:
                    line_total = 0.0

                unit_price = (
                    round(line_total / qty, 4)
                    if qty
                    else 0.0
                )

                updates.append(
                    (item["id"], unit_price, line_total)
                )

        else:
            mode = "MANUAL"
            adjustment_value = None

            for item in custom_items:
                qty = int(item["quantity"] or 1)

                try:
                    unit_price = float(
                        form.get(f"unit_price_{item['id']}") or 0
                    )
                except (TypeError, ValueError):
                    unit_price = 0.0

                unit_price = max(0.0, round(unit_price, 2))
                line_total = round(qty * unit_price, 2)

                updates.append(
                    (item["id"], unit_price, line_total)
                )

        for item_id, unit_price, line_total in updates:
            connection.execute(
                """
                UPDATE custom_invoice_items
                SET custom_unit_price = ?,
                    custom_line_total = ?
                WHERE id = ?
                """,
                (
                    unit_price,
                    line_total,
                    item_id,
                ),
            )

        custom_total = round(
            sum(row[2] for row in updates),
            2,
        )

        connection.execute(
            """
            UPDATE custom_invoices
            SET adjustment_mode = ?,
                adjustment_value = ?,
                custom_total = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                mode,
                adjustment_value,
                custom_total,
                custom_invoice["id"],
            ),
        )

        connection.commit()

        invoice, custom_invoice, custom_items = (
            get_or_create_custom_invoice(
                connection,
                invoice_id,
            )
        )

        from plg_core.documents.invoice_pdf import (
            generate_custom_invoice_pdf,
        )

        generate_custom_invoice_pdf(
            invoice,
            custom_invoice,
            custom_items,
        )

    return RedirectResponse(
        url="/invoices",
        status_code=303,
    )


@app.get("/invoices/{invoice_id}/custom/pdf")
def custom_invoice_pdf(
    invoice_id: int,
    download: int = 0,
):
    from plg_core.documents.invoice_pdf import (
        custom_invoice_path,
        generate_custom_invoice_pdf,
    )

    with closing(get_connection()) as connection:
        invoice, custom_invoice, custom_items = (
            get_or_create_custom_invoice(
                connection,
                invoice_id,
            )
        )

        path = custom_invoice_path(
            invoice,
            custom_invoice,
        )

        if not path.exists():
            generate_custom_invoice_pdf(
                invoice,
                custom_invoice,
                custom_items,
            )

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name if download else None,
    )

@app.get("/invoices", response_class=HTMLResponse)
def list_invoices(request: Request, view: str = "all"):
    if view not in {"active", "paid", "void", "all"}:
        view = "all"

    where = {
        "active": "WHERE invoices.status IN ('UNPAID', 'PARTIAL')",
        "paid": "WHERE invoices.status = 'PAID'",
        "void": "WHERE invoices.status = 'VOID'",
    }.get(view, "")

    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT
                invoices.*,
                jobs.customer_id,
                jobs.customer,
                jobs.job_number,
                jobs.manufacturer,
                jobs.machine,
                jobs.pin_serial,

                (
                    SELECT COUNT(*)
                    FROM invoice_items
                    WHERE invoice_items.invoice_id = invoices.id
                ) AS item_count,

                (
                    SELECT GROUP_CONCAT(
                        invoice_items.description,
                        ', '
                    )
                    FROM invoice_items
                    WHERE invoice_items.invoice_id = invoices.id
                ) AS item_descriptions,

                EXISTS (
                    SELECT 1
                    FROM custom_invoices
                    WHERE custom_invoices.invoice_id = invoices.id
                ) AS custom_invoice_exists

            FROM invoices
            JOIN jobs
              ON jobs.id = invoices.job_id
            {where}
            ORDER BY invoices.id DESC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="invoices.html",
        context={
            "invoices": rows,
            "view": view,
            "active_page": "invoices",
        },
    )


@app.post("/quotes/{quote_id}/convert-to-invoice")
def convert_quote_to_invoice(quote_id: int):
    with closing(get_connection()) as connection:
        quote, quote_items = load_quote(connection, quote_id)
        existing = connection.execute("SELECT id FROM invoices WHERE quote_id=?",(quote_id,)).fetchone()
        if existing is not None:
            return RedirectResponse(url=f"/invoices/{existing['id']}/documents",status_code=303)
        job = connection.execute("SELECT * FROM jobs WHERE id=?",(quote["job_id"],)).fetchone()
        invoice_number = next_invoice_number(connection)
        invoice_date = date.today().isoformat()
        customer_total = float(quote["customer_total"] or 0)
        available_credit = 0.0
        if job["customer_id"]:
            balance = connection.execute("SELECT COALESCE(SUM(amount),0) AS net_balance FROM customer_transactions WHERE customer_id=?",(job["customer_id"],)).fetchone()
            available_credit = max(float(balance["net_balance"] or 0),0.0)
        credit_applied = min(available_credit,customer_total)
        balance_due = max(customer_total-credit_applied,0.0)
        status = "PAID" if balance_due == 0 else ("PARTIAL" if credit_applied > 0 else "UNPAID")
        cur = connection.execute("""INSERT INTO invoices (invoice_number,quote_id,job_id,invoice_date,status,parts_subtotal,shipping_total,customer_total,supplier_total,profit_total,credit_applied,balance_due) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",(invoice_number,quote_id,quote["job_id"],invoice_date,status,float(quote["parts_subtotal"] or 0),float(quote["shipping_total"] or 0),customer_total,float(quote["supplier_total"] or 0),float(quote["profit_total"] or 0),credit_applied,balance_due))
        invoice_id = cur.lastrowid
        for item in quote_items:
            connection.execute("""INSERT INTO invoice_items (invoice_id,quote_item_id,part_id,source_id,quantity,description,supplier_name,source_type,brand,supplier_part_number,supplier_unit_cost,customer_unit_price,supplier_line_total,customer_line_total,line_profit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(invoice_id,item["id"],item["part_id"],item["source_id"],item["quantity"],item["description"],item["supplier_name"],item["source_type"],item["brand"],item["supplier_part_number"],item["supplier_unit_cost"],item["customer_unit_price"],item["supplier_line_total"],item["customer_line_total"],item["line_profit"]))
        if job["customer_id"]:
            connection.execute("""INSERT INTO customer_transactions (customer_id,transaction_date,transaction_type,amount,reference,reason,job_id,quote_id,invoice_id) VALUES (?,?,'INVOICE',?,?,?,?,?,?)""",(job["customer_id"],invoice_date,-customer_total,invoice_number,f"Invoice created from {quote['quote_number']}",quote["job_id"],quote_id,invoice_id))
        connection.execute("UPDATE quotes SET is_archived=1,status='CONVERTED' WHERE id=?",(quote_id,))
        connection.execute("UPDATE jobs SET status='CONFIRMED' WHERE id=?",(quote["job_id"],))
        connection.commit()
        invoice, items = load_invoice(connection, invoice_id)
        generate_invoice_pdfs(invoice, items)
    return RedirectResponse(url=f"/invoices/{invoice_id}/documents",status_code=303)


@app.get(
    "/invoices/{invoice_id}/documents",
    response_class=HTMLResponse,
)
def invoice_documents(request: Request, invoice_id: int):
    with closing(get_connection()) as connection:
        invoice, items = load_invoice(connection, invoice_id)

        payments = connection.execute(
            """
            SELECT *
            FROM customer_transactions
            WHERE invoice_id = ?
              AND transaction_type = 'PAYMENT'
            ORDER BY transaction_date DESC, id DESC
            """,
            (invoice_id,),
        ).fetchall()

        payment_total = sum(
            float(payment["amount"] or 0)
            for payment in payments
        )

        custom_invoice_exists = connection.execute(
            """
            SELECT 1
            FROM custom_invoices
            WHERE invoice_id = ?
            """,
            (invoice_id,),
        ).fetchone() is not None

    paths = invoice_paths(
        invoice["customer"],
        invoice["invoice_number"],
    )

    from plg_core.documents.invoice_pdf import (
        paid_invoice_paths,
    )

    paid_paths = paid_invoice_paths(
        invoice["customer"],
        invoice["invoice_number"],
    )

    if (
        not paths["customer"].exists()
        or not paths["internal"].exists()
    ):
        generate_invoice_pdfs(invoice, items)

    return templates.TemplateResponse(
        request=request,
        name="invoice_documents.html",
        context={
            "invoice": invoice,
            "items": items,
            "payments": payments,
            "payment_total": payment_total,
            "paid_customer_invoice_exists": (
                paid_paths["customer"].exists()
            ),
            "paid_internal_invoice_exists": (
                paid_paths["internal"].exists()
            ),
            "today": date.today().isoformat(),
            "parts_order_sheet_exists": parts_order_sheet_path(
                invoice
            ).exists(),
            "custom_invoice_exists": custom_invoice_exists,
            "active_page": "invoices",
        },
    )


@app.post("/invoices/{invoice_id}/payments")
def receive_invoice_payment(
    invoice_id: int,
    amount: Annotated[float, Form()],
    payment_method: Annotated[str, Form()],
    reference: Annotated[str, Form()] = "",
    payment_date: Annotated[str, Form()] = "",
):
    amount = round(float(amount or 0), 2)
    payment_method = (payment_method or "").strip().upper()
    reference = (reference or "").strip()
    payment_date = (
        (payment_date or "").strip()
        or date.today().isoformat()
    )

    if amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="Payment amount must be greater than zero.",
        )

    allowed_methods = {
        "CASH",
        "BANK TRANSFER",
        "ZELLE",
        "CARD",
        "CHEQUE",
        "OTHER",
    }

    if payment_method not in allowed_methods:
        raise HTTPException(
            status_code=400,
            detail="Select a valid payment method.",
        )

    with closing(get_connection()) as connection:
        invoice, items = load_invoice(connection, invoice_id)

        if invoice["status"] == "VOID":
            raise HTTPException(
                status_code=400,
                detail="A void invoice cannot receive payment.",
            )

        current_balance = round(
            float(invoice["balance_due"] or 0),
            2,
        )

        if current_balance <= 0:
            return RedirectResponse(
                url=f"/invoices/{invoice_id}/documents",
                status_code=303,
            )

        if amount > current_balance:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Payment cannot be greater than the "
                    f"current balance of ${current_balance:.2f}."
                ),
            )

        new_balance = round(current_balance - amount, 2)

        new_status = (
            "PAID"
            if new_balance <= 0
            else "PARTIAL"
        )

        connection.execute(
            """
            INSERT INTO customer_transactions (
                customer_id,
                transaction_date,
                transaction_type,
                amount,
                payment_method,
                reference,
                reason,
                job_id,
                quote_id,
                invoice_id
            )
            VALUES (
                ?, ?, 'PAYMENT', ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                invoice["customer_id"],
                payment_date,
                amount,
                payment_method,
                reference,
                f"Payment received for {invoice['invoice_number']}",
                invoice["job_id"],
                invoice["quote_id"],
                invoice_id,
            ),
        )

        connection.execute(
            """
            UPDATE invoices
            SET balance_due = ?,
                status = ?
            WHERE id = ?
            """,
            (
                max(new_balance, 0),
                new_status,
                invoice_id,
            ),
        )

        if new_status == "PAID":
            connection.execute(
                """
                UPDATE jobs
                SET status = 'CONFIRMED'
                WHERE id = ?
                """,
                (invoice["job_id"],),
            )

        try:
            connection.execute(
                """
                INSERT INTO job_timeline (
                    job_id,
                    event_type,
                    icon,
                    message
                )
                VALUES (?, 'PAYMENT_RECEIVED', '💳', ?)
                """,
                (
                    invoice["job_id"],
                    (
                        f"${amount:.2f} payment received "
                        f"for {invoice['invoice_number']} "
                        f"via {payment_method.title()}"
                    ),
                ),
            )
        except Exception:
            # Payment must still succeed if timeline support
            # is unavailable in an older database.
            pass

        connection.commit()

        updated_invoice, updated_items = load_invoice(
            connection,
            invoice_id,
        )

        if new_status == "PAID":
            from plg_core.documents.invoice_pdf import (
                generate_paid_invoice_pdfs,
            )

            generate_paid_invoice_pdfs(
                updated_invoice,
                updated_items,
            )

    return RedirectResponse(
        url=f"/invoices/{invoice_id}/documents",
        status_code=303,
    )


@app.get("/invoices/{invoice_id}/customer/paid-pdf")
def paid_customer_invoice_pdf(
    invoice_id: int,
    download: int = 0,
):
    from plg_core.documents.invoice_pdf import (
        generate_paid_invoice_pdfs,
        paid_invoice_paths,
    )

    with closing(get_connection()) as connection:
        invoice, items = load_invoice(connection, invoice_id)

    if str(invoice["status"] or "").upper() != "PAID":
        raise HTTPException(
            status_code=400,
            detail="The invoice has not been paid.",
        )

    path = paid_invoice_paths(
        invoice["customer"],
        invoice["invoice_number"],
    )["customer"]

    if not path.exists():
        generate_paid_invoice_pdfs(invoice, items)

    return FileResponse(
        path=path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type=(
            "attachment" if download else "inline"
        ),
    )


@app.get("/invoices/{invoice_id}/internal/paid-pdf")
def paid_internal_invoice_pdf(
    invoice_id: int,
    download: int = 0,
):
    from plg_core.documents.invoice_pdf import (
        generate_paid_invoice_pdfs,
        paid_invoice_paths,
    )

    with closing(get_connection()) as connection:
        invoice, items = load_invoice(connection, invoice_id)

    if str(invoice["status"] or "").upper() != "PAID":
        raise HTTPException(
            status_code=400,
            detail="The invoice has not been paid.",
        )

    path = paid_invoice_paths(
        invoice["customer"],
        invoice["invoice_number"],
    )["internal"]

    if not path.exists():
        generate_paid_invoice_pdfs(invoice, items)

    return FileResponse(
        path=path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type=(
            "attachment" if download else "inline"
        ),
    )


@app.post("/invoices/{invoice_id}/parts-order-sheet")
def create_parts_order_sheet(invoice_id: int):
    with closing(get_connection()) as connection:
        invoice, items = load_invoice(connection, invoice_id)

        if str(invoice["status"] or "").upper() != "PAID":
            raise HTTPException(
                status_code=400,
                detail=(
                    "The invoice must be paid before creating "
                    "a Parts Order Sheet."
                ),
            )

        supplier_rows = connection.execute(
            """
            SELECT
                name,
                contact_person,
                phone,
                email,
                website,
                account_number
            FROM suppliers
            """
        ).fetchall()

        supplier_details = {
            str(row["name"] or "").strip().lower(): dict(row)
            for row in supplier_rows
        }

    generated_path = generate_parts_order_sheet(
        invoice,
        items,
        supplier_details,
    )

    return FileResponse(
        path=generated_path,
        media_type="application/pdf",
        filename=Path(generated_path).name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": (
                "no-store, no-cache, must-revalidate, max-age=0"
            ),
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/invoices/{invoice_id}/parts-order-sheet/pdf")
def open_parts_order_sheet(invoice_id: int, download: int = 0):
    with closing(get_connection()) as connection:
        invoice, items = load_invoice(connection, invoice_id)

        supplier_rows = connection.execute(
            """
            SELECT
                name,
                contact_person,
                phone,
                email,
                website,
                account_number
            FROM suppliers
            """
        ).fetchall()

        supplier_details = {
            str(row["name"] or "").strip().lower(): dict(row)
            for row in supplier_rows
        }

    path = parts_order_sheet_path(invoice)

    if not path.exists():
        if str(invoice["status"] or "").upper() != "PAID":
            raise HTTPException(
                status_code=400,
                detail=(
                    "The invoice must be paid before creating "
                    "a Parts Order Sheet."
                ),
            )

        generate_parts_order_sheet(
            invoice,
            items,
            supplier_details,
        )

    return FileResponse(
        path=path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type=(
            "attachment" if download else "inline"
        ),
        headers={
            "Cache-Control": (
                "no-store, no-cache, must-revalidate, max-age=0"
            ),
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/invoices/{invoice_id}/customer/pdf")
def customer_invoice_pdf(invoice_id: int, download: int = 0):
    with closing(get_connection()) as connection:
        invoice, items = load_invoice(connection, invoice_id)
    path = invoice_paths(invoice["customer"],invoice["invoice_number"])["customer"]
    if not path.exists():
        generate_invoice_pdfs(invoice,items)
    return FileResponse(
        path=path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type=(
            "attachment" if download else "inline"
        ),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/invoices/{invoice_id}/internal/pdf")
def internal_invoice_pdf(invoice_id: int, download: int = 0):
    with closing(get_connection()) as connection:
        invoice, items = load_invoice(connection, invoice_id)
    path = invoice_paths(invoice["customer"],invoice["invoice_number"])["internal"]
    if not path.exists():
        generate_invoice_pdfs(invoice,items)
    return FileResponse(
        path=path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type=(
            "attachment" if download else "inline"
        ),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/suppliers", response_class=HTMLResponse)
def list_suppliers(request: Request, view: str = "active"):
    if view not in {"active","inactive","do_not_use","all"}: view="active"
    where={"active":"WHERE suppliers.status='ACTIVE'","inactive":"WHERE suppliers.status='INACTIVE'","do_not_use":"WHERE suppliers.status='DO_NOT_USE'"}.get(view,"")
    with closing(get_connection()) as connection:
        rows=connection.execute(f"""SELECT suppliers.*, EXISTS(SELECT 1 FROM part_sources WHERE LOWER(TRIM(part_sources.supplier_name))=LOWER(TRIM(suppliers.name))) AS has_history FROM suppliers {where} ORDER BY suppliers.preferred DESC,suppliers.name COLLATE NOCASE""").fetchall()
        suppliers=[]
        for row in rows:
            item=dict(row); item["rating"]=max(1,min(5,int(item.get("rating") or 3))); item["can_delete"]=not bool(item["has_history"]); suppliers.append(item)
    return templates.TemplateResponse(request=request,name="suppliers.html",context={"suppliers":suppliers,"view":view,"active_page":"suppliers"})

@app.get("/suppliers/new", response_class=HTMLResponse)
def new_supplier_form(request: Request):
    return templates.TemplateResponse(request=request,name="supplier_form.html",context={"title":"New Supplier","subtitle":"Add a supplier for sourcing.","form_action":"/suppliers/new","submit_label":"Save Supplier","supplier":{"name":"","website":"","phone":"","email":"","contact_person":"","account_number":"","rating":3,"status":"ACTIVE","preferred":0},"active_page":"suppliers"})

@app.post("/suppliers/new")
def create_supplier(name: Annotated[str, Form()], website: Annotated[str, Form()] = "", phone: Annotated[str, Form()] = "", email: Annotated[str, Form()] = "", contact_person: Annotated[str, Form()] = "", account_number: Annotated[str, Form()] = "", rating: Annotated[int, Form()] = 3, status: Annotated[str, Form()] = "ACTIVE", preferred: Annotated[int, Form()] = 0):
    with closing(get_connection()) as connection:
        cur=connection.execute("INSERT INTO suppliers (name,website,phone,email,contact_person,account_number,rating,status,preferred) VALUES (?,?,?,?,?,?,?,?,?)",(name.strip(),website.strip(),phone.strip(),email.strip(),contact_person.strip(),account_number.strip(),max(1,min(5,int(rating))),status,1 if preferred else 0)); connection.commit()
    return RedirectResponse(url=f"/suppliers/{cur.lastrowid}/edit",status_code=303)

@app.get("/suppliers/{supplier_id}/edit", response_class=HTMLResponse)
def edit_supplier_form(request: Request, supplier_id: int):
    with closing(get_connection()) as connection: s=connection.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
    if s is None: raise HTTPException(status_code=404,detail="Supplier not found.")
    return templates.TemplateResponse(request=request,name="supplier_form.html",context={"title":"Edit Supplier","subtitle":s["name"],"form_action":f"/suppliers/{supplier_id}/edit","submit_label":"Save Changes","supplier":s,"active_page":"suppliers"})

@app.post("/suppliers/{supplier_id}/edit")
def update_supplier(supplier_id: int, name: Annotated[str, Form()], website: Annotated[str, Form()] = "", phone: Annotated[str, Form()] = "", email: Annotated[str, Form()] = "", contact_person: Annotated[str, Form()] = "", account_number: Annotated[str, Form()] = "", rating: Annotated[int, Form()] = 3, status: Annotated[str, Form()] = "ACTIVE", preferred: Annotated[int, Form()] = 0):
    with closing(get_connection()) as connection:
        old=connection.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
        connection.execute("UPDATE suppliers SET name=?,website=?,phone=?,email=?,contact_person=?,account_number=?,rating=?,status=?,preferred=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(name.strip(),website.strip(),phone.strip(),email.strip(),contact_person.strip(),account_number.strip(),max(1,min(5,int(rating))),status,1 if preferred else 0,supplier_id))
        if old and old["name"].lower()!=name.strip().lower(): connection.execute("UPDATE part_sources SET supplier_name=? WHERE LOWER(TRIM(supplier_name))=LOWER(TRIM(?))",(name.strip(),old["name"]))
        connection.commit()
    return RedirectResponse(url="/suppliers",status_code=303)

@app.post("/suppliers/{supplier_id}/status")
def supplier_status(supplier_id: int, status: Annotated[str, Form()]):
    with closing(get_connection()) as connection: connection.execute("UPDATE suppliers SET status=? WHERE id=?",(status,supplier_id)); connection.commit()
    return RedirectResponse(url="/suppliers",status_code=303)

@app.post("/suppliers/{supplier_id}/delete")
def supplier_delete(supplier_id: int):
    with closing(get_connection()) as connection:
        s=connection.execute("SELECT * FROM suppliers WHERE id=?",(supplier_id,)).fetchone()
        used=connection.execute("SELECT 1 FROM part_sources WHERE LOWER(TRIM(supplier_name))=LOWER(TRIM(?)) LIMIT 1",(s["name"],)).fetchone()
        if used: raise HTTPException(status_code=400,detail="Supplier has history. Mark it Inactive or Do Not Use.")
        connection.execute("DELETE FROM suppliers WHERE id=?",(supplier_id,)); connection.commit()
    return RedirectResponse(url="/suppliers",status_code=303)


@app.get("/quotes", response_class=HTMLResponse)
def list_quotes(request: Request, view: str = "active"):
    if view not in {"active", "archived", "all"}:
        view = "active"

    where = {
        "active": "WHERE COALESCE(quotes.is_archived, 0) = 0",
        "archived": "WHERE COALESCE(quotes.is_archived, 0) = 1",
    }.get(view, "")

    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT
                quotes.*,
                jobs.customer_id,
                jobs.customer,
                jobs.job_number,
                jobs.manufacturer,
                jobs.machine,
                jobs.pin_serial,

                (
                    SELECT COUNT(*)
                    FROM quote_items
                    WHERE quote_items.quote_id = quotes.id
                ) AS item_count,

                (
                    SELECT GROUP_CONCAT(
                        quote_items.description,
                        ', '
                    )
                    FROM quote_items
                    WHERE quote_items.quote_id = quotes.id
                ) AS item_descriptions,

                (
                    SELECT invoices.id
                    FROM invoices
                    WHERE invoices.quote_id = quotes.id
                    ORDER BY invoices.id DESC
                    LIMIT 1
                ) AS invoice_id,

                (
                    SELECT invoices.invoice_number
                    FROM invoices
                    WHERE invoices.quote_id = quotes.id
                    ORDER BY invoices.id DESC
                    LIMIT 1
                ) AS invoice_number,

                (
                    SELECT invoices.status
                    FROM invoices
                    WHERE invoices.quote_id = quotes.id
                    ORDER BY invoices.id DESC
                    LIMIT 1
                ) AS invoice_status

            FROM quotes
            JOIN jobs
              ON jobs.id = quotes.job_id
            {where}
            ORDER BY quotes.id DESC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="quotes.html",
        context={
            "quotes": rows,
            "view": view,
            "active_page": "quotes",
        },
    )


@app.post("/quotes/{quote_id}/archive")
def archive_quote(quote_id: int):
    with closing(get_connection()) as connection: connection.execute("UPDATE quotes SET is_archived=1 WHERE id=?",(quote_id,)); connection.commit()
    return RedirectResponse(url="/quotes",status_code=303)

@app.post("/quotes/{quote_id}/restore")
def restore_quote(quote_id: int):
    with closing(get_connection()) as connection: connection.execute("UPDATE quotes SET is_archived=0 WHERE id=?",(quote_id,)); connection.commit()
    return RedirectResponse(url="/quotes?view=archived",status_code=303)


@app.post("/quotes/{quote_id}/decision")
def update_quote_decision(
    quote_id: int,
    decision: str = Form(...),
):
    from plg_core.timeline import log_job_event

    valid_decisions = {
        "APPROVED",
        "REVISION_REQUIRED",
        "REJECTED",
    }

    normalized = decision.strip().upper()

    if normalized not in valid_decisions:
        raise HTTPException(
            status_code=400,
            detail="Invalid quote decision.",
        )

    with closing(get_connection()) as connection:
        quote = connection.execute(
            """
            SELECT id, quote_number, job_id, status
            FROM quotes
            WHERE id = ?
            """,
            (quote_id,),
        ).fetchone()

        if quote is None:
            raise HTTPException(
                status_code=404,
                detail="Quote not found.",
            )

        connection.execute(
            """
            UPDATE quotes
            SET status = ?
            WHERE id = ?
            """,
            (normalized, quote_id),
        )

        if normalized == "APPROVED":
            event_type = "QUOTE_APPROVED"
            icon = "✅"
            message = f"Quote {quote['quote_number']} approved"
            job_status = "CONFIRMED"

        elif normalized == "REVISION_REQUIRED":
            event_type = "QUOTE_REVISION_REQUIRED"
            icon = "↺"
            message = (
                f"Changes requested for quote "
                f"{quote['quote_number']}"
            )
            job_status = "QUOTED"

        else:
            event_type = "QUOTE_REJECTED"
            icon = "✕"
            message = f"Quote {quote['quote_number']} rejected"
            job_status = "QUOTED"

        connection.execute(
            """
            UPDATE jobs
            SET status = ?
            WHERE id = ?
            """,
            (job_status, quote["job_id"]),
        )

        log_job_event(
            connection,
            job_id=int(quote["job_id"]),
            event_type=event_type,
            icon=icon,
            message=message,
        )

        connection.commit()

    return RedirectResponse(
        url=f"/quotes/{quote_id}/documents",
        status_code=303,
    )


@app.get("/quotes/{quote_id}/documents", response_class=HTMLResponse)
def quote_documents(request: Request, quote_id: int):
    with closing(get_connection()) as connection:
        quote, items = load_quote(connection, quote_id)
        invoice = connection.execute("SELECT id,invoice_number FROM invoices WHERE quote_id=?",(quote_id,)).fetchone()
    paths = quote_paths(quote["customer"],quote["quote_number"])
    if not paths["customer"].exists() or not paths["internal"].exists():
        generate_quote_pdfs(quote,items)
    customer_path = Path("documents")/"Customers"/sanitize_path_name(quote["customer"])/"Quotes"
    return templates.TemplateResponse(request=request,name="quote_documents.html",context={"quote":quote,"items":items,"invoice":invoice,"customer_path":str(customer_path),"active_page":"quotes"})


@app.get("/quotes/{quote_id}/customer/pdf")
def customer_quote_pdf(quote_id: int, download: int = 0):
    with closing(get_connection()) as connection:
        quote, items = load_quote(connection, quote_id)
    path = quote_paths(quote["customer"], quote["quote_number"])["customer"]
    if not path.exists():
        generate_quote_pdfs(quote, items)
    return FileResponse(path=path, media_type="application/pdf", filename=path.name, content_disposition_type="attachment" if download else "inline")

@app.get("/quotes/{quote_id}/internal/pdf")
def internal_quote_pdf(quote_id: int, download: int = 0):
    with closing(get_connection()) as connection:
        quote, items = load_quote(connection, quote_id)
    path = quote_paths(quote["customer"], quote["quote_number"])["internal"]
    if not path.exists():
        generate_quote_pdfs(quote, items)
    return FileResponse(path=path, media_type="application/pdf", filename=path.name, content_disposition_type="attachment" if download else "inline")

@app.get("/quotes/{quote_id}/customer", response_class=HTMLResponse)
def customer_quote(request: Request, quote_id: int):
    with closing(get_connection()) as connection:
        quote, items = load_quote(connection, quote_id)

    return templates.TemplateResponse(
        request=request,
        name="quote_customer.html",
        context={
            "quote": quote,
            "items": items,
            "active_page": "quotes",
        },
    )


@app.get("/quotes/{quote_id}/internal", response_class=HTMLResponse)
def internal_quote(request: Request, quote_id: int):
    with closing(get_connection()) as connection:
        quote, items = load_quote(connection, quote_id)

    return templates.TemplateResponse(
        request=request,
        name="quote_internal.html",
        context={
            "quote": quote,
            "items": items,
            "active_page": "quotes",
        },
    )



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
    source_url: Annotated[str, Form()] = "",
    confidence: Annotated[float | None, Form()] = None,
    verification_status: Annotated[str, Form()] = "UNVERIFIED",
    verification_note: Annotated[str, Form()] = "",
):
    supplier_name = supplier_name.strip()
    if not supplier_name:
        raise HTTPException(status_code=400, detail="Supplier name is required.")

    source_type = source_type.strip().upper()
    if source_type not in {"OEM", "AFTERMARKET", "USED", "REMAN"}:
        raise HTTPException(status_code=400, detail="Invalid source type.")

    verification_status = verification_status.strip().upper() or "UNVERIFIED"
    if verification_status not in {
        "UNVERIFIED",
        "VERIFIED",
        "REJECTED",
        "OVERRIDE",
    }:
        raise HTTPException(
            status_code=400,
            detail="Invalid verification status.",
        )

    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise HTTPException(
            status_code=400,
            detail="Confidence must be between 0.0 and 1.0.",
        )

    verification_note = verification_note.strip()
    if verification_status == "OVERRIDE" and not verification_note:
        raise HTTPException(
            status_code=400,
            detail="Manual Override requires a verification note.",
        )

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
                lead_time, quote_reference, trust_level,
                verification_status, verification_note, source_url,
                confidence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'MANUAL', ?, ?, ?, ?)
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
                verification_status,
                verification_note,
                source_url.strip(),
                confidence,
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

        verification_status = (
            source["verification_status"] or "UNVERIFIED"
        ).strip().upper()

        verification_note = (
            source["verification_note"] or ""
        ).strip()

        if (
            verification_status not in {"VERIFIED", "OVERRIDE"}
            or (
                verification_status == "OVERRIDE"
                and not verification_note
            )
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Only verified candidates or documented manual "
                    "overrides can be selected for quote."
                ),
            )

        compatibility_status = (
            source["compatibility_status"] or "UNCHECKED"
        ).strip().upper()
        compatibility_note = (
            source["compatibility_note"] or ""
        ).strip()

        if compatibility_status == "INCOMPATIBLE":
            raise HTTPException(
                status_code=400,
                detail=(
                    "This candidate is marked incompatible with the linked "
                    "machine and cannot be selected for quote."
                ),
            )

        if compatibility_status == "OVERRIDE" and not compatibility_note:
            raise HTTPException(
                status_code=400,
                detail="Compatibility override requires a note.",
            )

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


@app.post("/parts/{part_id}/sources/{source_id}/verification")
def update_part_source_verification(
    part_id: int,
    source_id: int,
    verification_status: Annotated[str, Form()],
    verification_note: Annotated[str, Form()] = "",
):
    from plg_core.timeline import log_job_event

    valid_statuses = {
        "UNVERIFIED",
        "VERIFIED",
        "REJECTED",
        "OVERRIDE",
    }

    normalized_status = (
        verification_status.strip().upper() or "UNVERIFIED"
    )
    if normalized_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail="Invalid verification status.",
        )

    normalized_note = verification_note.strip()
    if normalized_status == "OVERRIDE" and not normalized_note:
        raise HTTPException(
            status_code=400,
            detail="Manual Override requires a verification note.",
        )

    with closing(get_connection()) as connection:
        source = connection.execute(
            """
            SELECT
                part_sources.*,
                job_parts.job_id,
                job_parts.requested_description
            FROM part_sources
            JOIN job_parts
              ON job_parts.id = part_sources.part_id
            WHERE part_sources.id = ?
              AND part_sources.part_id = ?
            """,
            (source_id, part_id),
        ).fetchone()

        if source is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier source not found.",
            )

        old_status = (
            source["verification_status"] or "UNVERIFIED"
        ).strip().upper()
        old_note = (
            source["verification_note"] or ""
        ).strip()

        connection.execute(
            """
            UPDATE part_sources
            SET verification_status = ?,
                verification_note = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                normalized_status,
                normalized_note,
                source_id,
            ),
        )

        removed_from_quote = False
        if (
            source["selected_for_quote"]
            and normalized_status not in {"VERIFIED", "OVERRIDE"}
        ):
            connection.execute(
                """
                UPDATE part_sources
                SET selected_for_quote = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (source_id,),
            )
            connection.execute(
                """
                UPDATE job_parts
                SET supplier = '',
                    supplier_cost = NULL,
                    availability = ''
                WHERE id = ?
                """,
                (part_id,),
            )
            removed_from_quote = True

        if (
            old_status != normalized_status
            or old_note != normalized_note
        ):
            labels = {
                "UNVERIFIED": "Unverified",
                "VERIFIED": "Verified",
                "REJECTED": "Rejected",
                "OVERRIDE": "Manual Override",
            }

            part_label = (
                source["requested_description"] or "Part"
            ).strip()
            supplier_label = (
                source["supplier_name"] or "Unknown supplier"
            ).strip()

            if old_status != normalized_status:
                message = (
                    f"{part_label} candidate from {supplier_label} "
                    f"verification changed from "
                    f"{labels.get(old_status, old_status.title())} "
                    f"to {labels[normalized_status]}"
                )
            else:
                message = (
                    f"{part_label} candidate from {supplier_label} "
                    f"verification note updated"
                )

            if normalized_note:
                message += f" — Note: {normalized_note}"

            if removed_from_quote:
                message += " — Removed from quote selection"

            log_job_event(
                connection,
                job_id=int(source["job_id"]),
                event_type="PART_SOURCE_VERIFICATION_CHANGED",
                icon="✓",
                message=message,
            )

        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{source['job_id']}#part-{part_id}",
        status_code=303,
    )



@app.post("/parts/{part_id}/sources/{source_id}/compatibility")
def update_part_source_compatibility(
    part_id: int,
    source_id: int,
    compatibility_status: Annotated[str, Form()],
    compatibility_note: Annotated[str, Form()] = "",
):
    from plg_core.timeline import log_job_event

    valid_statuses = {
        "UNCHECKED",
        "COMPATIBLE",
        "INCOMPATIBLE",
        "OVERRIDE",
    }

    normalized_status = (
        compatibility_status.strip().upper() or "UNCHECKED"
    )
    if normalized_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail="Invalid compatibility status.",
        )

    normalized_note = compatibility_note.strip()
    if normalized_status == "OVERRIDE" and not normalized_note:
        raise HTTPException(
            status_code=400,
            detail="Compatibility override requires a note.",
        )

    with closing(get_connection()) as connection:
        source = connection.execute(
            """
            SELECT
                part_sources.*,
                job_parts.job_id,
                job_parts.requested_description,
                jobs.pin_serial,
                machines.engine_serial
            FROM part_sources
            JOIN job_parts
              ON job_parts.id = part_sources.part_id
            JOIN jobs
              ON jobs.id = job_parts.job_id
            LEFT JOIN machines
              ON machines.id = jobs.machine_id
            WHERE part_sources.id = ?
              AND part_sources.part_id = ?
            """,
            (source_id, part_id),
        ).fetchone()

        if source is None:
            raise HTTPException(
                status_code=404,
                detail="Supplier source not found.",
            )

        old_status = (
            source["compatibility_status"] or "UNCHECKED"
        ).strip().upper()
        old_note = (
            source["compatibility_note"] or ""
        ).strip()

        connection.execute(
            """
            UPDATE part_sources
            SET compatibility_status = ?,
                compatibility_note = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                normalized_status,
                normalized_note,
                source_id,
            ),
        )

        removed_from_quote = False
        if (
            source["selected_for_quote"]
            and normalized_status == "INCOMPATIBLE"
        ):
            connection.execute(
                """
                UPDATE part_sources
                SET selected_for_quote = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (source_id,),
            )
            connection.execute(
                """
                UPDATE job_parts
                SET supplier = '',
                    supplier_cost = NULL,
                    availability = ''
                WHERE id = ?
                """,
                (part_id,),
            )
            removed_from_quote = True

        if (
            old_status != normalized_status
            or old_note != normalized_note
        ):
            labels = {
                "UNCHECKED": "Unchecked",
                "COMPATIBLE": "Compatible",
                "INCOMPATIBLE": "Incompatible",
                "OVERRIDE": "Manual Override",
            }

            part_label = (
                source["requested_description"] or "Part"
            ).strip()
            supplier_label = (
                source["supplier_name"] or "Unknown supplier"
            ).strip()

            if old_status != normalized_status:
                message = (
                    f"{part_label} candidate from {supplier_label} "
                    f"compatibility changed from "
                    f"{labels.get(old_status, old_status.title())} "
                    f"to {labels[normalized_status]}"
                )
            else:
                message = (
                    f"{part_label} candidate from {supplier_label} "
                    f"compatibility note updated"
                )

            identity = (source["pin_serial"] or "").strip()
            engine_serial = (source["engine_serial"] or "").strip()

            if identity:
                message += f" — VIN/PIN/Serial: {identity}"
            if engine_serial:
                message += f" — Engine Serial: {engine_serial}"
            if normalized_note:
                message += f" — Note: {normalized_note}"
            if removed_from_quote:
                message += " — Removed from quote selection"

            log_job_event(
                connection,
                job_id=int(source["job_id"]),
                event_type="PART_SOURCE_COMPATIBILITY_CHANGED",
                icon="🔎",
                message=message,
            )

        connection.commit()

    return RedirectResponse(
        url=f"/jobs/{source['job_id']}#part-{part_id}",
        status_code=303,
    )



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



@app.post("/api/import-source-cart")
async def api_import_source_cart(request: Request):
    payload = await request.json()

    job_id = payload.get("job_id")
    source_key = str(payload.get("source_key", "")).strip()
    source_name = str(payload.get("source_name", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    trust_level = str(payload.get("trust_level", "SUPPLIER_VERIFIED")).strip()
    items = payload.get("items") or []
    charges = payload.get("charges") or []

    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required.")
    if not source_name:
        raise HTTPException(status_code=400, detail="source_name is required.")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="At least one cart item is required.")

    shipping_total = None
    for charge in charges:
        if str(charge.get("charge_type", "")).strip().upper() == "SHIPPING":
            try:
                shipping_total = float(charge.get("amount"))
            except (TypeError, ValueError):
                shipping_total = None

    imported = []

    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT id FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        cursor = connection.execute(
            """
            INSERT INTO source_cart_imports (
                job_id, source_key, source_name, source_url,
                shipping_total, currency
            )
            VALUES (?, ?, ?, ?, ?, 'USD')
            """,
            (job_id, source_key, source_name, source_url, shipping_total),
        )
        import_id = cursor.lastrowid

        for raw in items:
            description = str(raw.get("description", "")).strip() or "Imported Part"
            supplier_part_number = str(raw.get("supplier_part_number", "")).strip()
            manufacturer_part_number = str(raw.get("manufacturer_part_number", "")).strip()
            brand = str(raw.get("brand", "")).strip()
            availability = str(raw.get("availability", "")).strip() or "In Stock"
            lead_time = str(raw.get("lead_time", "")).strip()
            fitment = str(raw.get("fitment", "")).strip()
            warehouse = str(raw.get("warehouse", "")).strip()
            delivery = str(raw.get("delivery", "")).strip()
            shipping_method = str(raw.get("shipping_method", "")).strip()

            try:
                quantity = max(1, int(raw.get("quantity", 1)))
            except (TypeError, ValueError):
                quantity = 1

            try:
                supplier_cost = float(raw.get("supplier_cost"))
            except (TypeError, ValueError):
                supplier_cost = None

            try:
                raw_confidence = raw.get("confidence")
                confidence = (
                    float(raw_confidence)
                    if raw_confidence not in (None, "")
                    else None
                )
            except (TypeError, ValueError):
                confidence = None

            if confidence is not None and not 0.0 <= confidence <= 1.0:
                confidence = None

            if not supplier_part_number:
                continue

            existing = connection.execute(
                """
                SELECT part_sources.id, part_sources.part_id
                FROM part_sources
                JOIN job_parts ON job_parts.id = part_sources.part_id
                WHERE job_parts.job_id = ?
                  AND LOWER(TRIM(part_sources.supplier_name)) = LOWER(TRIM(?))
                  AND TRIM(COALESCE(part_sources.supplier_part_number, '')) = ?
                LIMIT 1
                """,
                (job_id, source_name, supplier_part_number),
            ).fetchone()

            reference = " | ".join(
                value for value in [
                    fitment,
                    warehouse,
                    delivery,
                    shipping_method,
                ] if value
            )

            if existing:
                part_id = existing["part_id"]
                connection.execute(
                    """
                    UPDATE job_parts
                    SET quantity = ?
                    WHERE id = ?
                    """,
                    (quantity, part_id),
                )
                connection.execute(
                    """
                    UPDATE part_sources
                    SET brand = ?, supplier_cost = ?, availability = ?,
                        lead_time = ?, quote_reference = ?, trust_level = ?,
                        source_url = ?, confidence = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        brand, supplier_cost, availability, lead_time,
                        reference, trust_level, source_url, confidence,
                        existing["id"],
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO job_parts (
                        job_id, requested_description, quantity,
                        oem_part_number, oem_description,
                        verification_status, verification_source,
                        source_url, captured_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        job_id, description, quantity,
                        manufacturer_part_number,
                        description if manufacturer_part_number else "",
                        source_name, source_url,
                    ),
                )
                part_id = cursor.lastrowid

                connection.execute(
                    """
                    INSERT INTO part_sources (
                        part_id, supplier_name, source_type, brand,
                        supplier_part_number, supplier_cost, availability,
                        lead_time, quote_reference, trust_level, source_url,
                        confidence
                    )
                    VALUES (?, ?, 'AFTERMARKET', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        part_id, source_name, brand, supplier_part_number,
                        supplier_cost, availability, lead_time,
                        reference, trust_level, source_url, confidence,
                    ),
                )

            connection.execute(
                "INSERT OR IGNORE INTO suppliers (name) VALUES (?)",
                (source_name,),
            )
            imported.append({
                "part_id": part_id,
                "description": description,
                "supplier_part_number": supplier_part_number,
            })

        remaining = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM job_parts
            WHERE job_id = ?
              AND verification_status != 'VERIFIED'
            """,
            (job_id,),
        ).fetchone()["count"]

        connection.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            ("VERIFIED" if remaining == 0 else "RESEARCHING", job_id),
        )
        connection.commit()

    return {
        "ok": True,
        "job_id": job_id,
        "import_id": import_id,
        "imported_count": len(imported),
        "shipping_total": shipping_total,
        "items": imported,
    }



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
                        availability = ?, trust_level = 'OEM_VERIFIED',
                        verification_status = 'VERIFIED',
                        verification_note = 'Verified via CAT SIS',
                        source_url = ?, updated_at = CURRENT_TIMESTAMP
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
                        trust_level, verification_status,
                        verification_note, source_url
                    ) VALUES (
                        ?, 'CAT SIS', 'OEM', 'CAT', ?, ?, ?,
                        'OEM_VERIFIED', 'VERIFIED', 'Verified via CAT SIS', ?
                    )
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



SOURCE_PROFILES = {
    "cat_sis": {"name": "CAT SIS", "url": "https://sis2.cat.com/#/cart"},
    "worldpac": {"name": "Worldpac", "url": "https://www.worldpac.com/"},
    "ssf": {"name": "SSF", "url": "https://www.ssfautoparts.com/"},
    "rockauto": {"name": "RockAuto", "url": "https://www.rockauto.com/"},
    "upload": {"name": "Upload Quote / Image", "url": ""},
}


@app.post("/jobs/{job_id}/start-source-import")
def start_source_import(job_id: int, source_key: str = Form(...)):
    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT id FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        profile = connection.execute(
            """
            SELECT *
            FROM connector_profiles
            WHERE connector_key = ? AND is_enabled = 1
            """,
            (source_key,),
        ).fetchone()
        if profile is None:
            raise HTTPException(status_code=400, detail="Unknown or disabled supplier/source.")

        connection.execute(
            """
            INSERT INTO active_source_import
                (id, job_id, source_key, source_name, activated_at)
            VALUES
                (1, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                job_id = excluded.job_id,
                source_key = excluded.source_key,
                source_name = excluded.source_name,
                activated_at = CURRENT_TIMESTAMP
            """,
            (job_id, source_key, profile["display_name"]),
        )
        connection.commit()

    if profile["connector_type"] == "UPLOAD":
        return RedirectResponse(url=f"/jobs/{job_id}#quote-upload", status_code=303)

    return RedirectResponse(url=profile["launch_url"], status_code=303)



ADMIN_TEMPLATE_DIR = Path("data/document_templates")
ADMIN_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    with closing(get_connection()) as connection:
        connector_count = connection.execute(
            "SELECT COUNT(*) AS count FROM connector_profiles"
        ).fetchone()["count"]

        supplier_count = connection.execute(
            "SELECT COUNT(*) AS count FROM suppliers"
        ).fetchone()["count"]

        active_template = connection.execute(
            """
            SELECT *
            FROM document_templates
            WHERE template_key = 'master_corporate'
              AND is_active = 1
            ORDER BY version_number DESC
            LIMIT 1
            """
        ).fetchone()

    return templates.TemplateResponse(
        request=request,
        name="admin_home.html",
        context={
            "connector_count": connector_count,
            "supplier_count": supplier_count,
            "active_template": active_template,
            "active_page": "admin",
        },
    )


@app.get("/admin/document-templates", response_class=HTMLResponse)
def admin_document_templates(request: Request):
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM document_templates
            WHERE template_key = 'master_corporate'
            ORDER BY version_number DESC, id DESC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="admin_document_templates.html",
        context={
            "document_templates": rows,
            "active_page": "admin",
        },
    )


@app.post("/admin/document-templates/upload")
async def upload_document_template(
    template_file: UploadFile = File(...),
    notes: str = Form(""),
):
    original_name = Path(template_file.filename or "template.pdf").name

    if Path(original_name).suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=400,
            detail="The master corporate template must be a PDF.",
        )

    data = await template_file.read()

    if not data.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400,
            detail="The uploaded file is not a valid PDF.",
        )

    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) AS current_version
            FROM document_templates
            WHERE template_key = 'master_corporate'
            """
        ).fetchone()

        version_number = int(row["current_version"] or 0) + 1
        stored_filename = (
            f"PLG_Master_Corporate_v{version_number}.pdf"
        )
        destination = ADMIN_TEMPLATE_DIR / stored_filename
        destination.write_bytes(data)

        connection.execute(
            """
            UPDATE document_templates
            SET is_active = 0
            WHERE template_key = 'master_corporate'
            """
        )

        connection.execute(
            """
            INSERT INTO document_templates (
                template_key,
                display_name,
                version_number,
                original_filename,
                stored_filename,
                file_path,
                is_active,
                notes
            )
            VALUES (
                'master_corporate',
                'PLG Master Corporate Template',
                ?,
                ?,
                ?,
                ?,
                1,
                ?
            )
            """,
            (
                version_number,
                original_name,
                stored_filename,
                str(destination),
                notes.strip(),
            ),
        )
        connection.commit()

    return RedirectResponse(
        url="/admin/document-templates",
        status_code=303,
    )


@app.post("/admin/document-templates/{template_id}/activate")
def activate_document_template(template_id: int):
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM document_templates
            WHERE id = ?
              AND template_key = 'master_corporate'
            """,
            (template_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Document template not found.",
            )

        connection.execute(
            """
            UPDATE document_templates
            SET is_active = 0
            WHERE template_key = 'master_corporate'
            """
        )

        connection.execute(
            """
            UPDATE document_templates
            SET is_active = 1
            WHERE id = ?
            """,
            (template_id,),
        )
        connection.commit()

    return RedirectResponse(
        url="/admin/document-templates",
        status_code=303,
    )


@app.get("/admin/document-templates/{template_id}/preview")
def preview_document_template(template_id: int):
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM document_templates
            WHERE id = ?
              AND template_key = 'master_corporate'
            """,
            (template_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Document template not found.",
        )

    file_path = Path(row["file_path"])

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="The template PDF is missing from storage.",
        )

    return FileResponse(
        path=file_path,
        media_type="application/pdf",
        filename=row["original_filename"],
        content_disposition_type="inline",
    )



@app.get("/connectors", response_class=HTMLResponse)
def connector_manager(request: Request, view: str = "active"):
    if view not in {"active","archived","all"}: view="active"
    where={"active":"WHERE connector_profiles.is_archived=0","archived":"WHERE connector_profiles.is_archived=1"}.get(view,"")
    with closing(get_connection()) as connection:
        rows=connection.execute(f"""SELECT connector_profiles.*, EXISTS(SELECT 1 FROM source_cart_imports WHERE source_key=connector_profiles.connector_key) AS has_history FROM connector_profiles {where} ORDER BY sort_order,display_name""").fetchall()
        connectors=[]
        for row in rows:
            item=dict(row); item["can_delete"]=bool(item["is_archived"]) and not bool(item["has_history"]); connectors.append(item)
    return templates.TemplateResponse(request=request,name="connectors.html",context={"connectors":connectors,"view":view,"active_page":"connectors"})

@app.get("/connectors/new", response_class=HTMLResponse)
def new_connector_form(request: Request):
    return templates.TemplateResponse(request=request,name="connector_form.html",context={"title":"New Connector","subtitle":"Add a source connector.","form_action":"/connectors/new","submit_label":"Save Connector","connector":{"display_name":"","launch_url":"","category":"Supplier","trust_level":"SUPPLIER_VERIFIED","connector_type":"CART"},"active_page":"connectors"})

@app.post("/connectors/new")
def add_connector(display_name: Annotated[str, Form()], launch_url: Annotated[str, Form()] = "", category: Annotated[str, Form()] = "Supplier", trust_level: Annotated[str, Form()] = "SUPPLIER_VERIFIED", connector_type: Annotated[str, Form()] = "CART"):
    key=re.sub(r"[^a-z0-9]+","_",display_name.lower()).strip("_")
    with closing(get_connection()) as connection: connection.execute("INSERT INTO connector_profiles (connector_key,display_name,category,trust_level,launch_url,connector_type,parser_key,is_enabled,is_archived,sort_order) VALUES (?,?,?,?,?,?,'',1,0,100)",(key,display_name.strip(),category.strip(),trust_level.strip(),launch_url.strip(),connector_type.strip())); connection.commit()
    return RedirectResponse(url="/connectors",status_code=303)

@app.get("/connectors/{connector_id}/edit", response_class=HTMLResponse)
def edit_connector_form(request: Request, connector_id: int):
    with closing(get_connection()) as connection: c=connection.execute("SELECT * FROM connector_profiles WHERE id=?",(connector_id,)).fetchone()
    return templates.TemplateResponse(request=request,name="connector_form.html",context={"title":"Edit Connector","subtitle":c["display_name"],"form_action":f"/connectors/{connector_id}/edit","submit_label":"Save Changes","connector":c,"active_page":"connectors"})

@app.post("/connectors/{connector_id}/edit")
def update_connector(connector_id: int, display_name: Annotated[str, Form()], launch_url: Annotated[str, Form()] = "", category: Annotated[str, Form()] = "Supplier", trust_level: Annotated[str, Form()] = "SUPPLIER_VERIFIED", connector_type: Annotated[str, Form()] = "CART"):
    with closing(get_connection()) as connection: connection.execute("UPDATE connector_profiles SET display_name=?,launch_url=?,category=?,trust_level=?,connector_type=? WHERE id=?",(display_name.strip(),launch_url.strip(),category.strip(),trust_level.strip(),connector_type.strip(),connector_id)); connection.commit()
    return RedirectResponse(url="/connectors",status_code=303)

@app.post("/connectors/{connector_id}/toggle")
def toggle_connector(connector_id: int):
    with closing(get_connection()) as connection:
        c=connection.execute("SELECT is_enabled FROM connector_profiles WHERE id=?",(connector_id,)).fetchone()
        connection.execute("UPDATE connector_profiles SET is_enabled=? WHERE id=?",(0 if c["is_enabled"] else 1,connector_id)); connection.commit()
    return RedirectResponse(url="/connectors",status_code=303)

@app.post("/connectors/{connector_id}/archive")
def archive_connector(connector_id: int):
    with closing(get_connection()) as connection: connection.execute("UPDATE connector_profiles SET is_archived=1,is_enabled=0 WHERE id=?",(connector_id,)); connection.commit()
    return RedirectResponse(url="/connectors",status_code=303)

@app.post("/connectors/{connector_id}/restore")
def restore_connector(connector_id: int):
    with closing(get_connection()) as connection: connection.execute("UPDATE connector_profiles SET is_archived=0 WHERE id=?",(connector_id,)); connection.commit()
    return RedirectResponse(url="/connectors?view=archived",status_code=303)

@app.post("/connectors/{connector_id}/delete")
def delete_connector(connector_id: int):
    with closing(get_connection()) as connection:
        c=connection.execute("SELECT * FROM connector_profiles WHERE id=?",(connector_id,)).fetchone()
        used=connection.execute("SELECT 1 FROM source_cart_imports WHERE source_key=? LIMIT 1",(c["connector_key"],)).fetchone()
        if used: raise HTTPException(status_code=400,detail="Connector has import history.")
        connection.execute("DELETE FROM connector_profiles WHERE id=?",(connector_id,)); connection.commit()
    return RedirectResponse(url="/connectors?view=archived",status_code=303)


@app.get("/api/active-source-import")
def api_active_source_import():
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT
                active_source_import.job_id,
                active_source_import.source_key,
                active_source_import.source_name,
                active_source_import.activated_at,
                jobs.job_number,
                jobs.customer,
                jobs.manufacturer,
                jobs.machine,
                jobs.pin_serial
            FROM active_source_import
            JOIN jobs ON jobs.id = active_source_import.job_id
            WHERE active_source_import.id = 1
            """
        ).fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "message": "No active source import job."},
        )

    return {"ok": True, **dict(row)}


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
