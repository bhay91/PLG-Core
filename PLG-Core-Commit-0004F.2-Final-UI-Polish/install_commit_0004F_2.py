from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
CSS = PROJECT / "static" / "app.css"
BASKET = PROJECT / "templates" / "basket.html"
PYTHON = PROJECT / ".venv" / "bin" / "python"
SOURCE = Path(__file__).resolve().parent / "payload"

for path in (CSS, BASKET, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004F.2-{stamp}"
backup.mkdir(parents=True)

for current in (CSS, BASKET):
    saved = backup / current.relative_to(PROJECT)
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)

def restore() -> None:
    for current in (CSS, BASKET):
        saved = backup / current.relative_to(PROJECT)
        if saved.exists():
            shutil.copy2(saved, current)

try:
    css = CSS.read_text()
    marker = "/* Commit 0004F.2 Final UI Polish */"

    if marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0004F.2.css").read_text()
        CSS.write_text(css)

    basket = BASKET.read_text()

    if "vendor-cart-chevron" not in basket:
        raise RuntimeError(
            "Vendor Quote expand arrow was not found. "
            "Install Commit 0004F.1 first."
        )

    if "final-basket-panel" not in basket:
        raise RuntimeError("Selected Parts panel was not found.")

    final_css = CSS.read_text()
    if marker not in final_css:
        raise RuntimeError("Final UI polish CSS was not installed.")

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
                "print('Final UI polish smoke test passed')"
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
        "Commit 0004F.2 installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004F.2 Final UI Polish installed successfully.")
print("Selected Parts text is larger.")
print("Vendor Quote expand arrow is larger.")
print("Primary gray text is now near-black.")
print(f"Backup created at: {backup}")
