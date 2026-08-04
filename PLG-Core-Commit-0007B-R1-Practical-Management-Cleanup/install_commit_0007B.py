from pathlib import Path
from datetime import datetime
import json, re, shutil, subprocess
PROJECT=Path.home()/'Desktop'/'PLG-Core'
SOURCE=Path(__file__).resolve().parent/'payload'
LEGACY=PROJECT/'legacy_app.py'
CSS=PROJECT/'static'/'app.css'
PYTHON=PROJECT/'.venv'/'bin'/'python'
TEMPLATES=PROJECT/'templates'
managed=[LEGACY,CSS]+[TEMPLATES/x for x in ('suppliers.html','supplier_form.html','connectors.html','connector_form.html','quotes.html')]
for p in (LEGACY,CSS,PYTHON):
    if not p.exists(): raise SystemExit(f'Required file not found: {p}')
backup=PROJECT/f"backup-commit-0007B-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
backup.mkdir(parents=True)
for p in managed:
    if p.exists():
        d=backup/p.relative_to(PROJECT); d.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,d)
def restore():
    for p in managed:
        s=backup/p.relative_to(PROJECT)
        if s.exists(): p.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,p)
try:
    text=LEGACY.read_text()
    schema=json.loads((SOURCE/'schema.json').read_text())
    if 'supplier_additions' not in text:
        anchor='        existing = column_names(connection, "job_parts")\n'
        i=text.find(anchor)
        if i<0: raise RuntimeError('Supplier schema anchor not found')
        text=text[:i]+schema['supplier']+text[i:]
    if 'connector_columns =' not in text:
        anchor='        default_connectors = [\n'
        i=text.find(anchor)
        if i<0: raise RuntimeError('Connector schema anchor not found')
        text=text[:i]+schema['connector']+text[i:]
    if 'quote_columns =' not in text:
        anchor='        connection.execute(\n            """\n            CREATE TABLE IF NOT EXISTS quote_items (\n'
        i=text.find(anchor)
        if i<0: raise RuntimeError('Quote schema anchor not found')
        text=text[:i]+schema['quote']+text[i:]
    p=re.compile(r'@app\.get\("/suppliers", response_class=HTMLResponse\).*?(?=\n@app\.get\("/quotes", response_class=HTMLResponse\))',re.S)
    text,n=p.subn((SOURCE/'supplier_routes.txt').read_text(),text,count=1)
    if n!=1: raise RuntimeError('Supplier routes not replaced')
    p=re.compile(r'@app\.get\("/quotes", response_class=HTMLResponse\).*?(?=\n@app\.get\("/quotes/\{quote_id\}/documents", response_class=HTMLResponse\))',re.S)
    text,n=p.subn((SOURCE/'quote_routes.txt').read_text(),text,count=1)
    if n!=1: raise RuntimeError('Quote routes not replaced')
    p=re.compile(r'@app\.get\("/connectors", response_class=HTMLResponse\).*?(?=\n@app\.get\("/api/active-source-import"\))',re.S)
    text,n=p.subn((SOURCE/'connector_routes.txt').read_text(),text,count=1)
    if n!=1: raise RuntimeError('Connector routes not replaced')
    LEGACY.write_text(text)
    for name in ('suppliers.html','supplier_form.html','connectors.html','connector_form.html','quotes.html'): shutil.copy2(SOURCE/'templates'/name,TEMPLATES/name)
    css=CSS.read_text(); patch=(SOURCE/'static'/'commit-0007B.css').read_text()
    if '/* Commit 0007B practical management cleanup */' not in css: CSS.write_text(css+'\n'+patch)
    r=subprocess.run([str(PYTHON),'-c',"from legacy_app import initialize_database; initialize_database(); from app import app; p={getattr(r,'path',None) for r in app.routes}; assert '/suppliers/new' in p and '/connectors/new' in p and '/quotes/{quote_id}/archive' in p; print('ok')"],cwd=PROJECT,text=True,capture_output=True)
    if r.returncode!=0: raise RuntimeError(r.stdout+r.stderr)
except Exception as exc:
    restore()
    raise SystemExit('Commit 0007B failed and files were restored.\n'+str(exc))
print('PLG Commit 0007B installed successfully.')
print('Suppliers, connectors, and quote archiving were updated.')
print(f'Backup created at: {backup}')
