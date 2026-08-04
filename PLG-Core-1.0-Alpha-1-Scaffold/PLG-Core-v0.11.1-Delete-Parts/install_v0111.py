from pathlib import Path
import shutil
from datetime import datetime
import re

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"
JOB = PROJECT / "templates" / "job_detail.html"
CSS = PROJECT / "static" / "app.css"
BASE = PROJECT / "templates" / "base.html"

for path in (APP, JOB, CSS):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.11.1-{stamp}"
(backup / "templates").mkdir(parents=True)
(backup / "static").mkdir(parents=True)

shutil.copy2(APP, backup / "app.py")
shutil.copy2(JOB, backup / "templates" / "job_detail.html")
shutil.copy2(CSS, backup / "static" / "app.css")
if BASE.exists():
    shutil.copy2(BASE, backup / "templates" / "base.html")

app = APP.read_text()
route = '\n@app.post("/parts/{part_id}/delete")\ndef delete_job_part(part_id: int):\n    with closing(get_connection()) as connection:\n        part = connection.execute(\n            """\n            SELECT id, job_id, requested_description\n            FROM job_parts\n            WHERE id = ?\n            """,\n            (part_id,),\n        ).fetchone()\n\n        if part is None:\n            raise HTTPException(status_code=404, detail="Part not found.")\n\n        # Clear any active verification connected to this part.\n        connection.execute(\n            "DELETE FROM active_verification WHERE part_id = ?",\n            (part_id,),\n        )\n\n        # Newer PLG versions store supplier choices separately.\n        tables = {\n            row["name"]\n            for row in connection.execute(\n                "SELECT name FROM sqlite_master WHERE type = \'table\'"\n            ).fetchall()\n        }\n\n        if "part_sources" in tables:\n            connection.execute(\n                "DELETE FROM part_sources WHERE part_id = ?",\n                (part_id,),\n            )\n\n        if "supplier_options" in tables:\n            connection.execute(\n                "DELETE FROM supplier_options WHERE part_id = ?",\n                (part_id,),\n            )\n\n        connection.execute(\n            "DELETE FROM job_parts WHERE id = ?",\n            (part_id,),\n        )\n\n        remaining = connection.execute(\n            """\n            SELECT\n                COUNT(*) AS total,\n                SUM(\n                    CASE\n                        WHEN verification_status = \'VERIFIED\' THEN 1\n                        ELSE 0\n                    END\n                ) AS verified\n            FROM job_parts\n            WHERE job_id = ?\n            """,\n            (part["job_id"],),\n        ).fetchone()\n\n        total = remaining["total"] or 0\n        verified = remaining["verified"] or 0\n\n        if total == 0:\n            new_status = "REQUESTED"\n        elif verified == total:\n            new_status = "VERIFIED"\n        else:\n            new_status = "RESEARCHING"\n\n        connection.execute(\n            "UPDATE jobs SET status = ? WHERE id = ?",\n            (new_status, part["job_id"]),\n        )\n\n        connection.commit()\n\n    return RedirectResponse(\n        url=f"/jobs/{part[\'job_id\']}",\n        status_code=303,\n    )\n\n\n'

if '@app.post("/parts/{part_id}/delete")' not in app:
    anchor = '@app.post("/jobs/{job_id}/status")'
    position = app.find(anchor)

    if position == -1:
        raise SystemExit("Could not find the job status route in app.py.")

    app = app[:position] + route + app[position:]

APP.write_text(app)

job = JOB.read_text()
button = '\n          <form\n            method="post"\n            action="/parts/{{ part.id }}/delete"\n            onsubmit="return confirm(\'Delete this part?\\n\\n{{ part.requested_description|e }}\\n\\nThis will also remove its saved supplier options. This cannot be undone.\');"\n          >\n            <button class="button delete-part-button" type="submit">\n              Delete\n            </button>\n          </form>\n'

if 'action="/parts/{{ part.id }}/delete"' not in job:
    anchor = '          <span class="status">{{ part.verification_status }}</span>'

    if anchor not in job:
        raise SystemExit("Could not find the part status in job_detail.html.")

    job = job.replace(
        anchor,
        button + '\n' + anchor,
        1,
    )

JOB.write_text(job)

css = CSS.read_text()
addition = '\n\n.button.delete-part-button {\n  border: 1px solid #b64a4a;\n  background: white;\n  color: #9b2f2f;\n}\n\n.button.delete-part-button:hover {\n  background: #fff2f2;\n}\n'

if ".button.delete-part-button {" not in css:
    CSS.write_text(css + addition)

if BASE.exists():
    base = BASE.read_text()
    base = re.sub(
        r"PLG Core v\d+\.\d+(?:\.\d+)?",
        "PLG Core v0.11.1",
        base,
    )
    BASE.write_text(base)

print("PLG Core v0.11.1 Delete Parts installed successfully.")
print(f"Backup created at: {backup}")
