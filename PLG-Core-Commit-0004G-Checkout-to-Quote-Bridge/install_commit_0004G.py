from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
ROUTES = PROJECT / "plg_core" / "basket" / "routes.py"
SERVICE = PROJECT / "plg_core" / "basket" / "service.py"
BASKET = PROJECT / "templates" / "basket.html"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (ROUTES, SERVICE, BASKET, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004G-{stamp}"
backup.mkdir(parents=True)

managed = [ROUTES, SERVICE, BASKET]
for current in managed:
    saved = backup / current.relative_to(PROJECT)
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)

def restore():
    for current in managed:
        saved = backup / current.relative_to(PROJECT)
        if saved.exists():
            shutil.copy2(saved, current)

try:
    service = SERVICE.read_text()
    start = service.find("def commit_basket(job_id: int):")
    if start == -1:
        raise RuntimeError("commit_basket() not found")

    block = service[start:start+800]
    if 'if basket["status"] == "COMMITTED":' not in block:
        anchor = '        basket = get_or_create_basket(connection, job_id)\n'
        guard = '''        basket = get_or_create_basket(connection, job_id)

        if basket["status"] == "COMMITTED":
            return {
                "ok": True,
                "job_id": job_id,
                "created_parts": 0,
                "already_committed": True,
            }
'''
        if anchor not in block:
            raise RuntimeError("commit_basket initialization not found")
        service = service[:start] + service[start:].replace(anchor, guard, 1)
        SERVICE.write_text(service)

    routes = ROUTES.read_text()
    pattern = re.compile(
        r'@router\.post\("/jobs/\{job_id\}/basket/checkout"\)\s*'
        r'def checkout_basket\(job_id: int\):.*?'
        r'(?=\n@router\.post|\Z)',
        re.DOTALL,
    )
    replacement = '''@router.post("/jobs/{job_id}/basket/checkout")
def checkout_basket(job_id: int):
    commit_basket(job_id)
    return RedirectResponse(
        url=f"/jobs/{job_id}/basket",
        status_code=303,
    )

'''
    routes, count = pattern.subn(replacement, routes, count=1)
    if count != 1:
        raise RuntimeError("Checkout route not found")
    ROUTES.write_text(routes)

    basket = BASKET.read_text()
    basket = basket.replace(
        'basket.status == "CHECKED_OUT"',
        'basket.status in ["CHECKED_OUT", "COMMITTED"]',
    )
    BASKET.write_text(basket)

    smoke = subprocess.run(
        [str(PYTHON), "-c",
         "from app import app; "
         "from plg_core.basket.routes import router; "
         "assert any(r.path == '/jobs/{job_id}/basket/checkout' for r in router.routes); "
         "print('ok')"],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

except Exception as exc:
    restore()
    raise SystemExit(
        "Commit 0004G installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004G Checkout-to-Quote Bridge installed successfully.")
print("Checkout Basket now commits selected Basket lines into job_parts and part_sources.")
print("Repeated checkout will not duplicate committed parts.")
print("Create Quote can now read the checked-out Basket.")
print(f"Backup created at: {backup}")
