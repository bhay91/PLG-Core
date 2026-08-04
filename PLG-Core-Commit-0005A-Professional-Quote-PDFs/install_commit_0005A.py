from pathlib import Path
from datetime import datetime
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
SOURCE = Path(__file__).resolve().parent / "payload"
LEGACY = PROJECT / "legacy_app.py"
CSS = PROJECT / "static" / "app.css"
REQ = PROJECT / "requirements.txt"
PYTHON = PROJECT / ".venv" / "bin" / "python"
PIP = PROJECT / ".venv" / "bin" / "pip"

managed = [
    LEGACY,
    CSS,
    REQ,
    PROJECT / "templates" / "quote_documents.html",
    PROJECT / "plg_core" / "documents" / "__init__.py",
    PROJECT / "plg_core" / "documents" / "quote_pdf.py",
]

for path in (LEGACY, CSS, REQ, PYTHON, PIP):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0005A-{stamp}"
backup.mkdir(parents=True)

for current in managed:
    if current.exists():
        saved = backup / current.relative_to(PROJECT)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, saved)


def restore():
    for current in managed:
        saved = backup / current.relative_to(PROJECT)
        if saved.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, current)


try:
    if subprocess.run([str(PYTHON), "-c", "import reportlab"], cwd=PROJECT).returncode != 0:
        result = subprocess.run([str(PIP), "install", "reportlab>=4.0"], cwd=PROJECT, text=True, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(result.stdout + result.stderr)

    requirements = REQ.read_text()
    if "reportlab" not in requirements.lower():
        REQ.write_text(requirements + ("" if requirements.endswith("\n") else "\n") + "reportlab>=4.0\n")

    docs_dir = PROJECT / "plg_core" / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE / "plg_core" / "documents" / "quote_pdf.py", docs_dir / "quote_pdf.py")
    shutil.copy2(SOURCE / "plg_core" / "documents" / "__init__.py", docs_dir / "__init__.py")
    shutil.copy2(SOURCE / "templates" / "quote_documents.html", PROJECT / "templates" / "quote_documents.html")

    css = CSS.read_text()
    marker = "/* Commit 0005A Professional Quote PDFs */"
    if marker not in css:
        CSS.write_text(css + "\n" + (SOURCE / "static.css").read_text())

    legacy = LEGACY.read_text()
    import_line = "from plg_core.documents.quote_pdf import generate_quote_pdfs, quote_paths, sanitize_path_name"
    if import_line not in legacy:
        legacy = legacy.replace(
            "from fastapi.responses import FileResponse",
            "from fastapi.responses import FileResponse\n" + import_line,
            1,
        )

    old_redirect = (
        "        connection.commit()\n\n"
        "    return RedirectResponse(\n"
        "        url=f\"/quotes/{quote_id}/customer\",\n"
        "        status_code=303,\n"
        "    )\n"
    )
    new_redirect = (
        "        connection.commit()\n\n"
        "        quote, pdf_items = load_quote(connection, quote_id)\n"
        "        generate_quote_pdfs(quote, pdf_items)\n\n"
        "    return RedirectResponse(\n"
        "        url=f\"/quotes/{quote_id}/documents\",\n"
        "        status_code=303,\n"
        "    )\n"
    )
    if 'url=f"/quotes/{quote_id}/documents"' not in legacy:
        if old_redirect not in legacy:
            raise RuntimeError("Quote redirect block not found")
        legacy = legacy.replace(old_redirect, new_redirect, 1)

    route_marker = '@app.get("/quotes/{quote_id}/documents"'
    if route_marker not in legacy:
        anchor = '@app.get("/quotes/{quote_id}/customer", response_class=HTMLResponse)'
        if anchor not in legacy:
            raise RuntimeError("Customer quote route anchor not found")
        routes = '''
@app.get("/quotes/{quote_id}/documents", response_class=HTMLResponse)
def quote_documents(request: Request, quote_id: int):
    with closing(get_connection()) as connection:
        quote, items = load_quote(connection, quote_id)
    paths = quote_paths(quote["customer"], quote["quote_number"])
    if not paths["customer"].exists() or not paths["internal"].exists():
        generate_quote_pdfs(quote, items)
    customer_path = Path("documents") / "Customers" / sanitize_path_name(quote["customer"]) / "Quotes"
    return templates.TemplateResponse(request=request, name="quote_documents.html", context={"quote": quote, "items": items, "customer_path": str(customer_path), "active_page": "quotes"})

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

'''
        legacy = legacy.replace(anchor, routes + anchor, 1)

    LEGACY.write_text(legacy)

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            "from app import app; p={getattr(r,'path',None) for r in app.routes}; assert '/quotes/{quote_id}/documents' in p; assert '/quotes/{quote_id}/customer/pdf' in p; assert '/quotes/{quote_id}/internal/pdf' in p",
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

    pdf_smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            "from legacy_app import get_connection,load_quote; from plg_core.documents.quote_pdf import generate_quote_pdfs; c=get_connection(); r=c.execute('SELECT id FROM quotes ORDER BY id DESC LIMIT 1').fetchone(); assert r; q,i=load_quote(c,r['id']); p=generate_quote_pdfs(q,i); from pathlib import Path; assert Path(p['customer']).exists() and Path(p['internal']).exists(); c.close()",
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if pdf_smoke.returncode != 0:
        raise RuntimeError(pdf_smoke.stdout + pdf_smoke.stderr)

except Exception as exc:
    restore()
    raise SystemExit("Commit 0005A failed and files were restored.\n" + str(exc))

print("PLG Commit 0005A Professional Quote PDFs installed successfully.")
print("Customer and internal quote PDFs are saved locally under documents/Customers/<Customer>/Quotes/.")
print(f"Backup created at: {backup}")
