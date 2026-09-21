from contextlib import closing
from pathlib import Path
import asyncio
import tempfile
import unittest
from unittest.mock import patch

import legacy_app
from plg_core.basket.models import BasketItemCreate
from plg_core.basket.routes import center_select_sourcing_option
from plg_core.basket.routes import job_center_v2_page
from starlette.requests import Request as StarletteRequest
from plg_core.application import app
from plg_core.basket.service import add_item
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_manual_research_result, create_requested_need
from plg_core.web_security import CSRF_COOKIE_NAME
import httpx


class Request:
    def __init__(self, values, valid=True):
        self.values = values
        self.cookies = {CSRF_COOKIE_NAME: "token"} if valid else {}

    async def form(self):
        return self.values


class Batch2B1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-job-center-2b1-")
        self.addCleanup(self.temp.cleanup)
        self.patchers = [patch.object(legacy_app, name, Path(self.temp.name) / name.lower()) for name in ("DB_PATH", "DOCUMENTS_DIR", "UPLOADS_DIR")]
        for p in self.patchers: p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patchers])
        legacy_app.initialize_database(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            self.job = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES ('2B1','2026-01-01','Customer','REQUESTED')").lastrowid
            c.commit()

    def candidate(self, need, name, cost, verification="VERIFIED"):
        result = create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need["id"], description=need["wording"], supplier_name=name, supplier_unit_cost=cost, verification_status=verification)
        return result["items"][-1]["id"]

    def test_selection_post_route_is_registered_on_application(self):
        """The browser action must resolve through the real ASGI app, not only direct calls."""
        expected = "/jobs/{job_id}/center/needs/{need_id}/sourcing/{item_id}/select"
        matches = []
        for included in app.routes:
            router = getattr(included, "original_router", None)
            for route in getattr(router, "routes", ()):
                if getattr(route, "path", None) == expected:
                    matches.append(route)
        self.assertEqual(len(matches), 1)
        self.assertIn("POST", matches[0].methods)
        self.assertEqual(matches[0].endpoint.__name__, "center_select_sourcing_option")

        # An invalid CSRF request reaches the registered endpoint (403), proving
        # the browser POST path is routed rather than returning FastAPI's 404.
        async def post_invalid_csrf():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post(f"/jobs/{self.job}/center/needs/1/sourcing/1/select", data={})
        response = asyncio.run(post_invalid_csrf())
        self.assertEqual(response.status_code, 403)

    def test_rendered_use_supplier_action_matches_registered_route(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        item = self.candidate(need, "Supplier A", 10)
        request = StarletteRequest({"type": "http", "method": "GET", "path": "/jobs/%s/center" % self.job, "headers": [], "query_string": b"", "scheme": "http", "server": ("localhost", 80), "app": app})
        html = job_center_v2_page(request, self.job).body.decode()
        action = f'/jobs/{self.job}/center/needs/{need["id"]}/sourcing/{item}/select'
        self.assertIn(f'action="{action}"', html)

    def test_select_supplier_delegates_and_replaces_current_candidate(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        first = self.candidate(need, "Supplier A", 10)
        second = self.candidate(need, "Supplier B", 12)
        from plg_core.research.service import set_quote_candidate
        set_quote_candidate(self.job, first, candidate=True, requested_need_ids=[need["id"]])
        asyncio.run(center_select_sourcing_option(Request({"csrf_token": "token"}), self.job, need["id"], second))
        with closing(legacy_app.get_connection()) as c:
            rows = c.execute("SELECT id,selected,research_state,quantity FROM basket_items WHERE id IN (?,?) ORDER BY id", (first, second)).fetchall()
        self.assertEqual([(r["selected"], r["research_state"]) for r in rows], [(1, "QUOTE_CANDIDATE"), (1, "QUOTE_CANDIDATE")])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT preferred FROM basket_item_need_links WHERE basket_item_id=? AND requested_need_id=?", (first, need["id"])).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT preferred FROM basket_item_need_links WHERE basket_item_id=? AND requested_need_id=?", (second, need["id"])).fetchone()[0], 1)

    def test_selection_preserves_requested_and_basket_quantities(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump", quantity=3)
        first = add_item(self.job, BasketItemCreate(primary_requested_need_id=need["id"], requested_description="Pump"))
        item_id = first["items"][0]["id"]
        from plg_core.research.service import set_quote_candidate
        set_quote_candidate(self.job, item_id, candidate=True, requested_need_ids=[need["id"]])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT quantity FROM requested_needs WHERE id=?", (need["id"],)).fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT quantity FROM basket_items WHERE id=?", (item_id,)).fetchone()[0], 3)

    def test_csrf_rejects_selection_without_mutation(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        item = self.candidate(need, "Supplier A", 10)
        with self.assertRaises(Exception):
            asyncio.run(center_select_sourcing_option(Request({}, valid=False), self.job, need["id"], item))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT selected FROM basket_items WHERE id=?", (item,)).fetchone()[0], 0)

    def test_stale_selection_redirects_without_changing_selection(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        first = self.candidate(need, "Supplier A", 10)
        second = self.candidate(need, "Supplier B", 12)
        from plg_core.revisions.service import ensure_initial_revision, touch_revision
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job)
            rid, version = revision["id"], revision["lock_version"]
            touch_revision(c, rid, version); c.commit()
        response = asyncio.run(center_select_sourcing_option(Request({"csrf_token":"token", "expected_revision_id":str(rid), "expected_version":str(version)}), self.job, need["id"], second))
        self.assertEqual(response.status_code, 303)
        self.assertIn("This+job+changed+after+you+opened+it", response.headers["location"])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT selected FROM basket_items WHERE id=?", (first,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT selected FROM basket_items WHERE id=?", (second,)).fetchone()[0], 0)

    def test_rejected_candidate_is_not_selectable(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        item = self.candidate(need, "Rejected", 10, "REJECTED")
        from plg_core.research.service import set_preferred_sourcing_option
        with self.assertRaises(Exception):
            set_preferred_sourcing_option(self.job, need["id"], item)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT selected FROM basket_items WHERE id=?", (item,)).fetchone()[0], 0)

    def test_shared_candidate_cannot_remain_current_for_unrelated_need(self):
        """Documents the global basket-item selected semantics of the current schema."""
        need_a = create_requested_need(self.job, job_asset_id=None, wording="Water pump")
        need_b = create_requested_need(self.job, job_asset_id=None, wording="Belt")
        shared = self.candidate(need_a, "Shared", 10)
        a2 = self.candidate(need_a, "A only", 12)
        from plg_core.research.service import set_quote_candidate
        set_quote_candidate(self.job, shared, candidate=True, requested_need_ids=[need_a["id"], need_b["id"]])
        from plg_core.research.service import set_preferred_sourcing_option
        set_preferred_sourcing_option(self.job, need_a["id"], shared)
        set_preferred_sourcing_option(self.job, need_b["id"], shared)
        asyncio.run(center_select_sourcing_option(Request({"csrf_token":"token"}), self.job, need_a["id"], a2))
        with closing(legacy_app.get_connection()) as c:
            shared_row = c.execute("SELECT selected FROM basket_items WHERE id=?", (shared,)).fetchone()[0]
            a2_row = c.execute("SELECT selected FROM basket_items WHERE id=?", (a2,)).fetchone()[0]
            links = c.execute("SELECT requested_need_id FROM basket_item_need_links WHERE basket_item_id=? ORDER BY requested_need_id", (shared,)).fetchall()
        self.assertEqual((shared_row, a2_row), (1, 1))
        self.assertEqual([r[0] for r in links], [need_a["id"], need_b["id"]])

    def test_independent_need_selection_does_not_change_other_need(self):
        need_a = create_requested_need(self.job, job_asset_id=None, wording="Water pump")
        need_b = create_requested_need(self.job, job_asset_id=None, wording="Belt")
        a1, a2, b1 = self.candidate(need_a, "A1", 10), self.candidate(need_a, "A2", 12), self.candidate(need_b, "B1", 8)
        from plg_core.research.service import set_quote_candidate
        set_quote_candidate(self.job, a1, candidate=True, requested_need_ids=[need_a["id"]])
        set_quote_candidate(self.job, b1, candidate=True, requested_need_ids=[need_b["id"]])
        asyncio.run(center_select_sourcing_option(Request({"csrf_token":"token"}), self.job, need_a["id"], a2))
        with closing(legacy_app.get_connection()) as c:
            states = {r["id"]: r["selected"] for r in c.execute("SELECT id,selected FROM basket_items WHERE id IN (?,?,?)", (a1,a2,b1)).fetchall()}
        self.assertEqual(states, {a1:1, a2:1, b1:1})

    def test_selection_rejects_committed_revision_without_links_or_state_changes(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        first, second = self.candidate(need, "A", 10), self.candidate(need, "B", 12)
        from plg_core.revisions.service import ensure_initial_revision
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job)
            c.execute("UPDATE work_revisions SET state='COMMITTED' WHERE id=?", (revision["id"],)); c.commit()
        response = asyncio.run(center_select_sourcing_option(Request({"csrf_token":"token"}), self.job, need["id"], second))
        self.assertEqual(response.status_code, 303)
        self.assertIn("This+job+changed+after+you+opened+it", response.headers["location"])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual([r[0] for r in c.execute("SELECT selected FROM basket_items WHERE id IN (?,?) ORDER BY id", (first,second)).fetchall()], [0,0])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_item_need_links WHERE requested_need_id=?", (need["id"],)).fetchone()[0], 2)

    def test_selection_does_not_mutate_commercial_or_fulfillment_tables(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        first, second = self.candidate(need, "A", 10), self.candidate(need, "B", 12)
        from plg_core.research.service import set_quote_candidate
        names = ("quotes","quote_items","quote_revisions","supplier_orders","actual_cost_adjustments","invoices","payments","transactions","receiving_events","deliveries")
        with closing(legacy_app.get_connection()) as c:
            existing={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables=[n for n in names if n in existing]
            before={n:c.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in tables}
        set_quote_candidate(self.job, first, candidate=True, requested_need_ids=[need["id"]])
        with closing(legacy_app.get_connection()) as c:
            after={n:c.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in tables}
        self.assertEqual(before, after)

    def test_preference_migration_is_idempotent_and_does_not_backfill_selected(self):
        from plg_core.database import migrations as migration_module
        with closing(legacy_app.get_connection()) as c:
            columns = {r[1] for r in c.execute("PRAGMA table_info(basket_item_need_links)")}
            self.assertIn("preferred", columns)
            indexes = {r[1] for r in c.execute("PRAGMA index_list(basket_item_need_links)")}
            self.assertIn("uq_need_preferred_candidate", indexes)
            need_a = create_requested_need(self.job, job_asset_id=None, wording="A")
            need_b = create_requested_need(self.job, job_asset_id=None, wording="B")
            item = self.candidate(need_a, "Shared", 10)
            c.execute("INSERT INTO basket_item_need_links(basket_item_id,requested_need_id,relationship) VALUES (?,?, 'SATISFIES')", (item, need_b["id"]))
            c.execute("UPDATE basket_items SET selected=1 WHERE id=?", (item,)); c.commit()
            migration_module._migration_0053_preferred_sourcing_option(c); c.commit()
            self.assertEqual(c.execute("SELECT preferred FROM basket_item_need_links WHERE basket_item_id=? AND requested_need_id=?", (item, need_b["id"])).fetchone()[0], 0)
            c.execute("UPDATE basket_item_need_links SET preferred=1 WHERE basket_item_id=? AND requested_need_id=?", (item, need_a["id"])).fetchone()
            c.commit()
            migration_module._migration_0053_preferred_sourcing_option(c); c.commit()
            self.assertEqual(c.execute("SELECT preferred FROM basket_item_need_links WHERE basket_item_id=? AND requested_need_id=?", (item, need_a["id"])).fetchone()[0], 1)

    def test_one_candidate_can_be_preferred_for_multiple_needs(self):
        need_a = create_requested_need(self.job, job_asset_id=None, wording="A")
        need_b = create_requested_need(self.job, job_asset_id=None, wording="B")
        item = self.candidate(need_a, "Shared", 10)
        from plg_core.research.service import set_quote_candidate, set_preferred_sourcing_option
        set_quote_candidate(self.job, item, candidate=True, requested_need_ids=[need_a["id"], need_b["id"]])
        set_preferred_sourcing_option(self.job, need_a["id"], item)
        set_preferred_sourcing_option(self.job, need_b["id"], item)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_item_need_links WHERE basket_item_id=? AND preferred=1", (item,)).fetchone()[0], 2)

    def test_job_center_renders_current_badge_and_use_supplier_only_for_other_candidate(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Pump")
        first = self.candidate(need, "Supplier A", 10)
        second = self.candidate(need, "Supplier B", 12)
        from plg_core.research.service import set_quote_candidate
        set_quote_candidate(self.job, first, candidate=True, requested_need_ids=[need["id"]])
        request = StarletteRequest({'type':'http','method':'GET','path':f'/jobs/{self.job}/center','headers':[], 'query_string':b'', 'scheme':'http','server':('localhost',80),'app':app})
        html = job_center_v2_page(request, self.job).body.decode()
        self.assertIn('Current', html)
        self.assertEqual(html.count('Use this supplier'), 1)
        self.assertIn('Manage sourcing', html)
