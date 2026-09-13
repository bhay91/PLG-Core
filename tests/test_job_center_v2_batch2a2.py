from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import asyncio
from starlette.requests import Request
from plg_core.application import app

import legacy_app
from plg_core.basket.models import BasketItemCreate
from plg_core.basket.service import add_item
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_requested_need, create_manual_research_result
from plg_core.basket.routes import center_add_sourcing_option
from plg_core.basket.routes import job_center_v2_page
from plg_core.jobs.workspace import build_workspace
from plg_core.pricing import pricing_assessment
from plg_core.web_security import CSRF_COOKIE_NAME

class FormRequest:
    def __init__(self, values, valid=True):
        self.values = values
        self.cookies = {CSRF_COOKIE_NAME: "token"} if valid else {}
    async def form(self):
        return self.values


class Batch2A2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.patches = [patch.object(legacy_app, n, Path(self.temp.name)/n.lower()) for n in ("DB_PATH","DOCUMENTS_DIR","UPLOADS_DIR")]
        for p in self.patches: p.start()
        self.addCleanup(lambda:[p.stop() for p in self.patches]); legacy_app.initialize_database(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            customer=c.execute("INSERT INTO customers(customer_number,name,active) VALUES ('C','C',1)").lastrowid
            self.job=c.execute("INSERT INTO jobs(job_number,created_date,customer,customer_id,status) VALUES ('J','2026-01-01','C',?,'REQUESTED')",(customer,)).lastrowid; c.commit()

    def request(self):
        return Request({'type':'http','method':'GET','path':f'/jobs/{self.job}/center','headers':[], 'query_string':b'', 'scheme':'http','server':('localhost',80),'app':app})
    def test_requested_quantity_inherits_only_when_quantity_omitted(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump",quantity=3)
        inherited=add_item(self.job,BasketItemCreate(primary_requested_need_id=need['id'],requested_description="Pump"))
        explicit=add_item(self.job,BasketItemCreate(primary_requested_need_id=need['id'],requested_description="Pump 2",quantity=5))
        self.assertEqual(inherited['items'][0]['quantity'],3)
        self.assertEqual(explicit['items'][-1]['quantity'],5)

        # An explicit quantity equal to the model default is still intentional.
        explicit_one=add_item(self.job,BasketItemCreate(primary_requested_need_id=need['id'],requested_description="Pump 1",quantity=1))
        self.assertEqual(explicit_one['items'][-1]['quantity'],1)

    def test_null_requested_quantity_keeps_basket_default_and_later_need_edits_do_not_sync(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Seal")
        first=add_item(self.job,BasketItemCreate(primary_requested_need_id=need['id'],requested_description="Seal"))
        self.assertEqual(first['items'][0]['quantity'],1)
        from plg_core.research.service import update_requested_need
        update_requested_need(self.job, need['id'], wording="Seal", quantity=4)
        with closing(legacy_app.get_connection()) as c:
            qty=c.execute("SELECT quantity FROM basket_items WHERE id=?",(first['items'][0]['id'],)).fetchone()[0]
        self.assertEqual(qty,1)

    def test_candidate_fields_persist_and_multiple_options_link(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Filter",quantity=2)
        add_item(self.job,BasketItemCreate(primary_requested_need_id=need['id'],requested_description="A",supplier_name="Supplier A",supplier_unit_cost=10,source_url="https://a.test",availability="In stock",verification_status="VERIFIED",quantity=2))
        result=add_item(self.job,BasketItemCreate(primary_requested_need_id=need['id'],requested_description="B",supplier_name="Supplier B",supplier_unit_cost=12,quantity=2))
        self.assertEqual(len(result['items']),2)

    def test_research_result_candidate_supports_all_operator_fields(self):
        from plg_core.research.service import create_manual_research_result
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump",quantity=2)
        result=create_manual_research_result(
            self.job, job_asset_id=None, requested_need_id=need['id'], description="Pump",
            supplier_name="Supplier A", supplier_unit_cost=14.5,
            source_url="https://example.test/pump", availability="In stock",
            verification_status="REJECTED", research_evidence="Catalog evidence",
            research_notes="Needs fitment review",
        )
        item=result['items'][0]
        self.assertEqual(item['quantity'],2)
        self.assertEqual(item['supplier_name'],'Supplier A')
        self.assertEqual(item['supplier_unit_cost'],14.5)
        self.assertEqual(item['source_url'],'https://example.test/pump')
        self.assertEqual(item['availability'],'In stock')
        self.assertEqual(item['verification_status'],'REJECTED')
        self.assertEqual(item['research_evidence'],'Catalog evidence')
        self.assertEqual(item['research_notes'],'Needs fitment review')

    def test_job_center_sourcing_post_uses_research_service_and_persists_candidate(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump",quantity=2)
        response=__import__('asyncio').run(center_add_sourcing_option(FormRequest({
            "csrf_token":"token",
            "supplier_name":"Supplier B", "supplier_unit_cost":"22.50",
            "source_url":"https://example.test/b", "availability":"Backorder",
            "verification_status":"PROVISIONAL", "research_evidence":"Evidence",
        }), self.job, need['id']))
        self.assertEqual(response.status_code,303)
        with closing(legacy_app.get_connection()) as c:
            row=c.execute("SELECT supplier_name,supplier_unit_cost,source_url,availability,verification_status,research_evidence,primary_requested_need_id FROM basket_items WHERE primary_requested_need_id=?",(need['id'],)).fetchone()
        self.assertEqual(tuple(row),('Supplier B',22.5,'https://example.test/b','Backorder','PROVISIONAL','Evidence',need['id']))

    def test_invalid_verification_rejected_without_candidate(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need['id'], description="Pump", supplier_name="Bad", verification_status="MADE_UP_STATE")
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_items WHERE primary_requested_need_id=?",(need['id'],)).fetchone()[0],0)

    def test_sourcing_post_requires_csrf_without_mutation(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump")
        with self.assertRaises(Exception):
            asyncio.run(center_add_sourcing_option(FormRequest({"supplier_name":"Nope"}, valid=False), self.job, need['id']))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_items WHERE primary_requested_need_id=?",(need['id'],)).fetchone()[0],0)

    def test_sourcing_post_honors_stale_revision_and_creates_nothing(self):
        from plg_core.revisions.service import ensure_initial_revision, touch_revision
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump")
        with closing(legacy_app.get_connection()) as c:
            revision=ensure_initial_revision(c,self.job); rid,version=revision['id'],revision['lock_version']
            touch_revision(c,rid,version); c.commit()
        response=asyncio.run(center_add_sourcing_option(FormRequest({
            "csrf_token":"token", "expected_revision_id":str(rid), "expected_version":str(version),
            "supplier_name":"Stale", "supplier_unit_cost":"9",
        }), self.job, need['id']))
        self.assertEqual(response.status_code,303)
        self.assertIn("This+job+changed+after+you+opened+it",response.headers['location'])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_items WHERE primary_requested_need_id=?",(need['id'],)).fetchone()[0],0)

    def test_workspace_exposes_candidates_and_ui_keeps_selection_read_only(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump")
        first=create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need['id'], description="Pump", supplier_name="A", supplier_unit_cost=10, source_url="https://a.test", availability="In stock", verification_status="VERIFIED", research_evidence="Evidence A", research_notes="Note A")
        create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need['id'], description="Pump", supplier_name="B", supplier_unit_cost=12, source_url="https://b.test", availability="Backorder", verification_status="PROVISIONAL", research_evidence="Evidence B", research_notes="Note B")
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE basket_items SET selected=1 WHERE id=?", (first['items'][-1]['id'],)); c.commit()
            model=build_workspace(c,self.job)
        item=model['parts'][0]
        self.assertEqual(len(item['options']),2)
        self.assertEqual(sum(bool(option['selected']) for option in item['options']),1)
        html=job_center_v2_page(self.request(),self.job).body.decode()
        self.assertIn('Manage sourcing',html); self.assertIn('View sourcing details',html)
        self.assertIn('Estimated supplier cost',html); self.assertNotIn('Actual / estimated cost',html)
        self.assertIn('>A <',html); self.assertIn('Evidence A',html)
        self.assertNotIn('Select supplier',html); self.assertNotIn('set_quote_candidate',html)
        self.assertEqual(pricing_assessment(10,None,None)['current_unit_price'], model['line_items'][0]['sell_price'])

    def test_manage_sourcing_nests_single_collapsed_add_form_and_keeps_edit_in_footer(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump")
        create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need['id'], description="Pump", supplier_name="A", supplier_unit_cost=10)
        html=job_center_v2_page(self.request(),self.job).body.decode()
        self.assertEqual(html.count('+ Add supplier option'),1)
        self.assertEqual(html.count('class="jv2-sourcing-panel"'),1)
        self.assertIn('<details class="jv2-sourcing-panel"><summary class="button ghost small">Manage sourcing</summary>',html)
        self.assertNotIn('<details class="jv2-sourcing-panel" open>',html)
        self.assertIn('<details class="jv2-add-source"><summary>+ Add supplier option</summary>',html)
        self.assertLess(html.index('+ Add supplier option'), html.index('Sourcing options'))
        source_form=html.split('<details class="jv2-add-source">',1)[1].split('</details>',1)[0]
        self.assertEqual(source_form.count('action="/jobs/1/center/needs/1/sourcing"'),1)
        self.assertIn('type="button"',source_form)
        self.assertNotIn('<summary>Edit</summary>',source_form)
        footer=html.split('</details><div class="jv2-item-foot">',1)[1]
        self.assertIn('View sourcing details',footer)
        self.assertIn('<summary>Edit</summary>',footer)

    def test_sourcing_does_not_mutate_commercial_or_fulfillment_records(self):
        need=create_requested_need(self.job,job_asset_id=None,wording="Pump")
        with closing(legacy_app.get_connection()) as c:
            names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables=[n for n in ('quotes','quote_items','quote_revisions','supplier_orders','actual_cost_adjustments','invoices','payments','transactions','receiving_events','deliveries') if n in names]
            before={n:c.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in tables}
        create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need['id'], description="Pump", supplier_name="A", supplier_unit_cost=10, source_url="https://a.test", availability="In stock", verification_status="VERIFIED")
        with closing(legacy_app.get_connection()) as c:
            after={n:c.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in tables}
        self.assertEqual(before,after)
