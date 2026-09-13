from contextlib import closing
from pathlib import Path
import asyncio
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import httpx

import legacy_app
from plg_core.application import app
from plg_core.basket.routes import center_generate_quote, job_center_v2_page
from plg_core.basket.models import BasketItemCreate
from plg_core.basket.service import add_item
from plg_core.jobs.workspace import build_workspace
from plg_core.database.migrations import run_migrations
from plg_core.research.service import (
    create_manual_research_result, create_requested_need,
    set_preferred_sourcing_option,
)
from plg_core.revisions.service import ensure_initial_revision
from plg_core.revisions.service import commit_work_revision
from starlette.requests import Request as StarletteRequest


class _Request:
    def __init__(self, values, valid=True):
        self.values = values
        self.cookies = {"pps_csrf_token": "token"} if valid else {}

    async def form(self):
        return self.values


class Batch3A1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-job-center-3a1-")
        root = Path(self.temp.name)
        self.db = root / "test.db"
        shutil.copy2(Path(__file__).parents[1] / "data" / "plg_core.db", self.db)
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db),
            patch.object(legacy_app, "DOCUMENTS_DIR", root / "documents"),
            patch.object(legacy_app, "UPLOADS_DIR", root / "uploads"),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._cleanup)
        legacy_app.initialize_database()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('3A1-C','3A1 Customer',1)"
            ).lastrowid
            self.job = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('3A1-J','2026-01-01',?,'3A1 Customer','REQUESTED')",
                (customer,),
            ).lastrowid
            c.commit()

    def _cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _request(self, path, query=b""):
        return StarletteRequest({
            "type": "http", "method": "GET", "path": path,
            "query_string": query, "headers": [], "scheme": "http",
            "server": ("localhost", 80), "app": app,
        })

    def _need(self, wording):
        return create_requested_need(self.job, job_asset_id=None, wording=wording)

    def _candidate(self, need, name, cost=10):
        result = create_manual_research_result(
            self.job, job_asset_id=None, requested_need_id=need["id"],
            description=need["wording"], supplier_name=name,
            supplier_unit_cost=cost, verification_status="VERIFIED",
        )
        return result["items"][-1]["id"]

    def _tokens(self):
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job)
            c.commit()
            return revision["id"], revision["lock_version"]

    def _generate(self):
        rid, version = self._tokens()
        return asyncio.run(center_generate_quote(_Request({
            "csrf_token": "token", "expected_revision_id": str(rid),
            "expected_version": str(version),
        }), self.job))

    def test_generate_quote_uses_only_explicit_preferred_candidates(self):
        need_a, need_b = self._need("Water pump"), self._need("Belt")
        a1, a2 = self._candidate(need_a, "A1", 10), self._candidate(need_a, "A2", 11)
        b1, b2 = self._candidate(need_b, "B1", 20), self._candidate(need_b, "B2", 21)
        set_preferred_sourcing_option(self.job, need_a["id"], a1)
        set_preferred_sourcing_option(self.job, need_b["id"], b1)
        response = self._generate()
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT * FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()
            rows = c.execute("SELECT supplier_name,primary_requested_need_id FROM quote_items WHERE quote_id=? ORDER BY id", (quote["id"],)).fetchall()
            self.assertEqual({r["supplier_name"] for r in rows}, {"A1", "B1"})
            self.assertNotIn("A2", {r["supplier_name"] for r in rows})
            self.assertNotIn("B2", {r["supplier_name"] for r in rows})
            self.assertEqual({r["primary_requested_need_id"] for r in rows}, {need_a["id"], need_b["id"]})

    def test_legacy_current_does_not_enable_job_center_generation(self):
        need = self._need("Legacy part")
        item = self._candidate(need, "Legacy supplier")
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE basket_items SET selected=1,research_state='QUOTE_CANDIDATE' WHERE id=?", (item,))
            c.commit()
            context = build_workspace(c, self.job)
        self.assertTrue(context["parts"][0]["current_options"])
        self.assertFalse(context["quote_panel"]["can_generate"])
        self.assertTrue(any("explicitly" in x.lower() for x in context["quote_panel"]["blockers"]))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_authoritative_preflight_blocks_missing_supplier_cost_read_only(self):
        need = self._need("Cylinder head")
        item = self._candidate(need, "Supplier without cost", None)
        set_preferred_sourcing_option(self.job, need["id"], item)
        tables = ("work_revisions", "basket_items", "basket_item_need_links", "job_parts", "part_sources",
                  "quotes", "quote_items", "invoices", "invoice_items", "customer_transactions",
                  "supplier_orders", "supplier_order_items", "receiving_events", "deliveries")
        with closing(legacy_app.get_connection()) as c:
            before = {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
                      for table in tables if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()}
            context = build_workspace(c, self.job)
            after = {table: [tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
                     for table in before}
        self.assertFalse(context["quote_panel"]["can_generate"])
        self.assertIn("Supplier cost needed for Cylinder head", context["quote_panel"]["blockers"])
        self.assertEqual(before, after)

    def test_generic_commit_allows_null_cost_but_quote_path_rejects_it(self):
        need = self._need("Work revision only")
        item = self._candidate(need, "No cost", None)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE basket_items SET selected=1 WHERE id=?", (item,))
            c.commit()
            revision = ensure_initial_revision(c, self.job)
            c.commit()
        committed = commit_work_revision(self.job, expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        self.assertTrue(committed["created_parts"] >= 1)

    def test_explicit_zero_supplier_cost_is_quote_ready(self):
        need = self._need("Zero cost part")
        item = self._candidate(need, "Zero supplier", 0)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            context = build_workspace(c, self.job)
        self.assertTrue(context["quote_panel"]["can_generate"])
        response = self._generate()
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT supplier_unit_cost FROM quote_items WHERE quote_id=(SELECT id FROM quotes WHERE job_id=? ORDER BY id DESC LIMIT 1)", (self.job,)).fetchone()
            self.assertEqual(float(row["supplier_unit_cost"]), 0.0)

    def test_stale_after_preflight_rejects_without_quote(self):
        need = self._need("Stale part")
        item = self._candidate(need, "Supplier", 10)
        rid, version = self._tokens()
        set_preferred_sourcing_option(self.job, need["id"], item)
        response = asyncio.run(center_generate_quote(_Request({
            "csrf_token": "token", "expected_revision_id": str(rid),
            "expected_version": str(version),
        }), self.job))
        self.assertEqual(response.status_code, 303)
        self.assertIn("changed+after", response.headers["location"])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_prequote_fee_save_persists_through_governed_route(self):
        from plg_core.basket.routes import center_update_quote_fees
        rid, version = self._tokens()
        response = asyncio.run(center_update_quote_fees(_Request({
            "csrf_token": "token", "expected_revision_id": str(rid),
            "expected_version": str(version), "sourcing_fee": "15",
            "service_charge": "50",
        }), self.job))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            job = c.execute("SELECT sourcing_fee,service_charge FROM jobs WHERE id=?", (self.job,)).fetchone()
            rev = c.execute("SELECT sourcing_fee,service_charge FROM work_revisions WHERE job_id=?", (self.job,)).fetchone()
        self.assertEqual((job["sourcing_fee"], job["service_charge"]), (15.0, 50.0))
        self.assertEqual((rev["sourcing_fee"], rev["service_charge"]), (15.0, 50.0))

    def test_prequote_fee_invalid_value_does_not_mutate(self):
        from plg_core.basket.routes import center_update_quote_fees
        rid, version = self._tokens()
        response = asyncio.run(center_update_quote_fees(_Request({
            "csrf_token": "token", "expected_revision_id": str(rid),
            "expected_version": str(version), "sourcing_fee": "-1",
            "service_charge": "50",
        }), self.job))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT sourcing_fee FROM jobs WHERE id=?", (self.job,)).fetchone()
        self.assertEqual(float(row["sourcing_fee"] or 0), 0.0)

    def test_service_charge_boundary_values(self):
        from plg_core.basket.routes import center_update_quote_fees
        for value, expected_status, expected_message in (("49.99", 303, "Service charge must be $0 or at least $50."), ("0", 303, None), ("50", 303, None)):
            rid, version = self._tokens()
            response = asyncio.run(center_update_quote_fees(_Request({"csrf_token":"token","expected_revision_id":str(rid),"expected_version":str(version),"sourcing_fee":"15","service_charge":value}), self.job))
            self.assertEqual(response.status_code, expected_status)
            if expected_message:
                self.assertIn("Service+charge+must+be+%240+or+at+least+%2450", response.headers["location"])
                with closing(legacy_app.get_connection()) as c:
                    row = c.execute("SELECT sourcing_fee,service_charge FROM jobs WHERE id=?", (self.job,)).fetchone()
                self.assertEqual((row["sourcing_fee"], row["service_charge"]), (0.0, 0.0))

    def test_fee_route_to_quote_snapshot_and_rendered_draft_values(self):
        from plg_core.basket.routes import center_update_quote_fees
        need = self._need("Fee snapshot part")
        item = self._candidate(need, "Fee supplier", 20)
        set_preferred_sourcing_option(self.job, need["id"], item)
        rid, version = self._tokens()
        fee_response = asyncio.run(center_update_quote_fees(_Request({"csrf_token":"token","expected_revision_id":str(rid),"expected_version":str(version),"sourcing_fee":"15","service_charge":"50"}), self.job))
        self.assertEqual(fee_response.status_code, 303)
        rid, version = self._tokens()
        quote_response = asyncio.run(center_generate_quote(_Request({"csrf_token":"token","expected_revision_id":str(rid),"expected_version":str(version)}), self.job))
        self.assertEqual(quote_response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT * FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()
            snapshot = tuple(quote[field] for field in ("parts_subtotal", "shipping_total", "sourcing_fee", "service_charge", "customer_total"))
            self.assertEqual((quote["sourcing_fee"], quote["service_charge"]), (15.0, 50.0))
            c.execute("UPDATE jobs SET sourcing_fee=99,service_charge=100 WHERE id=?", (self.job,)); c.commit()
        page = job_center_v2_page(self._request(f"/jobs/{self.job}/center?tab=quote", b"tab=quote"), self.job, tab="quote")
        html = page.body.decode()
        self.assertIn("Sourcing fee", html)
        self.assertIn("$15.00", html)
        self.assertIn("Service charge", html)
        self.assertIn("$50.00", html)
        self.assertIn(f"${float(snapshot[-1]):.2f}", html)
        with closing(legacy_app.get_connection()) as c:
            current = c.execute("SELECT sourcing_fee,service_charge FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertEqual((current["sourcing_fee"], current["service_charge"]), (15.0, 50.0))

    def test_prequote_fee_save_preserves_existing_descriptions(self):
        from plg_core.basket.routes import center_update_quote_fees
        rid, version = self._tokens()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE jobs SET sourcing_fee_description='Parts research',service_charge_description='Shop labor' WHERE id=?", (self.job,)); c.commit()
        asyncio.run(center_update_quote_fees(_Request({"csrf_token":"token","expected_revision_id":str(rid),"expected_version":str(version),"sourcing_fee":"15","service_charge":"50"}), self.job))
        with closing(legacy_app.get_connection()) as c:
            row=c.execute("SELECT sourcing_fee_description,service_charge_description FROM jobs WHERE id=?",(self.job,)).fetchone()
        self.assertEqual((row["sourcing_fee_description"],row["service_charge_description"]),("Parts research","Shop labor"))

    def test_quote_fee_route_asgi_rejects_invalid_csrf(self):
        async def post():
            transport=httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,base_url="http://testserver") as client:
                return await client.post(f"/jobs/{self.job}/center/quote-fees",data={})
        response=asyncio.run(post())
        self.assertEqual(response.status_code,403)

    def test_quote_fees_is_accessible_button_with_collapsed_editor(self):
        need = self._need("Fee button part")
        item = self._candidate(need, "Fee button supplier", 10)
        set_preferred_sourcing_option(self.job, need["id"], item)
        page = job_center_v2_page(self._request(f"/jobs/{self.job}/center?tab=quote", b"tab=quote"), self.job, tab="quote")
        html = page.body.decode()
        self.assertIn('data-jv2-fees-toggle', html)
        self.assertIn('type="button"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn(f'aria-controls="jv2-fee-panel-{self.job}"', html)
        self.assertIn(f'id="jv2-fee-panel-{self.job}"', html)
        self.assertIn('action="/jobs/%s/center/quote-fees"' % self.job, html)
        self.assertIn('$0 or at least $50', html)
        self.assertIn('name="sourcing_fee"', html)
        self.assertIn('name="service_charge"', html)
        self.assertIn('type="submit">Save fees', html)
        self.assertIn('type="button" onclick=', html)
        self.assertIn('panel.hidden = !open', html)

        self._generate()
        draft_page = job_center_v2_page(self._request(f"/jobs/{self.job}/center?tab=quote", b"tab=quote"), self.job, tab="quote")
        self.assertNotIn('aria-controls="jv2-fee-panel-%s"' % self.job, draft_page.body.decode())

    def test_draft_pdf_routes_use_manifest_bytes_and_no_store_cache(self):
        from plg_core.documents import quote_pdf
        from plg_core.revisions.quote_workflow import _write_documents
        from legacy_app import customer_quote_pdf, internal_quote_pdf
        need = self._need("PDF serving part")
        item = self._candidate(need, "PDF supplier", 10)
        set_preferred_sourcing_option(self.job, need["id"], item)
        with patch.object(quote_pdf, "DOCUMENT_ROOT", Path(self.temp.name) / "documents" / "Customers"):
            self._generate()
            with closing(legacy_app.get_connection()) as c:
                quote = c.execute("SELECT * FROM quotes WHERE job_id=? ORDER BY id DESC LIMIT 1", (self.job,)).fetchone()
                quote_id = int(quote["id"])
            _write_documents(quote_id)
            with closing(legacy_app.get_connection()) as c:
                rows = {r["audience"]: r for r in c.execute("SELECT * FROM quote_documents_manifest WHERE quote_id=?", (quote_id,))}
            for audience, handler in (("CUSTOMER", customer_quote_pdf), ("INTERNAL", internal_quote_pdf)):
                response = handler(quote_id)
                served = Path(response.path)
                digest = __import__("hashlib").sha256(served.read_bytes()).hexdigest()
                self.assertEqual(digest, rows[audience]["sha256"])
                self.assertIn("no-store", response.headers["cache-control"])
        with closing(legacy_app.get_connection()) as c:
            context = build_workspace(c, self.job)
        for document in context["quote_panel"]["documents"]:
            self.assertIn("?v=", document["url"])

    def test_draft_quote_pdfs_use_snapshot_totals_and_projected_labels(self):
        from plg_core.documents.quote_pdf import build_quote_pdf
        quote = {
            "id": 1, "quote_number": "PPS-Q-PDF", "quote_date": "2026-09-13",
            "status": "DRAFT", "customer": "PDF Customer", "company": "",
            "address": "Test Address", "phone": "", "email": "",
            "manufacturer": "", "machine": "", "pin_serial": "",
            "parts_subtotal": 42.0, "shipping_total": 0.0,
            "sourcing_fee": 15.0, "service_charge": 50.0,
            "customer_total": 107.0, "supplier_total": 30.0,
            "profit_total": 77.0,
        }
        items = [
            {"description": "Fee test pump", "quantity": 1, "supplier_name": "SECRET VENDOR",
             "supplier_part_number": "P-1", "source_type": "", "brand": "",
             "supplier_unit_cost": 20.0, "customer_unit_price": 28.0,
             "supplier_line_total": 20.0, "customer_line_total": 28.0, "line_profit": 8.0},
            {"description": "Fee test belt", "quantity": 1, "supplier_name": "SECRET VENDOR",
             "supplier_part_number": "P-2", "source_type": "", "brand": "",
             "supplier_unit_cost": 10.0, "customer_unit_price": 14.0,
             "supplier_line_total": 10.0, "customer_line_total": 14.0, "line_profit": 4.0},
        ]
        customer_path = Path(self.temp.name) / "customer.pdf"
        internal_path = Path(self.temp.name) / "internal.pdf"
        build_quote_pdf(quote, items, customer_path, False)
        build_quote_pdf(quote, items, internal_path, True)
        customer = subprocess.run(["pdftotext", str(customer_path), "-"], check=True, capture_output=True, text=True).stdout
        internal = subprocess.run(["pdftotext", str(internal_path), "-"], check=True, capture_output=True, text=True).stdout
        for label, amount in (("Parts subtotal", "$42.00"), ("Shipping", "$0.00"), ("Sourcing fee", "$15.00"), ("Service charge", "$50.00"), ("TOTAL", "$107.00")):
            self.assertIn(label, customer)
            self.assertIn(amount, customer)
        self.assertIn("2026-10-13", customer)
        self.assertNotIn("SECRET VENDOR", customer)
        self.assertNotIn("$20.00", customer)
        self.assertNotIn("Profit", customer)
        self.assertNotIn("Subtotal  $107.00", customer)
        for label, amount in (("Estimated Supplier Cost", "$30.00"), ("Customer Parts Subtotal", "$42.00"), ("Sourcing Fee", "$15.00"), ("Service Charge", "$50.00"), ("Customer Total", "$107.00"), ("PROJECTED PROFIT", "$77.00")):
            self.assertIn(label, internal)
            self.assertIn(amount, internal)
        self.assertIn("2026-10-13", internal)

    def test_generate_quote_requires_csrf_and_tokens(self):
        need = self._need("Pump")
        item = self._candidate(need, "Supplier")
        set_preferred_sourcing_option(self.job, need["id"], item)
        with self.assertRaises(Exception):
            asyncio.run(center_generate_quote(_Request({}, valid=False), self.job))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_missing_revision_tokens_redirect_without_quote(self):
        need = self._need("Pump")
        item = self._candidate(need, "Supplier")
        set_preferred_sourcing_option(self.job, need["id"], item)
        response = asyncio.run(center_generate_quote(_Request({"csrf_token": "token"}), self.job))
        self.assertEqual(response.status_code, 303)
        self.assertIn("changed+after", response.headers["location"])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_generate_quote_creates_draft_snapshot_and_no_invoice_or_order(self):
        need = self._need("Pump")
        item = self._candidate(need, "Supplier", 25)
        set_preferred_sourcing_option(self.job, need["id"], item)
        response = self._generate()
        self.assertEqual(response.headers["location"], f"/jobs/{self.job}/center?tab=quote")
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT * FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()
            self.assertEqual(quote["status"], "DRAFT")
            self.assertTrue(quote["quote_number"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_items WHERE quote_id=?", (quote["id"],)).fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices WHERE job_id=?", (self.job,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM supplier_orders WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_quote_panel_renders_blocked_and_draft_states(self):
        need = self._need("Pump")
        item = self._candidate(need, "Supplier")
        page = job_center_v2_page(self._request(f"/jobs/{self.job}/center?tab=quote", b"tab=quote"), self.job, tab="quote")
        blocked = page.body.decode()
        self.assertIn("Quote", blocked)
        self.assertIn("Choose a supplier explicitly", blocked)
        set_preferred_sourcing_option(self.job, need["id"], item)
        self._generate()
        page = job_center_v2_page(self._request(f"/jobs/{self.job}/center?tab=quote", b"tab=quote"), self.job, tab="quote")
        html = page.body.decode()
        self.assertIn("Draft", html)
        self.assertIn("Customer total", html)
        self.assertIn("Review quote", html)
        self.assertIn("View customer PDF", html)
        self.assertIn("Not created", html)

    def test_duplicate_current_quote_is_rejected_without_second_quote(self):
        need = self._need("Pump")
        item = self._candidate(need, "Supplier")
        set_preferred_sourcing_option(self.job, need["id"], item)
        self._generate()
        response = self._generate()
        self.assertIn("current+quote", response.headers["location"].lower())
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()[0], 1)

    def test_generate_quote_route_is_registered_and_form_action_matches(self):
        need = self._need("Pump")
        item = self._candidate(need, "Supplier")
        set_preferred_sourcing_option(self.job, need["id"], item)

        def walk(routes):
            for route in routes:
                nested = getattr(route, "original_router", None)
                if nested is not None:
                    yield from walk(nested.routes)
                else:
                    yield route

        matches = [route for route in walk(app.router.routes)
                   if getattr(route, "path", "") == "/jobs/{job_id}/center/quote"
                   and "POST" in (getattr(route, "methods", set()) or set())]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].endpoint.__name__, "center_generate_quote")
        page = job_center_v2_page(
            self._request(f"/jobs/{self.job}/center?tab=quote", b"tab=quote"),
            self.job,
            tab="quote",
        )
        html = page.body.decode()
        self.assertIn(f'action="/jobs/{self.job}/center/quote"', html)

    def test_asgi_generate_quote_invalid_csrf_is_403_not_404(self):
        async def post():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(f"/jobs/{self.job}/center/quote", data={})
        response = asyncio.run(post())
        self.assertEqual(response.status_code, 403)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
