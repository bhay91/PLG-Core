from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess


SOURCE = Path(__file__).resolve().parent / "payload"
PROJECT = Path.home() / "Desktop" / "PLG-Core"
TEMPLATES = PROJECT / "templates"
STATIC = PROJECT / "static"
DASHBOARD = TEMPLATES / "dashboard.html"
JOB = TEMPLATES / "job_detail.html"
CSS = STATIC / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (DASHBOARD, JOB, CSS, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0003B.2-{stamp}"
backup.mkdir(parents=True)

for current in (DASHBOARD, JOB, CSS):
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
    # Remove the duplicate New Job button from the Dashboard heading.
    dashboard = DASHBOARD.read_text()
    dashboard, removed_new_job = re.subn(
        r'\s*<a\s+class="button"\s+href="/jobs/new">\s*[＋+]?\s*New\s+Job\s*</a>',
        "",
        dashboard,
        count=1,
        flags=re.IGNORECASE,
    )
    DASHBOARD.write_text(dashboard)

    # Remove duplicate Parts Basket and Edit Job Information actions from
    # the very top Job heading. Keep the status selector and Update button.
    job = JOB.read_text()

    first_section_end = job.find("</section>")
    if first_section_end == -1:
        raise RuntimeError("Could not locate the Job heading section.")

    heading = job[: first_section_end + len("</section>")]
    remainder = job[first_section_end + len("</section>") :]

    heading = re.sub(
        r'\s*<a\s+class="button[^"]*"\s+href="/jobs/\{\{\s*job\.id\s*\}\}/basket">\s*(?:Open\s+)?Parts\s+Basket\s*</a>',
        "",
        heading,
        flags=re.IGNORECASE,
    )
    heading = re.sub(
        r'\s*<a\s+class="button[^"]*"\s+href="/jobs/\{\{\s*job\.id\s*\}\}/edit">\s*Edit\s+Job\s+Information\s*</a>',
        "",
        heading,
        flags=re.IGNORECASE,
    )
    heading = heading.replace(
        '<div class="header-actions">',
        '<div class="header-actions job-top-actions-clean">',
        1,
    )
    job = heading + remainder

    # Add compact classes to the stacked operational panels.
    panel_labels = {
        "IMPORT SUPPLIER CART OR QUOTE": "job-compact-panel",
        "SOURCE IMPORTS": "job-compact-panel",
        "QUOTE": "job-compact-panel",
        "QUOTE READINESS": "job-status-strip",
    }

    for label, extra_class in panel_labels.items():
        pattern = re.compile(
            rf'<section\s+class="panel">(?=(?:(?!</section>).)*<p\s+class="eyebrow">\s*{re.escape(label)}\s*</p>)',
            re.IGNORECASE | re.DOTALL,
        )
        job = pattern.sub(
            f'<section class="panel {extra_class}">',
            job,
            count=1,
        )

    JOB.write_text(job)

    css = CSS.read_text()
    marker = "/* Commit 0003B.2 Workspace Cleanup */"
    if marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0003B.2.css").read_text()
        CSS.write_text(css)

    # Validation
    dashboard_final = DASHBOARD.read_text()
    job_final = JOB.read_text()
    css_final = CSS.read_text()

    heading_final = job_final[: job_final.find("</section>") + len("</section>")]

    if re.search(
        r'<a[^>]+href="/jobs/new"[^>]*>\s*[＋+]?\s*New\s+Job\s*</a>',
        dashboard_final,
        re.IGNORECASE,
    ):
        raise RuntimeError("The duplicate Dashboard New Job button is still present.")

    if "/basket" in heading_final or "/edit" in heading_final:
        raise RuntimeError("Duplicate Job heading actions are still present.")

    if "Open Parts Basket" not in job_final:
        raise RuntimeError("The main Open Parts Basket action was removed unexpectedly.")

    if marker not in css_final:
        raise RuntimeError("Workspace cleanup CSS was not installed.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "assert app is not None; "
                "print('Workspace cleanup smoke test passed')"
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
        "Commit 0003B.2 installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0003B.2 Workspace Cleanup installed successfully.")
print("Removed the duplicate Dashboard New Job action.")
print("Removed duplicate Job heading actions.")
print("Kept the main Parts Basket workspace action.")
print("Tightened the operational panels for less scrolling.")
print(f"Backup created at: {backup}")
