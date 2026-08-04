from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
BASKET = PROJECT / "templates" / "basket.html"
DASHBOARD = PROJECT / "templates" / "dashboard.html"
JOBS = PROJECT / "templates" / "jobs.html"
ROUTES = PROJECT / "plg_core" / "basket" / "routes.py"
CSS = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"
SOURCE = Path(__file__).resolve().parent / "payload"

for path in (BASKET, DASHBOARD, JOBS, ROUTES, CSS, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004B-{stamp}"
backup.mkdir(parents=True)

managed = [BASKET, DASHBOARD, JOBS, ROUTES, CSS]
for current in managed:
    saved = backup / current.relative_to(PROJECT)
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)

def restore() -> None:
    for saved in backup.rglob("*"):
        if saved.is_file():
            target = PROJECT / saved.relative_to(backup)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)

try:
    basket = BASKET.read_text()

    heading_pattern = re.compile(
        r'<section\s+class="basket-page-heading">.*?</section>',
        re.IGNORECASE | re.DOTALL,
    )

    merged_heading = '<section class="merged-workspace-heading">\n  <div>\n    <p class="eyebrow">{{ job.job_number }} · JOB WORKSPACE</p>\n    <h1>{{ job.customer }}</h1>\n    <p>{{ job.manufacturer }} {{ job.machine }}{% if job.pin_serial %} · {{ job.pin_serial }}{% endif %}</p>\n  </div>\n\n  <div class="merged-workspace-actions">\n    <a class="button ghost" href="/jobs/{{ job.id }}/edit">Edit Job Information</a>\n    <form method="post" action="/jobs/{{ job.id }}/status" class="status-form">\n      <select name="status">\n        {% for value in ["REQUESTED", "RESEARCHING", "VERIFIED", "QUOTED", "CONFIRMED", "ORDERED", "RECEIVED", "DELIVERED", "VOID"] %}\n        <option value="{{ value }}" {% if job.status == value %}selected{% endif %}>{{ value }}</option>\n        {% endfor %}\n      </select>\n      <button class="button small" type="submit">Update</button>\n    </form>\n  </div>\n</section>\n\n<section class="job-workspace-nav merged-job-nav">\n  <a class="active" href="/jobs/{{ job.id }}/basket">Overview &amp; Basket</a>\n  <span>Timeline</span>\n  <span>Documents</span>\n  <span>Notes</span>\n</section>\n\n<section class="merged-workspace-notice">\n  <div>\n    <strong>The Parts Basket is now the Job workspace.</strong>\n    <span>Add parts, compare pricing, review totals, and prepare the quote without opening a separate page.</span>\n  </div>\n</section>'
    basket, count = heading_pattern.subn(merged_heading, basket, count=1)
    if count != 1:
        raise RuntimeError("Could not replace the Basket page heading safely.")
    BASKET.write_text(basket)

    for template in (DASHBOARD, JOBS):
        text = template.read_text()
        text = re.sub(
            r'href="/jobs/\{\{\s*job\.id\s*\}\}"',
            'href="/jobs/{{ job.id }}/basket"',
            text,
        )
        template.write_text(text)

    routes = ROUTES.read_text()
    routes = routes.replace(
        'return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)',
        'return RedirectResponse(url=f"/jobs/{job_id}/basket", status_code=303)',
    )
    ROUTES.write_text(routes)

    css = CSS.read_text()
    marker = "/* Commit 0004B Merged Job and Basket Workspace */"
    if marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0004B.css").read_text()
        CSS.write_text(css)

    basket_final = BASKET.read_text()
    dashboard_final = DASHBOARD.read_text()
    jobs_final = JOBS.read_text()
    routes_final = ROUTES.read_text()
    css_final = CSS.read_text()

    for value in [
        "JOB WORKSPACE",
        "Overview &amp; Basket",
        "The Parts Basket is now the Job workspace.",
        "Edit Job Information",
    ]:
        if value not in basket_final:
            raise RuntimeError(f"Merged workspace marker missing: {value}")

    if 'href="/jobs/{{ job.id }}/basket"' not in dashboard_final:
        raise RuntimeError("Dashboard Recent Jobs did not switch to the merged workspace.")

    if 'href="/jobs/{{ job.id }}/basket"' not in jobs_final:
        raise RuntimeError("Jobs Register did not switch to the merged workspace.")

    if 'url=f"/jobs/{job_id}/basket"' not in routes_final:
        raise RuntimeError("Basket commit redirect was not updated.")

    if marker not in css_final:
        raise RuntimeError("Merged workspace CSS was not installed.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "from plg_core.basket.routes import router; "
                "paths={r.path for r in router.routes}; "
                "assert '/jobs/{job_id}/basket' in paths; "
                "print('Merged Job and Basket workspace smoke test passed')"
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
        "Commit 0004B installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004B Merged Job and Basket Workspace installed successfully.")
print("Opening a Job now goes directly to the unified Overview and Basket workspace.")
print("The separate Basket click is removed from the normal workflow.")
print(f"Backup created at: {backup}")
