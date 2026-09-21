import asyncio
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx

import legacy_app
from plg_core.basket.routes import center_update_pricing, job_center_v2_page
from plg_core.database.migrations import run_migrations
from plg_core.jobs.workspace import build_workspace
from plg_core.research.service import create_manual_research_result, create_requested_need, set_preferred_sourcing_option
from plg_core.revisions.service import ensure_initial_revision
from plg_core.revisions.service import touch_revision
from plg_core.research.service import set_quote_candidate
from plg_core.application import app
from plg_core.web_security import CSRF_COOKIE_NAME
from starlette.requests import Request


class FormRequest:
    def __init__(self, values, valid=True):
        self.values = values
        self.cookies = {CSRF_COOKIE_NAME: "token"} if valid else {}

    async def form(self):
        return self.values


class Batch2B2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-job-center-2b2-")
        self.addCleanup(self.temp.cleanup)
        self.patches = [patch.object(legacy_app, n, Path(self.temp.name) / n.lower()) for n in ("DB_PATH", "DOCUMENTS_DIR", "UPLOADS_DIR")]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        legacy_app.initialize_database()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            self.job = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES ('2B2','2026-01-01','Customer','REQUESTED')").lastrowid
            c.commit()

    def candidate(self, need, name="Supplier", cost=25, quantity=None):
        result = create_manual_research_result(
            self.job, job_asset_id=None, requested_need_id=need["id"], description=need["wording"],
            supplier_name=name, supplier_unit_cost=cost,
        )
        item_id = result["items"][-1]["id"]
        if quantity is not None:
            with closing(legacy_app.get_connection()) as c:
                c.execute("UPDATE basket_items SET quantity=? WHERE id=?", (quantity, item_id)); c.commit()
        return item_id

    def test_projected_kpis_use_preferred_quantity_and_authoritative_price(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=4)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            model = build_workspace(c, self.job)
        projection = model["financial_projection"]
        self.assertEqual(projection["mode"], "PROJECTED")
        self.assertEqual(projection["cost"], 100.0)
        self.assertEqual(projection["customer_total"], 140.0)
        self.assertEqual(projection["profit"], 40.0)
        self.assertTrue(projection["complete"])
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn("Estimated cost", html)
        self.assertIn("Projected profit", html)
        self.assertNotIn(">Profit</span>", html)

    def test_missing_preferred_cost_marks_projection_incomplete(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=2)
        item = self.candidate(need, cost=None)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            projection = build_workspace(c, self.job)["financial_projection"]
        self.assertFalse(projection["complete"])
        self.assertIsNone(projection["profit"])
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn("Projected profit", html)
        self.assertIn(">Incomplete</strong>", html)

    def test_alternate_candidate_does_not_change_projected_kpis(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=4)
        preferred = self.candidate(need, "Preferred", 25)
        self.candidate(need, "Alternate", 1000)
        set_preferred_sourcing_option(self.job, need["id"], preferred)
        with closing(legacy_app.get_connection()) as c:
            projection = build_workspace(c, self.job)["financial_projection"]
        self.assertEqual(projection["cost"], 100.0)
        self.assertEqual(projection["customer_total"], 140.0)

    def test_projected_revenue_is_independent_of_missing_supplier_cost(self):
        need_a = create_requested_need(self.job, job_asset_id=None, wording="Cylinder head", quantity=1)
        item_a = self.candidate(need_a, "Supplier A", None)
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE basket_items SET customer_unit_price_override=160,pricing_mode='OVERRIDE' WHERE id=?", (item_a,)); c.commit()
        need_b = create_requested_need(self.job, job_asset_id=None, wording="Water pump", quantity=4)
        item_b = self.candidate(need_b, "Supplier B", 25)
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE basket_items SET customer_unit_price_override=40,pricing_mode='OVERRIDE' WHERE id=?", (item_b,)); c.commit()
        set_preferred_sourcing_option(self.job, need_a["id"], item_a)
        set_preferred_sourcing_option(self.job, need_b["id"], item_b)
        with closing(legacy_app.get_connection()) as c:
            projection = build_workspace(c, self.job)["financial_projection"]
        self.assertEqual(projection["customer_total"], 320.0)
        self.assertEqual(projection["cost"], 100.0)
        self.assertFalse(projection["estimated_cost_complete"])
        self.assertIsNone(projection["profit"])

    def test_null_and_explicit_zero_supplier_cost_are_distinct_in_item_card(self):
        need_null = create_requested_need(self.job, job_asset_id=None, wording="Unknown", quantity=1)
        null_item = self.candidate(need_null, "Unknown supplier", None)
        need_zero = create_requested_need(self.job, job_asset_id=None, wording="Free item", quantity=1)
        zero_item = self.candidate(need_zero, "Zero supplier", 0)
        set_preferred_sourcing_option(self.job, need_null["id"], null_item)
        set_preferred_sourcing_option(self.job, need_zero["id"], zero_item)
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn("Unknown supplier", html)
        self.assertIn("Not recorded", html)
        self.assertIn("$0.00", html)

    def test_markup_edit_uses_governed_job_center_pricing_route(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=4)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job)
        response = asyncio.run(center_update_pricing(FormRequest({
            "csrf_token": "token", "markup_percent": "50", "customer_unit_price_override": "",
            "expected_revision_id": str(revision["id"]), "expected_version": str(revision["lock_version"]),
        }), self.job, need["id"], item))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT markup_percent,customer_unit_price_override,supplier_unit_cost,quantity,pricing_mode FROM basket_items WHERE id=?", (item,)).fetchone()
            projection = build_workspace(c, self.job)["financial_projection"]
        self.assertEqual((row["markup_percent"], row["customer_unit_price_override"], row["supplier_unit_cost"], row["quantity"], row["pricing_mode"]), (50.0, None, 25.0, 4, "AUTO"))
        self.assertEqual(projection["customer_total"], 150.0)
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn("Manual customer unit price override", html)
        self.assertIn("Optional — leave blank to use automatic pricing.", html)
        self.assertIn('name="customer_unit_price_override" type="number" min="0" step="0.01" placeholder="Leave blank to use automatic pricing."', html)
        self.assertNotIn("Return to automatic pricing", html)

    def test_override_and_return_to_auto_use_authoritative_mode(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=4)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job)
        asyncio.run(center_update_pricing(FormRequest({
            "csrf_token": "token", "markup_percent": "40", "customer_unit_price_override": "37.50",
            "expected_revision_id": str(revision["id"]), "expected_version": str(revision["lock_version"]),
        }), self.job, need["id"], item))
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT customer_unit_price_override,pricing_mode FROM basket_items WHERE id=?", (item,)).fetchone()
            revision = ensure_initial_revision(c, self.job)
        self.assertEqual((row["customer_unit_price_override"], row["pricing_mode"]), (37.5, "OVERRIDE"))
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn("OVERRIDE", html)
        self.assertIn("Calculated automatic unit price", html)
        self.assertIn("Manual customer unit price", html)
        self.assertIn("Return to automatic pricing", html)
        asyncio.run(center_update_pricing(FormRequest({
            "csrf_token": "token", "markup_percent": "40", "customer_unit_price_override": "",
            "expected_revision_id": str(revision["id"]), "expected_version": str(revision["lock_version"]),
        }), self.job, need["id"], item))
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT customer_unit_price_override,pricing_mode FROM basket_items WHERE id=?", (item,)).fetchone()
        self.assertEqual((row["customer_unit_price_override"], row["pricing_mode"]), (None, "AUTO"))
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn("Pricing mode", html)
        self.assertNotIn("Return to automatic pricing", html)

    def test_pricing_route_rejects_csrf_without_mutation(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=2)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            before = dict(c.execute("SELECT markup_percent,customer_unit_price_override FROM basket_items WHERE id=?", (item,)).fetchone())
        with self.assertRaises(Exception):
            asyncio.run(center_update_pricing(FormRequest({"markup_percent": "80"}, valid=False), self.job, need["id"], item))
        with closing(legacy_app.get_connection()) as c:
            after = dict(c.execute("SELECT markup_percent,customer_unit_price_override FROM basket_items WHERE id=?", (item,)).fetchone())
        self.assertEqual(before, after)

    def test_return_to_auto_carries_tokens_and_rejects_stale_form(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=2)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job)
        asyncio.run(center_update_pricing(FormRequest({"csrf_token":"token", "markup_percent":"40", "customer_unit_price_override":"37.5", "expected_revision_id":str(revision["id"]), "expected_version":str(revision["lock_version"])}), self.job, need["id"], item))
        with closing(legacy_app.get_connection()) as c:
            stale = ensure_initial_revision(c, self.job)
            touch_revision(c, stale["id"], stale["lock_version"]); c.commit()
        response = asyncio.run(center_update_pricing(FormRequest({"csrf_token":"token", "markup_percent":"40", "customer_unit_price_override":"", "expected_revision_id":str(stale["id"]), "expected_version":str(stale["lock_version"])}), self.job, need["id"], item))
        self.assertEqual(response.status_code, 303)
        self.assertIn("This+job+changed+after+you+opened+it", response.headers["location"])
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT customer_unit_price_override,pricing_mode FROM basket_items WHERE id=?", (item,)).fetchone()
        self.assertEqual((row["customer_unit_price_override"], row["pricing_mode"]), (37.5, "OVERRIDE"))

    def test_legacy_derived_current_has_no_editable_pricing(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        item = self.candidate(need, cost=25)
        set_quote_candidate(self.job, item, candidate=True, requested_need_ids=[need["id"]])
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertNotIn('class="jv2-pricing"', html)

    def test_explicit_preferred_current_has_editable_pricing(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn('class="jv2-pricing"', html)
        self.assertIn(f"/jobs/{self.job}/center/needs/{need['id']}/sourcing/{item}/pricing", html)

    def test_non_preferred_pricing_post_is_non_mutating(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        preferred = self.candidate(need, "Preferred", 25)
        alternate = self.candidate(need, "Alternate", 50)
        set_preferred_sourcing_option(self.job, need["id"], preferred)
        with closing(legacy_app.get_connection()) as c:
            before = dict(c.execute("SELECT markup_percent,customer_unit_price_override FROM basket_items WHERE id=?", (alternate,)).fetchone())
        response = asyncio.run(center_update_pricing(FormRequest({"csrf_token":"token", "markup_percent":"90"}), self.job, need["id"], alternate))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            after = dict(c.execute("SELECT markup_percent,customer_unit_price_override FROM basket_items WHERE id=?", (alternate,)).fetchone())
        self.assertEqual(before, after)

    def test_actual_cost_state_transitions_and_zero_is_confirmed(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=1)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(build_workspace(c, self.job)["financial_projection"]["mode"], "PROJECTED")
            c.execute("INSERT INTO supplier_orders(po_number,job_id,supplier_name,status,parts_total,order_total,actual_shipping_total) VALUES ('PO-2B2',?,'Supplier','RECEIVED',0,0,0)", (self.job,))
            order = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO supplier_order_items(order_id,description,quantity_ordered,quantity_received,unit_cost,line_cost,actual_unit_cost) VALUES (?, 'Pump', 1, 1, 0, 0, 0)", (order,)); c.commit()
            projection = build_workspace(c, self.job)["financial_projection"]
        self.assertEqual(projection["mode"], "REALIZED")
        self.assertEqual(projection["cost"], 0.0)
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertIn("Actual cost", html); self.assertIn(">Profit</span>", html)

    def test_invoiced_customer_total_remains_authoritative(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=1)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status) VALUES ('Q-2B2',?,DATE('now'),'CONVERTED')", (self.job,))
            quote = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) VALUES ('INV-2B2',?,?,DATE('now'),'PAID',999,0)", (quote, self.job))
            c.execute("INSERT INTO supplier_orders(po_number,job_id,supplier_name,status,parts_total,order_total,actual_shipping_total) VALUES ('PO-INV',?,'Supplier','RECEIVED',0,0,0)", (self.job,))
            order = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("INSERT INTO supplier_order_items(order_id,description,quantity_ordered,quantity_received,unit_cost,line_cost,actual_unit_cost) VALUES (?, 'Pump', 1, 1, 0, 0, 0)", (order,))
            c.commit()
            projection = build_workspace(c, self.job)["financial_projection"]
        self.assertEqual(projection["mode"], "REALIZED")
        self.assertEqual(projection["customer_total"], 999.0)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT customer_total FROM invoices WHERE job_id=?", (self.job,)).fetchone()[0], 999.0)

    def test_pricing_route_is_registered_and_invalid_csrf_is_not_404(self):
        expected = "/jobs/{job_id}/center/needs/{need_id}/sourcing/{item_id}/pricing"
        routes = []
        for included in app.routes:
            router = getattr(included, "original_router", None)
            routes.extend(r for r in getattr(router, "routes", ()) if getattr(r, "path", None) == expected)
        self.assertEqual(len(routes), 1); self.assertIn("POST", routes[0].methods)
        async def post():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post(f"/jobs/{self.job}/center/needs/1/sourcing/1/pricing", data={})
        self.assertEqual(asyncio.run(post()).status_code, 403)

    def test_invalid_pricing_values_and_committed_revision_do_not_mutate(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=2)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            before = dict(c.execute("SELECT markup_percent,customer_unit_price_override,quantity,supplier_unit_cost FROM basket_items WHERE id=?", (item,)).fetchone())
            revision = ensure_initial_revision(c, self.job)
        for values in ({"markup_percent": "-1"}, {"markup_percent": "abc"}, {"markup_percent": "40", "customer_unit_price_override": "-2"}, {"markup_percent": "40", "customer_unit_price_override": "abc"}):
            values.update({"csrf_token":"token", "expected_revision_id":str(revision["id"]), "expected_version":str(revision["lock_version"])})
            response = asyncio.run(center_update_pricing(FormRequest(values), self.job, need["id"], item))
            self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            after = dict(c.execute("SELECT markup_percent,customer_unit_price_override,quantity,supplier_unit_cost FROM basket_items WHERE id=?", (item,)).fetchone())
        self.assertEqual(before, after)
        with closing(legacy_app.get_connection()) as c:
            current = ensure_initial_revision(c, self.job)
            touch_revision(c, current["id"], current["lock_version"]); c.commit()
        response = asyncio.run(center_update_pricing(FormRequest({"csrf_token":"token", "markup_percent":"50", "expected_revision_id":str(current["id"]), "expected_version":str(current["lock_version"])}), self.job, need["id"], item))
        self.assertIn("This+job+changed+after+you+opened+it", response.headers["location"])

    def test_committed_revision_blocks_pricing_without_mutation(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=1)
        item = self.candidate(need, cost=25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job)
            c.execute("UPDATE work_revisions SET state='COMMITTED' WHERE id=?", (revision["id"],)); c.commit()
            before = dict(c.execute("SELECT markup_percent,customer_unit_price_override FROM basket_items WHERE id=?", (item,)).fetchone())
        response = asyncio.run(center_update_pricing(FormRequest({"csrf_token":"token", "markup_percent":"55", "expected_revision_id":str(revision["id"]), "expected_version":str(revision["lock_version"])}), self.job, need["id"], item))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            after = dict(c.execute("SELECT markup_percent,customer_unit_price_override FROM basket_items WHERE id=?", (item,)).fetchone())
        self.assertEqual(before, after)

    def request(self):
        return Request({"type": "http", "method": "GET", "path": f"/jobs/{self.job}/center", "headers": [], "query_string": b"", "scheme": "http", "server": ("localhost", 80), "app": __import__("plg_core.application", fromlist=["app"]).app})
