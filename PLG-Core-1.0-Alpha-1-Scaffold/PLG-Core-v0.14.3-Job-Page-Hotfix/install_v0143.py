from pathlib import Path
import shutil
from datetime import datetime

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"

if not APP.exists():
    raise SystemExit(f"Required file not found: {APP}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.14.3-{stamp}"
backup.mkdir(parents=True)
shutil.copy2(APP, backup / "app.py")

app = APP.read_text()

broken = '''            ORDER BY source_cart_imports.id DESC
            """
            (job_id,),
        ).fetchall()'''

fixed = '''            ORDER BY source_cart_imports.id DESC
            """,
            (job_id,),
        ).fetchall()'''

if broken not in app:
    raise SystemExit("Could not find the broken source_imports query. No files were changed.")

app = app.replace(broken, fixed, 1)
APP.write_text(app)

print("PLG Core v0.14.3 job-page hotfix installed successfully.")
print(f"Backup created at: {backup}")