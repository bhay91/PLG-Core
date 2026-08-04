from pathlib import Path
import shutil
from datetime import datetime
import py_compile
import re
import sys

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"

if not APP.exists():
    raise SystemExit(f"Required file not found: {APP}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup_dir = PROJECT / f"backup-v0.16.2-{stamp}"
backup_dir.mkdir(parents=True)
backup_path = backup_dir / "app.py"
shutil.copy2(APP, backup_path)

text = APP.read_text()

future_line = "from __future__ import annotations"

if future_line not in text:
    raise SystemExit(
        "Could not find 'from __future__ import annotations' in app.py. "
        "No changes were made."
    )

# Remove every existing occurrence, then restore exactly one at the top.
lines = text.splitlines()
cleaned = [line for line in lines if line.strip() != future_line]

# Preserve a shebang and/or encoding declaration if present.
prefix = []
while cleaned:
    stripped = cleaned[0].strip()
    if (
        stripped.startswith("#!")
        or re.match(r"^#.*coding[:=]\s*[-\w.]+", stripped)
    ):
        prefix.append(cleaned.pop(0))
    else:
        break

# Remove leading blank lines after shebang/encoding.
while cleaned and not cleaned[0].strip():
    cleaned.pop(0)

new_lines = prefix + [future_line, ""] + cleaned
APP.write_text("\n".join(new_lines).rstrip() + "\n")

try:
    py_compile.compile(str(APP), doraise=True)
except Exception as exc:
    shutil.copy2(backup_path, APP)
    raise SystemExit(
        "Repair failed validation, so the original app.py was restored.\n"
        f"Python error: {exc}"
    )

print("PLG Core startup repair completed successfully.")
print(f"Backup created at: {backup_dir}")
print("app.py passed Python compile validation.")
