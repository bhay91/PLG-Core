from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess


PROJECT = Path.home() / "Desktop" / "PLG-Core"
TEMPLATE = PROJECT / "templates" / "basket.html"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (TEMPLATE, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004A.1-{stamp}"
backup.mkdir(parents=True)

saved = backup / "templates" / "basket.html"
saved.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(TEMPLATE, saved)

try:
    text = TEMPLATE.read_text()

    if "{% for item in basket.items %}" not in text:
        raise RuntimeError("Expected Basket loop was not found.")

    text = text.replace(
        "{% for item in basket.items %}",
        "{% for item in basket['items'] %}",
    )

    TEMPLATE.write_text(text)

    updated = TEMPLATE.read_text()
    if "{% for item in basket['items'] %}" not in updated:
        raise RuntimeError("Basket items loop was not corrected.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "from jinja2 import Environment, FileSystemLoader; "
                "env=Environment(loader=FileSystemLoader('templates')); "
                "env.get_template('basket.html'); "
                "assert app is not None; "
                "print('Basket template fix smoke test passed')"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )

    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

except Exception as exc:
    shutil.copy2(saved, TEMPLATE)
    raise SystemExit(
        "Commit 0004A.1 installation failed and the template was restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004A.1 Basket Template Fix installed successfully.")
print("Corrected the Basket item loop for Jinja dictionary rendering.")
print(f"Backup created at: {backup}")
