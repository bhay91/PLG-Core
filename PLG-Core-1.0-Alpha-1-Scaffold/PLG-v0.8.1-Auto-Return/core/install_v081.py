from pathlib import Path
import shutil
from datetime import datetime

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
CSS = PROJECT / "static" / "app.css"

if not CSS.exists():
    raise SystemExit(f"Required file not found: {CSS}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.8.1-{stamp}"
(backup / "static").mkdir(parents=True)
shutil.copy2(CSS, backup / "static" / "app.css")

css = CSS.read_text()
addition = '\n.part-workspace:target {\n  scroll-margin-top: 90px;\n  outline: 4px solid #d4a017;\n  box-shadow: 0 0 0 8px rgba(212, 160, 23, 0.16);\n  animation: plg-target-highlight 2.5s ease-out;\n}\n\n@keyframes plg-target-highlight {\n  0% {\n    outline-color: #3aa76d;\n    box-shadow: 0 0 0 12px rgba(58, 167, 109, 0.28);\n  }\n  100% {\n    outline-color: #d4a017;\n    box-shadow: 0 0 0 8px rgba(212, 160, 23, 0.16);\n  }\n}\n'

if ".part-workspace:target {" not in css:
    CSS.write_text(css + addition)

print("PLG Core v0.8.1 auto-return styling installed successfully.")
print(f"Backup created at: {backup}")
