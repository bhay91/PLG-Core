from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import subprocess

PROJECT = Path.home() / "Desktop" / "PLG-Core"
SOURCE = Path(__file__).resolve().parent / "payload"
ENGINE = PROJECT / "plg_core" / "documents" / "quote_pdf.py"
PYTHON = PROJECT / ".venv" / "bin" / "python"

for path in (ENGINE, PYTHON):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-commit-0005B-{stamp}"
backup.mkdir(parents=True)

saved = backup / ENGINE.relative_to(PROJECT)
saved.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(ENGINE, saved)

try:
    shutil.copy2(
        SOURCE / "plg_core" / "documents" / "quote_pdf.py",
        ENGINE,
    )

    compile_test = subprocess.run(
        [str(PYTHON), "-m", "py_compile", str(ENGINE)],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if compile_test.returncode != 0:
        raise RuntimeError(compile_test.stdout + compile_test.stderr)

    # Regenerate every existing quote using the new Corporate Document Standard.
    regenerate = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from legacy_app import get_connection, load_quote; "
                "from plg_core.documents.quote_pdf import generate_quote_pdfs; "
                "conn=get_connection(); "
                "ids=[r['id'] for r in conn.execute('SELECT id FROM quotes ORDER BY id')]; "
                "[generate_quote_pdfs(*load_quote(conn,qid)) for qid in ids]; "
                "print(f'Regenerated {len(ids)} quote(s)'); "
                "conn.close()"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if regenerate.returncode != 0:
        raise RuntimeError(regenerate.stdout + regenerate.stderr)

    route_smoke = subprocess.run(
        [
            str(PYTHON),
            "-c",
            (
                "from app import app; "
                "paths={getattr(r,'path',None) for r in app.routes}; "
                "assert '/quotes/{quote_id}/customer/pdf' in paths; "
                "assert '/quotes/{quote_id}/internal/pdf' in paths; "
                "print('Corporate quote routes verified')"
            ),
        ],
        cwd=PROJECT,
        text=True,
        capture_output=True,
    )
    if route_smoke.returncode != 0:
        raise RuntimeError(route_smoke.stdout + route_smoke.stderr)

except Exception as exc:
    shutil.copy2(saved, ENGINE)
    raise SystemExit(
        "Commit 0005B installation failed and the previous quote engine was restored.\n"
        f"{exc}"
    )

print("PLG Commit 0005B Corporate Quote Standard installed successfully.")
print(regenerate.stdout.strip())
print("Customer and internal quote PDFs now use PLG Corporate Document Standard v1.0.")
print(f"Backup created at: {backup}")
