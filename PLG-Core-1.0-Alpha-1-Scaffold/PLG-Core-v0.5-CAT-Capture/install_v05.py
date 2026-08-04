from pathlib import Path
import shutil
from datetime import datetime

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
APP = PROJECT / "app.py"
JOB_TEMPLATE = PROJECT / "templates" / "job_detail.html"
CSS = PROJECT / "static" / "app.css"

for path in (APP, JOB_TEMPLATE, CSS):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.5-{stamp}"
(backup / "templates").mkdir(parents=True)
(backup / "static").mkdir(parents=True)

shutil.copy2(APP, backup / "app.py")
shutil.copy2(JOB_TEMPLATE, backup / "templates" / "job_detail.html")
shutil.copy2(CSS, backup / "static" / "app.css")

app_text = APP.read_text()

migration_anchor = '''            "customer_unit_price": "ALTER TABLE job_parts ADD COLUMN customer_unit_price REAL",\n'''
migration_replacement = '''            "customer_unit_price": "ALTER TABLE job_parts ADD COLUMN customer_unit_price REAL",\n            "diagram_url": "ALTER TABLE job_parts ADD COLUMN diagram_url TEXT",\n            "product_url": "ALTER TABLE job_parts ADD COLUMN product_url TEXT",\n            "captured_at": "ALTER TABLE job_parts ADD COLUMN captured_at TEXT",\n'''

if '"diagram_url": "ALTER TABLE job_parts ADD COLUMN diagram_url TEXT"' not in app_text:
    if migration_anchor not in app_text:
        raise SystemExit("Could not find the job_parts migration block in app.py.")
    app_text = app_text.replace(migration_anchor, migration_replacement, 1)

old_payload = '''    diagram_name = str(payload.get("diagram_name", "")).strip()\n    callout_number = str(payload.get("callout_number", "")).strip()\n    source_url = str(payload.get("source_url", "")).strip()\n    verification_notes = str(payload.get("verification_notes", "")).strip()\n'''
new_payload = '''    diagram_name = str(payload.get("diagram_name", "")).strip()\n    callout_number = str(payload.get("callout_number", "")).strip()\n    source_url = str(payload.get("source_url", "")).strip()\n    diagram_url = str(payload.get("diagram_url", "")).strip()\n    product_url = str(payload.get("product_url", "")).strip()\n    verification_notes = str(payload.get("verification_notes", "")).strip()\n'''

if "diagram_url = str(payload.get" not in app_text:
    if old_payload not in app_text:
        raise SystemExit("Could not find the capture payload section in app.py.")
    app_text = app_text.replace(old_payload, new_payload, 1)

old_update = '''                source_url = ?,\n                verification_notes = ?,\n                verification_status = 'VERIFIED'\n            WHERE id = ?\n            """,\n            (\n                oem_part_number,\n                oem_description,\n                diagram_name,\n                callout_number,\n                source_url,\n                verification_notes,\n                part_id,\n            ),\n'''
new_update = '''                source_url = ?,\n                diagram_url = ?,\n                product_url = ?,\n                captured_at = CURRENT_TIMESTAMP,\n                verification_notes = ?,\n                verification_status = 'VERIFIED'\n            WHERE id = ?\n            """,\n            (\n                oem_part_number,\n                oem_description,\n                diagram_name,\n                callout_number,\n                source_url,\n                diagram_url,\n                product_url,\n                verification_notes,\n                part_id,\n            ),\n'''

if "captured_at = CURRENT_TIMESTAMP" not in app_text:
    if old_update not in app_text:
        raise SystemExit("Could not find the capture UPDATE statement in app.py.")
    app_text = app_text.replace(old_update, new_update, 1)

APP.write_text(app_text)

job_text = JOB_TEMPLATE.read_text()
evidence_panel = '''\n          {% if part.diagram_url or part.product_url %}\n          <div class="capture-evidence">\n            <strong>CAT evidence</strong>\n            {% if part.diagram_url %}\n            <a href="{{ part.diagram_url }}" target="_blank" rel="noopener">Open machine-specific diagram</a>\n            {% endif %}\n            {% if part.product_url %}\n            <a href="{{ part.product_url }}" target="_blank" rel="noopener">Open CAT product page</a>\n            {% endif %}\n            {% if part.captured_at %}\n            <small>Captured {{ part.captured_at }}</small>\n            {% endif %}\n          </div>\n          {% endif %}\n'''
target = '''          <button class="button" type="submit">Save OEM Verification</button>\n        </form>\n'''

if 'class="capture-evidence"' not in job_text:
    if target not in job_text:
        raise SystemExit("Could not find the OEM verification form in job_detail.html.")
    job_text = job_text.replace(target, '          <button class="button" type="submit">Save OEM Verification</button>\n' + evidence_panel + '        </form>\n', 1)

JOB_TEMPLATE.write_text(job_text)

css_text = CSS.read_text()
evidence_css = '''\n\n.capture-evidence {\n  margin-top: 16px;\n  padding: 12px;\n  display: grid;\n  gap: 7px;\n  border: 1px solid #d9dde1;\n  border-radius: 8px;\n  background: #f6f8f9;\n}\n\n.capture-evidence a {\n  color: #185a9d;\n  font-weight: 700;\n  text-decoration: none;\n}\n\n.capture-evidence a:hover { text-decoration: underline; }\n.capture-evidence small { color: var(--muted); }\n'''

if ".capture-evidence {" not in css_text:
    CSS.write_text(css_text + evidence_css)

print("PLG Core v0.5 CAT Capture installed successfully.")
print(f"Backup created at: {backup}")
