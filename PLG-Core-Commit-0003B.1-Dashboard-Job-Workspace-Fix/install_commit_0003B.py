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

required = [
    TEMPLATES / "dashboard.html",
    TEMPLATES / "job_detail.html",
    STATIC / "app.css",
    PROJECT / ".venv" / "bin" / "python",
]
for path in required:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0003B-{stamp}"
backup.mkdir(parents=True)

managed = [
    Path("templates/dashboard.html"),
    Path("templates/job_detail.html"),
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
    shutil.copy2(SOURCE / "templates" / "dashboard.html", TEMPLATES / "dashboard.html")

    job_file = TEMPLATES / "job_detail.html"
    job_text = job_file.read_text()
    insert = (SOURCE / "templates" / "job_workspace_insert.html").read_text()

    if "job-workspace-nav" not in job_text:
        heading_end = job_text.find("</section>")
        if heading_end == -1:
            raise RuntimeError("Could not locate the Job Details heading.")
        heading_end += len("</section>")
        job_text = job_text[:heading_end] + "\n" + insert + job_text[heading_end:]

    detail_pattern = re.compile(
        r'<section\s+class="detail-grid">.*?<p\s+class="eyebrow">\s*JOB\s+INFORMATION\s*</p>.*?</section>',
        re.IGNORECASE | re.DOTALL,
    )
    job_text = detail_pattern.sub("", job_text, count=1)
    job_file.write_text(job_text)

    css_file = STATIC / "app.css"
    css_text = css_file.read_text()
    marker = "/* Commit 0003B Dashboard and Job Workspace */"
    if marker not in css_text:
        css_text += "\n" + (SOURCE / "static" / "commit-0003B.css").read_text()
        css_file.write_text(css_text)

    dashboard_text = (TEMPLATES / "dashboard.html").read_text()
    final_job_text = job_file.read_text()

    for marker_text in ["Continue where you left off", "RECENT JOBS", "PLG WORKFLOW"]:
        if marker_text not in dashboard_text:
            raise RuntimeError(f"Dashboard marker missing: {marker_text}")

    for marker_text in ["job-workspace-nav", "Open Parts Basket", "job-summary-grid"]:
        if marker_text not in final_job_text:
            raise RuntimeError(f"Job workspace marker missing: {marker_text}")

    python = PROJECT / ".venv" / "bin" / "python"
    smoke = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from app import app; "
                "from plg_core.basket.routes import router as basket_router; "
                "app_paths={getattr(r,'path',None) for r in app.routes}; "
                "basket_paths={r.path for r in basket_router.routes}; "
                "assert '/' in app_paths; "
                "assert '/jobs/{job_id}' in app_paths; "
                "assert '/jobs/{job_id}/basket' in basket_paths; "
                "print('Dashboard and job workspace smoke test passed')"
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
        "Commit 0003B installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0003B.1 Dashboard and Job Workspace installed successfully.")
print("Added optimized Dashboard, Recent Jobs, summary cards, workflow panel,")
print("Job tabs, Job summary cards, and Open Parts Basket action.")
print(f"Backup created at: {backup}")
