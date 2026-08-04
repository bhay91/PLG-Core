from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shutil

PROJECT = Path.home() / "Desktop" / "PLG-Core"
CSS = PROJECT / "static" / "app.css"
SOURCE = Path(__file__).resolve().parent / "payload" / "standard_ui.css"

if not CSS.exists():
    raise SystemExit(f"Required file not found: {CSS}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0008-{stamp}"
backup.mkdir(parents=True)
shutil.copy2(CSS, backup / "app.css")

css = CSS.read_text()

# Remove only the temporary/overlapping UI patches added during 0007A/0007B cleanup.
markers = [
    "/* Commit 0007A Quotes and Suppliers Navigation */",
    "/* Commit 0007B practical management cleanup */",
    "/* PLG management visibility and cleanup repair */",
    "/* PLG Standard Filter Buttons */",
    "/* =========================================================\n   PLG Standard Filter Buttons\n   ========================================================= */",
    "/* Supplier toolbar alignment */",
    "/* PLG Shared Management Toolbar */",
]

positions = [css.find(marker) for marker in markers if css.find(marker) >= 0]

if positions:
    css = css[:min(positions)].rstrip() + "\n\n"

css += SOURCE.read_text()

# Basic CSS safety checks.
if css.count("{") != css.count("}"):
    raise SystemExit(
        "Commit 0008 stopped because app.css has unbalanced braces. "
        f"Backup is at: {backup}"
    )

required = [
    "/* Commit 0008 UI Standardization */",
    ".management-tools",
    ".filter-links a.active",
    ".management-new-button",
]

for token in required:
    if token not in css:
        raise SystemExit(f"Commit 0008 stopped: missing required CSS token {token}")

CSS.write_text(css)

print("PLG Commit 0008 UI Standardization installed successfully.")
print("Duplicate temporary management CSS was removed.")
print("Customers, Suppliers, Quotes, and Connectors now share one toolbar standard.")
print(f"Backup created at: {backup}")
