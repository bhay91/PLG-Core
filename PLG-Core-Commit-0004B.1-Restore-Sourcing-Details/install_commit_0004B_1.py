from pathlib import Path
from datetime import datetime
import shutil, subprocess

PROJECT=Path.home()/'Desktop'/'PLG-Core'
ROUTES=PROJECT/'plg_core'/'basket'/'routes.py'
BASKET=PROJECT/'templates'/'basket.html'
CSS=PROJECT/'static'/'app.css'
PYTHON=PROJECT/'.venv'/'bin'/'python'
SOURCE=Path(__file__).resolve().parent/'payload'

for p in (ROUTES,BASKET,CSS,PYTHON):
    if not p.exists(): raise SystemExit(f'Required file not found: {p}')

stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
backup=PROJECT/f'backup-commit-0004B.1-{stamp}'
backup.mkdir(parents=True)
for p in (ROUTES,BASKET,CSS):
    s=backup/p.relative_to(PROJECT); s.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,s)

def restore():
    for s in backup.rglob('*'):
        if s.is_file():
            t=PROJECT/s.relative_to(backup); t.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,t)

try:
    routes=ROUTES.read_text()
    anchor='''        connectors = connection.execute(
            """
            SELECT * FROM connector_profiles
            WHERE is_enabled=1 ORDER BY display_name
            """
        ).fetchall()
'''
    addition=anchor+'''
        parts = connection.execute("SELECT * FROM job_parts WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        source_rows = connection.execute("""
            SELECT part_sources.* FROM part_sources
            JOIN job_parts ON job_parts.id = part_sources.part_id
            WHERE job_parts.job_id = ?
            ORDER BY part_sources.part_id, part_sources.selected_for_quote DESC, part_sources.id
        """, (job_id,)).fetchall()
        sources_by_part = {}
        for source in source_rows:
            sources_by_part.setdefault(source["part_id"], []).append(source)
        selected_count = sum(1 for part in parts if any(source["selected_for_quote"] for source in sources_by_part.get(part["id"], [])))
        ready_for_quote = bool(parts) and selected_count == len(parts)
        source_imports = connection.execute("""
            SELECT source_cart_imports.id, source_cart_imports.source_name,
                   source_cart_imports.shipping_total, source_cart_imports.currency,
                   source_cart_imports.imported_at,
                   COALESCE(SUM(part_sources.supplier_cost * job_parts.quantity), 0) AS parts_total
            FROM source_cart_imports
            LEFT JOIN job_parts ON job_parts.job_id = source_cart_imports.job_id
            LEFT JOIN part_sources ON part_sources.part_id = job_parts.id
              AND LOWER(TRIM(part_sources.supplier_name)) = LOWER(TRIM(source_cart_imports.source_name))
            WHERE source_cart_imports.job_id = ?
            GROUP BY source_cart_imports.id
            ORDER BY source_cart_imports.id DESC
        """, (job_id,)).fetchall()
'''
    if 'source_imports = connection.execute(' not in routes:
        if anchor not in routes: raise RuntimeError('Basket route query anchor not found')
        routes=routes.replace(anchor,addition,1)
    context='''            "connectors": connectors,
            "active_page": "jobs",
'''
    newcontext='''            "connectors": connectors,
            "parts": parts,
            "sources_by_part": sources_by_part,
            "selected_count": selected_count,
            "ready_for_quote": ready_for_quote,
            "source_imports": source_imports,
            "active_page": "jobs",
'''
    if '"source_imports": source_imports' not in routes:
        if context not in routes: raise RuntimeError('Basket route context anchor not found')
        routes=routes.replace(context,newcontext,1)
    ROUTES.write_text(routes)

    basket=BASKET.read_text()
    if 'REQUESTED PARTS &amp; SOURCES' not in basket:
        target='<section class="panel basket-workspace">'
        if target not in basket: raise RuntimeError('Basket insertion point not found')
        basket=basket.replace(target,(SOURCE/'templates'/'insert.html').read_text()+'\n'+target,1)
        BASKET.write_text(basket)

    css=CSS.read_text()
    marker='/* Commit 0004B.1 Restore Sourcing Details */'
    if marker not in css:
        CSS.write_text(css+'\n'+(SOURCE/'static'/'patch.css').read_text())

    smoke=subprocess.run([str(PYTHON),'-c',"from app import app; from plg_core.basket.routes import router; assert any(r.path == '/jobs/{job_id}/basket' for r in router.routes); print('ok')"],cwd=PROJECT,text=True,capture_output=True)
    if smoke.returncode!=0: raise RuntimeError(smoke.stdout+smoke.stderr)
except Exception as exc:
    restore(); raise SystemExit(f'Commit 0004B.1 failed and files were restored.\n{exc}')

print('PLG Commit 0004B.1 Restore Sourcing Details installed successfully.')
print(f'Backup created at: {backup}')
