from __future__ import annotations

from datetime import datetime
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys


SOURCE = Path(__file__).resolve().parent / "payload"
PROJECT = Path.home() / "Desktop" / "PLG-Core"

if not PROJECT.exists():
    raise SystemExit(f"PLG Core project not found: {PROJECT}")

required = [
    PROJECT / "app.py",
    PROJECT / "legacy_app.py",
    PROJECT / "plg_core" / "application.py",
    PROJECT / ".venv" / "bin" / "python",
]

for path in required:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0002-{stamp}"
backup.mkdir(parents=True)

for relative in (
    Path("plg_core/application.py"),
    Path("plg_core/database/migrations.py"),
    Path("plg_core/basket"),
):
    source_path = PROJECT / relative
    backup_path = backup / relative

    if source_path.is_dir():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, backup_path)
    elif source_path.exists():
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, backup_path)

def restore() -> None:
    for relative in (
        Path("plg_core/application.py"),
        Path("plg_core/database/migrations.py"),
        Path("plg_core/basket"),
    ):
        target = PROJECT / relative
        saved = backup / relative

        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

        if saved.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(saved, target)
        elif saved.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)

try:
    for file in SOURCE.rglob("*"):
        if not file.is_file():
            continue

        target = PROJECT / file.relative_to(SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)

    for py_file in [
        PROJECT / "plg_core" / "application.py",
        PROJECT / "plg_core" / "database" / "migrations.py",
        PROJECT / "plg_core" / "basket" / "models.py",
        PROJECT / "plg_core" / "basket" / "service.py",
        PROJECT / "plg_core" / "basket" / "routes.py",
    ]:
        py_compile.compile(str(py_file), doraise=True)

    venv_python = PROJECT / ".venv" / "bin" / "python"
    smoke = subprocess.run(
        [
            str(venv_python),
            "-c",
            (
                "from plg_core.database.migrations import run_migrations; "
                "run_migrations(); "
                "from app import app; "
                "from plg_core.basket.routes import router; "
                "paths={route.path for route in router.routes}; "
                "assert '/api/baskets/{job_id}' in paths; "
                "assert app is not None; "
                "print('Basket foundation smoke test passed')"
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
        "Commit 0002 installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Basket Foundation Commit 0002.2 installed successfully.")
print("Database migration completed.")
print("Basket API routes registered.")
print(f"Backup created at: {backup}")
print("")
print("Next:")
print("1. Restart PLG with ./run.sh")
print("2. Open http://127.0.0.1:8000/docs")
print("3. Look for the basket API section")
