from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parent / "payload"
PROJECT = Path.home() / "Desktop" / "PLG-Core"

required = [
    PROJECT / "templates" / "base.html",
    PROJECT / "static" / "app.css",
    PROJECT / ".venv" / "bin" / "python",
]
for path in required:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0003A.1-{stamp}"
backup.mkdir(parents=True)

managed = [
    Path("templates/base.html"),
    Path("static/app.css"),
]

for relative in managed:
    current = PROJECT / relative
    saved = backup / relative
    saved.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current, saved)

def restore() -> None:
    for relative in managed:
        current = PROJECT / relative
        saved = backup / relative
        if current.exists():
            current.unlink()
        if saved.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, current)

try:
    for file in SOURCE.rglob("*"):
        if not file.is_file():
            continue
        target = PROJECT / file.relative_to(SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)

    base_text = (PROJECT / "templates" / "base.html").read_text()
    css_text = (PROJECT / "static" / "app.css").read_text()

    if "Parts Sourcing Control Center" not in base_text:
        raise RuntimeError("Updated subtitle was not found.")

    if "topbar-new-job" in base_text:
        raise RuntimeError("The duplicate top-right New Job button is still present.")

    if "Commit 0003A.1 Final Typography Polish" not in css_text:
        raise RuntimeError("Typography polish marker was not found.")

    python = PROJECT / ".venv" / "bin" / "python"
    smoke = subprocess.run(
        [
            str(python),
            "-c",
            "from app import app; assert app is not None; print('UI polish smoke test passed')",
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
        "Commit 0003A.1 installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0003A.1 Final Polish installed successfully.")
print("Removed the duplicate top-right New Job button.")
print("Updated subtitle to Parts Sourcing Control Center.")
print("Applied the crisper application-wide typography.")
print(f"Backup created at: {backup}")
