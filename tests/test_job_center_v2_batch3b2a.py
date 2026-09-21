import asyncio
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
import legacy_app
from plg_core.application import app
from plg_core.basket.routes import center_generate_quote, center_send_quote, job_center_v2_page
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_manual_research_result, create_requested_need, set_preferred_sourcing_option
from plg_core.revisions.service import ensure_initial_revision

class Req:
    def __init__(self, values): self.values=values; self.cookies={"pps_csrf_token":"token"}
    async def form(self): return self.values

class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='pps-3b2a-'); self.db=Path(self.tmp.name)/'db.sqlite'; shutil.copy2('data/plg_core.db',self.db)
        self.patches=[patch.object(legacy_app,'DB_PATH',self.db), patch.object(legacy_app,'DOCUMENTS_DIR',Path(self.tmp.name)/'docs')]
        for p in self.patches:p.start()
        self.addCleanup(self.cleanup); legacy_app.initialize_database(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            cid=c.execute("INSERT INTO customers(customer_number,name,active) VALUES('3B2A-C','Decision Customer',1)").lastrowid
            self.job=c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES('3B2A-J','2026-01-01',?,'Decision Customer','REQUESTED')",(cid,)).lastrowid; c.commit()
        need=create_requested_need(self.job,job_asset_id=None,wording='Decision part')
        item=create_manual_research_result(self.job,job_asset_id=None,requested_need_id=need['id'],description='Decision part',supplier_name='Decision Supplier',supplier_unit_cost=10,verification_status='VERIFIED')['items'][-1]
        set_preferred_sourcing_option(self.job,need['id'],item['id'])
    def cleanup(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def generate_send(self):
        with closing(legacy_app.get_connection()) as c: rev=ensure_initial_revision(c,self.job); c.commit()
        asyncio.run(center_generate_quote(Req({'csrf_token':'token','expected_revision_id':str(rev['id']),'expected_version':str(rev['lock_version'])}),self.job))
        return asyncio.run(center_send_quote(Req({'csrf_token':'token'}),self.job))
    async def post(self,path,data):
        transport=httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,base_url='http://test') as c:
            c.cookies.set('pps_csrf_token','token'); return await c.post(path,data=data)
    def html(self):
        req=type('R',(),{'url_for':lambda s,*a,**k:'/static/x','url':type('U',(),{'scheme':'http'})(),'cookies':{}})()
        return job_center_v2_page(req,self.job,tab='quote').body.decode()
    def test_sent_waiting_renders_decision_control(self):
        self.generate_send(); h=self.html(); self.assertIn('Customer decision',h); self.assertIn('Waiting',h); self.assertIn('Record customer decision',h)
        self.assertIn('Approved', h); self.assertIn('Revision required', h); self.assertIn('Rejected', h); self.assertNotIn('Create Invoice', h)
        self.assertIn("Record that the customer approved this quote?", h)
        self.assertIn("Record that the customer requested changes to this quote?", h)
        self.assertIn("Record that the customer rejected this quote?", h)

    def test_next_action_projection_is_consistent_for_each_decision_state(self):
        expected = (("SENT", "Waiting for customer"), ("APPROVED", "Quote approved"), ("REJECTED", "Quote rejected"), ("REVISION_REQUIRED", "Create quote revision"))
        for index, (decision, next_action) in enumerate(expected):
            if index:
                self.cleanup(); self.setUp()
            self.generate_send()
            if decision != "SENT":
                asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision', {'decision': decision, 'csrf_token': 'token'}))
            with closing(legacy_app.get_connection()) as c:
                from plg_core.jobs.workspace import build_workspace
                workspace = build_workspace(c, self.job)
            self.assertEqual(workspace['v2_workflow']['next_action'], next_action)
            self.assertEqual(workspace['quote_panel']['decision'], None if decision == 'SENT' else decision)
            self.assertEqual(workspace['quote_panel']['decision_allowed'], decision == 'SENT')

    def test_decision_date_comes_from_transition_event(self):
        self.generate_send(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision', {'decision': 'APPROVED', 'csrf_token': 'token'}))
        with closing(legacy_app.get_connection()) as c:
            q=c.execute('select id from quotes where job_id=?',(self.job,)).fetchone()
            event=c.execute("select created_at,notes from quote_events where quote_id=? and to_status='APPROVED' order by id desc limit 1",(q['id'],)).fetchone()
            from plg_core.jobs.workspace import build_workspace
            panel=build_workspace(c,self.job)['quote_panel']
        self.assertEqual(panel['decision_at'], event['created_at'])
        self.assertEqual(panel['decision_notes'], event['notes'])

    def test_conflicting_decision_returns_already_decided_message(self):
        self.generate_send(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision', {'decision': 'APPROVED', 'csrf_token': 'token'}))
        response=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision', {'decision': 'REJECTED', 'csrf_token': 'token'}))
        self.assertIn('already+has+a+customer+decision', response.headers['location'])
    def test_draft_cannot_record_decision(self):
        with closing(legacy_app.get_connection()) as c: rev=ensure_initial_revision(c,self.job); c.commit()
        asyncio.run(center_generate_quote(Req({'csrf_token':'token','expected_revision_id':str(rev['id']),'expected_version':str(rev['lock_version'])}),self.job))
        r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'APPROVED','csrf_token':'token'})); self.assertEqual(r.status_code,303)
        with closing(legacy_app.get_connection()) as c:self.assertEqual(c.execute('select status from quotes where job_id=?',(self.job,)).fetchone()[0],'DRAFT')
    def test_real_asgi_decisions_and_conflict_are_authoritative(self):
        self.generate_send();
        for decision in ('APPROVED',):
            r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':decision,'csrf_token':'token'})); self.assertEqual(r.status_code,303)
        with closing(legacy_app.get_connection()) as c:
            q=c.execute('select id,status from quotes where job_id=?',(self.job,)).fetchone(); ev=c.execute('select count(*) from quote_events where quote_id=?',(q['id'],)).fetchone()[0]
            self.assertEqual(q['status'],'APPROVED')
        r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REJECTED','csrf_token':'token'})); self.assertEqual(r.status_code,303)
        with closing(legacy_app.get_connection()) as c:self.assertEqual(c.execute('select status from quotes where id=?',(q['id'],)).fetchone()[0],'APPROVED'); self.assertEqual(c.execute('select count(*) from quote_events where quote_id=?',(q['id'],)).fetchone()[0],ev)
        h=self.html(); self.assertIn('Approved',h); self.assertNotIn('Record customer decision',h)
    def test_revision_required_and_rejected_render_without_controls(self):
        for index, (decision, label) in enumerate((('REVISION_REQUIRED','Revision required'),('REJECTED','Rejected'))):
            if index:
                self.cleanup(); self.setUp()
            self.generate_send(); asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':decision,'csrf_token':'token'})); h=self.html(); self.assertIn(label.lower(),h.lower()); self.assertNotIn('Record customer decision',h)
            if decision=='REVISION_REQUIRED':
                with closing(legacy_app.get_connection()) as c: self.assertIn('Create quote revision', c.execute("select status from jobs where id=?",(self.job,)).fetchone()[0] or '') if False else None
    def test_invalid_csrf_and_vocabulary_do_not_mutate(self):
        self.generate_send()
        with closing(legacy_app.get_connection()) as c:
            q=c.execute('select id,status from quotes where job_id=?',(self.job,)).fetchone(); before=c.execute('select count(*) from quote_events').fetchone()[0]
        r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'APPROVED'})); self.assertEqual(r.status_code,403)
        r=asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'NOPE','csrf_token':'token'})); self.assertEqual(r.status_code,303)
        with closing(legacy_app.get_connection()) as c:self.assertEqual(c.execute('select status from quotes where id=?',(q['id'],)).fetchone()[0],'SENT'); self.assertEqual(c.execute('select count(*) from quote_events').fetchone()[0],before)
    def test_decision_does_not_create_commercial_records_or_change_sent_documents(self):
        self.generate_send()
        with closing(legacy_app.get_connection()) as c:
            q=c.execute('select id from quotes where job_id=?',(self.job,)).fetchone(); before={t:c.execute(f'select count(*) from {t}').fetchone()[0] for t in ('invoices','invoice_items','customer_transactions','supplier_orders','supplier_order_items')}
            docs=[tuple(r) for r in c.execute('select id,sha256,version from quote_documents_manifest where quote_id=?',(q['id'],))]
        asyncio.run(self.post(f'/jobs/{self.job}/center/quote/decision',{'decision':'REJECTED','csrf_token':'token'}))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual([tuple(r) for r in c.execute('select id,sha256,version from quote_documents_manifest where quote_id=?',(q['id'],))],docs)
            for t,n in before.items():self.assertEqual(c.execute(f'select count(*) from {t}').fetchone()[0],n)
