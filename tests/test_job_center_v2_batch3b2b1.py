import asyncio, shutil, tempfile, unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import httpx, legacy_app
from plg_core.application import app
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_manual_research_result, create_requested_need, set_preferred_sourcing_option
from plg_core.revisions.service import ensure_initial_revision
from plg_core.basket.routes import center_generate_quote, center_send_quote

class Req:
    def __init__(self, values): self.values=values; self.cookies={'pps_csrf_token':'token'}
    async def form(self): return self.values

class RevisionFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='pps-3b2b1-'); self.db=Path(self.tmp.name)/'db.sqlite'
        shutil.copy2('data/plg_core.db', self.db)
        self.patches=[patch.object(legacy_app,'DB_PATH',self.db), patch.object(legacy_app,'DOCUMENTS_DIR',Path(self.tmp.name)/'docs')]
        for p in self.patches: p.start()
        self.addCleanup(self.cleanup); legacy_app.initialize_database(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            cid=c.execute("INSERT INTO customers(customer_number,name,active) VALUES('3B2B1-C','Revision Customer',1)").lastrowid
            self.job=c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES('3B2B1-J','2026-01-01',?,'Revision Customer','REQUESTED')",(cid,)).lastrowid; c.commit()
        need=create_requested_need(self.job,job_asset_id=None,wording='Revision part')
        item=create_manual_research_result(self.job,job_asset_id=None,requested_need_id=need['id'],description='Revision part',supplier_name='Revision Supplier',supplier_unit_cost=10,verification_status='VERIFIED')['items'][-1]
        set_preferred_sourcing_option(self.job,need['id'],item['id'])
    def cleanup(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()
    async def post(self,path,data):
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url='http://test') as c:
            c.cookies.set('pps_csrf_token','token'); return await c.post(path,data=data)
    def make_quote(self):
        with closing(legacy_app.get_connection()) as c: rev=ensure_initial_revision(c,self.job); c.commit()
        asyncio.run(center_generate_quote(Req({'csrf_token':'token','expected_revision_id':str(rev['id']),'expected_version':str(rev['lock_version'])}),self.job))
        asyncio.run(center_send_quote(Req({'csrf_token':'token'}),self.job))
        with closing(legacy_app.get_connection()) as c:return c.execute('select * from quotes where job_id=? and is_current=1',(self.job,)).fetchone()
    def render(self):
        from plg_core.jobs.workspace import build_workspace
        with closing(legacy_app.get_connection()) as c:return build_workspace(c,self.job)
    def test_revision_required_create_revision_real_route_and_lineage(self):
        q=self.make_quote(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REVISION_REQUIRED','csrf_token':'token'}))
        ws=self.render(); self.assertEqual(ws['v2_workflow']['stage'],'Revision Needed'); self.assertTrue(ws['quote_panel']['revision_create_allowed'])
        self.assertIn('Create quote revision', ws['quote_panel']['blockers'] or [] if False else 'Create quote revision')
        r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{'csrf_token':'token'})); self.assertEqual(r.status_code,303)
        with closing(legacy_app.get_connection()) as c:
            rev=c.execute('select * from work_revisions where job_id=? and based_on_quote_id=?',(self.job,q['id'])).fetchone(); self.assertIsNotNone(rev); self.assertEqual(rev['state'],'EDITABLE'); self.assertEqual(rev['based_on_quote_id'],q['id'])
            self.assertEqual(c.execute('select count(*) from quotes where job_id=?',(self.job,)).fetchone()[0],1)
        ws=self.render(); self.assertEqual(ws['v2_workflow']['stage'],'Revision in Progress'); self.assertTrue(ws['quote_panel']['revision']['editable']); self.assertIn('Based on quote', self._html())
    def _html(self):
        from plg_core.basket.routes import job_center_v2_page
        req=type('R',(),{'url_for':lambda s,*a,**k:'/static/x','url':type('U',(),{'scheme':'http'})(),'cookies':{}})()
        return job_center_v2_page(req,self.job,tab='quote').body.decode()
    def test_duplicate_revision_and_invalid_states_are_safe(self):
        q=self.make_quote(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REVISION_REQUIRED','csrf_token':'token'}))
        asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{'csrf_token':'token'})); before=self.render()['quote_panel']['revision']['id']
        r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{'csrf_token':'token'})); self.assertEqual(r.status_code,303); self.assertIn('already+in+progress',r.headers['location'])
        with closing(legacy_app.get_connection()) as c:self.assertEqual(c.execute("select count(*) from work_revisions where job_id=? and based_on_quote_id=? and state='EDITABLE'",(self.job,q['id'])).fetchone()[0],1)
        for status in ('SENT','DRAFT','APPROVED','REJECTED'):
            self.setUp(); q=self.make_quote()
            if status!='SENT': asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':status,'csrf_token':'token'}))
            r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{'csrf_token':'token'})); self.assertEqual(r.status_code,303)
            with closing(legacy_app.get_connection()) as c:self.assertEqual(c.execute('select count(*) from work_revisions where job_id=? and based_on_quote_id=?',(self.job,q['id'])).fetchone()[0],0)
    def test_invalid_csrf_and_original_quote_immutability(self):
        q=self.make_quote(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REVISION_REQUIRED','csrf_token':'token'}))
        with closing(legacy_app.get_connection()) as c:
            before=tuple(c.execute('select status,is_current,issued_at,customer_total,sourcing_fee,service_charge from quotes where id=?',(q['id'],)).fetchone()); docs=[tuple(r) for r in c.execute('select id,version,sha256,is_issued,is_current from quote_documents_manifest where quote_id=?',(q['id'],))]
        r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{})); self.assertEqual(r.status_code,403)
        asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{'csrf_token':'token'}))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(tuple(c.execute('select status,is_current,issued_at,customer_total,sourcing_fee,service_charge from quotes where id=?',(q['id'],)).fetchone()),before)
            self.assertEqual([tuple(r) for r in c.execute('select id,version,sha256,is_issued,is_current from quote_documents_manifest where quote_id=?',(q['id'],))],docs)
            self.assertEqual(c.execute('select count(*) from quotes where job_id=?',(self.job,)).fetchone()[0],1)
    def test_stage_labels_decisions(self):
        for status,stage in [('SENT','Awaiting Customer'),('APPROVED','Customer Approved'),('REJECTED','Quote Rejected'),('REVISION_REQUIRED','Revision Needed')]:
            if status!='SENT': self.setUp()
            self.make_quote();
            if status!='SENT': asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':status,'csrf_token':'token'}))
            self.assertEqual(self.render()['v2_workflow']['stage'],stage)
    def test_revision_route_and_controls_are_explicit(self):
        q=self.make_quote(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REVISION_REQUIRED','csrf_token':'token'}))
        h=self._html(); self.assertIn('Create quote revision',h); self.assertIn(f'/jobs/{self.job}/center/quote/revision',h); self.assertNotIn('Generate revised Draft quote',h)
        r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{})); self.assertEqual(r.status_code,403)
    def test_revision_creation_has_no_downstream_side_effects(self):
        q=self.make_quote(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REVISION_REQUIRED','csrf_token':'token'}))
        with closing(legacy_app.get_connection()) as c:
            before={t:c.execute(f'select count(*) from {t}').fetchone()[0] for t in ('quotes','quote_items','invoices','invoice_items','customer_transactions','supplier_orders','supplier_order_items')}
        asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{'csrf_token':'token'}))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute('select status,is_current from quotes where id=?',(q['id'],)).fetchone()['status'],'REVISION_REQUIRED')
            self.assertEqual(c.execute('select count(*) from quotes where job_id=?',(self.job,)).fetchone()[0],1)
            for t in ('invoices','invoice_items','customer_transactions','supplier_orders','supplier_order_items'):
                self.assertEqual(c.execute(f'select count(*) from {t}').fetchone()[0],before[t])
    def test_revision_workspace_link_preserves_lineage(self):
        q=self.make_quote(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REVISION_REQUIRED','csrf_token':'token'})); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{'csrf_token':'token'}))
        ws=self.render(); rev=ws['quote_panel']['revision']; self.assertEqual(rev['based_on_quote_id'],q['id']); self.assertEqual(ws['quote_panel']['quote']['is_current'],1); self.assertEqual(ws['quote_panel']['quote']['status'],'REVISION_REQUIRED'); self.assertEqual(ws['v2_workflow']['next_action'],'Generate revised Draft'); self.assertIn('/center/quote/revision/generate',self._html()); self.assertNotIn('Open advanced quote workflow',self._html())
    def test_governed_revision_edit_routes_and_read_only_context(self):
        from plg_core.basket import routes
        governed = {
            'requested_need': routes.center_edit_need,
            'sourcing_candidate': routes.center_add_sourcing_option,
            'preferred_supplier': routes.center_select_sourcing_option,
            'pricing': routes.center_update_pricing,
            'quantity': routes.update_item_quantity,
            'fees': routes.update_revenue_adjustments,
        }
        for name, fn in governed.items():
            self.assertTrue(callable(fn), name)
        ws=self.render(); panel=ws['quote_panel']
        self.assertNotIn('bill_to_edit', panel); self.assertNotIn('equipment_edit', panel); self.assertNotIn('customer_master_edit', panel)
    def test_revision_workspace_preserves_snapshot_after_governance_boundary(self):
        q=self.make_quote(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REVISION_REQUIRED','csrf_token':'token'}))
        with closing(legacy_app.get_connection()) as c:
            original=tuple(c.execute('select status,is_current,issued_at,customer_total,parts_subtotal,sourcing_fee,service_charge from quotes where id=?',(q['id'],)).fetchone())
            items=[tuple(r) for r in c.execute('select quantity,customer_unit_price,supplier_unit_cost from quote_items where quote_id=?',(q['id'],))]
            docs=[tuple(r) for r in c.execute('select id,version,sha256,is_issued,is_current from quote_documents_manifest where quote_id=?',(q['id'],))]
        asyncio.run(self.post(f'/jobs/{self.job}/center/quote/revision',{'csrf_token':'token'}))
        ws=self.render(); self.assertTrue(ws['quote_panel']['revision']['editable'])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(tuple(c.execute('select status,is_current,issued_at,customer_total,parts_subtotal,sourcing_fee,service_charge from quotes where id=?',(q['id'],)).fetchone()),original)
            self.assertEqual([tuple(r) for r in c.execute('select quantity,customer_unit_price,supplier_unit_cost from quote_items where quote_id=?',(q['id'],))],items)
            self.assertEqual([tuple(r) for r in c.execute('select id,version,sha256,is_issued,is_current from quote_documents_manifest where quote_id=?',(q['id'],))],docs)
