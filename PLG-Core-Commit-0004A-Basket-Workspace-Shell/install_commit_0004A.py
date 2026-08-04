from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess


SOURCE = Path(__file__).resolve().parent / "payload"
PROJECT = Path.home() / "Desktop" / "PLG-Core"
BASKET_TEMPLATE = PROJECT / "templates" / "basket.html"
CSS_FILE = PROJECT / "static" / "app.css"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (BASKET_TEMPLATE, CSS_FILE, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004A-{stamp}"
backup.mkdir(parents=True)

for current in (BASKET_TEMPLATE, CSS_FILE):
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
    shutil.copy2(
        SOURCE / "templates" / "basket.html",
        BASKET_TEMPLATE,
    )

    css = CSS_FILE.read_text()
    marker = "/* Commit 0004A Parts Basket Workspace Shell */"
    if marker not in css:
        css += "\n" + (SOURCE / "static" / "commit-0004A.css").read_text()
        CSS_FILE.write_text(css)

    basket_text = BASKET_TEMPLATE.read_text()
    css_text = CSS_FILE.read_text()

    for marker_text in [
        "Compare supplier pricing",
        "Supplier Price",
        "Customer Total",
        "Add Used Part",
        "Commit Selected to Job",
    ]:
        if marker_text not in basket_text:
            raise RuntimeError(f"Basket workspace marker missing: {marker_text}")

    if marker not in css_text:
        raise RuntimeError("Basket workspace CSS marker is missing.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "from plg_core.basket.routes import router; "
                "paths={r.path for r in router.routes}; "
                "assert '/jobs/{job_id}/basket' in paths; "
                "assert '/jobs/{job_id}/basket/items' in paths; "
                "assert '/jobs/{job_id}/basket/commit' in paths; "
                "print('Basket workspace shell smoke test passed')"
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
        "Commit 0004A installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004A Parts Basket Workspace Shell installed successfully.")
print("Added the approved table-style Basket, totals bar, selection controls,")
print("manual Add Part, dedicated Add Used Part, and commit action.")
print(f"Backup created at: {backup}")
