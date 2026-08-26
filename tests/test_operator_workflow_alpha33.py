from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.assets.service import add_job_asset
from plg_core.basket.service import get_basket
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_requested_need
from plg_core.sources.routes import open_research_source
from plg_core.verification.service import start_one_time_research


ROOT = Path(__file__).resolve().parents[1]


class OperatorWorkflowAlpha33Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-alpha33-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('A33-C','Operator Test',1)"
            ).lastrowid
            self.job_id = int(connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) "
                "VALUES ('A33-J','2026-08-11',?,'Operator Test','REQUESTED')",
                (customer_id,),
            ).lastrowid)
            connection.commit()
        self.asset = add_job_asset(
            self.job_id, manufacturer="CAT", model="420D",
            vin_pin_serial="A33-CAT-420D", asset_type="Heavy Equipment", make_primary=True,
        )

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def revision(self):
        return get_basket(self.job_id)["work_revision"]

    def need(self, wording="Hydraulic Filter"):
        revision = self.revision()
        return create_requested_need(
            self.job_id, job_asset_id=self.asset["id"], wording=wording,
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )

    def test_one_time_website_preserves_context_without_creating_source(self):
        need = self.need()
        revision = self.revision()
        with closing(legacy_app.get_connection()) as connection:
            source_count = connection.execute("SELECT COUNT(*) FROM connector_profiles").fetchone()[0]
        session = start_one_time_research(
            self.job_id, self.asset["id"], "https://example.com/cat-filter",
            requested_need_id=need["id"], expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        opened = open_research_source(self.job_id, session["id"])
        self.assertEqual(opened.headers["location"], "https://example.com/cat-filter")
        with closing(legacy_app.get_connection()) as connection:
            saved = connection.execute(
                "SELECT * FROM verification_sessions WHERE id=?", (session["id"],)
            ).fetchone()
            final_count = connection.execute("SELECT COUNT(*) FROM connector_profiles").fetchone()[0]
        self.assertEqual(saved["job_asset_id"], self.asset["id"])
        self.assertEqual(saved["requested_need_id"], need["id"])
        self.assertEqual(saved["source_name_snapshot"], "One-time Website")
        self.assertEqual(saved["source_url_snapshot"], "https://example.com/cat-filter")
        self.assertEqual(final_count, source_count)

    def test_one_time_website_rejects_unsafe_url(self):
        need = self.need()
        revision = self.revision()
        with self.assertRaises(HTTPException) as blocked:
            start_one_time_research(
                self.job_id, self.asset["id"], "javascript:alert(1)",
                requested_need_id=need["id"], expected_revision_id=revision["id"],
                expected_version=revision["lock_version"],
            )
        self.assertEqual(blocked.exception.status_code, 400)

    def test_multiple_needs_still_require_operator_selection(self):
        self.need("Hydraulic Filter")
        self.need("Fuel Filter")
        revision = self.revision()
        with self.assertRaises(HTTPException) as blocked:
            start_one_time_research(
                self.job_id, self.asset["id"], "https://example.com/catalog",
                requested_need_id=None, expected_revision_id=revision["id"],
                expected_version=revision["lock_version"],
            )
        self.assertEqual(blocked.exception.status_code, 409)

    def test_operator_template_hides_internal_authority_language(self):
        template = (ROOT / "templates" / "job_command_center.html").read_text()
        self.assertIn("Open Website", template)
        self.assertIn("ONE-TIME WEBSITE", template)
        self.assertIn("Confirm for Quote", template)
        self.assertIn("Items Ready for Quote", template)
        self.assertNotIn("ACTIVE RESEARCH CONTEXT", template)
        self.assertNotIn("<strong>Quote Candidate</strong>", template)

    def test_legacy_verification_endpoint_remains_available(self):
        paths = {getattr(route, "path", "") for route in legacy_app.app.routes}
        self.assertIn("/parts/{part_id}/sources/{source_id}/verification", paths)


if __name__ == "__main__":
    unittest.main()
