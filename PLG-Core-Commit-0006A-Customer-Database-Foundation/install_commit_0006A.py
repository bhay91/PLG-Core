from pathlib import Path
from datetime import datetime
import re, shutil, subprocess

PROJECT=Path.home()/"Desktop"/"PLG-Core"
SOURCE=Path(__file__).resolve().parent/"payload"
LEGACY=PROJECT/"legacy_app.py"
CSS=PROJECT/"static"/"app.css"
PYTHON=PROJECT/".venv"/"bin"/"python"
TEMPLATES=PROJECT/"templates"
for p in (LEGACY,CSS,PYTHON,TEMPLATES/"new_job.html"):
    if not p.exists(): raise SystemExit(f"Required file not found: {p}")
managed=[LEGACY,CSS,TEMPLATES/"new_job.html",TEMPLATES/"customers.html",TEMPLATES/"customer_account.html"]
stamp=datetime.now().strftime("%Y%m%d-%H%M%S")
backup=PROJECT/f"backup-commit-0006A-{stamp}"
backup.mkdir(parents=True)
for p in managed:
    if p.exists():
        d=backup/p.relative_to(PROJECT); d.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,d)
def restore():
    for p in managed:
        s=backup/p.relative_to(PROJECT)
        if s.exists(): p.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,p)
try:
    text=LEGACY.read_text()
    if "CREATE TABLE IF NOT EXISTS customers (" not in text:
        anchor='''        connection.execute(\n            """\n            CREATE TABLE IF NOT EXISTS quotes (\n'''
        block='''        connection.execute(\n            """\n            CREATE TABLE IF NOT EXISTS customers (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                customer_number TEXT UNIQUE,\n                name TEXT NOT NULL,\n                company TEXT DEFAULT '',\n                phone TEXT DEFAULT '',\n                email TEXT DEFAULT '',\n                address TEXT DEFAULT '',\n                active INTEGER NOT NULL DEFAULT 1,\n                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP\n            )\n            """\n        )\n\n        job_columns={row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}\n        if "customer_id" not in job_columns:\n            connection.execute("ALTER TABLE jobs ADD COLUMN customer_id INTEGER")\n        if "address" not in job_columns:\n            connection.execute("ALTER TABLE jobs ADD COLUMN address TEXT DEFAULT ''")\n\n        for old_job in connection.execute("SELECT id, customer, company, phone, email, address FROM jobs ORDER BY id").fetchall():\n            name=str(old_job["customer"] or "").strip()\n            if not name:\n                continue\n            row=connection.execute("""\n                SELECT id FROM customers\n                WHERE LOWER(TRIM(name))=LOWER(TRIM(?))\n                  AND LOWER(TRIM(COALESCE(company,'')))=LOWER(TRIM(COALESCE(?,'')))\n                ORDER BY id LIMIT 1\n            """,(name,old_job["company"] or "")).fetchone()\n            if row is None:\n                cur=connection.execute("INSERT INTO customers (name,company,phone,email,address) VALUES (?,?,?,?,?)",(name,old_job["company"] or "",old_job["phone"] or "",old_job["email"] or "",old_job["address"] or ""))\n                customer_id=cur.lastrowid\n                connection.execute("UPDATE customers SET customer_number=? WHERE id=?",(f"PLG-C{customer_id:05d}",customer_id))\n            else:\n                customer_id=row["id"]\n            connection.execute("UPDATE jobs SET customer_id=? WHERE id=?",(customer_id,old_job["id"]))\n\n'''
        if anchor not in text: raise RuntimeError("Database insertion point not found")
        text=text.replace(anchor,block+anchor,1)
    gp=re.compile(r'@app\.get\("/jobs/new", response_class=HTMLResponse\)\s*def new_job_form\(request: Request\):.*?(?=\n@app\.post\("/jobs"\))',re.S)
    gr='''@app.get("/jobs/new", response_class=HTMLResponse)\ndef new_job_form(request: Request, customer_id: int | None = None):\n    with closing(get_connection()) as connection:\n        customers=connection.execute("SELECT * FROM customers WHERE active=1 ORDER BY name COLLATE NOCASE, company COLLATE NOCASE").fetchall()\n    return templates.TemplateResponse(request=request,name="new_job.html",context={"customers":customers,"selected_customer_id":customer_id,"active_page":"jobs"})\n\n\n'''
    text,n=gp.subn(gr,text,1)
    if n!=1: raise RuntimeError("New Job route not replaced")
    pp=re.compile(r'@app\.post\("/jobs"\)\s*def create_job\(.*?(?=\n@app\.get\("/jobs", response_class=HTMLResponse\))',re.S)
    pr='''@app.post("/jobs")\ndef create_job(\n    customer: Annotated[str, Form()],\n    customer_id: Annotated[int | None, Form()] = None,\n    company: Annotated[str, Form()] = "",\n    phone: Annotated[str, Form()] = "",\n    email: Annotated[str, Form()] = "",\n    address: Annotated[str, Form()] = "",\n    manufacturer: Annotated[str, Form()] = "",\n    machine: Annotated[str, Form()] = "",\n    pin_serial: Annotated[str, Form()] = "",\n    requested_parts: Annotated[str, Form()] = "",\n    notes: Annotated[str, Form()] = "",\n):\n    with closing(get_connection()) as connection:\n        if customer_id:\n            customer_row=connection.execute("SELECT * FROM customers WHERE id=? AND active=1",(customer_id,)).fetchone()\n            if customer_row is None:\n                raise HTTPException(status_code=400,detail="Selected customer not found.")\n        else:\n            name=customer.strip()\n            if not name:\n                raise HTTPException(status_code=400,detail="Customer is required.")\n            cur=connection.execute("INSERT INTO customers (name,company,phone,email,address) VALUES (?,?,?,?,?)",(name,company.strip(),phone.strip(),email.strip(),address.strip()))\n            customer_id=cur.lastrowid\n            connection.execute("UPDATE customers SET customer_number=? WHERE id=?",(f"PLG-C{customer_id:05d}",customer_id))\n            customer_row=connection.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()\n        job_number=next_job_number(connection)\n        cur=connection.execute("""\n            INSERT INTO jobs (job_number,created_date,customer_id,customer,company,phone,email,address,manufacturer,machine,pin_serial,status,notes)\n            VALUES (?,?,?,?,?,?,?,?,?,?,?,'REQUESTED',?)\n        """,(job_number,date.today().isoformat(),customer_row["id"],customer_row["name"],customer_row["company"] or "",customer_row["phone"] or "",customer_row["email"] or "",customer_row["address"] or "",manufacturer.strip(),machine.strip(),pin_serial.strip(),notes.strip()))\n        job_id=cur.lastrowid\n        connection.commit()\n    return RedirectResponse(url=f"/jobs/{job_id}/basket",status_code=303)\n\n\n'''
    text,n=pp.subn(pr,text,1)
    if n!=1: raise RuntimeError("Create Job route not replaced")
    if '@app.get("/customers", response_class=HTMLResponse)' not in text:
        anchor='@app.get("/jobs", response_class=HTMLResponse)'
        routes='''@app.get("/customers", response_class=HTMLResponse)\ndef list_customers(request: Request):\n    with closing(get_connection()) as connection:\n        customers=connection.execute("""SELECT customers.*,COUNT(jobs.id) AS jobs_count FROM customers LEFT JOIN jobs ON jobs.customer_id=customers.id WHERE customers.active=1 GROUP BY customers.id ORDER BY customers.name COLLATE NOCASE""").fetchall()\n    return templates.TemplateResponse(request=request,name="customers.html",context={"customers":customers,"active_page":"customers"})\n\n\n@app.get("/customers/{customer_id}", response_class=HTMLResponse)\ndef customer_account(request: Request, customer_id: int):\n    with closing(get_connection()) as connection:\n        customer=connection.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()\n        if customer is None:\n            raise HTTPException(status_code=404,detail="Customer not found.")\n        jobs=connection.execute("SELECT * FROM jobs WHERE customer_id=? ORDER BY id DESC",(customer_id,)).fetchall()\n        quotes=connection.execute("""SELECT quotes.* FROM quotes JOIN jobs ON jobs.id=quotes.job_id WHERE jobs.customer_id=? ORDER BY quotes.id DESC""",(customer_id,)).fetchall()\n    return templates.TemplateResponse(request=request,name="customer_account.html",context={"customer":customer,"jobs":jobs,"quotes":quotes,"active_page":"customers"})\n\n\n'''
        if anchor not in text: raise RuntimeError("Customer route insertion point not found")
        text=text.replace(anchor,routes+anchor,1)
    LEGACY.write_text(text)
    for name in ("new_job.html","customers.html","customer_account.html"):
        shutil.copy2(SOURCE/"templates"/name,TEMPLATES/name)
    css=CSS.read_text(); marker="/* Commit 0006A Customer Database Foundation */"
    if marker not in css: CSS.write_text(css+"\n"+(SOURCE/"static"/"commit-0006A.css").read_text())
    smoke=subprocess.run([str(PYTHON),"-c","from legacy_app import initialize_database; initialize_database(); from app import app; p={getattr(r,'path',None) for r in app.routes}; assert '/customers' in p and '/customers/{customer_id}' in p; print('ok')"],cwd=PROJECT,text=True,capture_output=True)
    if smoke.returncode!=0: raise RuntimeError(smoke.stdout+smoke.stderr)
except Exception as exc:
    restore(); raise SystemExit("Commit 0006A failed and files were restored.\n"+str(exc))
print("PLG Commit 0006A Customer Database Foundation installed successfully.")
print("Existing customers were migrated and linked to jobs.")
print("New jobs can select an existing customer or create a new one.")
print("Customer list and Customer Account pages are now available.")
print(f"Backup created at: {backup}")
