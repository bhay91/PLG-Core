from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parent / "payload"
PROJECT = Path.home() / "Desktop" / "PLG-Core"
JOB_TEMPLATE = PROJECT / "templates" / "job_detail.html"
CSS_FILE = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (JOB_TEMPLATE, CSS_FILE, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0003B.4-{stamp}"
backup.mkdir(parents=True)

for current in (JOB_TEMPLATE, CSS_FILE):
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
    job = JOB_TEMPLATE.read_text()

    pattern = re.compile(
        r'<div\s+class="panel-heading">\s*'
        r'<div><p\s+class="eyebrow">\s*REQUESTED\s+PARTS\s*</p>'
        r'<h2>.*?</h2></div>\s*</div>',
        re.IGNORECASE | re.DOTALL,
    )

    replacement = (
        '<div class="panel-heading parts-workspace-heading">\n'
        '    <div>\n'
        '      <p class="eyebrow">PARTS SOURCING WORKSPACE</p>\n'
        '      <h2>{{ parts|length }} item{% if parts|length != 1 %}s{% endif %}</h2>\n'
        '      <p>Review OEM identification, supplier options, pricing, and the selected source for each part.</p>\n'
        '    </div>\n'
        '  </div>'
    )

    job, count = pattern.subn(replacement, job, count=1)
    if count == 0 and "PARTS SOURCING WORKSPACE" not in job:
        raise RuntimeError("Could not update the Requested Parts heading safely.")

    JOB_TEMPLATE.write_text(job)

    css = CSS_FILE.read_text()
    marker = "/* Commit 0003B.4 Parts Workspace Refresh */"
    if marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0003B.4.css").read_text()
        CSS_FILE.write_text(css)

    final_job = JOB_TEMPLATE.read_text()
    final_css = CSS_FILE.read_text()

    for marker_text in [
        "PARTS SOURCING WORKSPACE",
        "Review OEM identification",
        "source-option.selected",
    ]:
        if marker_text not in final_job and marker_text not in final_css:
            raise RuntimeError(f"Parts workspace marker missing: {marker_text}")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            "from app import app; assert app is not None; print('Parts workspace refresh smoke test passed')",
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
        "Commit 0003B.4 installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0003B.4 Parts Workspace Refresh installed successfully.")
print("Updated the requested-parts section with cleaner OEM cards,")
print("modern supplier options, stronger price emphasis, and improved forms.")
print(f"Backup created at: {backup}")
