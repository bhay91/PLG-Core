from pathlib import Path
import shutil
from datetime import datetime

PROJECT = Path.home() / "Desktop" / "PLG-Core-Starter-v1"
JOB_TEMPLATE = PROJECT / "templates" / "job_detail.html"
CSS = PROJECT / "static" / "app.css"

for path in (JOB_TEMPLATE, CSS):
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = PROJECT / f"backup-v0.6-{stamp}"
(backup / "templates").mkdir(parents=True)
(backup / "static").mkdir(parents=True)

shutil.copy2(JOB_TEMPLATE, backup / "templates" / "job_detail.html")
shutil.copy2(CSS, backup / "static" / "app.css")

job_text = JOB_TEMPLATE.read_text()

start_marker = '      <form method="post" action="/parts/{{ part.id }}/quick-capture" class="quick-capture-panel">'
end_marker = '      </form>\n\n      <div class="part-columns">'

start = job_text.find(start_marker)
end = job_text.find(end_marker, start)

if start == -1 or end == -1:
    raise SystemExit(
        "Could not find the OEM Quick Capture section. "
        "Confirm PLG Core v0.4 or later is installed."
    )

new_quick_block = '''      {% if part.verification_status != "VERIFIED" %}
      <form method="post" action="/parts/{{ part.id }}/quick-capture" class="quick-capture-panel">
        <div>
          <p class="eyebrow">OEM QUICK CAPTURE</p>
          <p class="quick-help">
            Copy the CAT line exactly as shown, paste it here, then verify.
          </p>
        </div>

        <div class="quick-capture-row">
          <input
            name="raw_oem_text"
            required
            autocomplete="off"
            placeholder="221-3133: Cable Assembly"
          >
          <button class="button" type="submit">Parse & Verify</button>
        </div>
      </form>
      {% else %}
      <section class="verified-summary">
        <div class="verified-summary-heading">
          <div>
            <p class="eyebrow">CAT VERIFIED</p>
            <h4>{{ part.oem_part_number }}</h4>
            <p>{{ part.oem_description or "CAT OEM part" }}</p>
          </div>

          <form
            method="post"
            action="/parts/{{ part.id }}/start-cat-verification"
            target="_blank"
            onsubmit="return confirm('Are you sure you want to re-verify this part? Current OEM: {{ part.oem_part_number }}. The existing CAT verification will remain saved until a new capture succeeds.');"
          >
            <button class="button danger-outline" type="submit">
              Re-Verify in CAT
            </button>
          </form>
        </div>

        <div class="verified-meta">
          <span><strong>OEM:</strong> {{ part.oem_part_number }}</span>
          <span><strong>Description:</strong> {{ part.oem_description or "—" }}</span>
          {% if part.captured_at %}
          <span><strong>Captured:</strong> {{ part.captured_at }}</span>
          {% endif %}
        </div>

        {% if part.diagram_url or part.product_url %}
        <div class="verified-links">
          {% if part.diagram_url %}
          <a href="{{ part.diagram_url }}" target="_blank" rel="noopener">
            Open machine-specific diagram
          </a>
          {% endif %}

          {% if part.product_url %}
          <a href="{{ part.product_url }}" target="_blank" rel="noopener">
            Open CAT product page
          </a>
          {% endif %}
        </div>
        {% endif %}
      </section>
      {% endif %}

'''

job_text = job_text[:start] + new_quick_block + job_text[end + len('      </form>\n\n'):]

old_header_form = '''          {% if job.manufacturer and job.manufacturer|upper in ["CAT", "CATERPILLAR"] %}
          <form method="post" action="/parts/{{ part.id }}/start-cat-verification" target="_blank">
            <button class="button cat-button" type="submit">
              {% if active_part_id == part.id %}CAT Verification Active{% else %}Verify in CAT{% endif %}
            </button>
          </form>
          {% endif %}'''

new_header_form = '''          {% if job.manufacturer and job.manufacturer|upper in ["CAT", "CATERPILLAR"] and part.verification_status != "VERIFIED" %}
          <form method="post" action="/parts/{{ part.id }}/start-cat-verification" target="_blank">
            <button class="button cat-button" type="submit">
              {% if active_part_id == part.id %}CAT Verification Active{% else %}Verify in CAT{% endif %}
            </button>
          </form>
          {% endif %}'''

if old_header_form in job_text:
    job_text = job_text.replace(old_header_form, new_header_form, 1)

job_text = job_text.replace(
    'name="oem_part_number" value="{{ part.oem_part_number or \'\' }}" placeholder="221-3133">',
    'name="oem_part_number" value="{{ part.oem_part_number or \'\' }}" placeholder="221-3133" {% if part.verification_status == "VERIFIED" %}readonly{% endif %}>'
)

job_text = job_text.replace(
    'name="oem_description" value="{{ part.oem_description or \'\' }}" placeholder="Cable Assembly (Governor Control)">',
    'name="oem_description" value="{{ part.oem_description or \'\' }}" placeholder="Cable Assembly (Governor Control)" {% if part.verification_status == "VERIFIED" %}readonly{% endif %}>'
)

JOB_TEMPLATE.write_text(job_text)

css_text = CSS.read_text()

css_addition = '''
.verified-summary {
  margin: 18px 18px 0;
  padding: 18px;
  border: 1px solid #a9d7b5;
  border-radius: 10px;
  background: #edf8f0;
}

.verified-summary-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.verified-summary h4 {
  margin: 5px 0 2px;
  font-size: 22px;
}

.verified-summary p {
  margin: 0;
  color: var(--muted);
}

.verified-meta {
  margin-top: 14px;
  display: grid;
  gap: 6px;
  font-size: 13px;
}

.verified-links {
  margin-top: 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.verified-links a {
  padding: 8px 10px;
  border: 1px solid #b8c6d2;
  border-radius: 7px;
  background: white;
  color: #185a9d;
  font-size: 13px;
  font-weight: 750;
  text-decoration: none;
}

.verified-links a:hover {
  text-decoration: underline;
}

.button.danger-outline {
  border: 1px solid #b64a4a;
  background: white;
  color: #9b2f2f;
}

input[readonly],
textarea[readonly] {
  background: #f0f2f3;
  color: #4d5256;
  cursor: not-allowed;
}

@media (max-width: 700px) {
  .verified-summary-heading {
    flex-direction: column;
  }

  .verified-summary-heading form,
  .verified-summary-heading .button {
    width: 100%;
  }
}
'''

if ".verified-summary {" not in css_text:
    CSS.write_text(css_text + css_addition)

print("PLG Core v0.6 Verification UI installed successfully.")
print(f"Backup created at: {backup}")
