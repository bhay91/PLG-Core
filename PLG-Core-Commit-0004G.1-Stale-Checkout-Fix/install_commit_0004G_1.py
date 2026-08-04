from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
BASKET = PROJECT / "templates" / "basket.html"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (BASKET, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0004G.1-{stamp}"
backup.mkdir(parents=True)

saved = backup / "templates" / "basket.html"
saved.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(BASKET, saved)

try:
    text = BASKET.read_text()

    text = text.replace(
        'basket.status in ["CHECKED_OUT", "COMMITTED"]',
        'basket.status == "COMMITTED"',
    )

    BASKET.write_text(text)

    migrate = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from legacy_app import get_connection; "
                "conn=get_connection(); "
                "cur=conn.execute("
                "\"UPDATE baskets SET status='OPEN', updated_at=CURRENT_TIMESTAMP "
                "WHERE status='CHECKED_OUT' "
                "AND NOT EXISTS (SELECT 1 FROM job_parts WHERE job_parts.job_id=baskets.job_id)\""
                "); "
                "conn.commit(); "
                "print(f'Reset {cur.rowcount} stale checked-out basket(s)'); "
                "conn.close()"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )

    if migrate.returncode != 0:
        raise RuntimeError(migrate.stdout + migrate.stderr)

    final_text = BASKET.read_text()
    if 'basket.status in ["CHECKED_OUT", "COMMITTED"]' in final_text:
        raise RuntimeError("Old checkout-complete condition is still present.")
    if 'basket.status == "COMMITTED"' not in final_text:
        raise RuntimeError("Committed-only checkout condition is missing.")

    smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from jinja2 import Environment, FileSystemLoader; "
                "env=Environment(loader=FileSystemLoader('templates')); "
                "env.get_template('basket.html'); "
                "from app import app; assert app is not None; "
                "print('Stale checkout fix smoke test passed')"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if smoke.returncode != 0:
        raise RuntimeError(smoke.stdout + smoke.stderr)

except Exception as exc:
    shutil.copy2(saved, BASKET)
    raise SystemExit(
        "Commit 0004G.1 installation failed and the template was restored.\n"
        f"{exc}"
    )

print("PLG Commit 0004G.1 Stale Checkout Fix installed successfully.")
print(migrate.stdout.strip())
print("Only COMMITTED baskets now show Create Quote.")
print("Stale CHECKED_OUT baskets with no job parts were reset to OPEN.")
print(f"Backup created at: {backup}")
