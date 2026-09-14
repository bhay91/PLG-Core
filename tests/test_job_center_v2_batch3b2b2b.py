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
from plg_core.basket.routes import center_generate_quote, center_send_quote
from plg_core.database.migrations import run_migrations
from plg_core.research.service import (
    create_manual_research_result,
    create_requested_need,
    set_preferred_sourcing_option,
)
from plg_core.revisions.quote_workflow import start_quote_revision
from plg_core.revisions.service import ensure_initial_revision, commit_work_revision


class Req:
    def __init__(self, values):
        self.values = values
        self.cookies = {"pps_csrf_token": "token"}

    async def form(self):
        return self.values


class GenerateRevisedDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pps-3b2b2b-")
        self.db = Path(self.tmp.name) / "db.sqlite"
        shutil.copy2("data/plg_core.db", self.db)
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db),
            patch.object(legacy_app, "DOCUMENTS_DIR", Path(self.tmp.name) / "docs"),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(self.cleanup)
        legacy_app.initialize_database()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES('3B2B2B-C','Generate Customer',1)"
            ).lastrowid
            self.job = connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES('3B2B2B-J','2026-01-01',?,'Generate Customer','REQUESTED')",
                (customer_id,),
            ).lastrowid
            connection.commit()
        need = create_requested_need(self.job, job_asset_id=None, wording="Revision part")
        item = create_manual_research_result(
            self.job,
            job_asset_id=None,
            requested_need_id=need["id"],
            description="Revision part",
            supplier_name="Revision Supplier",
            supplier_unit_cost=12,
            verification_status="VERIFIED",
        )["items"][-1]
        set_preferred_sourcing_option(self.job, need["id"], item["id"])

    def cleanup(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    async def post(self, path, data, *, cookie=True):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            if cookie:
                client.cookies.set("pps_csrf_token", "token")
            return await client.post(path, data=data)

    def setup_revision(self):
        with closing(legacy_app.get_connection()) as connection:
            revision = ensure_initial_revision(connection, self.job)
            connection.commit()
        asyncio.run(center_generate_quote(Req({
            "csrf_token": "token",
            "expected_revision_id": str(revision["id"]),
            "expected_version": str(revision["lock_version"]),
        }), self.job))
        asyncio.run(center_send_quote(Req({"csrf_token": "token"}), self.job))
        with closing(legacy_app.get_connection()) as connection:
            quote = connection.execute(
                "SELECT * FROM quotes WHERE job_id=? AND is_current=1", (self.job,)
            ).fetchone()
        asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/decision",
            {"csrf_token": "token", "decision": "REVISION_REQUIRED"},
        ))
        revision = start_quote_revision(quote["id"], "Customer requested quote revision")
        return quote, revision

    def html(self, tab="quote"):
        from plg_core.basket.routes import job_center_v2_page

        request = type("Request", (), {
            "url_for": lambda self, *args, **kwargs: "/static/x",
            "url": type("URL", (), {"scheme": "http"})(),
            "cookies": {},
        })()
        return job_center_v2_page(request, self.job, tab=tab).body.decode()

    def test_editable_revision_renders_generate_action_and_v2_route(self):
        _, revision = self.setup_revision()
        with closing(legacy_app.get_connection()) as connection:
            from plg_core.jobs.workspace import build_workspace
            workspace = build_workspace(connection, self.job)
        self.assertTrue(workspace["quote_panel"]["revision_generation_allowed"])
        html = self.html("job")
        self.assertIn("Generate revised Draft", html)
        self.assertIn(f"/jobs/{self.job}/center/quote/revision/generate", html)
        self.assertNotIn("/basket?view=advanced", html)
        self.assertIn("Revision in progress", html)

    def test_invalid_csrf_and_revision_context_do_not_generate(self):
        _, revision = self.setup_revision()
        response = asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/revision/generate",
            {"expected_revision_id": revision["id"], "expected_version": revision["lock_version"]},
            cookie=False,
        ))
        self.assertEqual(response.status_code, 403)
        response = asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/revision/generate",
            {"csrf_token": "token", "expected_revision_id": revision["id"] + 999, "expected_version": revision["lock_version"]},
        ))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0], 0)
        response = asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/revision/generate",
            {"csrf_token": "token", "expected_revision_id": revision["id"], "expected_version": revision["lock_version"] + 99},
        ))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0], 0)

    def test_success_creates_current_successor_and_historical_source_card(self):
        source, revision = self.setup_revision()
        response = asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/revision/generate",
            {"csrf_token": "token", "expected_revision_id": revision["id"], "expected_version": revision["lock_version"]},
        ))
        self.assertEqual(response.status_code, 303)
        self.assertIn("Revised+Draft+created", response.headers["location"])
        with closing(legacy_app.get_connection()) as connection:
            successor = connection.execute("SELECT * FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()
            original = connection.execute("SELECT status,is_current FROM quotes WHERE id=?", (source["id"],)).fetchone()
        self.assertEqual(successor["status"], "DRAFT")
        self.assertEqual(successor["is_current"], 1)
        self.assertEqual(tuple(original), ("SUPERSEDED", 0))
        html = self.html("quote")
        self.assertIn(successor["quote_number"], html)
        self.assertIn(f"Revised from", html)
        self.assertIn(source["quote_number"], html)
        self.assertIn("View customer PDF", html)
        self.assertIn("Not created", html)
        asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/revision/generate",
            {"csrf_token": "token", "expected_revision_id": revision["id"], "expected_version": revision["lock_version"]},
        ))
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0], 1)

    def test_identical_completed_generation_post_is_idempotent(self):
        source, revision = self.setup_revision()
        form = {"csrf_token": "token", "expected_revision_id": revision["id"], "expected_version": revision["lock_version"]}
        first = asyncio.run(self.post(f"/jobs/{self.job}/center/quote/revision/generate", form))
        self.assertEqual(first.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            successor = connection.execute("SELECT id,quote_number,quote_track_id FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()
            before = {
                "quotes": connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0],
                "current": connection.execute("SELECT COUNT(*) FROM quotes WHERE quote_track_id=? AND is_current=1", (successor["quote_track_id"],)).fetchone()[0],
                "events": connection.execute("SELECT COUNT(*) FROM quote_events WHERE quote_id=? AND event_type='QUOTE_SUPERSEDED'", (source["id"],)).fetchone()[0],
                "lineage": connection.execute("SELECT COUNT(*) FROM quote_item_lineage WHERE successor_quote_id=?", (successor["id"],)).fetchone()[0],
            }
        second = asyncio.run(self.post(f"/jobs/{self.job}/center/quote/revision/generate", form))
        self.assertEqual(second.status_code, 303)
        self.assertIn("Revised+Draft+already+created", second.headers["location"])
        self.assertNotIn("workflow+has+changed", second.headers["location"])
        with closing(legacy_app.get_connection()) as connection:
            after = {
                "quotes": connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0],
                "current": connection.execute("SELECT COUNT(*) FROM quotes WHERE quote_track_id=? AND is_current=1", (successor["quote_track_id"],)).fetchone()[0],
                "events": connection.execute("SELECT COUNT(*) FROM quote_events WHERE quote_id=? AND event_type='QUOTE_SUPERSEDED'", (source["id"],)).fetchone()[0],
                "lineage": connection.execute("SELECT COUNT(*) FROM quote_item_lineage WHERE successor_quote_id=?", (successor["id"],)).fetchone()[0],
                "same": tuple(connection.execute("SELECT id,quote_number FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()),
            }
        self.assertEqual(after, {**before, "same": (successor["id"], successor["quote_number"])})

    def test_send_action_is_owned_by_current_quote_section(self):
        source, revision = self.setup_revision()
        form = {"csrf_token": "token", "expected_revision_id": revision["id"], "expected_version": revision["lock_version"]}
        response = asyncio.run(self.post(f"/jobs/{self.job}/center/quote/revision/generate", form))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            successor = connection.execute(
                "SELECT id, quote_number FROM quotes WHERE work_revision_id=?", (revision["id"],)
            ).fetchone()
        html = self.html("quote")
        send_action = f'action="/jobs/{self.job}/center/quote/send"'
        self.assertEqual(html.count(send_action), 1)
        self.assertEqual(html.count("Issue / Mark as Sent"), 1)
        current_marker = f"Quote <strong>{successor['quote_number']}</strong>"
        previous_marker = f"<p class=\"jv2-eyebrow\">Previous quote</p>"
        self.assertIn(current_marker, html)
        self.assertIn(previous_marker, html)
        self.assertLess(html.index(send_action), html.index(previous_marker))
        previous_section = html[html.index(previous_marker):]
        self.assertNotIn("/center/quote/send", previous_section)
        self.assertIn(f"/quotes/{successor['id']}/customer/pdf", html)
        self.assertIn(f"/quotes/{successor['id']}/internal/pdf", html)
        self.assertIn(f"/quotes/{source['id']}/customer/pdf", html)
        self.assertIn(f"/quotes/{source['id']}/internal/pdf", html)

    def test_committed_without_successor_recovers_through_v2_route(self):
        _, revision = self.setup_revision()
        commit_work_revision(self.job, expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        response = asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/revision/generate",
            {"csrf_token": "token", "expected_revision_id": revision["id"], "expected_version": revision["lock_version"]},
        ))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0], 1)

    def test_staged_successor_is_visible_and_recoverable_in_v2(self):
        _, revision = self.setup_revision()
        from plg_core.revisions.quote_workflow import generate_quote_from_revision
        with patch("plg_core.revisions.quote_workflow._write_documents", side_effect=RuntimeError("pause")):
            with self.assertRaises(RuntimeError):
                generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            from plg_core.jobs.workspace import build_workspace
            workspace = build_workspace(connection, self.job)
        projection = workspace["quote_panel"]["revision"]
        self.assertTrue(workspace["quote_panel"]["revision_generation_allowed"])
        self.assertTrue(projection["staged"])
        self.assertFalse(projection["successor_is_current"])
        response = asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/revision/generate",
            {"csrf_token": "token", "expected_revision_id": revision["id"], "expected_version": revision["lock_version"]},
        ))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0], 1)

    def test_successor_send_and_customer_decision_compatibility(self):
        _, revision = self.setup_revision()
        asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/revision/generate",
            {"csrf_token": "token", "expected_revision_id": revision["id"], "expected_version": revision["lock_version"]},
        ))
        response = asyncio.run(self.post(f"/jobs/{self.job}/center/quote/send", {"csrf_token": "token"}))
        self.assertEqual(response.status_code, 303)
        response = asyncio.run(self.post(
            f"/jobs/{self.job}/center/quote/decision",
            {"csrf_token": "token", "decision": "APPROVED"},
        ))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT status FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()[0], "APPROVED")
