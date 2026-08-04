from pathlib import Path
import shutil
from datetime import datetime

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"
JOB = PROJECT / "templates" / "job_detail.html"
CSS = PROJECT / "static" / "app.css"

for p in (APP, JOB, CSS):
    if not p.exists():
        raise SystemExit(f"Required file not found: {p}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.7.1-{stamp}"
(backup / "templates").mkdir(parents=True)
(backup / "static").mkdir(parents=True)

shutil.copy2(APP, backup / "app.py")
shutil.copy2(JOB, backup / "templates" / "job_detail.html")
shutil.copy2(CSS, backup / "static" / "app.css")

app = APP.read_text()

anchor = '            "captured_at": "ALTER TABLE job_parts ADD COLUMN captured_at TEXT",\n'
columns = (
    anchor
    + '            "oem_dealer_name": "ALTER TABLE job_parts ADD COLUMN oem_dealer_name TEXT",\n'
    + '            "oem_dealer_price": "ALTER TABLE job_parts ADD COLUMN oem_dealer_price REAL",\n'
    + '            "oem_dealer_availability": "ALTER TABLE job_parts ADD COLUMN oem_dealer_availability TEXT",\n'
    + '            "oem_dealer_lead_time": "ALTER TABLE job_parts ADD COLUMN oem_dealer_lead_time TEXT",\n'
)

if '"oem_dealer_name":' not in app:
    if anchor not in app:
        raise SystemExit("Could not find captured_at migration. Confirm v0.5 is installed.")
    app = app.replace(anchor, columns, 1)

payload_anchor = '    verification_notes = str(payload.get("verification_notes", "")).strip()\n'
payload_block = (
    '    oem_dealer_name = str(payload.get("oem_dealer_name", "")).strip()\n'
    '    oem_dealer_availability = str(payload.get("oem_dealer_availability", "")).strip()\n'
    '    oem_dealer_lead_time = str(payload.get("oem_dealer_lead_time", "")).strip()\n'
    '    raw_dealer_price = payload.get("oem_dealer_price", None)\n'
    '    try:\n'
    '        oem_dealer_price = float(raw_dealer_price) if raw_dealer_price not in (None, "") else None\n'
    '    except (TypeError, ValueError):\n'
    '        oem_dealer_price = None\n'
    + payload_anchor
)

if "raw_dealer_price = payload.get" not in app:
    if payload_anchor not in app:
        raise SystemExit("Could not find capture payload parser.")
    app = app.replace(payload_anchor, payload_block, 1)

sql_anchor = '                captured_at = CURRENT_TIMESTAMP,\n                verification_notes = ?,\n'
sql_fields = (
    '                captured_at = CURRENT_TIMESTAMP,\n'
    '                oem_dealer_name = ?,\n'
    '                oem_dealer_price = ?,\n'
    '                oem_dealer_availability = ?,\n'
    '                oem_dealer_lead_time = ?,\n'
    '                verification_notes = ?,\n'
)

if "oem_dealer_lead_time = ?" not in app:
    if sql_anchor not in app:
        raise SystemExit("Could not find capture SQL fields.")
    app = app.replace(sql_anchor, sql_fields, 1)

tuple_anchor = '                product_url,\n                verification_notes,\n'
tuple_values = (
    '                product_url,\n'
    '                oem_dealer_name,\n'
    '                oem_dealer_price,\n'
    '                oem_dealer_availability,\n'
    '                oem_dealer_lead_time,\n'
    '                verification_notes,\n'
)

if "oem_dealer_lead_time,\n                verification_notes" not in app:
    if tuple_anchor not in app:
        raise SystemExit("Could not find capture SQL values.")
    app = app.replace(tuple_anchor, tuple_values, 1)

APP.write_text(app)

job = JOB.read_text()
dealer_card = '\n        {% if part.oem_dealer_name or part.oem_dealer_price is not none or part.oem_dealer_availability or part.oem_dealer_lead_time %}\n        <div class="dealer-import-card">\n          <p class="eyebrow">OEM DEALER</p>\n          <div class="dealer-import-grid">\n            <span><small>Dealer</small><strong>{{ part.oem_dealer_name or "CAT Dealer" }}</strong></span>\n            <span>\n              <small>Price</small>\n              <strong>\n                {% if part.oem_dealer_price is not none %}\n                ${{ "%.2f"|format(part.oem_dealer_price) }} USD\n                {% else %}\n                Not captured\n                {% endif %}\n              </strong>\n            </span>\n            <span><small>Availability</small><strong>{{ part.oem_dealer_availability or "Not captured" }}</strong></span>\n            <span><small>Lead time</small><strong>{{ part.oem_dealer_lead_time or "Not captured" }}</strong></span>\n          </div>\n        </div>\n        {% endif %}\n'

if 'class="dealer-import-card"' not in job:
    marker = '        {% if part.diagram_url or part.product_url %}'
    if marker not in job:
        raise SystemExit("Could not find verified summary. Confirm v0.6 installed.")
    job = job.replace(marker, dealer_card + '\n' + marker, 1)

JOB.write_text(job)

css = CSS.read_text()
dealer_css = '\n.dealer-import-card {\n  margin-top: 14px;\n  padding: 14px;\n  border: 1px solid #d4c27a;\n  border-radius: 9px;\n  background: #fffbed;\n}\n.dealer-import-grid {\n  margin-top: 10px;\n  display: grid;\n  grid-template-columns: repeat(4, 1fr);\n  gap: 12px;\n}\n.dealer-import-grid span { display: grid; gap: 3px; }\n.dealer-import-grid small { color: var(--muted); }\n@media (max-width: 700px) {\n  .dealer-import-grid { grid-template-columns: 1fr; }\n}\n'

if ".dealer-import-card {" not in css:
    CSS.write_text(css + dealer_css)

print("PLG Core v0.7.1 installed successfully.")
print(f"Backup created at: {backup}")
