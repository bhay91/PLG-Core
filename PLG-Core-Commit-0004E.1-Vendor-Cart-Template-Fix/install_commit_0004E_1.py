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
backup = PROJECT / f"backup-commit-0004E.1-{stamp}"
backup.mkdir(parents=True)

saved = backup / "templates" / "basket.html"
saved.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(TEMPLATE, saved)

try:
    text = TEMPLATE.read_text()

    replacements = {
        "vendor_cart.items": "vendor_cart['items']",
        "vendor_cart.source": "vendor_cart['source']",
    }

    changed = 0
    for old, new in replacements.items():
        count = text.count(old)
        if count:
            text = text.replace(old, new)
            changed += count

    if changed == 0:
        raise RuntimeError("No Vendor Cart dictionary expressions were found to update.")

    TEMPLATE.write_text(text)

    updated = TEMPLATE.read_text()

    if "vendor_cart.items" in updated:
        raise RuntimeError("Old vendor_cart.items references are still present.")

    if "vendor_cart.source" in updated:
        raise RuntimeError("Old vendor_cart.source references are still present.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from jinja2 import Environment, FileSystemLoader; "
                "env=Environment(loader=FileSystemLoader('templates')); "
                "env.get_template('basket.html'); "
                "from app import app; "
                "assert app is not None; "
                "print('Vendor Cart template fix smoke test passed')"
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
        "Commit 0004E.1 installation failed and the Basket template was restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004E.1 Vendor Cart Template Fix installed successfully.")
print("Corrected Vendor Cart dictionary rendering in Jinja.")
print(f"Backup created at: {backup}")
