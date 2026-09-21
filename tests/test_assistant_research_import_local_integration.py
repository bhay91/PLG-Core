"""Offline STEP 3D integration through both adapters and the real PPS staging route.

Run with the harness site-packages on PYTHONPATH (see operator integration notes).
The existing connector fixture redirects every PPS store into a TemporaryDirectory.
"""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

HARNESS = Path(__file__).resolve().parents[2] / 'pps-ai-harness'
_original_sys_path = list(sys.path)
_preexisting_app_modules = {
    name: module for name, module in sys.modules.items()
    if name == 'app' or name.startswith('app.')
}
try:
    sys.path.insert(0, str(HARNESS))
    import httpx
    from fastapi import FastAPI
    from app import assistant_research_import as adapter
    from app.research_import import ResearchImportPublisher, build_research_import_package
finally:
    sys.path[:] = _original_sys_path
    for name in list(sys.modules):
        if (name == 'app' or name.startswith('app.')) and name not in _preexisting_app_modules:
            del sys.modules[name]
    sys.modules.update(_preexisting_app_modules)
import test_research_import_connector as connector
PDF_BYTES = connector.PDF_BYTES
import legacy_app
from plg_core.application import app as pps_app

WEBUI = HARNESS.parent / 'Open-WebUI-Dev/backend/open_webui/utils/pps_research_import.py'
spec = importlib.util.spec_from_file_location('step3d_webui_integration', WEBUI)
webui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webui)


def test_assistant_to_real_disposable_pps_stays_draft(monkeypatch, tmp_path):
    fixture = connector.ResearchImportConnectorTests()
    fixture.setUp()
    try:
        assert Path(legacy_app.DB_PATH).is_relative_to(Path(fixture.temp.name))
        def no_network(*args, **kwargs):
            pytest.fail('Network forbidden; only ASGI disposable transports permitted')
        monkeypatch.setattr(socket.socket, 'connect', no_network)
        monkeypatch.setenv('PPS_ASSISTANT_IMPORT_KEY', 'synthetic-private-bridge-credential-00000')
        monkeypatch.setenv('PPS_RESEARCH_IMPORT_BASE_URL', 'http://127.0.0.1:8000')
        calls = []
        class PPSTransport(httpx.ASGITransport):
            async def handle_async_request(self, request):
                assert request.method == 'POST'
                assert request.url.path == '/api/extension/v1/research-import/packages'
                calls.append(request.url.path)
                return await super().handle_async_request(request)
        publisher = ResearchImportPublisher('http://127.0.0.1:8000', 'research-import-connector-test-token',
                                            transport=PPSTransport(app=pps_app, client=('127.0.0.1', 12345)))
        monkeypatch.setattr(adapter, 'ResearchImportPublisher', lambda: publisher)
        harness_app = FastAPI(); harness_app.include_router(adapter.router)
        monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kwargs: httpx.ASGITransport(app=harness_app))
        pdf = tmp_path/'research.pdf'; pdf.write_bytes(PDF_BYTES)
        data = {'schema_version': '1', **fixture.package(package_id='SYNTHETIC-ASSISTANT-INTEGRATION')}
        package = build_research_import_package(pdf_bytes=PDF_BYTES, filename=pdf.name, package_data=data)
        sidecar = tmp_path/'sidecar.json'; sidecar.write_text(package.model_dump_json())
        records = {name: SimpleNamespace(user_id='synthetic-operator', filename=p.name, path=str(p),
                    meta={'content_type':mime}) for name,p,mime in [('pdf',pdf,'application/pdf'),('json',sidecar,'application/json')]}
        async def get(id): return records.get(id)
        monkeypatch.setitem(sys.modules, 'open_webui.config', SimpleNamespace(UPLOAD_DIR=tmp_path, STORAGE_PROVIDER='local'))
        monkeypatch.setitem(sys.modules, 'open_webui.env', SimpleNamespace(DATA_DIR=tmp_path))
        monkeypatch.setitem(sys.modules, 'open_webui.models.files', SimpleNamespace(Files=SimpleNamespace(get_file_by_id=get)))
        async def read_local_without_thread(func, *args, **kwargs):
            return func(*args, **kwargs)
        monkeypatch.setattr(webui, 'asyncio', SimpleNamespace(to_thread=read_local_without_thread))
        current={'id':'message-1','role':'user','content':'/stage_research_import',
                 'files':[{'id':'pdf','type':'file'},{'id':'json','type':'file'}]}
        form={'model':'pps-ai-harness','messages':[current]}
        meta={'user_message':current,'user_message_id':'message-1','chat_id':'synthetic-chat','session_id':'synthetic-session'}
        def snapshot():
            with legacy_app.get_connection() as db:
                tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                return {t:[tuple(r) for r in db.execute('SELECT * FROM "'+t+'"')] for t in tables}
        before=snapshot()
        result=asyncio.run(webui.stage_research_import(form,meta,SimpleNamespace(id='synthetic-operator')))
        assert result['outcome']=='ACCEPTED' and result['status']=='DRAFT' and result['review_required']
        assert len(calls)==1 and not result['duplicate']
        proposal_id=result['proposal_id']
        with legacy_app.get_connection() as db:
            row=db.execute('SELECT status,review_state,created_request_id,created_job_id,confirmed_at FROM intake_proposals WHERE id=?',(proposal_id,)).fetchone()
            assert tuple(row)==('DRAFT','REVIEW',None,None,None)
        after=snapshot()
        assert len(after['intake_proposals'])==len(before['intake_proposals'])+1
        allowed={'intake_proposals','intake_proposal_assets','intake_proposal_needs','intake_proposal_contributions',
                 'intake_proposal_attachments','intake_proposal_identifiers','audit_logs','sqlite_sequence'}
        assert {t:rows for t,rows in before.items() if t not in allowed} == {t:rows for t,rows in after.items() if t not in allowed}
        # A reply regeneration returns the saved result without another submission.
        assert asyncio.run(webui.stage_research_import(form,meta,SimpleNamespace(id='synthetic-operator')))==result
        assert len(calls)==1
        # A separately requested identical replay reaches existing PPS idempotency.
        current['id']='message-2';meta['user_message_id']='message-2'
        replay=asyncio.run(webui.stage_research_import(form,meta,SimpleNamespace(id='synthetic-operator')))
        assert replay['duplicate'] and replay['proposal_id']==proposal_id and len(calls)==2
        assert snapshot()==after
        # Changed PDF / unchanged sidecar is rejected before the PPS POST.
        pdf.write_bytes(PDF_BYTES+b'changed')
        current['id']='message-3';meta['user_message_id']='message-3'
        rejected=asyncio.run(webui.stage_research_import(form,meta,SimpleNamespace(id='synthetic-operator')))
        assert rejected['outcome']=='REJECTED' and len(calls)==2
        assert snapshot()==after
        assert 'research-import-connector-test-token' not in json.dumps([result,replay,rejected])
        assert pdf.exists() and sidecar.exists()
    finally:
        fixture.tearDown()
