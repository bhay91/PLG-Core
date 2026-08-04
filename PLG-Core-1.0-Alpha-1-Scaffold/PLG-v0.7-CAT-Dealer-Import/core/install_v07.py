from pathlib import Path
import shutil
from datetime import datetime
import re

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"
JOB = PROJECT / "templates" / "job_detail.html"
CSS = PROJECT / "static" / "app.css"

for p in (APP, JOB, CSS):
    if not p.exists():
        raise SystemExit(f"Required file not found: {p}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.7-{stamp}"
(backup / "templates").mkdir(parents=True)
(backup / "static").mkdir(parents=True)
shutil.copy2(APP, backup / "app.py")
shutil.copy2(JOB, backup / "templates" / "job_detail.html")
shutil.copy2(CSS, backup / "static" / "app.css")

app = APP.read_text()

# Database columns.
anchor = '            "captured_at": "ALTER TABLE job_parts ADD COLUMN captured_at TEXT",\n'
cols = (
    anchor
    + '            "oem_dealer_name": "ALTER TABLE job_parts ADD COLUMN oem_dealer_name TEXT",\n'
    + '            "oem_dealer_price": "ALTER TABLE job_parts ADD COLUMN oem_dealer_price REAL",\n'
    + '            "oem_dealer_availability": "ALTER TABLE job_parts ADD COLUMN oem_dealer_availability TEXT",\n'
    + '            "oem_dealer_lead_time": "ALTER TABLE job_parts ADD COLUMN oem_dealer_lead_time TEXT",\n'
)
if '"oem_dealer_name":' not in app:
    if anchor not in app:
        raise SystemExit("Could not find captured_at migration. Install v0.5 first.")
    app = app.replace(anchor, cols, 1)

# Capture payload parsing.
payload_anchor = '    verification_notes = str(payload.get("verification_notes", "")).strip()\n'
payload = (
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
    app = app.replace(payload_anchor, payload, 1)

# SQL update fields.
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

# SQL values tuple.
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

# Hide quick capture after verification and show summary.
quick_start = job.find('      <form method="post" action="/parts/{ part.id }/quick-capture" class="quick-capture-panel">')
if quick_start == -1:
    raise SystemExit("Could not find OEM Quick Capture block.")

quick_end_marker = '      </form>\n\n      <div class="part-columns">'
quick_end = job.find(quick_end_marker, quick_start)
if quick_end == -1:
    raise SystemExit("Could not find end of OEM Quick Capture block.")

new_block = '\n      {% if part.verification_status != "VERIFIED" %}\n      <form method="post" action="/parts/{{ part.id }}/quick-capture" class="quick-capture-panel">\n        <div>\n          <p class="eyebrow">OEM QUICK CAPTURE</p>\n          <p class="quick-help">Copy the CAT line exactly as shown, paste it here, then verify.</p>\n        </div>\n        <div class="quick-capture-row">\n          <input name="raw_oem_text" required autocomplete="off" placeholder="221-3133: Cable Assembly">\n          <button class="button" type="submit">Parse & Verify</button>\n        </div>\n      </form>\n      {% else %}\n      <section class="verified-summary">\n        <div class="verified-summary-heading">\n          <div>\n            <p class="eyebrow">CAT VERIFIED</p>\n            <h4>{{ part.oem_part_number }}</h4>\n            <p>{{ part.oem_description or "CAT OEM part" }}</p>\n          </div>\n          <form method="post"\n                action="/parts/{{ part.id }}/start-cat-verification"\n                target="_blank"\n                onsubmit="return confirm(\'Are you sure you want to re-verify this part? Current OEM: {{ part.oem_part_number }}. The existing CAT verification will remain saved until a new capture succeeds.\');">\n            <button class="button danger-outline" type="submit">Re-Verify in CAT</button>\n          </form>\n        </div>\n\n        <div class="verified-meta">\n          <span><strong>OEM:</strong> {{ part.oem_part_number }}</span>\n          <span><strong>Description:</strong> {{ part.oem_description or "—" }}</span>\n          {% if part.captured_at %}\n          <span><strong>Captured:</strong> {{ part.captured_at }}</span>\n          {% endif %}\n        </div>\n\n        {% if part.oem_dealer_name or part.oem_dealer_price is not none or part.oem_dealer_availability or part.oem_dealer_lead_time %}\n        <div class="dealer-import-card">\n          <p class="eyebrow">OEM DEALER</p>\n          <div class="dealer-import-grid">\n            <span><small>Dealer</small><strong>{{ part.oem_dealer_name or "CAT Dealer" }}</strong></span>\n            <span>\n              <small>Price</small>\n              <strong>\n                {% if part.oem_dealer_price is not none %}\n                ${{ "%.2f"|format(part.oem_dealer_price) }} USD\n                {% else %}\n                Not captured\n                {% endif %}\n              </strong>\n            </span>\n            <span><small>Availability</small><strong>{{ part.oem_dealer_availability or "Not captured" }}</strong></span>\n            <span><small>Lead time</small><strong>{{ part.oem_dealer_lead_time or "Not captured" }}</strong></span>\n          </div>\n        </div>\n        {% endif %}\n\n        {% if part.diagram_url or part.product_url %}\n        <div class="verified-links">\n          {% if part.diagram_url %}\n          <a href="{{ part.diagram_url }}" target="_blank" rel="noopener">Open machine-specific diagram</a>\n          {% endif %}\n          {% if part.product_url %}\n          <a href="{{ part.product_url }}" target="_blank" rel="noopener">Open CAT product page</a>\n          {% endif %}\n        </div>\n        {% endif %}\n      </section>\n      {% endif %}\n\n'
job = job[:quick_start] + new_block + job[quick_end + len('      </form>\n\n'):]

# Hide top Verify button once verified.
job = job.replace(
    '{% if job.manufacturer and job.manufacturer|upper in ["CAT", "CATERPILLAR"] %}',
    '{% if job.manufacturer and job.manufacturer|upper in ["CAT", "CATERPILLAR"] and part.verification_status != "VERIFIED" %}'
)

JOB.write_text(job)

css = CSS.read_text()
addition = '\n.verified-summary {\n  margin: 18px 18px 0;\n  padding: 18px;\n  border: 1px solid #a9d7b5;\n  border-radius: 10px;\n  background: #edf8f0;\n}\n.verified-summary-heading {\n  display: flex;\n  align-items: flex-start;\n  justify-content: space-between;\n  gap: 18px;\n}\n.verified-summary h4 { margin: 5px 0 2px; font-size: 22px; }\n.verified-summary p { margin: 0; color: var(--muted); }\n.verified-meta { margin-top: 14px; display: grid; gap: 6px; font-size: 13px; }\n.verified-links { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 10px; }\n.verified-links a {\n  padding: 8px 10px;\n  border: 1px solid #b8c6d2;\n  border-radius: 7px;\n  background: white;\n  color: #185a9d;\n  font-size: 13px;\n  font-weight: 750;\n  text-decoration: none;\n}\n.button.danger-outline { border: 1px solid #b64a4a; background: white; color: #9b2f2f; }\n.dealer-import-card {\n  margin-top: 14px;\n  padding: 14px;\n  border: 1px solid #d4c27a;\n  border-radius: 9px;\n  background: #fffbed;\n}\n.dealer-import-grid {\n  margin-top: 10px;\n  display: grid;\n  grid-template-columns: repeat(4, 1fr);\n  gap: 12px;\n}\n.dealer-import-grid span { display: grid; gap: 3px; }\n.dealer-import-grid small { color: var(--muted); }\ninput[readonly], textarea[readonly] { background: #f0f2f3; color: #4d5256; cursor: not-allowed; }\n@media (max-width: 700px) {\n  .verified-summary-heading { flex-direction: column; }\n  .verified-summary-heading form,\n  .verified-summary-heading .button { width: 100%; }\n  .dealer-import-grid { grid-template-columns: 1fr; }\n}\n'
if ".verified-summary {" not in css:
    CSS.write_text(css + addition)

print("PLG Core v0.7 installed successfully.")
print(f"Backup created at: {backup}")
