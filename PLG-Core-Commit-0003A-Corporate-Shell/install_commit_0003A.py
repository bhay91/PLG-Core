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
backup = PROJECT / f"backup-commit-0003A-{stamp}"
backup.mkdir(parents=True)

managed = [
    Path("templates/base.html"),
    Path("static/app.css"),
    Path("static/plg-logo.webp"),
]

for relative in managed:
    current = PROJECT / relative
    saved = backup / relative
    if current.exists():
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
        if file.is_file():
            target = PROJECT / file.relative_to(SOURCE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)

    base_text = (PROJECT / "templates" / "base.html").read_text()
    css_text = (PROJECT / "static" / "app.css").read_text()

    required_markers = [
        "PLG Core · Version 1.0 Alpha",
        "Worldwide Parts Sourcing, Technical Identification",
        "global-search",
        "user-chip",
    ]
    for marker in required_markers:
        if marker not in base_text:
            raise RuntimeError(f"UI marker missing: {marker}")

    if "PLG Corporate Shell" not in css_text:
        # This marker is inserted below so validation is explicit.
        raise RuntimeError("Corporate CSS marker is missing.")

    python = PROJECT / ".venv" / "bin" / "python"
    smoke = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from app import app; "
                "assert app is not None; "
                "print('PLG corporate shell smoke test passed')"
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
        "Commit 0003A installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Commit 0003A Corporate Shell installed successfully.")
print("Logo, corporate sidebar, header, typography, cards, tables, and buttons updated.")
print(f"Backup created at: {backup}")
print("")
print("Restart PLG and open http://127.0.0.1:8000")
