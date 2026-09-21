from contextlib import closing
from pathlib import Path
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.application import app
from plg_core.database.migrations import run_migrations
from plg_core.disposable.routes import review_test_chain_purge
from plg_core.requests.routes import assistant_research_upload, smart_intake_form
from plg_core.basket.routes import basket_page


ROOT = Path(__file__).resolve().parents[1]


class NavigationRouteConsolidationBatch3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-navigation-batch3-")
        self.db = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db)
        self.db_patch.start()
        run_migrations()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def request(path):
        path, _, query = path.partition("?")
        return Request({
            "type": "http", "method": "GET", "path": path,
            "query_string": query.encode(), "headers": [], "scheme": "http",
            "server": ("localhost", 80), "app": app,
        })

    def test_dashboard_canonical_and_navigation(self):
        alias = legacy_app.dashboard_alias(self.request("/dashboard"))
        self.assertEqual(alias.status_code, 303)
        self.assertEqual(alias.headers["location"], "/")
        base = (ROOT / "templates" / "base.html").read_text()
        self.assertEqual(base.count('nav-link--primary {% if active_page == \'dashboard\' %}active{% endif %}" href="/"'), 1)
        self.assertNotIn('href="/dashboard"', base)

    def test_smart_intake_retires_old_get_without_changing_post_route(self):
        old = assistant_research_upload(self.request("/requests/assistant/research"))
        self.assertEqual(old.status_code, 303)
        self.assertEqual(old.headers["location"], "/requests/smart-intake")
        current = smart_intake_form(self.request("/requests/smart-intake"))
        self.assertEqual(current.status_code, 200)
        smart = (ROOT / "templates" / "smart_intake.html").read_text()
        self.assertNotIn("/requests/assistant/research", smart)
        legacy = (ROOT / "plg_core" / "requests" / "routes.py").read_text()
        self.assertIn('@router.post("/assistant/research/upload"', legacy)
        self.assertIn("/assistant/research/review/", legacy)

    def test_inbox_has_one_smart_intake_cta_and_follow_up_has_one_nav_entry(self):
        inbox = (ROOT / "templates" / "requests.html").read_text()
        self.assertEqual(inbox.count('href="/requests/smart-intake"'), 1)
        base = (ROOT / "templates" / "base.html").read_text()
        self.assertEqual(base.count('href="/follow-up"'), 1)
        self.assertEqual(base.count("<span>Follow-Up</span>"), 1)
        with closing(legacy_app.get_connection()) as connection:
            connection.execute("SELECT 1")
        follow = legacy_app.follow_up_center(self.request("/follow-up?view=HISTORY"), view="HISTORY")
        self.assertEqual(follow.status_code, 200)

    def test_purge_route_is_test_mode_only(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PPS_ENABLE_DISPOSABLE_TEST_CHAIN", None)
            with self.assertRaises(HTTPException) as blocked:
                review_test_chain_purge(self.request("/jobs/1/purge-test-chain?invoice_number=TEST"), 1, "TEST")
            self.assertEqual(blocked.exception.status_code, 404)
        with patch.dict(os.environ, {"PPS_ENABLE_DISPOSABLE_TEST_CHAIN": "1"}):
            with self.assertRaises(HTTPException) as enabled:
                review_test_chain_purge(self.request("/jobs/1/purge-test-chain?invoice_number=TEST"), 1, "TEST")
            self.assertEqual(enabled.exception.status_code, 404)

    def test_batch1_compatibility_routes_remain(self):
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES('B3-C','Batch3 Customer',1)"
            ).lastrowid
            job_id = connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,company,status) VALUES('B3-J','2026-09-11',?,'Batch3 Customer','Batch3 Co','REQUESTED')",
                (customer_id,),
            ).lastrowid
            connection.commit()
        detail = legacy_app.job_detail(self.request(f"/jobs/{job_id}"), job_id)
        basket = basket_page(self.request(f"/jobs/{job_id}/basket"), job_id)
        self.assertEqual(detail.status_code, 303)
        self.assertEqual(basket.status_code, 303)
        self.assertEqual(detail.headers["location"], f"/jobs/{job_id}/center")
        self.assertEqual(basket.headers["location"], f"/jobs/{job_id}/basket?view=advanced")


if __name__ == "__main__":
    unittest.main()
