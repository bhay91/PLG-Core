from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
SOURCE = Path(__file__).resolve().parent / "payload"

NEW_JOB = PROJECT / "templates" / "new_job.html"
BASKET = PROJECT / "templates" / "basket.html"
LEGACY_APP = PROJECT / "legacy_app.py"
BASKET_ROUTES = PROJECT / "plg_core" / "basket" / "routes.py"
CSS = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"

managed = [NEW_JOB, BASKET, LEGACY_APP, BASKET_ROUTES, CSS]
for path in managed + [PYTHON]:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004D-{stamp}"
backup.mkdir(parents=True)

for current in managed:
    saved = backup / current.relative_to(PROJECT)
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)

def restore() -> None:
    for current in managed:
        saved = backup / current.relative_to(PROJECT)
        if saved.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, current)

try:
    shutil.copy2(SOURCE / "templates" / "new_job.html", NEW_JOB)
    shutil.copy2(SOURCE / "templates" / "basket.html", BASKET)

    app_text = LEGACY_APP.read_text()
    create_start = app_text.find('@app.post("/jobs")')
    create_end = app_text.find('@app.get("/jobs"', create_start)
    if create_start == -1 or create_end == -1:
        raise RuntimeError("Could not locate the create-job route safely.")

    create_block = app_text[create_start:create_end]
    old_redirect = 'return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)'
    new_redirect = 'return RedirectResponse(url=f"/jobs/{job_id}/basket", status_code=303)'

    if old_redirect in create_block:
        create_block = create_block.replace(old_redirect, new_redirect, 1)
        app_text = app_text[:create_start] + create_block + app_text[create_end:]
        LEGACY_APP.write_text(app_text)
    elif new_redirect not in create_block:
        raise RuntimeError("Create-job redirect was not found.")

    routes = BASKET_ROUTES.read_text()
    checkout_marker = '@router.post("/jobs/{job_id}/basket/checkout")'
    if checkout_marker not in routes:
        checkout_route = '\n@router.post("/jobs/{job_id}/basket/checkout")\ndef checkout_basket(job_id: int):\n    basket = get_basket(job_id)\n    if basket["totals"]["selected_items"] < 1:\n        return RedirectResponse(\n            url=f"/jobs/{job_id}/basket",\n            status_code=303,\n        )\n\n    with closing(get_connection()) as connection:\n        stored = get_or_create_basket(connection, job_id)\n        connection.execute(\n            """\n            UPDATE baskets\n            SET status = \'CHECKED_OUT\',\n                updated_at = CURRENT_TIMESTAMP\n            WHERE id = ?\n            """,\n            (stored["id"],),\n        )\n        connection.commit()\n\n    return RedirectResponse(\n        url=f"/jobs/{job_id}/basket",\n        status_code=303,\n    )\n\n'
        insert_at = routes.find('@router.post("/jobs/{job_id}/basket/commit")')
        if insert_at == -1:
            routes += "\n" + checkout_route
        else:
            routes = routes[:insert_at] + checkout_route + routes[insert_at:]
        BASKET_ROUTES.write_text(routes)

    css = CSS.read_text()
    css_marker = "/* Commit 0004D Streamlined Job to Quote Flow */"
    if css_marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0004D.css").read_text()
        CSS.write_text(css)

    new_job_text = NEW_JOB.read_text()
    basket_text = BASKET.read_text()
    app_final = LEGACY_APP.read_text()
    routes_final = BASKET_ROUTES.read_text()

    for marker in ['name="requested_parts"', 'name="notes"', "Parts requested", "Internal notes"]:
        if marker in new_job_text:
            raise RuntimeError(f"Old New Job field is still present: {marker}")

    for marker in ["Create Job &amp; Open Parts Basket", "Checkout Basket", "Create Quote", "CHECKOUT COMPLETE"]:
        if marker not in new_job_text and marker not in basket_text:
            raise RuntimeError(f"Workflow marker missing: {marker}")

    create_start = app_final.find('@app.post("/jobs")')
    create_end = app_final.find('@app.get("/jobs"', create_start)
    if 'url=f"/jobs/{job_id}/basket"' not in app_final[create_start:create_end]:
        raise RuntimeError("New jobs do not redirect to the Parts Basket.")

    if checkout_marker not in routes_final:
        raise RuntimeError("Basket checkout route is missing.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "from plg_core.basket.routes import router; "
                "paths={r.path for r in router.routes}; "
                "assert '/jobs/{job_id}/basket/checkout' in paths; "
                "print('Streamlined workflow smoke test passed')"
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
    raise SystemExit(
        "Commit 0004D installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004D Streamlined Job-to-Quote Flow installed successfully.")
print("New Job now collects customer and equipment information only.")
print("New jobs open directly in the Parts Basket.")
print("Checkout Basket now leads to Create Quote.")
print(f"Backup created at: {backup}")
