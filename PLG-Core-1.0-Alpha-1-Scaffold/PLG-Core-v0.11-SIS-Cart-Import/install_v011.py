from pathlib import Path
import shutil
from datetime import datetime

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "payload"

required = [
    PROJECT / "app.py",
    PROJECT / "templates" / "job_detail.html",
    PROJECT / "templates" / "base.html",
    PROJECT / "static" / "app.css",
]
for path in required:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.11-{stamp}"
(backup / "templates").mkdir(parents=True)
(backup / "static").mkdir(parents=True)

shutil.copy2(PROJECT / "app.py", backup / "app.py")
shutil.copy2(PROJECT / "templates" / "job_detail.html", backup / "templates" / "job_detail.html")
shutil.copy2(PROJECT / "templates" / "base.html", backup / "templates" / "base.html")
shutil.copy2(PROJECT / "static" / "app.css", backup / "static" / "app.css")

shutil.copy2(PAYLOAD / "app.py", PROJECT / "app.py")
shutil.copy2(PAYLOAD / "templates" / "job_detail.html", PROJECT / "templates" / "job_detail.html")
shutil.copy2(PAYLOAD / "templates" / "base.html", PROJECT / "templates" / "base.html")
shutil.copy2(PAYLOAD / "static" / "app.css", PROJECT / "static" / "app.css")

print("PLG Core v0.11 SIS Cart Import installed successfully.")
print(f"Backup created at: {backup}")
print("Your database was preserved.")
