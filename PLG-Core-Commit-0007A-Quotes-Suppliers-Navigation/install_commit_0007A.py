from pathlib import Path
from datetime import datetime
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
SOURCE = Path(__file__).resolve().parent / "payload"
LEGACY = PROJECT / "legacy_app.py"
BASE = PROJECT / "templates" / "base.html"
QUOTES_TEMPLATE = PROJECT / "templates" / "quotes.html"
SUPPLIERS_TEMPLATE = PROJECT / "templates" / "suppliers.html"
CSS = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (LEGACY, BASE, CSS, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

managed = [LEGACY, BASE, QUOTES_TEMPLATE, SUPPLIERS_TEMPLATE, CSS]
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0007A-{stamp}"
backup.mkdir(parents=True)

for path in managed:
    if path.exists():
        saved = backup / path.relative_to(PROJECT)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, saved)

def restore():
    for path in managed:
        saved = backup / path.relative_to(PROJECT)
        if saved.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, path)

try:
    text = LEGACY.read_text()

    quote_route = '@app.get("/quotes", response_class=HTMLResponse)\ndef list_quotes(request: Request):\n    with closing(get_connection()) as connection:\n        quote_rows = connection.execute(\n            """\n            SELECT\n                quotes.*,\n                jobs.customer_id,\n                jobs.customer,\n                jobs.job_number,\n                jobs.manufacturer,\n                jobs.machine\n            FROM quotes\n            JOIN jobs ON jobs.id=quotes.job_id\n            ORDER BY quotes.id DESC\n            """\n        ).fetchall()\n\n    return templates.TemplateResponse(\n        request=request,\n        name="quotes.html",\n        context={"quotes": quote_rows, "active_page": "quotes"},\n    )\n\n\n'
    if '@app.get("/quotes", response_class=HTMLResponse)' not in text:
        anchor = '@app.get("/quotes/{quote_id}/documents", response_class=HTMLResponse)'
        if anchor not in text:
            raise RuntimeError("Quote route insertion point not found.")
        text = text.replace(anchor, quote_route + anchor, 1)

    supplier_route = '@app.get("/suppliers", response_class=HTMLResponse)\ndef list_suppliers(request: Request):\n    with closing(get_connection()) as connection:\n        supplier_rows = connection.execute(\n            """\n            SELECT\n                part_sources.supplier_name AS name,\n                COUNT(part_sources.id) AS source_lines,\n                SUM(CASE WHEN part_sources.selected_for_quote=1 THEN 1 ELSE 0 END) AS selected_lines,\n                COUNT(DISTINCT job_parts.job_id) AS jobs_count,\n                COALESCE(SUM(\n                    CASE\n                        WHEN part_sources.selected_for_quote=1\n                        THEN part_sources.supplier_cost * job_parts.quantity\n                        ELSE 0\n                    END\n                ), 0) AS quoted_spend,\n                MAX(part_sources.updated_at) AS last_used\n            FROM part_sources\n            JOIN job_parts ON job_parts.id=part_sources.part_id\n            WHERE TRIM(COALESCE(part_sources.supplier_name, \'\')) != \'\'\n            GROUP BY LOWER(TRIM(part_sources.supplier_name))\n            ORDER BY part_sources.supplier_name COLLATE NOCASE\n            """\n        ).fetchall()\n\n        summary_row = connection.execute(\n            """\n            SELECT\n                COUNT(DISTINCT LOWER(TRIM(supplier_name))) AS suppliers_total,\n                COUNT(id) AS parts_total,\n                SUM(CASE WHEN selected_for_quote=1 THEN 1 ELSE 0 END) AS selected_total\n            FROM part_sources\n            WHERE TRIM(COALESCE(supplier_name, \'\')) != \'\'\n            """\n        ).fetchone()\n\n        spend_row = connection.execute(\n            """\n            SELECT COALESCE(SUM(part_sources.supplier_cost * job_parts.quantity), 0) AS spend_total\n            FROM part_sources\n            JOIN job_parts ON job_parts.id=part_sources.part_id\n            WHERE part_sources.selected_for_quote=1\n            """\n        ).fetchone()\n\n        summary = {\n            "suppliers_total": int(summary_row["suppliers_total"] or 0),\n            "parts_total": int(summary_row["parts_total"] or 0),\n            "selected_total": int(summary_row["selected_total"] or 0),\n            "spend_total": float(spend_row["spend_total"] or 0),\n        }\n\n    return templates.TemplateResponse(\n        request=request,\n        name="suppliers.html",\n        context={\n            "suppliers": supplier_rows,\n            "summary": summary,\n            "active_page": "suppliers",\n        },\n    )\n\n\n'
    if '@app.get("/suppliers", response_class=HTMLResponse)' not in text:
        anchor = '@app.get("/quotes", response_class=HTMLResponse)'
        if anchor not in text:
            raise RuntimeError("Supplier route insertion point not found.")
        text = text.replace(anchor, supplier_route + anchor, 1)

    LEGACY.write_text(text)

    base_text = BASE.read_text()
    old_quotes = '      <span class="disabled"><span class="nav-icon">＄</span><span>Quotes</span></span>'
    new_quotes = '      <a class="{% if active_page == \'quotes\' %}active{% endif %}" href="/quotes">\n        <span class="nav-icon" aria-hidden="true">＄</span><span>Quotes</span>\n      </a>'
    if old_quotes in base_text:
        base_text = base_text.replace(old_quotes, new_quotes, 1)

    old_suppliers = '      <span class="disabled"><span class="nav-icon">▰</span><span>Suppliers</span></span>'
    new_suppliers = '      <a class="{% if active_page == \'suppliers\' %}active{% endif %}" href="/suppliers">\n        <span class="nav-icon" aria-hidden="true">▰</span><span>Suppliers</span>\n      </a>'
    if old_suppliers in base_text:
        base_text = base_text.replace(old_suppliers, new_suppliers, 1)

    BASE.write_text(base_text)

    shutil.copy2(SOURCE / "templates" / "quotes.html", QUOTES_TEMPLATE)
    shutil.copy2(SOURCE / "templates" / "suppliers.html", SUPPLIERS_TEMPLATE)

    css_text = CSS.read_text()
    css_patch = (SOURCE / "static" / "commit-0007A.css").read_text()
    if "/* Commit 0007A Quotes and Suppliers Navigation */" not in css_text:
        CSS.write_text(css_text + "\n" + css_patch)

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "paths={getattr(r,'path',None) for r in app.routes}; "
                "assert '/quotes' in paths and '/suppliers' in paths; "
                "from jinja2 import Environment, FileSystemLoader; "
                "env=Environment(loader=FileSystemLoader('templates')); "
                "env.get_template('quotes.html'); "
                "env.get_template('suppliers.html'); "
                "env.get_template('base.html'); "
                "print('ok')"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

except Exception as exc:
    restore()
    raise SystemExit("Commit 0007A failed and files were restored.\n" + str(exc))

print("PLG Commit 0007A Quotes and Suppliers Navigation installed successfully.")
print("Quotes and Suppliers are now clickable in the sidebar.")
print("Quote Register and Supplier Register pages are active.")
print(f"Backup created at: {backup}")
