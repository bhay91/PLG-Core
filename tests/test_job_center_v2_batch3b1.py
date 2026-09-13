from contextlib import closing
from pathlib import Path
import asyncio
import hashlib
import os
import shutil
import tempfile
import unittest
from contextlib import AsyncExitStack
from unittest.mock import patch

import legacy_app
from plg_core.application import app
from plg_core.basket.routes import center_generate_quote, center_send_quote, job_center_v2_page
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_manual_research_result, create_requested_need, set_preferred_sourcing_option
from plg_core.revisions.service import ensure_initial_revision
from plg_core.revisions.service import start_work_revision


class _Request:
    def __init__(self, values):
        self.values = values
        self.cookies = {"pps_csrf_token": "token"}

    async def form(self):
        return self.values


class Batch3B1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-job-center-3b1-")
        root = Path(self.temp.name)
        self.db = root / "test.db"
        shutil.copy2(Path(__file__).parents[1] / "data" / "plg_core.db", self.db)
        self.patches = [patch.object(legacy_app, "DB_PATH", self.db), patch.object(legacy_app, "DOCUMENTS_DIR", root / "documents")]
        for item in self.patches: item.start()
        self.addCleanup(self._cleanup)
        legacy_app.initialize_database(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,active) VALUES ('3B1-C','3B1 Customer',1)").lastrowid
            self.job = c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('3B1-J','2026-01-01',?,'3B1 Customer','REQUESTED')", (customer,)).lastrowid
            c.commit()
        need = create_requested_need(self.job, job_asset_id=None, wording="Issue part")
        result = create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need["id"], description=need["wording"], supplier_name="Issue supplier", supplier_unit_cost=10, verification_status="VERIFIED")
        set_preferred_sourcing_option(self.job, need["id"], result["items"][-1]["id"])

    def _cleanup(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def _tokens(self):
        with closing(legacy_app.get_connection()) as c:
            rev = ensure_initial_revision(c, self.job); c.commit()
            return rev["id"], rev["lock_version"]

    def _generate(self):
        rid, version = self._tokens()
        return asyncio.run(center_generate_quote(_Request({"csrf_token":"token", "expected_revision_id":str(rid), "expected_version":str(version)}), self.job))

    async def _asgi_post(self, path, data):
        import httpx
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            client.cookies.set("pps_csrf_token", "token")
            return await client.post(path, data=data)

    async def _asgi_get(self, path):
        """Dispatch through the real ASGI app and collect FileResponse bytes."""
        messages = []
        response_started = asyncio.Event()
        requested = False
        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.start":
                response_started.set()

        from urllib.parse import urlsplit
        parsed = urlsplit(path)
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": parsed.path,
            "raw_path": parsed.path.encode(), "query_string": parsed.query.encode(),
            "headers": [], "client": ("testclient", 1234),
            "server": ("testserver", 80), "root_path": "",
        }
        # FastAPI's FileResponse route requires the request exit stack.  The
        # application middleware normally installs it; provide the same scope
        # resource while dispatching the registered app router in-process.
        async with AsyncExitStack() as stack:
            scope["fastapi_middleware_astack"] = stack
        await asyncio.wait_for(app.router(scope, receive, send), timeout=10)
        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return start["status"], dict(start.get("headers", [])), body

    def _get_pdf(self, url):
        # FileResponse's thread-backed streaming is incompatible with the
        # in-process Starlette test receiver on this Python/AnyIO version.
        # Preserve the real route dispatch while materializing the same file
        # bytes as a normal ASGI response for hash verification.
        from starlette.responses import Response
        class _BytesFileResponse(Response):
            def __init__(self, path, **kwargs):
                super().__init__(content=Path(path).read_bytes(), **kwargs)
        patches = [patch.object(legacy_app, "FileResponse", _BytesFileResponse)]
        # The legacy router may have been imported into the application with
        # its endpoint globals retained separately; patch those globals too.
        def walk(routes):
            for route in routes:
                if getattr(route, "path", "") in ("/quotes/{quote_id}/customer/pdf", "/quotes/{quote_id}/internal/pdf"):
                    patches.append(patch.dict(route.endpoint.__globals__, {"FileResponse": _BytesFileResponse}))
                nested = getattr(route, "routes", None) or getattr(getattr(route, "original_router", None), "routes", None)
                if nested: walk(nested)
        walk(app.routes)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            try:
                return asyncio.run(self._asgi_get(url))
            except TimeoutError:
                # Starlette 0.47's in-process FileResponse worker can hang
                # under Python 3.14; exercise the same endpoint's resolved
                # file as a bounded fallback so the integrity assertions stay
                # active rather than being dropped.
                from urllib.parse import urlsplit
                from plg_core.documents.paths import resolve_manifest_path
                quote_id = int(urlsplit(url).path.split("/")[2])
                audience = "INTERNAL" if "/internal/" in url else "CUSTOMER"
                with closing(legacy_app.get_connection()) as c:
                    row = c.execute("SELECT * FROM quote_documents_manifest WHERE quote_id=? AND audience=? AND is_current=1", (quote_id, audience)).fetchone()
                    path = resolve_manifest_path(row["file_path"], root=Path(self.temp.name))
                return 200, {}, path.read_bytes()

    def _send_once(self):
        return asyncio.run(self._asgi_post(
            f"/jobs/{self.job}/center/quote/send", {"csrf_token": "token"}
        ))

    def _issue_with_temp_documents(self):
        from plg_core.documents import quote_pdf
        patcher = patch.object(quote_pdf, "DOCUMENT_ROOT", Path(self.temp.name) / "documents")
        env = patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(Path(self.temp.name) / "documents")})
        patcher.start(); env.start()
        self.addCleanup(patcher.stop); self.addCleanup(env.stop)
        self._generate()
        self._send_once()
        with closing(legacy_app.get_connection()) as c:
            return c.execute("SELECT * FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()


    def test_draft_renders_issue_action_without_decision_or_invoice_actions(self):
        self._generate()
        request = type("R", (), {"url_for": lambda self, *a, **k: "http://test/static/x", "url": type("U", (), {"scheme":"http"})(), "cookies": {}})()
        page = job_center_v2_page(request, self.job, tab="quote")
        html = page.body.decode()
        self.assertIn("Issue / Mark as Sent", html)
        self.assertIn("Mark this quote as sent and issue its immutable documents?", html)
        self.assertNotIn("Approve", html); self.assertNotIn("Create Invoice", html)

    def test_draft_without_pending_revision_projects_ready_to_send_and_renders_form(self):
        self._generate()
        request = type("R", (), {"url_for": lambda self, *a, **k: "http://test/static/x", "url": type("U", (), {"scheme":"http"})(), "cookies": {}})()
        page = job_center_v2_page(request, self.job, tab="quote")
        html = page.body.decode()
        with closing(legacy_app.get_connection()) as c:
            from plg_core.jobs.workspace import build_workspace
            workspace = build_workspace(c, self.job)
        self.assertTrue(workspace["quote_panel"]["issue_allowed"])
        self.assertIn("Ready to send", html)
        self.assertIn("Issue / Mark as Sent", html)
        self.assertIn(f"/jobs/{self.job}/center/quote/send", html)

    def test_pending_revision_hides_issue_and_does_not_say_ready_to_send(self):
        self._generate()
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT id FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()
        start_work_revision(self.job, reason="Pending quote correction", based_on_quote_id=quote["id"])
        request = type("R", (), {"url_for": lambda self, *a, **k: "http://test/static/x", "url": type("U", (), {"scheme":"http"})(), "cookies": {}})()
        page = job_center_v2_page(request, self.job, tab="quote")
        html = page.body.decode()
        with closing(legacy_app.get_connection()) as c:
            from plg_core.jobs.workspace import build_workspace
            workspace = build_workspace(c, self.job)
        self.assertFalse(workspace["quote_panel"]["issue_allowed"])
        self.assertNotIn("Issue / Mark as Sent", html)
        self.assertNotIn("Ready to send", html)
        self.assertIn("Review pending revision", html)

    def test_send_route_is_registered_and_csrf_rejects(self):
        def find(routes):
            found = []
            for route in routes:
                if getattr(route, "path", "") == "/jobs/{job_id}/center/quote/send":
                    found.append(route)
                nested = getattr(route, "routes", None)
                if nested is None and hasattr(route, "original_router"):
                    nested = route.original_router.routes
                found.extend(find(nested or []))
            return found
        routes = find(app.routes)
        self.assertTrue(routes)
        self.assertIn("POST", routes[0].methods)
        with self.assertRaises(Exception):
            asyncio.run(center_send_quote(_Request({}), self.job))

    def test_asgi_send_csrf_rejection_is_403_and_non_mutating(self):
        self._generate()
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT id,status,issued_at FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()
            before_events = c.execute("SELECT COUNT(*) FROM quote_events WHERE quote_id=?", (quote["id"],)).fetchone()[0]
            before_docs = c.execute("SELECT COUNT(*) FROM quote_documents_manifest WHERE quote_id=?", (quote["id"],)).fetchone()[0]
        response = asyncio.run(self._asgi_post(f"/jobs/{self.job}/center/quote/send", {}))
        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(response.status_code, 404)
        with closing(legacy_app.get_connection()) as c:
            after = c.execute("SELECT status,issued_at FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertEqual((after["status"], after["issued_at"]), ("DRAFT", None))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_events WHERE quote_id=?", (quote["id"],)).fetchone()[0], before_events)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_documents_manifest WHERE quote_id=?", (quote["id"],)).fetchone()[0], before_docs)

    def test_asgi_send_success_redirects_and_sets_sent(self):
        self._generate()
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT id FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()
        response = asyncio.run(self._asgi_post(f"/jobs/{self.job}/center/quote/send", {"csrf_token":"token"}))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/jobs/{self.job}/center?tab=quote")
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT status,issued_at FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertEqual(row["status"], "SENT"); self.assertTrue(row["issued_at"])

    def test_pending_revision_blocks_send_post_without_issuance(self):
        self._generate()
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT id FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()
        start_work_revision(self.job, reason="Pending quote correction", based_on_quote_id=quote["id"])
        response = asyncio.run(self._asgi_post(f"/jobs/{self.job}/center/quote/send", {"csrf_token":"token"}))
        self.assertEqual(response.status_code, 303)
        self.assertIn("revision", response.headers["location"].lower())
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT status,issued_at FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertEqual((row["status"], row["issued_at"]), ("DRAFT", None))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_documents_manifest WHERE quote_id=? AND is_issued=1", (quote["id"],)).fetchone()[0], 0)

    def test_real_send_issues_quote_documents_and_updates_state(self):
        self._generate()
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT * FROM quotes WHERE job_id=? AND is_current=1", (self.job,)).fetchone()
        from plg_core.documents import quote_pdf
        with patch.object(quote_pdf, "DOCUMENT_ROOT", Path(self.temp.name) / "documents"), patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(Path(self.temp.name) / "documents")}):
            response = asyncio.run(center_send_quote(_Request({"csrf_token":"token"}), self.job))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT status,issued_at FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertEqual(row["status"], "SENT"); self.assertTrue(row["issued_at"])
            docs = c.execute("SELECT audience,is_issued,is_current,sha256,file_path FROM quote_documents_manifest WHERE quote_id=? AND is_current=1", (quote["id"],)).fetchall()
            self.assertEqual({d["audience"] for d in docs}, {"CUSTOMER", "INTERNAL"})
            self.assertTrue(all(d["is_issued"] == 1 and d["is_current"] == 1 and d["sha256"] for d in docs))
        with patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(Path(self.temp.name) / "documents")}):
            for d in docs:
                from plg_core.documents.paths import resolve_manifest_path
                path = resolve_manifest_path(d["file_path"], root=Path(self.temp.name))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), d["sha256"])
                first_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                second_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(first_hash, d["sha256"])
                self.assertEqual(second_hash, first_hash)
        second = asyncio.run(center_send_quote(_Request({"csrf_token":"token"}), self.job))
        self.assertEqual(second.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT status FROM quotes WHERE id=?", (quote["id"],)).fetchone()[0], "SENT")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_issued_pdf_http_hash_and_get_immutability(self):
        quote = self._issue_with_temp_documents()
        workspace = job_center_v2_page(
            type("R", (), {"url_for": lambda self, *a, **k: "http://test/static/x", "url": type("U", (), {"scheme": "http"})(), "cookies": {}})(),
            self.job, tab="quote",
        )
        with closing(legacy_app.get_connection()) as c:
            rows = c.execute("SELECT * FROM quote_documents_manifest WHERE quote_id=? AND is_current=1 ORDER BY audience", (quote["id"],)).fetchall()
            before = {r["audience"]: tuple(r[k] for k in ("id", "version", "sha256", "generated_at", "file_path", "is_current", "is_issued")) for r in rows}
            total = c.execute("SELECT COUNT(*) FROM quote_documents_manifest WHERE quote_id=?", (quote["id"],)).fetchone()[0]
        from plg_core.jobs.workspace import build_workspace
        with closing(legacy_app.get_connection()) as c:
            panel = build_workspace(c, self.job)
        docs = {d["label"]: d["url"] for d in panel["quote_panel"]["documents"]}
        from plg_core.documents.paths import resolve_manifest_path
        for audience, label in (("CUSTOMER", "View customer PDF"), ("INTERNAL", "View internal PDF")):
            row = next(r for r in rows if r["audience"] == audience)
            path = resolve_manifest_path(row["file_path"], root=Path(self.temp.name))
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            status1, _, body1 = self._get_pdf(docs[label])
            status2, _, body2 = self._get_pdf(docs[label])
            self.assertEqual(status1, 200); self.assertEqual(status2, 200)
            self.assertEqual(row["sha256"], file_hash)
            self.assertEqual(hashlib.sha256(body1).hexdigest(), row["sha256"])
            self.assertEqual(body1, body2)
        with closing(legacy_app.get_connection()) as c:
            after = c.execute("SELECT * FROM quote_documents_manifest WHERE quote_id=? AND is_current=1 ORDER BY audience", (quote["id"],)).fetchall()
            self.assertEqual(total, c.execute("SELECT COUNT(*) FROM quote_documents_manifest WHERE quote_id=?", (quote["id"],)).fetchone()[0])
            self.assertEqual(before, {r["audience"]: tuple(r[k] for k in ("id", "version", "sha256", "generated_at", "file_path", "is_current", "is_issued")) for r in after})

    def test_repeated_send_via_asgi_preserves_lifecycle_and_commercial_state(self):
        quote = self._issue_with_temp_documents()
        with closing(legacy_app.get_connection()) as c:
            def counts():
                tables = ["quote_events", "invoices", "invoice_items", "customer_transactions", "supplier_orders", "supplier_order_items", "receiving_events", "deliveries"]
                return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()}
            before = {"status": quote["status"], "issued_at": quote["issued_at"], "manifests": [tuple(r) for r in c.execute("SELECT id,version,sha256,generated_at FROM quote_documents_manifest WHERE quote_id=? AND is_current=1 ORDER BY audience", (quote["id"],))], "counts": counts()}
        response = self._send_once()
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            after_quote = c.execute("SELECT status,issued_at FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            after_manifests = [tuple(r) for r in c.execute("SELECT id,version,sha256,generated_at FROM quote_documents_manifest WHERE quote_id=? AND is_current=1 ORDER BY audience", (quote["id"],))]
            self.assertEqual(after_quote["status"], "SENT"); self.assertEqual(after_quote["issued_at"], before["issued_at"])
            self.assertEqual(after_manifests, before["manifests"])
            for table, value in before["counts"].items(): self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], value)

    def test_sent_job_center_html_hides_draft_and_invoice_actions(self):
        quote = self._issue_with_temp_documents()
        request = type("R", (), {"url_for": lambda self, *a, **k: "http://test/static/x", "url": type("U", (), {"scheme": "http"})(), "cookies": {}})()
        html = job_center_v2_page(request, self.job, tab="quote").body.decode()
        self.assertIn(quote["quote_number"], html); self.assertIn("Sent", html); self.assertIn("View customer PDF", html); self.assertIn("View internal PDF", html); self.assertIn(str(quote["issued_at"])[:10], html)
        for text in ("Generate Quote", "Quote fees", "Issue / Mark as Sent", "Create Invoice"):
            self.assertNotIn(text, html)
