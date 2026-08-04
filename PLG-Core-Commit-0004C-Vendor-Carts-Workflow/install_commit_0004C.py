from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
SOURCE = Path(__file__).resolve().parent / "payload"
PATCH_SERVICE = Path(__file__).resolve().parent / "patch_service.py"
PYTHON = PROJECT / ".venv" / "bin" / "python"

managed = [
    Path("plg_core/basket/routes.py"),
    Path("plg_core/basket/service.py"),
    Path("templates/basket.html"),
    Path("static/app.css"),
]

for relative in managed:
    path = PROJECT / relative
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004C-{stamp}"
backup.mkdir(parents=True)

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
        SOURCE / "plg_core" / "basket" / "routes.py",
        PROJECT / "plg_core" / "basket" / "routes.py",
    )
    shutil.copy2(
        SOURCE / "templates" / "basket.html",
        PROJECT / "templates" / "basket.html",
    )

    patch = subprocess.run(
        [str(PYTHON), str(PATCH_SERVICE)],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if patch.returncode != 0:
        raise RuntimeError(patch.stdout + patch.stderr)

    css_file = PROJECT / "static" / "app.css"
    css = css_file.read_text()
    marker = "/* Commit 0004C Vendor Carts Workflow */"
    if marker not in css:
        css += "\n" + (
            SOURCE / "static" / "commit-0004C.css"
        ).read_text()
        css_file.write_text(css)

    template = (PROJECT / "templates" / "basket.html").read_text()
    routes = (PROJECT / "plg_core" / "basket" / "routes.py").read_text()
    service = (PROJECT / "plg_core" / "basket" / "service.py").read_text()

    for value in [
        "STEP 1 · VENDOR CARTS",
        "Add to Parts Basket",
        "STEP 2 · PARTS BASKET",
        "Add Vendor Line Item",
    ]:
        if value not in template:
            raise RuntimeError(f"Vendor workflow marker missing: {value}")

    if "/jobs/{job_id}/vendor-carts/manual" not in routes:
        raise RuntimeError("Manual Vendor route is missing.")

    if "0, 1.0" not in service:
        raise RuntimeError(
            "Imported Vendor Cart items are not defaulting to unselected."
        )

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "from plg_core.basket.routes import router; "
                "paths={r.path for r in router.routes}; "
                "assert '/jobs/{job_id}/basket' in paths; "
                "assert '/jobs/{job_id}/vendor-carts/manual' in paths; "
                "print('Vendor Carts workflow smoke test passed')"
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
        "Commit 0004C installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004C Vendor Carts Workflow installed successfully.")
print("Vendor imports and manual entries now stay grouped by Vendor.")
print("Line items must be added to the final Parts Basket.")
print(f"Backup created at: {backup}")
