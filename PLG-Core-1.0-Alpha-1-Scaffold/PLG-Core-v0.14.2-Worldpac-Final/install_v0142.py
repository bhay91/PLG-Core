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
backup = PROJECT / f"backup-v0.14.2-{stamp}"
(backup / "templates").mkdir(parents=True)
shutil.copy2(APP, backup / "app.py")
shutil.copy2(JOB, backup / "templates" / "job_detail.html")
shutil.copy2(BASE, backup / "templates" / "base.html")

app = APP.read_text()

# Add source-import summaries to job_detail.
if '"source_imports": source_imports' not in app:
    anchor = '''        connectors = connection.execute(
            """
            SELECT *
            FROM connector_profiles
            WHERE is_enabled = 1
            ORDER BY sort_order, display_name
            """
        ).fetchall()
'''
    addition = anchor + '''

        source_imports = connection.execute(
            """
            SELECT
                source_cart_imports.id,
                source_cart_imports.source_name,
                source_cart_imports.shipping_total,
                source_cart_imports.currency,
                source_cart_imports.imported_at,
                COALESCE(SUM(part_sources.supplier_cost * job_parts.quantity), 0) AS parts_total
            FROM source_cart_imports
            LEFT JOIN job_parts
                ON job_parts.job_id = source_cart_imports.job_id
            LEFT JOIN part_sources
                ON part_sources.part_id = job_parts.id
               AND LOWER(TRIM(part_sources.supplier_name)) = LOWER(TRIM(source_cart_imports.source_name))
            WHERE source_cart_imports.job_id = ?
            GROUP BY source_cart_imports.id
            ORDER BY source_cart_imports.id DESC
            """
            (job_id,),
        ).fetchall()
'''
    if anchor not in app:
        raise SystemExit("Could not find connector query in job_detail.")
    app = app.replace(anchor, addition, 1)

    context_anchor = '"connectors": connectors,'
    if context_anchor not in app:
        raise SystemExit("Could not find job_detail template context.")
    app = app.replace(
        context_anchor,
        context_anchor + '\n            "source_imports": source_imports,',
        1,
    )

APP.write_text(app)

job = JOB.read_text()

summary_section = '''
<section class="panel source-import-summary">
  <div class="panel-heading">
    <div>
      <p class="eyebrow">SOURCE IMPORTS</p>
      <h2>Imported cart costs</h2>
    </div>
  </div>

  {% if source_imports %}
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Source</th>
          <th>Parts</th>
          <th>Shipping</th>
          <th>Supplier Total</th>
        </tr>
      </thead>
      <tbody>
        {% for source_import in source_imports %}
        <tr>
          <td>
            <strong>{{ source_import.source_name }}</strong>
            <div class="muted">{{ source_import.imported_at }}</div>
          </td>
          <td>${{ "%.2f"|format(source_import.parts_total or 0) }}</td>
          <td>
            {% if source_import.shipping_total is not none %}
            ${{ "%.2f"|format(source_import.shipping_total) }}
            {% else %}
            —
            {% endif %}
          </td>
          <td>
            <strong>
              ${{ "%.2f"|format((source_import.parts_total or 0) + (source_import.shipping_total or 0)) }}
            </strong>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <p class="muted">No supplier carts imported yet.</p>
  {% endif %}
</section>
'''

if "SOURCE IMPORTS" not in job:
    marker = '<section class="detail-grid">'
    if marker not in job:
        raise SystemExit("Could not find detail-grid insertion point.")
    job = job.replace(marker, summary_section + "\n" + marker, 1)

JOB.write_text(job)

base = BASE.read_text()
base = re.sub(
    r"PLG Core v\d+\.\d+(?:\.\d+)?",
    "PLG Core v0.14.2",
    base,
)
BASE.write_text(base)

# Correct the known test import if it is still the erroneous subtotal value.
import sqlite3
db_path = PROJECT / "data" / "plg_core.db"
if db_path.exists():
    db = sqlite3.connect(db_path)
    db.execute(
        """
        UPDATE source_cart_imports
        SET shipping_total = 45.14
        WHERE id = 1
          AND job_id = 3
          AND source_name = 'Worldpac'
          AND ABS(COALESCE(shipping_total, 0) - 56.76) < 0.001
        """
    )
    db.commit()
    db.close()

print("PLG Core v0.14.2 Worldpac Final installed successfully.")
print(f"Backup created at: {backup}")