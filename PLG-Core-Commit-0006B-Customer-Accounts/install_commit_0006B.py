from pathlib import Path
from datetime import datetime
import re
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
SOURCE = Path(__file__).resolve().parent / "payload"
LEGACY = PROJECT / "legacy_app.py"
TEMPLATE = PROJECT / "templates" / "customer_account.html"
CSS = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (LEGACY, TEMPLATE, CSS, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

managed = [LEGACY, TEMPLATE, CSS]
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0006B-{stamp}"
backup.mkdir(parents=True)

for path in managed:
    saved = backup / path.relative_to(PROJECT)
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, saved)

def restore():
    for path in managed:
        saved = backup / path.relative_to(PROJECT)
        if saved.exists():
            shutil.copy2(saved, path)

try:
    text = LEGACY.read_text()

    schema_block = '        connection.execute(\n            """\n            CREATE TABLE IF NOT EXISTS customer_transactions (\n                id INTEGER PRIMARY KEY AUTOINCREMENT,\n                customer_id INTEGER NOT NULL,\n                transaction_date TEXT NOT NULL,\n                transaction_type TEXT NOT NULL,\n                amount REAL NOT NULL,\n                payment_method TEXT DEFAULT \'\',\n                reference TEXT DEFAULT \'\',\n                reason TEXT DEFAULT \'\',\n                job_id INTEGER,\n                quote_id INTEGER,\n                invoice_id INTEGER,\n                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,\n                FOREIGN KEY (customer_id) REFERENCES customers(id)\n            )\n            """\n        )\n\n'
    if "CREATE TABLE IF NOT EXISTS customer_transactions (" not in text:
        anchor = "        connection.commit()\n\n\n"
        position = text.find(anchor)
        if position == -1:
            raise RuntimeError("Database commit anchor was not found.")
        text = text[:position] + schema_block + text[position:]

    route_pattern = re.compile(
        r'@app\.get\("/customers/\{customer_id\}", response_class=HTMLResponse\)\s*'
        r'def customer_account\(request: Request, customer_id: int\):.*?'
        r'(?=\n@app\.get\("/jobs", response_class=HTMLResponse\))',
        re.S,
    )
    route_replacement = '@app.get("/customers/{customer_id}", response_class=HTMLResponse)\ndef customer_account(request: Request, customer_id: int):\n    with closing(get_connection()) as connection:\n        customer = connection.execute(\n            "SELECT * FROM customers WHERE id=?",\n            (customer_id,),\n        ).fetchone()\n        if customer is None:\n            raise HTTPException(status_code=404, detail="Customer not found.")\n\n        jobs = connection.execute(\n            "SELECT * FROM jobs WHERE customer_id=? ORDER BY id DESC",\n            (customer_id,),\n        ).fetchall()\n\n        quotes = connection.execute(\n            """\n            SELECT quotes.*\n            FROM quotes\n            JOIN jobs ON jobs.id=quotes.job_id\n            WHERE jobs.customer_id=?\n            ORDER BY quotes.id DESC\n            """,\n            (customer_id,),\n        ).fetchall()\n\n        transactions = connection.execute(\n            """\n            SELECT *\n            FROM customer_transactions\n            WHERE customer_id=?\n            ORDER BY transaction_date DESC, id DESC\n            """,\n            (customer_id,),\n        ).fetchall()\n\n        summary = connection.execute(\n            """\n            SELECT\n                COALESCE(SUM(amount), 0) AS net_balance,\n                COALESCE(SUM(\n                    CASE WHEN transaction_type=\'PAYMENT\' THEN amount ELSE 0 END\n                ), 0) AS total_payments\n            FROM customer_transactions\n            WHERE customer_id=?\n            """,\n            (customer_id,),\n        ).fetchone()\n\n        net_balance = float(summary["net_balance"] or 0)\n        account = {\n            "available_credit": max(net_balance, 0),\n            "outstanding_balance": max(-net_balance, 0),\n            "total_payments": float(summary["total_payments"] or 0),\n        }\n\n    return templates.TemplateResponse(\n        request=request,\n        name="customer_account.html",\n        context={\n            "customer": customer,\n            "jobs": jobs,\n            "quotes": quotes,\n            "transactions": transactions,\n            "account": account,\n            "active_page": "customers",\n        },\n    )\n\n\n@app.post("/customers/{customer_id}/transactions/payment")\ndef record_customer_payment(\n    customer_id: int,\n    amount: Annotated[float, Form()],\n    payment_method: Annotated[str, Form()],\n    reference: Annotated[str, Form()] = "",\n):\n    if amount <= 0:\n        raise HTTPException(status_code=400, detail="Payment amount must be greater than zero.")\n\n    with closing(get_connection()) as connection:\n        customer = connection.execute(\n            "SELECT id FROM customers WHERE id=?",\n            (customer_id,),\n        ).fetchone()\n        if customer is None:\n            raise HTTPException(status_code=404, detail="Customer not found.")\n\n        connection.execute(\n            """\n            INSERT INTO customer_transactions (\n                customer_id, transaction_date, transaction_type,\n                amount, payment_method, reference\n            )\n            VALUES (?, ?, \'PAYMENT\', ?, ?, ?)\n            """,\n            (\n                customer_id,\n                date.today().isoformat(),\n                round(float(amount), 2),\n                payment_method.strip().upper(),\n                reference.strip(),\n            ),\n        )\n        connection.commit()\n\n    return RedirectResponse(url=f"/customers/{customer_id}", status_code=303)\n\n\n@app.post("/customers/{customer_id}/transactions/refund")\ndef record_customer_refund(\n    customer_id: int,\n    amount: Annotated[float, Form()],\n    reason: Annotated[str, Form()],\n    reference: Annotated[str, Form()] = "",\n):\n    if amount <= 0:\n        raise HTTPException(status_code=400, detail="Refund amount must be greater than zero.")\n    if not reason.strip():\n        raise HTTPException(status_code=400, detail="Refund reason is required.")\n\n    with closing(get_connection()) as connection:\n        customer = connection.execute(\n            "SELECT id FROM customers WHERE id=?",\n            (customer_id,),\n        ).fetchone()\n        if customer is None:\n            raise HTTPException(status_code=404, detail="Customer not found.")\n\n        connection.execute(\n            """\n            INSERT INTO customer_transactions (\n                customer_id, transaction_date, transaction_type,\n                amount, reference, reason\n            )\n            VALUES (?, ?, \'REFUND\', ?, ?, ?)\n            """,\n            (\n                customer_id,\n                date.today().isoformat(),\n                -round(float(amount), 2),\n                reference.strip(),\n                reason.strip(),\n            ),\n        )\n        connection.commit()\n\n    return RedirectResponse(url=f"/customers/{customer_id}", status_code=303)\n\n\n@app.post("/customers/{customer_id}/transactions/adjustment")\ndef record_customer_adjustment(\n    customer_id: int,\n    amount: Annotated[float, Form()],\n    reason: Annotated[str, Form()],\n    reference: Annotated[str, Form()] = "",\n):\n    if amount == 0:\n        raise HTTPException(status_code=400, detail="Adjustment amount cannot be zero.")\n    if not reason.strip():\n        raise HTTPException(status_code=400, detail="Adjustment reason is required.")\n\n    with closing(get_connection()) as connection:\n        customer = connection.execute(\n            "SELECT id FROM customers WHERE id=?",\n            (customer_id,),\n        ).fetchone()\n        if customer is None:\n            raise HTTPException(status_code=404, detail="Customer not found.")\n\n        connection.execute(\n            """\n            INSERT INTO customer_transactions (\n                customer_id, transaction_date, transaction_type,\n                amount, reference, reason\n            )\n            VALUES (?, ?, \'ADJUSTMENT\', ?, ?, ?)\n            """,\n            (\n                customer_id,\n                date.today().isoformat(),\n                round(float(amount), 2),\n                reference.strip(),\n                reason.strip(),\n            ),\n        )\n        connection.commit()\n\n    return RedirectResponse(url=f"/customers/{customer_id}", status_code=303)\n\n\n'
    text, count = route_pattern.subn(route_replacement, text, count=1)
    if count != 1:
        raise RuntimeError("Customer account route block was not found.")

    LEGACY.write_text(text)
    shutil.copy2(SOURCE / "templates" / "customer_account.html", TEMPLATE)

    css_text = CSS.read_text()
    css_patch = (SOURCE / "static" / "commit-0006B.css").read_text()
    if "/* Commit 0006B Customer Accounts */" not in css_text:
        CSS.write_text(css_text + "\n" + css_patch)

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from legacy_app import initialize_database; initialize_database(); "
                "from app import app; "
                "paths={getattr(r,'path',None) for r in app.routes}; "
                "assert '/customers/{customer_id}/transactions/payment' in paths; "
                "assert '/customers/{customer_id}/transactions/refund' in paths; "
                "assert '/customers/{customer_id}/transactions/adjustment' in paths; "
                "from jinja2 import Environment, FileSystemLoader; "
                "Environment(loader=FileSystemLoader('templates')).get_template('customer_account.html'); "
                "print('Customer Accounts smoke test passed')"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

    schema_check = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from legacy_app import get_connection; "
                "c=get_connection(); "
                "tables={r['name'] for r in c.execute("
                "\"SELECT name FROM sqlite_master WHERE type='table'\""
                ")}; "
                "assert 'customer_transactions' in tables; "
                "c.close(); print('Customer transaction table verified')"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if schema_check.returncode != 0:
        raise RuntimeError(schema_check.stdout + schema_check.stderr)

except Exception as exc:
    restore()
    raise SystemExit(
        "Commit 0006B installation failed and files were restored.\n" + str(exc)
    )

print("PLG Commit 0006B Customer Accounts installed successfully.")
print("Payments, refunds, adjustments, available credit, and outstanding balance are active.")
print(f"Backup created at: {backup}")
