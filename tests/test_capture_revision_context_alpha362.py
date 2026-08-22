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
from plg_core.basket.service import get_basket, import_cart
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_requested_need
from plg_core.verification.service import (
    start_extension_one_time_research,
    start_one_time_research,
)


ROOT = Path(__file__).resolve().parents[1]


class UniversalCaptureRevisionContextAlpha362Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-alpha362-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('A362-C','Capture Context TEST',1)"
            ).lastrowid
            self.job_id = int(connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('A362-J','2026-08-11',?,'Capture Context TEST','REQUESTED')",
                (customer_id,),
            ).lastrowid)
            connection.commit()
        self.asset = add_job_asset(
            self.job_id, manufacturer="CAT", model="420D",
            vin_pin_serial="A362-CAT", asset_type="Heavy Equipment", make_primary=True,
        )
        revision = get_basket(self.job_id)["work_revision"]
        self.need = create_requested_need(
            self.job_id, job_asset_id=self.asset["id"], wording="Fuel Filter",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        revision = get_basket(self.job_id)["work_revision"]
        with closing(legacy_app.get_connection()) as connection:
            connection.execute(
                "UPDATE work_revisions SET is_synthetic=0 WHERE id=?", (revision["id"],)
            )
            connection.commit()
        self.revision = get_basket(self.job_id)["work_revision"]
        start_one_time_research(
            self.job_id, self.asset["id"], "https://supplier.test/cart",
            requested_need_id=self.need["id"],
            expected_revision_id=self.revision["id"],
            expected_version=self.revision["lock_version"],
        )

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def payload(self):
        return {
            "source_key": "one_time_website",
            "source_name": "One-time Website",
            "source_url": "https://supplier.test/cart",
            "items": [{
                "description": "Fuel Filter", "sku": "A362-FILTER",
                "quantity": 2, "supplier_cost": 20,
            }],
        }

    def test_active_context_exposes_current_revision_and_preserves_lineage(self):
        active = legacy_app.api_active_source_import()
        self.assertEqual(active["job_id"], self.job_id)
        self.assertEqual(active["job_asset_id"], self.asset["id"])
        self.assertEqual(active["requested_need_id"], self.need["id"])
        self.assertEqual(active["expected_revision_id"], self.revision["id"])
        self.assertEqual(active["expected_version"], self.revision["lock_version"])

        session = start_extension_one_time_research(
            job_id=self.job_id, page_url="https://supplier.test/product/filter",
            job_asset_id=self.asset["id"], requested_need_id=self.need["id"],
            expected_revision_id=active["expected_revision_id"],
            expected_version=active["expected_version"],
        )
        self.assertEqual(session["job_asset_id"], self.asset["id"])
        self.assertEqual(session["requested_need_id"], self.need["id"])

        result = import_cart(
            self.job_id, self.payload(),
            expected_revision_id=active["expected_revision_id"],
            expected_version=active["expected_version"],
        )
        self.assertEqual(result["imported_count"], 1)
        with closing(legacy_app.get_connection()) as connection:
            item = connection.execute(
                "SELECT * FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?) ORDER BY id DESC LIMIT 1",
                (self.job_id,),
            ).fetchone()
        self.assertEqual(item["job_asset_id"], self.asset["id"])
        self.assertEqual(item["primary_requested_need_id"], self.need["id"])
        self.assertEqual(item["research_session_id"], session["id"])

    def test_legitimate_stale_revision_is_still_rejected(self):
        with closing(legacy_app.get_connection()) as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)",
                (self.job_id,),
            ).fetchone()[0]
        with self.assertRaises(HTTPException) as stale:
            import_cart(
                self.job_id, self.payload(),
                expected_revision_id=self.revision["id"],
                expected_version=self.revision["lock_version"] + 99,
            )
        self.assertEqual(stale.exception.status_code, 409)
        self.assertIn("changed in another tab", stale.exception.detail)
        with closing(legacy_app.get_connection()) as connection:
            after = connection.execute(
                "SELECT COUNT(*) FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)",
                (self.job_id,),
            ).fetchone()[0]
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
