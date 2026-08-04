from pathlib import Path
from datetime import datetime
import re, shutil, subprocess

PROJECT=Path.home()/"Desktop"/"PLG-Core"
SOURCE=Path(__file__).resolve().parent/"payload"
LEGACY=PROJECT/"legacy_app.py"
CSS=PROJECT/"static"/"app.css"
PYTHON=PROJECT/".venv"/"bin"/"python"
TEMPLATES=PROJECT/"templates"
managed=[LEGACY,CSS,TEMPLATES/"customers.html",TEMPLATES/"customer_form.html"]
for p in (LEGACY,CSS,PYTHON,TEMPLATES/"customers.html"):
    if not p.exists(): raise SystemExit(f"Required file not found: {p}")
stamp=datetime.now().strftime("%Y%m%d-%H%M%S")
backup=PROJECT/f"backup-commit-0006C-{stamp}"; backup.mkdir(parents=True)
for p in managed:
    if p.exists():
        dst=backup/p.relative_to(PROJECT); dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,dst)
def restore():
    for p in managed:
        src=backup/p.relative_to(PROJECT)
        if src.exists(): p.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,p)
try:
    text=LEGACY.read_text()
    anchor='        job_columns={row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}\n'
    if 'ALTER TABLE customers ADD COLUMN last_viewed_at TEXT' not in text:
        patch='''        customer_columns={row["name"] for row in connection.execute("PRAGMA table_info(customers)").fetchall()}\n        if "last_viewed_at" not in customer_columns:\n            connection.execute("ALTER TABLE customers ADD COLUMN last_viewed_at TEXT")\n\n'''
        if anchor not in text: raise RuntimeError("Customer schema anchor not found")
        text=text.replace(anchor,patch+anchor,1)

    pattern=re.compile(r'@app\.get\("/customers", response_class=HTMLResponse\).*?(?=\n@app\.get\("/jobs", response_class=HTMLResponse\))',re.S)
    replacement='''@app.get("/customers", response_class=HTMLResponse)
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
        connection.execute("UPDATE customers SET customer_number=? WHERE id=?",(f"PLG-C{customer_id:05d}",customer_id)); connection.commit()
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
        quotes=connection.execute("SELECT quotes.* FROM quotes JOIN jobs ON jobs.id=quotes.job_id WHERE jobs.customer_id=? ORDER BY quotes.id DESC",(customer_id,)).fetchall()
        transactions=connection.execute("SELECT * FROM customer_transactions WHERE customer_id=? ORDER BY transaction_date DESC,id DESC",(customer_id,)).fetchall()
        summary=connection.execute("SELECT COALESCE(SUM(amount),0) AS net_balance,COALESCE(SUM(CASE WHEN transaction_type='PAYMENT' THEN amount ELSE 0 END),0) AS total_payments FROM customer_transactions WHERE customer_id=?",(customer_id,)).fetchone(); connection.commit()
        net=float(summary["net_balance"] or 0); account={"available_credit":max(net,0),"outstanding_balance":max(-net,0),"total_payments":float(summary["total_payments"] or 0)}
    return templates.TemplateResponse(request=request,name="customer_account.html",context={"customer":customer,"jobs":jobs,"quotes":quotes,"transactions":transactions,"account":account,"active_page":"customers"})

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

'''
    text,n=pattern.subn(replacement,text,count=1)
    if n!=1: raise RuntimeError("Customer route block not replaced")
    LEGACY.write_text(text)
    shutil.copy2(SOURCE/"templates"/"customers.html",TEMPLATES/"customers.html")
    shutil.copy2(SOURCE/"templates"/"customer_form.html",TEMPLATES/"customer_form.html")
    css=CSS.read_text(); patch=(SOURCE/"static"/"commit-0006C.css").read_text()
    if "/* Commit 0006C Customer Management */" not in css: CSS.write_text(css+"\n"+patch)
    smoke=subprocess.run([str(PYTHON),"-c","from legacy_app import initialize_database; initialize_database(); from app import app; p={getattr(r,'path',None) for r in app.routes}; assert '/customers/new' in p; assert '/customers/{customer_id}/edit' in p; assert '/customers/{customer_id}/deactivate' in p; assert '/customers/{customer_id}/reactivate' in p; print('ok')"],cwd=PROJECT,text=True,capture_output=True)
    if smoke.returncode!=0: raise RuntimeError(smoke.stdout+smoke.stderr)
except Exception as exc:
    restore(); raise SystemExit("Commit 0006C failed and files were restored.\n"+str(exc))
print("PLG Commit 0006C Customer Management installed successfully.")
print("Add, edit, deactivate, reactivate, quick search, duplicate detection, and Recent Customers are active.")
print(f"Backup created at: {backup}")
