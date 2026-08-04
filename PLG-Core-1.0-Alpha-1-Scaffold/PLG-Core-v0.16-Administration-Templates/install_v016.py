from pathlib import Path
import shutil
from datetime import datetime
import sqlite3
import re

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"
BASE = PROJECT / "templates" / "base.html"
ADMIN_HOME = PROJECT / "templates" / "admin_home.html"
ADMIN_TEMPLATES = PROJECT / "templates" / "admin_document_templates.html"
DATA_DIR = PROJECT / "data" / "document_templates"
DB_PATH = PROJECT / "data" / "plg_core.db"

for path in (APP, BASE):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.16-{stamp}"
(backup / "templates").mkdir(parents=True)

shutil.copy2(APP, backup / "app.py")
shutil.copy2(BASE, backup / "templates" / "base.html")

for source in (ADMIN_HOME, ADMIN_TEMPLATES):
    if source.exists():
        shutil.copy2(source, backup / "templates" / source.name)

app = APP.read_text()

if "from pathlib import Path" not in app:
    app = "from pathlib import Path\n" + app

if "from fastapi import File, UploadFile" not in app:
    app = "from fastapi import File, UploadFile\n" + app

if "from fastapi.responses import FileResponse" not in app:
    app = "from fastapi.responses import FileResponse\n" + app

if "CREATE TABLE IF NOT EXISTS document_templates" not in app:
    anchor = "        connection.commit()\n\n\ndef next_job_number"
    if anchor not in app:
        raise SystemExit("Could not find database migration insertion point.")
    app = app.replace(anchor, '        connection.execute(\n            """\n            CREATE TABLE IF NOT EXISTS document_templates (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                template_key TEXT NOT NULL DEFAULT \'master_corporate\',\n                display_name TEXT NOT NULL,\n                version_number INTEGER NOT NULL DEFAULT 1,\n                original_filename TEXT NOT NULL,\n                stored_filename TEXT NOT NULL,\n                file_path TEXT NOT NULL,\n                is_active INTEGER NOT NULL DEFAULT 0,\n                uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,\n                notes TEXT DEFAULT \'\',\n                UNIQUE(template_key, version_number)\n            )\n            """\n        )\n\n        connection.commit()\n\n\ndef next_job_number', 1)

if '@app.get("/admin", response_class=HTMLResponse)' not in app:
    anchor = '@app.get("/connectors", response_class=HTMLResponse)'
    if anchor not in app:
        anchor = '@app.get("/", response_class=HTMLResponse)'
    if anchor not in app:
        raise SystemExit("Could not find route insertion point.")
    app = app.replace(anchor, '\nADMIN_TEMPLATE_DIR = Path("data/document_templates")\nADMIN_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)\n\n\n@app.get("/admin", response_class=HTMLResponse)\ndef admin_home(request: Request):\n    with closing(get_connection()) as connection:\n        connector_count = connection.execute(\n            "SELECT COUNT(*) AS count FROM connector_profiles"\n        ).fetchone()["count"]\n\n        supplier_count = connection.execute(\n            "SELECT COUNT(*) AS count FROM suppliers"\n        ).fetchone()["count"]\n\n        active_template = connection.execute(\n            """\n            SELECT *\n            FROM document_templates\n            WHERE template_key = \'master_corporate\'\n              AND is_active = 1\n            ORDER BY version_number DESC\n            LIMIT 1\n            """\n        ).fetchone()\n\n    return templates.TemplateResponse(\n        request=request,\n        name="admin_home.html",\n        context={\n            "connector_count": connector_count,\n            "supplier_count": supplier_count,\n            "active_template": active_template,\n            "active_page": "admin",\n        },\n    )\n\n\n@app.get("/admin/document-templates", response_class=HTMLResponse)\ndef admin_document_templates(request: Request):\n    with closing(get_connection()) as connection:\n        rows = connection.execute(\n            """\n            SELECT *\n            FROM document_templates\n            WHERE template_key = \'master_corporate\'\n            ORDER BY version_number DESC, id DESC\n            """\n        ).fetchall()\n\n    return templates.TemplateResponse(\n        request=request,\n        name="admin_document_templates.html",\n        context={\n            "document_templates": rows,\n            "active_page": "admin",\n        },\n    )\n\n\n@app.post("/admin/document-templates/upload")\nasync def upload_document_template(\n    template_file: UploadFile = File(...),\n    notes: str = Form(""),\n):\n    original_name = Path(template_file.filename or "template.pdf").name\n\n    if Path(original_name).suffix.lower() != ".pdf":\n        raise HTTPException(\n            status_code=400,\n            detail="The master corporate template must be a PDF.",\n        )\n\n    data = await template_file.read()\n\n    if not data.startswith(b"%PDF"):\n        raise HTTPException(\n            status_code=400,\n            detail="The uploaded file is not a valid PDF.",\n        )\n\n    with closing(get_connection()) as connection:\n        row = connection.execute(\n            """\n            SELECT COALESCE(MAX(version_number), 0) AS current_version\n            FROM document_templates\n            WHERE template_key = \'master_corporate\'\n            """\n        ).fetchone()\n\n        version_number = int(row["current_version"] or 0) + 1\n        stored_filename = (\n            f"PLG_Master_Corporate_v{version_number}.pdf"\n        )\n        destination = ADMIN_TEMPLATE_DIR / stored_filename\n        destination.write_bytes(data)\n\n        connection.execute(\n            """\n            UPDATE document_templates\n            SET is_active = 0\n            WHERE template_key = \'master_corporate\'\n            """\n        )\n\n        connection.execute(\n            """\n            INSERT INTO document_templates (\n                template_key,\n                display_name,\n                version_number,\n                original_filename,\n                stored_filename,\n                file_path,\n                is_active,\n                notes\n            )\n            VALUES (\n                \'master_corporate\',\n                \'PLG Master Corporate Template\',\n                ?,\n                ?,\n                ?,\n                ?,\n                1,\n                ?\n            )\n            """,\n            (\n                version_number,\n                original_name,\n                stored_filename,\n                str(destination),\n                notes.strip(),\n            ),\n        )\n        connection.commit()\n\n    return RedirectResponse(\n        url="/admin/document-templates",\n        status_code=303,\n    )\n\n\n@app.post("/admin/document-templates/{template_id}/activate")\ndef activate_document_template(template_id: int):\n    with closing(get_connection()) as connection:\n        row = connection.execute(\n            """\n            SELECT *\n            FROM document_templates\n            WHERE id = ?\n              AND template_key = \'master_corporate\'\n            """,\n            (template_id,),\n        ).fetchone()\n\n        if row is None:\n            raise HTTPException(\n                status_code=404,\n                detail="Document template not found.",\n            )\n\n        connection.execute(\n            """\n            UPDATE document_templates\n            SET is_active = 0\n            WHERE template_key = \'master_corporate\'\n            """\n        )\n\n        connection.execute(\n            """\n            UPDATE document_templates\n            SET is_active = 1\n            WHERE id = ?\n            """,\n            (template_id,),\n        )\n        connection.commit()\n\n    return RedirectResponse(\n        url="/admin/document-templates",\n        status_code=303,\n    )\n\n\n@app.get("/admin/document-templates/{template_id}/preview")\ndef preview_document_template(template_id: int):\n    with closing(get_connection()) as connection:\n        row = connection.execute(\n            """\n            SELECT *\n            FROM document_templates\n            WHERE id = ?\n              AND template_key = \'master_corporate\'\n            """,\n            (template_id,),\n        ).fetchone()\n\n    if row is None:\n        raise HTTPException(\n            status_code=404,\n            detail="Document template not found.",\n        )\n\n    file_path = Path(row["file_path"])\n\n    if not file_path.exists():\n        raise HTTPException(\n            status_code=404,\n            detail="The template PDF is missing from storage.",\n        )\n\n    return FileResponse(\n        path=file_path,\n        media_type="application/pdf",\n        filename=row["original_filename"],\n        content_disposition_type="inline",\n    )\n\n\n' + "\n" + anchor, 1)

APP.write_text(app)

base = BASE.read_text()

if 'href="/admin"' not in base:
    nav_end = "</nav>"
    if nav_end not in base:
        raise SystemExit("Could not find navigation menu.")
    admin_link = (
        '      <a class="{% if active_page == \'admin\' %}active'
        '{% endif %}" href="/admin">Administration</a>\n'
    )
    base = base.replace(nav_end, admin_link + nav_end, 1)

base = re.sub(
    r"PLG Core v\d+\.\d+(?:\.\d+)?",
    "PLG Core v0.16",
    base,
)
BASE.write_text(base)

shutil.copy2(
    Path(__file__).with_name("admin_home.html"),
    ADMIN_HOME,
)
shutil.copy2(
    Path(__file__).with_name("admin_document_templates.html"),
    ADMIN_TEMPLATES,
)

DATA_DIR.mkdir(parents=True, exist_ok=True)

seed_pdf = Path(__file__).parent / "seed" / "PLG_Master_Corporate_v1.pdf"

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute(
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

existing = db.execute(
    """
    SELECT id
    FROM document_templates
    WHERE template_key = 'master_corporate'
    LIMIT 1
    """
).fetchone()

if existing is None and seed_pdf.exists():
    destination = DATA_DIR / "PLG_Master_Corporate_v1.pdf"
    shutil.copy2(seed_pdf, destination)

    db.execute(
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
            1,
            'PLG Master Corporate.pdf',
            'PLG_Master_Corporate_v1.pdf',
            ?,
            1,
            'Initial approved corporate template'
        )
        """,
        (str(destination),),
    )

db.commit()
db.close()

print("PLG Core v0.16 Administration and Templates installed successfully.")
print(f"Backup created at: {backup}")
