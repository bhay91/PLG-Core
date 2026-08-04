from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess


SOURCE = Path(__file__).resolve().parent / "payload"
PROJECT = Path.home() / "Desktop" / "PLG-Core"
JOBS_TEMPLATE = PROJECT / "templates" / "jobs.html"
CSS_FILE = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (JOBS_TEMPLATE, CSS_FILE, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0003B.3-{stamp}"
backup.mkdir(parents=True)

managed = [
    Path("templates/jobs.html"),
    Path("static/app.css"),
]

for relative in managed:
    current = PROJECT / relative
    saved = backup / relative
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)


def restore() -> None:
    for relative in managed:
        saved = backup / relative
        current = PROJECT / relative
        if saved.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, current)


try:
    shutil.copy2(
        SOURCE / "templates" / "jobs.html",
        JOBS_TEMPLATE,
    )

    css = CSS_FILE.read_text()
    marker = "/* Commit 0003B.3 Jobs Register Refresh */"
    if marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0003B.3.css").read_text()
        CSS_FILE.write_text(css)

    jobs_text = JOBS_TEMPLATE.read_text()
    css_text = CSS_FILE.read_text()

    for marker_text in [
        "Manage customer requests",
        "jobs-filter-controls",
        "job-register-row",
        "verification-bar",
        "No matching jobs",
    ]:
        if marker_text not in jobs_text and marker_text not in css_text:
            raise RuntimeError(f"Jobs refresh marker missing: {marker_text}")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "paths={getattr(r,'path',None) for r in app.routes}; "
                "assert '/jobs' in paths; "
                "print('Jobs register refresh smoke test passed')"
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
        "Commit 0003B.3 installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0003B.3 Jobs Register Refresh installed successfully.")
print("Updated the Jobs page with summary cards, search, status filtering,")
print("modern clickable rows, verification progress, and responsive layout.")
print(f"Backup created at: {backup}")
