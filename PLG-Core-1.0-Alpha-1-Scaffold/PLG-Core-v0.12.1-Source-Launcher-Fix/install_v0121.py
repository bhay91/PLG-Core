from pathlib import Path
import shutil
from datetime import datetime
import re

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"
JOB = PROJECT / "templates" / "job_detail.html"
BASE = PROJECT / "templates" / "base.html"

for path in (APP, JOB):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.12.1-{stamp}"
(backup / "templates").mkdir(parents=True)
shutil.copy2(APP, backup / "app.py")
shutil.copy2(JOB, backup / "templates" / "job_detail.html")
if BASE.exists():
    shutil.copy2(BASE, backup / "templates" / "base.html")

app = APP.read_text()

migration_anchor = '        connection.commit()\n\n\ndef next_job_number'
migration_insert = '''        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS active_source_import (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                job_id INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                source_name TEXT NOT NULL,
                activated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (job_id) REFERENCES jobs(id)
            )
            """
        )

        connection.commit()


def next_job_number'''

if "CREATE TABLE IF NOT EXISTS active_source_import" not in app:
    if migration_anchor not in app:
        raise SystemExit("Could not find database migration insertion point.")
    app = app.replace(migration_anchor, migration_insert, 1)

route_anchor = '@app.get("/api/active-verification")'
source_routes = '''
SOURCE_PROFILES = {
    "cat_sis": {"name": "CAT SIS", "url": "https://sis2.cat.com/#/cart"},
    "worldpac": {"name": "Worldpac", "url": "https://www.worldpac.com/"},
    "ssf": {"name": "SSF", "url": "https://www.ssfautoparts.com/"},
    "rockauto": {"name": "RockAuto", "url": "https://www.rockauto.com/"},
    "upload": {"name": "Upload Quote / Image", "url": ""},
}


@app.post("/jobs/{job_id}/start-source-import")
def start_source_import(job_id: int, source_key: str = Form(...)):
    profile = SOURCE_PROFILES.get(source_key)
    if profile is None:
        raise HTTPException(status_code=400, detail="Unknown supplier/source.")

    with closing(get_connection()) as connection:
        job = connection.execute(
            "SELECT id FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()

        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        connection.execute(
            """
            INSERT INTO active_source_import
                (id, job_id, source_key, source_name, activated_at)
            VALUES
                (1, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                job_id = excluded.job_id,
                source_key = excluded.source_key,
                source_name = excluded.source_name,
                activated_at = CURRENT_TIMESTAMP
            """,
            (job_id, source_key, profile["name"]),
        )
        connection.commit()

    if source_key == "upload":
        return RedirectResponse(
            url=f"/jobs/{job_id}#quote-upload",
            status_code=303,
        )

    return RedirectResponse(url=profile["url"], status_code=303)


@app.get("/api/active-source-import")
def api_active_source_import():
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT
                active_source_import.job_id,
                active_source_import.source_key,
                active_source_import.source_name,
                active_source_import.activated_at,
                jobs.job_number,
                jobs.customer,
                jobs.manufacturer,
                jobs.machine,
                jobs.pin_serial
            FROM active_source_import
            JOIN jobs ON jobs.id = active_source_import.job_id
            WHERE active_source_import.id = 1
            """
        ).fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "message": "No active source import job."},
        )

    return {"ok": True, **dict(row)}


'''

if '@app.post("/jobs/{job_id}/start-source-import")' not in app:
    if route_anchor not in app:
        raise SystemExit("Could not find route insertion point.")
    app = app.replace(route_anchor, source_routes + route_anchor, 1)

APP.write_text(app)

job = JOB.read_text()
job = job.replace("Import SIS Cart", "Import Cart")
job = job.replace("SIS Cart", "Supplier Cart")

launcher = '''
<section class="panel source-import-panel">
  <div class="panel-heading">
    <div>
      <p class="eyebrow">IMPORT SUPPLIER CART OR QUOTE</p>
      <h2>Choose a source</h2>
    </div>
  </div>

  <form method="post" action="/jobs/{{ job.id }}/start-source-import" target="_blank">
    <div class="form-grid compact-grid">
      <label>
        Supplier / Source
        <select name="source_key" required>
          <option value="cat_sis">CAT SIS</option>
          <option value="worldpac">Worldpac</option>
          <option value="ssf">SSF</option>
          <option value="rockauto">RockAuto</option>
          <option value="upload">Upload Quote / Image</option>
        </select>
      </label>

      <div class="form-action">
        <button class="button" type="submit">Open Source</button>
      </div>
    </div>
  </form>

  <p class="muted">
    CAT SIS cart import is active. Worldpac, SSF, and RockAuto readers will be added after each cart is tested.
  </p>
</section>
'''

if "IMPORT SUPPLIER CART OR QUOTE" not in job:
    marker = '</section>\n\n<section class="detail-grid">'
    if marker not in job:
        raise SystemExit(
            "Could not find the page-heading/detail-grid boundary in job_detail.html."
        )
    job = job.replace(
        marker,
        '</section>\n\n' + launcher + '\n<section class="detail-grid">',
        1,
    )

JOB.write_text(job)

if BASE.exists():
    base = BASE.read_text()
    base = re.sub(
        r"PLG Core v\d+\.\d+(?:\.\d+)?",
        "PLG Core v0.12.1",
        base,
    )
    BASE.write_text(base)

print("PLG Core v0.12.1 Source Launcher installed successfully.")
print(f"Backup created at: {backup}")
