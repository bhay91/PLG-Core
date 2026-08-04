from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess


PROJECT = Path.home() / "Desktop" / "PLG-Core"
TEMPLATES = PROJECT / "templates"
JOB_TEMPLATE = TEMPLATES / "job_detail.html"
BASE_TEMPLATE = TEMPLATES / "base.html"
PYTHON = PROJECT / ".venv" / "bin" / "python"

required = [JOB_TEMPLATE, BASE_TEMPLATE, PYTHON]
for path in required:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0003A.2-{stamp}"
backup.mkdir(parents=True)

managed = list(TEMPLATES.glob("*.html"))
for current in managed:
    saved = backup / "templates" / current.name
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)


def restore() -> None:
    for saved in (backup / "templates").glob("*.html"):
        shutil.copy2(saved, TEMPLATES / saved.name)


try:
    replacements = {
        "Parts sourcing control center": "Parts Sourcing Control Center",
        "PARTS SOURCING CONTROL CENTER": "Parts Sourcing Control Center",
    }

    for template in TEMPLATES.glob("*.html"):
        text = template.read_text()
        for old, new in replacements.items():
            text = text.replace(old, new)
        template.write_text(text)

    text = JOB_TEMPLATE.read_text()

    old_panel_pattern = re.compile(
        r'''\s*<article\s+class="panel">\s*
        <p\s+class="eyebrow">\s*ADD\s+MORE\s+PARTS\s*</p>\s*
        <form\s+method="post"\s+action="/jobs/\{\{\s*job\.id\s*\}\}/parts">\s*
        <label>\s*Requested\s+description
        <input[^>]*name="requested_description"[^>]*>
        </label>\s*
        <label>\s*Quantity
        <input[^>]*name="quantity"[^>]*>
        </label>\s*
        <button[^>]*>\s*Add\s+to\s+Job\s*</button>\s*
        </form>\s*
        </article>\s*''',
        re.IGNORECASE | re.VERBOSE | re.DOTALL,
    )

    updated, count = old_panel_pattern.subn("\n", text, count=1)

    if count == 0:
        start_marker = '<p class="eyebrow">ADD MORE PARTS</p>'
        start = text.find(start_marker)
        if start != -1:
            article_start = text.rfind('<article class="panel">', 0, start)
            article_end = text.find('</article>', start)
            if article_start != -1 and article_end != -1:
                updated = (
                    text[:article_start]
                    + "\n"
                    + text[article_end + len("</article>"):]
                )
                count = 1

    if count != 1:
        raise RuntimeError(
            "The old Add More Parts panel could not be identified safely."
        )

    JOB_TEMPLATE.write_text(updated)

    all_template_text = "\n".join(
        path.read_text() for path in TEMPLATES.glob("*.html")
    )

    forbidden = [
        "Parts sourcing control center",
        ">ADD MORE PARTS<",
        ">Add to Job<",
        'name="requested_description" required placeholder="Water pump"',
    ]
    for marker in forbidden:
        if marker in all_template_text:
            raise RuntimeError(f"Old interface text is still present: {marker}")

    if "Parts Sourcing Control Center" not in BASE_TEMPLATE.read_text():
        raise RuntimeError("The corrected header subtitle is missing.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "assert app is not None; "
                "print('Workflow cleanup smoke test passed')"
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
        "Commit 0003A.2 installation failed and templates were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0003A.2 Workflow Cleanup installed successfully.")
print("Updated subtitle: Parts Sourcing Control Center.")
print("Removed Add More Parts from the Job Details page.")
print("Removed the old Add to Job workflow from the webpage.")
print("Used-part entry will be added inside the Parts Basket.")
print(f"Backup created at: {backup}")
