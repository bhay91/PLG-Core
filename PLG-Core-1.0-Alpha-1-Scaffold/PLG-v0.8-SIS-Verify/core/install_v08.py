from pathlib import Path
import shutil
from datetime import datetime
import re

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"
JOB = PROJECT / "templates" / "job_detail.html"
BASE = PROJECT / "templates" / "base.html"

for path in (APP, JOB, BASE):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.8-{stamp}"
(backup / "templates").mkdir(parents=True)
shutil.copy2(APP, backup / "app.py")
shutil.copy2(JOB, backup / "templates" / "job_detail.html")
shutil.copy2(BASE, backup / "templates" / "base.html")

app = APP.read_text()

migration_anchor = '            "oem_dealer_lead_time": "ALTER TABLE job_parts ADD COLUMN oem_dealer_lead_time TEXT",\n'
if '"verification_source": "ALTER TABLE job_parts ADD COLUMN verification_source TEXT"' not in app:
    if migration_anchor in app:
        app = app.replace(
            migration_anchor,
            migration_anchor + '            "verification_source": "ALTER TABLE job_parts ADD COLUMN verification_source TEXT",\n',
            1,
        )
    else:
        fallback = '            "captured_at": "ALTER TABLE job_parts ADD COLUMN captured_at TEXT",\n'
        if fallback not in app:
            raise SystemExit("Could not find the job_parts migration block.")
        app = app.replace(
            fallback,
            fallback + '            "verification_source": "ALTER TABLE job_parts ADD COLUMN verification_source TEXT",\n',
            1,
        )

payload_anchor = '    verification_notes = str(payload.get("verification_notes", "")).strip()\n'
if 'verification_source = str(payload.get("verification_source"' not in app:
    if payload_anchor not in app:
        raise SystemExit("Could not find capture payload parser.")
    app = app.replace(
        payload_anchor,
        '    verification_source = str(payload.get("verification_source", "")).strip()\n' + payload_anchor,
        1,
    )

sql_anchor = '                verification_notes = ?,\n                verification_status = \'VERIFIED\'\n'
if '                verification_source = ?,' not in app:
    if sql_anchor not in app:
        raise SystemExit("Could not find capture SQL update section.")
    app = app.replace(
        sql_anchor,
        '                verification_source = ?,\n' + sql_anchor,
        1,
    )

tuple_anchor = '                verification_notes,\n                part_id,\n'
if '                verification_source,\n                verification_notes,' not in app:
    if tuple_anchor not in app:
        raise SystemExit("Could not find capture SQL values tuple.")
    app = app.replace(
        tuple_anchor,
        '                verification_source,\n' + tuple_anchor,
        1,
    )

query_old = '                job_parts.id AS part_id,\n                job_parts.job_id,\n                jobs.manufacturer\n'
query_new = '                job_parts.id AS part_id,\n                job_parts.job_id,\n                jobs.manufacturer,\n                jobs.pin_serial\n'
if query_old in app:
    app = app.replace(query_old, query_new, 1)

old_redirect = 'url=f"https://parts.cat.com/en/catcorp?plg_part_id={part_id}",'
new_redirect = 'url=f"https://sis2.cat.com/#/detail?serialNumber={part[\'pin_serial\']}&tab=parts",'
if old_redirect in app:
    app = app.replace(old_redirect, new_redirect, 1)
elif 'https://sis2.cat.com/#/detail?serialNumber=' not in app:
    raise SystemExit("Could not find the CAT redirect URL in app.py.")

APP.write_text(app)

job = JOB.read_text()
job = job.replace("Verify in CAT", "Verify Parts")
job = job.replace("Re-Verify in CAT", "Re-Verify Parts")
job = job.replace("Open CAT product page", "Open source page")
job = job.replace("CAT VERIFIED", "PART VERIFIED")

captured_line = """          {% if part.captured_at %}
          <span><strong>Captured:</strong> {{ part.captured_at }}</span>
          {% endif %}
"""
source_line = '          <span><strong>Source:</strong> {{ part.verification_source or "CAT SIS" }}</span>\n'
if "<strong>Source:</strong>" not in job:
    if captured_line not in job:
        raise SystemExit("Could not find verified summary metadata.")
    job = job.replace(captured_line, source_line + captured_line, 1)

JOB.write_text(job)

base = BASE.read_text()
base = re.sub(r"PLG Core v\d+\.\d+(?:\.\d+)?", "PLG Core v0.8", base)
BASE.write_text(base)

print("PLG Core v0.8 SIS Verify installed successfully.")
print(f"Backup created at: {backup}")
