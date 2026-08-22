from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from legacy_app import api_active_source_import
from plg_core.assets.service import add_job_asset
from plg_core.basket.service import get_basket, import_cart
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_requested_need
from plg_core.verification.service import (
    start_asset_research,
    start_extension_one_time_research,
)


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions/firefox/PLG-Firefox-Extension-v0.15-Connector-SDK"


class FirefoxOneTimeSourceAlpha34Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-alpha34-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('A34-C','Synthetic Source Customer',1)"
            ).lastrowid
            self.job_id = int(connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) "
                "VALUES ('A34-J','2026-08-11',?,'Synthetic Source Customer','REQUESTED')",
                (customer_id,),
            ).lastrowid)
            connection.commit()
        self.asset = add_job_asset(
            self.job_id, manufacturer="CAT", model="420D",
            vin_pin_serial="A34-CAT-420D", asset_type="Heavy Equipment", make_primary=True,
        )
        revision = get_basket(self.job_id)["work_revision"]
        self.need = create_requested_need(
            self.job_id, job_asset_id=self.asset["id"], wording="Fuel Filter",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def activate_configured_source(self):
        revision = get_basket(self.job_id)["work_revision"]
        with closing(legacy_app.get_connection()) as connection:
            source = connection.execute(
                "SELECT id FROM connector_profiles WHERE lower(display_name) IN ('cat sis','general research') "
                "AND is_enabled=1 AND is_archived=0 ORDER BY CASE WHEN lower(display_name)='cat sis' THEN 0 ELSE 1 END LIMIT 1"
            ).fetchone()
        return start_asset_research(
            self.job_id, self.asset["id"], int(source["id"]),
            requested_need_id=self.need["id"], expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )

    def test_unknown_website_uses_one_time_context_and_imports_review_only_result(self):
        self.activate_configured_source()
        with closing(legacy_app.get_connection()) as connection:
            source_count = connection.execute("SELECT COUNT(*) FROM connector_profiles").fetchone()[0]
        session = start_extension_one_time_research(
            job_id=self.job_id,
            job_asset_id=self.asset["id"],
            requested_need_id=self.need["id"],
            page_url="https://example.com/catalog/fuel-filter",
        )
        result = import_cart(self.job_id, {
            "source_key": "one_time_website",
            "source_name": "One-time Website",
            "source_url": "https://example.com/catalog/fuel-filter",
            "trust_level": "NEEDS_REVIEW",
            "items": [{
                "description": "CAT Fuel Filter",
                "manufacturer_part_number": "A34-FILTER",
                "supplier_part_number": "A34-FILTER",
                "quantity": 1,
            }],
        })
        self.assertEqual(result["imported_count"], 1)
        with closing(legacy_app.get_connection()) as connection:
            item = connection.execute(
                "SELECT * FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)",
                (self.job_id,),
            ).fetchone()
            final_source_count = connection.execute("SELECT COUNT(*) FROM connector_profiles").fetchone()[0]
        self.assertEqual(session["source_name_snapshot"], "One-time Website")
        self.assertEqual(session["source_url_snapshot"], "https://example.com/catalog/fuel-filter")
        self.assertEqual(item["job_asset_id"], self.asset["id"])
        self.assertEqual(item["primary_requested_need_id"], self.need["id"])
        self.assertEqual(item["research_session_id"], session["id"])
        self.assertEqual(item["research_state"], "RESEARCH_RESULT")
        self.assertEqual(item["selected"], 0)
        self.assertEqual(final_source_count, source_count)

    def test_configured_source_capture_still_preserves_context(self):
        session = self.activate_configured_source()
        active = api_active_source_import()
        self.assertEqual(active["verification_session_id"], session["id"])
        self.assertEqual(active["connector_profile_id"], session["connector_profile_id"])
        self.assertEqual(active["source_url_snapshot"], session["source_url_snapshot"])
        result = import_cart(self.job_id, {
            "source_key": "cat_sis", "source_name": "CAT SIS",
            "source_url": "https://sis2.cat.com/cart", "trust_level": "OEM_VERIFIED",
            "items": [{"description": "CAT Filter", "manufacturer_part_number": "1R-TEST",
                       "supplier_part_number": "1R-TEST", "quantity": 1}],
        })
        self.assertEqual(result["imported_count"], 1)
        with closing(legacy_app.get_connection()) as connection:
            item = connection.execute(
                "SELECT * FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(item["research_session_id"], session["id"])
        self.assertEqual(item["research_state"], "RESEARCH_RESULT")
        self.assertEqual(item["selected"], 0)

    def test_unknown_website_rejects_unsafe_url_and_context_mismatch(self):
        self.activate_configured_source()
        with self.assertRaises(HTTPException) as unsafe:
            start_extension_one_time_research(
                job_id=self.job_id, page_url="javascript:alert(1)",
                job_asset_id=self.asset["id"], requested_need_id=self.need["id"],
            )
        self.assertEqual(unsafe.exception.status_code, 400)
        with self.assertRaises(HTTPException) as mismatch:
            start_extension_one_time_research(
                job_id=self.job_id, page_url="https://example.com/part",
                job_asset_id=self.asset["id"] + 999, requested_need_id=self.need["id"],
            )
        self.assertEqual(mismatch.exception.status_code, 409)

    def test_extension_no_longer_blocks_unknown_http_sites(self):
        content = (EXTENSION / "content.js").read_text()
        popup = (EXTENSION / "popup.js").read_text()
        self.assertNotIn("This supplier site is not supported yet", content)
        self.assertNotIn("does not have a connector yet", content)
        self.assertIn('key: "one_time_website"', content)
        self.assertIn("/api/research/extension/one-time-context", popup)
        self.assertIn("sameOrigin", popup)
        self.assertIn("Add an identifier before sending this part to PPS.", popup)

    def test_legacy_verification_and_source_directory_remain_available(self):
        paths = {getattr(route, "path", "") for route in legacy_app.app.routes}
        self.assertIn("/parts/{part_id}/sources/{source_id}/verification", paths)
        with closing(legacy_app.get_connection()) as connection:
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM connector_profiles").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
