from pathlib import Path
from datetime import datetime
import re, shutil, subprocess
PROJECT=Path.home()/'Desktop'/'PLG-Core'
SOURCE=Path(__file__).resolve().parent/'payload'
LEGACY=PROJECT/'legacy_app.py'
BASE=PROJECT/'templates'/'base.html'
QUOTE_DOCS=PROJECT/'templates'/'quote_documents.html'
CSS=PROJECT/'static'/'app.css'
QUOTE_PDF=PROJECT/'plg_core'/'documents'/'quote_pdf.py'
INVOICE_PDF=PROJECT/'plg_core'/'documents'/'invoice_pdf.py'
PYTHON=PROJECT/'.venv'/'bin'/'python'
NEW=[PROJECT/'templates'/'invoices.html',PROJECT/'templates'/'invoice_documents.html']
MANAGED=[LEGACY,BASE,QUOTE_DOCS,CSS,INVOICE_PDF,*NEW]
for p in (LEGACY,BASE,QUOTE_DOCS,CSS,QUOTE_PDF,PYTHON):
    if not p.exists(): raise SystemExit(f'Required file not found: {p}')
backup=PROJECT/f"backup-commit-0009-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
backup.mkdir(parents=True)
for p in MANAGED:
    if p.exists():
        d=backup/p.relative_to(PROJECT); d.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(p,d)
def restore():
    for p in MANAGED:
        s=backup/p.relative_to(PROJECT)
        if s.exists(): p.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,p)
        elif p in NEW+[INVOICE_PDF] and p.exists(): p.unlink()
try:
    text=LEGACY.read_text()
    qimp='from plg_core.documents.quote_pdf import generate_quote_pdfs, quote_paths, sanitize_path_name'
    iimp='from plg_core.documents.invoice_pdf import generate_invoice_pdfs, invoice_paths'
    if iimp not in text:
        if qimp not in text: raise RuntimeError('Quote PDF import anchor not found')
        text=text.replace(qimp,qimp+'\n'+iimp,1)
    m=re.search(r'from fastapi\.responses import ([^\n]+)',text)
    if m and 'FileResponse' not in m.group(1): text=text[:m.start()]+f"from fastapi.responses import FileResponse, {m.group(1).strip()}"+text[m.end():]
    if 'CREATE TABLE IF NOT EXISTS invoices (' not in text:
        anchor='        connection.execute(\n            """\n            CREATE TABLE IF NOT EXISTS customer_transactions (\n'
        i=text.find(anchor)
        if i<0: raise RuntimeError('Customer transaction schema anchor not found')
        text=text[:i]+(SOURCE/'invoice_schema.txt').read_text()+text[i:]
    if '@app.get("/invoices", response_class=HTMLResponse)' not in text:
        anchor='@app.get("/suppliers", response_class=HTMLResponse)'
        i=text.find(anchor)
        if i<0: raise RuntimeError('Invoice route anchor not found')
        text=text[:i]+(SOURCE/'invoice_routes.txt').read_text()+text[i:]
    p=re.compile(r'@app\.get\("/quotes/\{quote_id\}/documents", response_class=HTMLResponse\)\ndef quote_documents\(request: Request, quote_id: int\):.*?(?=\n@app\.get\("/quotes/\{quote_id\}/customer/pdf"\))',re.S)
    text,n=p.subn((SOURCE/'quote_documents_route.txt').read_text(),text,count=1)
    if n!=1: raise RuntimeError('Quote documents route not updated')
    LEGACY.write_text(text)
    b=BASE.read_text()
    old='      <span class="disabled"><span class="nav-icon">▤</span><span>Invoices</span></span>'
    new='      <a class="{% if active_page == \'invoices\' %}active{% endif %}" href="/invoices">\n        <span class="nav-icon" aria-hidden="true">▤</span><span>Invoices</span>\n      </a>'
    if old in b: b=b.replace(old,new,1)
    elif 'href="/invoices"' not in b: raise RuntimeError('Invoices sidebar anchor not found')
    BASE.write_text(b)
    shutil.copy2(SOURCE/'templates'/'invoices.html',NEW[0])
    shutil.copy2(SOURCE/'templates'/'invoice_documents.html',NEW[1])
    shutil.copy2(SOURCE/'templates'/'quote_documents.html',QUOTE_DOCS)
    pdf=QUOTE_PDF.read_text()
    for old,new in [('generate_quote_pdfs','generate_invoice_pdfs'),('quote_paths','invoice_paths'),('quote_number','invoice_number'),('quote_date','invoice_date'),('"Quotes"','"Invoices"'),("'Quotes'","'Invoices'"),('QUOTE','INVOICE'),('Quote','Invoice'),('quote','invoice')]: pdf=pdf.replace(old,new)
    if 'def sanitize_path_name' not in pdf: raise RuntimeError('Invoice PDF transformation failed')
    INVOICE_PDF.write_text(pdf)
    css=CSS.read_text(); patch=(SOURCE/'static'/'invoice_foundation.css').read_text()
    if '/* Commit 0009 Invoice Foundation */' not in css: CSS.write_text(css+'\n'+patch)
    r=subprocess.run([str(PYTHON),'-c',"from legacy_app import initialize_database; initialize_database(); from app import app; p={getattr(r,'path',None) for r in app.routes}; assert '/invoices' in p and '/quotes/{quote_id}/convert-to-invoice' in p and '/invoices/{invoice_id}/documents' in p; from plg_core.documents.invoice_pdf import generate_invoice_pdfs, invoice_paths; print('ok')"],cwd=PROJECT,text=True,capture_output=True)
    if r.returncode!=0: raise RuntimeError(r.stdout+r.stderr)
except Exception as exc:
    restore()
    raise SystemExit('Commit 0009 failed and files were restored.\n'+str(exc))
print('PLG Commit 0009 Invoice Foundation installed successfully.')
print('Quote-to-Invoice conversion, invoice register, two PDFs, quote archiving, customer credit, and customer invoice folders are active.')
print(f'Backup created at: {backup}')
