from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import legacy_app
from starlette.requests import Request
from plg_core.application import app
from plg_core.basket.routes import job_center_v2_page
from plg_core.database.migrations import run_migrations
from plg_core.jobs.workspace import build_workspace, derive_v2_workflow, derive_v2_stage
from plg_core.research.service import create_requested_need, create_manual_research_result


class JobCenterV2Batch1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-job-center-v2-")
        self.addCleanup(self.temp.cleanup)
        self.patchers = []
        for name in ("DB_PATH", "DOCUMENTS_DIR", "UPLOADS_DIR"):
            value = Path(self.temp.name) / name.lower()
            p = patch.object(legacy_app, name, value)
            p.start(); self.patchers.append(p)
        legacy_app.initialize_database(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            self.job = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES ('V2-001','2026-09-12','V2 Customer','REQUESTED')").lastrowid
            c.commit()

    def request(self, query=b""):
        return Request({'type':'http','method':'GET','path':f'/jobs/{self.job}/center','headers':[], 'query_string':query, 'scheme':'http','server':('localhost',80),'app':app})

    def test_v2_route_renders_read_only_shell_and_tabs(self):
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn('data-job-center-v2', html)
        self.assertIn('V2-001', html)
        self.assertIn('V2 Customer', html)
        for label in ('Job', 'Quote &amp; Invoice', 'Purchasing', 'Fulfillment', 'Documents &amp; History'):
            self.assertIn(label, html)
        self.assertIn('Next action', html)
        self.assertIn('No requested items yet.', html)

    def test_populated_job_renders_line_item_values(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Hydraulic Pump")
        option = create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need["id"], description="Hydraulic Pump", supplier_name="Supplier A", supplier_unit_cost=100, availability="In stock", verification_status="VERIFIED")
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE basket_items SET selected=1 WHERE id=?", (option["items"][-1]["id"],)); c.commit()
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn("Hydraulic Pump", html)
        self.assertIn("Supplier A", html)
        self.assertIn("Ready for quote", html)

    def test_partial_job_uses_authoritative_projection_without_writes(self):
        need = create_requested_need(self.job, job_asset_id=None, wording='Hydraulic Pump')
        with closing(legacy_app.get_connection()) as c:
            before = '\n'.join(c.iterdump())
            model = build_workspace(c, self.job)
            after = '\n'.join(c.iterdump())
        self.assertEqual(before, after)
        self.assertEqual(model['line_items'][0]['description'], need['wording'])
        self.assertEqual(model['operational_snapshot']['workflow']['stage'], 'Research')
        self.assertEqual(model['operational_snapshot']['workflow']['next_action'], 'Research Need')

class V2NextActionTests(unittest.TestCase):
    def snap(self, **kwargs):
        base = {"quote": None, "invoice": None, "movement": {"ordered_units": 0, "received_units": 0, "delivered_units": 0}, "supplier_orders": [], "workflow": {"next_url": "#quote"}, "delivery_url": "/delivery"}
        base.update(kwargs)
        return base


    def test_stage_is_distinct_from_next_action(self):
        from plg_core.jobs.workspace import derive_v2_workflow
        self.assertEqual(derive_v2_workflow(self.snap(), [], {})["stage"], "Sourcing")
        self.assertEqual(derive_v2_workflow(self.snap(quote={"status": "SENT"}), [], {})["stage"], "Awaiting Customer")
        self.assertEqual(derive_v2_workflow(self.snap(quote={"status": "APPROVED"}), [], {})["stage"], "Quote")
        self.assertEqual(derive_v2_workflow(self.snap(movement={"ordered_units": 1, "received_units": 1, "delivered_units": 0}), [], {})["stage"], "Fulfillment")
        self.assertEqual(derive_v2_workflow(self.snap(invoice={"status": "OPEN", "balance_due": 1}, movement={"ordered_units": 1, "received_units": 1, "delivered_units": 1}), [], {})["stage"], "Billing")
        self.assertEqual(derive_v2_workflow(self.snap(quote={"status": "CONVERTED"}, invoice={"status": "PAID", "balance_due": 0}), [], {})["next_action"], "Complete")

    def test_approved_operator_next_actions(self):
        from plg_core.jobs.workspace import derive_v2_workflow
        cases = [
            (self.snap(), [], "Needs supplier"),
            (self.snap(), [{"selected_options": [{"supplier_unit_cost": 10}]}], "Ready to quote"),
            (self.snap(quote={"status": "DRAFT"}), [], "Ready to send"),
            (self.snap(quote={"status": "SENT"}), [], "Waiting for customer"),
            (self.snap(quote={"status": "APPROVED"}), [], "Ready to order"),
            (self.snap(movement={"ordered_units": 2, "received_units": 1, "delivered_units": 0}), [], "Waiting on supplier"),
            (self.snap(movement={"ordered_units": 2, "received_units": 2, "delivered_units": 0}), [], "Ready to deliver"),
            (self.snap(invoice={"status": "OPEN", "balance_due": 20}, movement={"ordered_units": 2, "received_units": 2, "delivered_units": 2}), [], "Payment due"),
            (self.snap(invoice={"status": "PAID", "balance_due": 0}, movement={"ordered_units": 2, "received_units": 2, "delivered_units": 2}), [], "Complete"),
        ]
        for snapshot, parts, expected in cases:
            self.assertEqual(derive_v2_workflow(snapshot, parts, {} )["next_action"], expected)
