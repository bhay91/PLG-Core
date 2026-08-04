from pathlib import Path
import shutil
from datetime import datetime
import py_compile

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
PACKAGE = Path(__file__).resolve().parent
PAYLOAD = PACKAGE / "payload"

required = [
    PROJECT / "app.py",
    PROJECT / "templates" / "job_detail.html",
    PROJECT / "templates" / "base.html",
    PROJECT / "static" / "app.css",
    PROJECT / "data" / "plg_core.db",
]
for path in required:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.9-{stamp}"
(backup / "templates").mkdir(parents=True)
(backup / "static").mkdir(parents=True)
(backup / "data").mkdir(parents=True)

shutil.copy2(PROJECT / "app.py", backup / "app.py")
shutil.copy2(PROJECT / "templates" / "job_detail.html", backup / "templates" / "job_detail.html")
shutil.copy2(PROJECT / "templates" / "base.html", backup / "templates" / "base.html")
shutil.copy2(PROJECT / "static" / "app.css", backup / "static" / "app.css")
shutil.copy2(PROJECT / "data" / "plg_core.db", backup / "data" / "plg_core.db")

for rel in [
    Path("app.py"),
    Path("templates/job_detail.html"),
    Path("templates/base.html"),
    Path("static/app.css"),
]:
    source = PAYLOAD / rel
    destination = PROJECT / rel
    shutil.copy2(source, destination)

py_compile.compile(str(PROJECT / "app.py"), doraise=True)

print("PLG Core v0.9 Supplier Selection installed successfully.")
print(f"Backup created at: {backup}")
print("Your live database was preserved. A safety copy was included in the backup.")
