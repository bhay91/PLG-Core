from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
CSS = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"
SOURCE = Path(__file__).resolve().parent / "payload"

for path in (CSS, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004F.3-{stamp}"
backup.mkdir(parents=True)

saved = backup / "static" / "app.css"
saved.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(CSS, saved)

try:
    css = CSS.read_text()
    marker = "/* Commit 0004F.3 Global Contrast and Headings */"

    if marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0004F.3.css").read_text()
        CSS.write_text(css)

    final_css = CSS.read_text()

    required = [
        marker,
        "h1 {",
        "h2 {",
        "color: #1f2937;",
    ]
    for value in required:
        if value not in final_css:
            raise RuntimeError(f"Global UI marker missing: {value}")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "assert app is not None; "
                "print('Global contrast and heading smoke test passed')"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )

    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

except Exception as exc:
    shutil.copy2(saved, CSS)
    raise SystemExit(
        "Commit 0004F.3 installation failed and app.css was restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004F.3 Global Contrast and Headings installed successfully.")
print("Body font sizes were preserved.")
print("Headings are larger across the website.")
print("Primary gray text is now near-black across the website.")
print(f"Backup created at: {backup}")
