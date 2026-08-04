from pathlib import Path
import shutil
from datetime import datetime
import re

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"
JOB = PROJECT / "templates" / "job_detail.html"
BASE = PROJECT / "templates" / "base.html"
CUSTOMER_TEMPLATE = PROJECT / "templates" / "quote_customer.html"
INTERNAL_TEMPLATE = PROJECT / "templates" / "quote_internal.html"

for path in (APP, JOB, BASE):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.15-{stamp}"
(backup / "templates").mkdir(parents=True)

shutil.copy2(APP, backup / "app.py")
shutil.copy2(JOB, backup / "templates" / "job_detail.html")
shutil.copy2(BASE, backup / "templates" / "base.html")

app = APP.read_text()

if "\nimport math\n" not in app:
    anchor = "import sqlite3\n"
    if anchor not in app:
        raise SystemExit("Could not find sqlite3 import.")
    app = app.replace(anchor, anchor + "import math\n", 1)

if "CREATE TABLE IF NOT EXISTS quotes" not in app:
    anchor = "        connection.commit()\n\n\ndef next_job_number"
    if anchor not in app:
        raise SystemExit("Could not find database migration insertion point.")
    app = app.replace(anchor, '        connection.execute(\n            """\n            CREATE TABLE IF NOT EXISTS quotes (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                quote_number TEXT UNIQUE NOT NULL,\n                job_id INTEGER NOT NULL,\n                quote_date TEXT NOT NULL,\n                status TEXT NOT NULL DEFAULT \'DRAFT\',\n                parts_subtotal REAL NOT NULL DEFAULT 0,\n                shipping_total REAL NOT NULL DEFAULT 0,\n                customer_total REAL NOT NULL DEFAULT 0,\n                supplier_total REAL NOT NULL DEFAULT 0,\n                profit_total REAL NOT NULL DEFAULT 0,\n                created_at TEXT DEFAULT CURRENT_TIMESTAMP,\n                FOREIGN KEY (job_id) REFERENCES jobs(id)\n            )\n            """\n        )\n\n        connection.execute(\n            """\n            CREATE TABLE IF NOT EXISTS quote_items (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                quote_id INTEGER NOT NULL,\n                part_id INTEGER NOT NULL,\n                source_id INTEGER NOT NULL,\n                quantity INTEGER NOT NULL DEFAULT 1,\n                description TEXT NOT NULL,\n                supplier_name TEXT NOT NULL,\n                source_type TEXT NOT NULL,\n                brand TEXT DEFAULT \'\',\n                supplier_part_number TEXT DEFAULT \'\',\n                supplier_unit_cost REAL NOT NULL DEFAULT 0,\n                customer_unit_price REAL NOT NULL DEFAULT 0,\n                supplier_line_total REAL NOT NULL DEFAULT 0,\n                customer_line_total REAL NOT NULL DEFAULT 0,\n                line_profit REAL NOT NULL DEFAULT 0,\n                FOREIGN KEY (quote_id) REFERENCES quotes(id)\n            )\n            """\n        )\n\n        connection.commit()\n\n\ndef next_job_number', 1)

if '@app.post("/jobs/{job_id}/generate-quote")' not in app:
    anchor = '@app.post("/jobs/{job_id}/status")'
    if anchor not in app:
        raise SystemExit("Could not find quote route insertion point.")
    app = app.replace(anchor, '\ndef next_quote_number(connection: sqlite3.Connection) -> str:\n    current_year = date.today().year\n    prefix = f"PLG-Q-{current_year}-"\n\n    row = connection.execute(\n        """\n        SELECT quote_number\n        FROM quotes\n        WHERE quote_number LIKE ?\n        ORDER BY id DESC\n        LIMIT 1\n        """,\n        (f"{prefix}%",),\n    ).fetchone()\n\n    if row is None:\n        sequence = 1\n    else:\n        try:\n            sequence = int(row["quote_number"].split("-")[-1]) + 1\n        except (ValueError, IndexError):\n            sequence = 1\n\n    return f"{prefix}{sequence:03d}"\n\n\ndef calculate_customer_unit_price(cost: float) -> float:\n    if cost <= 50:\n        markup = 0.40\n    elif cost <= 200:\n        markup = 0.30\n    elif cost <= 500:\n        markup = 0.25\n    else:\n        markup = 0.20\n\n    return float(math.ceil(cost * (1 + markup)))\n\n\n@app.post("/jobs/{job_id}/generate-quote")\ndef generate_quote(job_id: int):\n    with closing(get_connection()) as connection:\n        job = connection.execute(\n            "SELECT * FROM jobs WHERE id = ?",\n            (job_id,),\n        ).fetchone()\n\n        if job is None:\n            raise HTTPException(status_code=404, detail="Job not found.")\n\n        selected = connection.execute(\n            """\n            SELECT\n                job_parts.id AS part_id,\n                job_parts.requested_description,\n                job_parts.oem_description,\n                job_parts.quantity,\n                part_sources.id AS source_id,\n                part_sources.supplier_name,\n                part_sources.source_type,\n                part_sources.brand,\n                part_sources.supplier_part_number,\n                part_sources.supplier_cost\n            FROM job_parts\n            JOIN part_sources\n                ON part_sources.part_id = job_parts.id\n               AND part_sources.selected_for_quote = 1\n            WHERE job_parts.job_id = ?\n            ORDER BY job_parts.id\n            """,\n            (job_id,),\n        ).fetchall()\n\n        total_parts = connection.execute(\n            "SELECT COUNT(*) AS count FROM job_parts WHERE job_id = ?",\n            (job_id,),\n        ).fetchone()["count"]\n\n        if total_parts == 0:\n            raise HTTPException(\n                status_code=400,\n                detail="Add at least one part before generating a quote.",\n            )\n\n        if len(selected) != total_parts:\n            raise HTTPException(\n                status_code=400,\n                detail="Select one supplier source for every part first.",\n            )\n\n        quote_number = next_quote_number(connection)\n        quote_date = date.today().isoformat()\n\n        shipping_rows = connection.execute(\n            """\n            SELECT source_name, shipping_total\n            FROM source_cart_imports\n            WHERE id IN (\n                SELECT MAX(id)\n                FROM source_cart_imports\n                WHERE job_id = ?\n                GROUP BY LOWER(TRIM(source_name))\n            )\n            """,\n            (job_id,),\n        ).fetchall()\n\n        selected_suppliers = {\n            str(row["supplier_name"] or "").strip().lower()\n            for row in selected\n        }\n\n        shipping_total = sum(\n            float(row["shipping_total"] or 0)\n            for row in shipping_rows\n            if str(row["source_name"] or "").strip().lower()\n            in selected_suppliers\n        )\n\n        parts_subtotal = 0.0\n        supplier_parts_total = 0.0\n        item_rows = []\n\n        for row in selected:\n            quantity = max(1, int(row["quantity"] or 1))\n            supplier_unit_cost = float(row["supplier_cost"] or 0)\n            customer_unit_price = calculate_customer_unit_price(\n                supplier_unit_cost\n            )\n            supplier_line_total = supplier_unit_cost * quantity\n            customer_line_total = customer_unit_price * quantity\n            line_profit = customer_line_total - supplier_line_total\n\n            description = (\n                str(row["requested_description"] or "").strip()\n                or str(row["oem_description"] or "").strip()\n                or "Part"\n            )\n\n            supplier_parts_total += supplier_line_total\n            parts_subtotal += customer_line_total\n\n            item_rows.append(\n                (\n                    row["part_id"],\n                    row["source_id"],\n                    quantity,\n                    description,\n                    row["supplier_name"],\n                    row["source_type"],\n                    row["brand"] or "",\n                    row["supplier_part_number"] or "",\n                    supplier_unit_cost,\n                    customer_unit_price,\n                    supplier_line_total,\n                    customer_line_total,\n                    line_profit,\n                )\n            )\n\n        customer_total = parts_subtotal + shipping_total\n        supplier_total = supplier_parts_total + shipping_total\n        profit_total = customer_total - supplier_total\n\n        cursor = connection.execute(\n            """\n            INSERT INTO quotes (\n                quote_number,\n                job_id,\n                quote_date,\n                status,\n                parts_subtotal,\n                shipping_total,\n                customer_total,\n                supplier_total,\n                profit_total\n            )\n            VALUES (?, ?, ?, \'DRAFT\', ?, ?, ?, ?, ?)\n            """,\n            (\n                quote_number,\n                job_id,\n                quote_date,\n                parts_subtotal,\n                shipping_total,\n                customer_total,\n                supplier_total,\n                profit_total,\n            ),\n        )\n        quote_id = cursor.lastrowid\n\n        for item in item_rows:\n            connection.execute(\n                """\n                INSERT INTO quote_items (\n                    quote_id,\n                    part_id,\n                    source_id,\n                    quantity,\n                    description,\n                    supplier_name,\n                    source_type,\n                    brand,\n                    supplier_part_number,\n                    supplier_unit_cost,\n                    customer_unit_price,\n                    supplier_line_total,\n                    customer_line_total,\n                    line_profit\n                )\n                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                """,\n                (quote_id, *item),\n            )\n\n        connection.execute(\n            "UPDATE jobs SET status = \'QUOTED\' WHERE id = ?",\n            (job_id,),\n        )\n        connection.commit()\n\n    return RedirectResponse(\n        url=f"/quotes/{quote_id}/customer",\n        status_code=303,\n    )\n\n\ndef load_quote(connection: sqlite3.Connection, quote_id: int):\n    quote = connection.execute(\n        """\n        SELECT\n            quotes.*,\n            jobs.job_number,\n            jobs.customer,\n            jobs.company,\n            jobs.phone,\n            jobs.email,\n            jobs.manufacturer,\n            jobs.machine,\n            jobs.pin_serial\n        FROM quotes\n        JOIN jobs ON jobs.id = quotes.job_id\n        WHERE quotes.id = ?\n        """,\n        (quote_id,),\n    ).fetchone()\n\n    if quote is None:\n        raise HTTPException(status_code=404, detail="Quote not found.")\n\n    items = connection.execute(\n        """\n        SELECT *\n        FROM quote_items\n        WHERE quote_id = ?\n        ORDER BY id\n        """,\n        (quote_id,),\n    ).fetchall()\n\n    return quote, items\n\n\n@app.get("/quotes/{quote_id}/customer", response_class=HTMLResponse)\ndef customer_quote(request: Request, quote_id: int):\n    with closing(get_connection()) as connection:\n        quote, items = load_quote(connection, quote_id)\n\n    return templates.TemplateResponse(\n        request=request,\n        name="quote_customer.html",\n        context={\n            "quote": quote,\n            "items": items,\n            "active_page": "quotes",\n        },\n    )\n\n\n@app.get("/quotes/{quote_id}/internal", response_class=HTMLResponse)\ndef internal_quote(request: Request, quote_id: int):\n    with closing(get_connection()) as connection:\n        quote, items = load_quote(connection, quote_id)\n\n    return templates.TemplateResponse(\n        request=request,\n        name="quote_internal.html",\n        context={\n            "quote": quote,\n            "items": items,\n            "active_page": "quotes",\n        },\n    )\n\n\n' + "\n" + anchor, 1)

APP.write_text(app)

job = JOB.read_text()
if "Generate customer quote" not in job:
    marker = '<section class="detail-grid">'
    if marker not in job:
        raise SystemExit("Could not find quote panel insertion point.")
    job = job.replace(marker, '\n<section class="panel quote-actions-panel">\n  <div class="panel-heading">\n    <div>\n      <p class="eyebrow">QUOTE</p>\n      <h2>Generate customer quote</h2>\n    </div>\n  </div>\n\n  {% if ready_for_quote %}\n  <p>\n    All {{ selected_count }} part source{{ "" if selected_count == 1 else "s" }}\n    selected.\n  </p>\n\n  <form method="post" action="/jobs/{{ job.id }}/generate-quote">\n    <button\n      class="button"\n      type="submit"\n      onclick="return confirm(\'Generate the quote using the currently selected supplier for every part?\');"\n    >\n      Generate Quote\n    </button>\n  </form>\n  {% else %}\n  <p class="muted">\n    Select one supplier source for every part before generating the quote.\n  </p>\n  {% endif %}\n</section>\n' + "\n" + marker, 1)
JOB.write_text(job)

base = BASE.read_text()
base = re.sub(
    r"PLG Core v\d+\.\d+(?:\.\d+)?",
    "PLG Core v0.15",
    base,
)
BASE.write_text(base)

shutil.copy2(
    Path(__file__).with_name("quote_customer.html"),
    CUSTOMER_TEMPLATE,
)
shutil.copy2(
    Path(__file__).with_name("quote_internal.html"),
    INTERNAL_TEMPLATE,
)

print("PLG Core v0.15 Quote Engine installed successfully.")
print(f"Backup created at: {backup}")
