from __future__ import annotations

from datetime import datetime
from pathlib import Path
import py_compile
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parent / "payload"
PROJECT = Path.home() / "Desktop" / "PLG-Core"

required = [
    PROJECT / "legacy_app.py",
    PROJECT / "plg_core" / "application.py",
    PROJECT / ".venv" / "bin" / "python",
    PROJECT / "templates" / "job_detail.html",
    PROJECT / "static" / "app.css",
]
for path in required:
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-sprint-1-{stamp}"
backup.mkdir(parents=True)

paths = [
    Path("plg_core/application.py"),
    Path("plg_core/database/migrations.py"),
    Path("plg_core/basket"),
    Path("templates/basket.html"),
    Path("templates/job_detail.html"),
    Path("static/app.css"),
]
for relative in paths:
    current = PROJECT / relative
    saved = backup / relative
    if current.is_dir():
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(current, saved)
    elif current.exists():
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, saved)

def restore():
    for relative in paths:
        current = PROJECT / relative
        saved = backup / relative
        if current.is_dir():
            shutil.rmtree(current)
        elif current.exists():
            current.unlink()
        if saved.is_dir():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(saved, current)
        elif saved.exists():
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, current)

try:
    for file in SOURCE.rglob("*"):
        if file.is_file():
            target = PROJECT / file.relative_to(SOURCE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)

    job_template = PROJECT / "templates" / "job_detail.html"
    text = job_template.read_text()
    if 'href="/jobs/{{ job.id }}/basket"' not in text:
        anchor = '<div class="header-actions">'
        if anchor not in text:
            raise RuntimeError("Could not find job header insertion point.")
        text = text.replace(
            anchor,
            anchor + '\n    <a class="button" href="/jobs/{{ job.id }}/basket">Parts Basket</a>',
            1,
        )
        job_template.write_text(text)

    css_file = PROJECT / "static" / "app.css"
    existing_css = css_file.read_text()
    marker = "/* Sprint 1 Parts Basket */"
    if marker not in existing_css:
        existing_css += (SOURCE / "static" / "basket.css").read_text()
        css_file.write_text(existing_css)

    for py_file in [
        PROJECT / "plg_core" / "application.py",
        PROJECT / "plg_core" / "database" / "migrations.py",
        PROJECT / "plg_core" / "basket" / "models.py",
        PROJECT / "plg_core" / "basket" / "service.py",
        PROJECT / "plg_core" / "basket" / "routes.py",
    ]:
        py_compile.compile(str(py_file), doraise=True)

    python = PROJECT / ".venv" / "bin" / "python"
    smoke = subprocess.run(
        [
            str(python), "-c",
            (
                "from plg_core.database.migrations import run_migrations; "
                "run_migrations(); "
                "from app import app; "
                "from plg_core.basket.routes import router; "
                "paths={r.path for r in router.routes}; "
                "assert '/jobs/{job_id}/basket' in paths; "
                "assert '/api/basket/import-source-cart' in paths; "
                "assert '/jobs/{job_id}/basket/commit' in paths; "
                "assert app is not None; "
                "print('Sprint 1 smoke test passed')"
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
        "Sprint 1 installation failed and files were restored.\n"
        f"{exc}"
    )

print("PLG Sprint 1 Integration Build installed successfully.")
print("Basket UI, CAT SIS and Worldpac Basket imports, totals, commit-to-job, and quote handoff are ready.")
print(f"Backup created at: {backup}")
