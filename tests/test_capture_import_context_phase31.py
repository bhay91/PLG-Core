from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.assets.service import add_job_asset
from plg_core.basket.routes import import_source_cart
from plg_core.basket.service import get_basket, import_cart
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_requested_need
from plg_core.verification.service import start_one_time_research


ROOT = Path(__file__).resolve().parents[1]


class UniversalCaptureContextPhase31Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-capture31-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,active) VALUES ('C31','Capture 3.1',1)").lastrowid
            self.job_id = c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('J-C31',DATE('now'),?,'Capture 3.1','REQUESTED')", (customer,)).lastrowid
            c.commit()
        self.asset = add_job_asset(self.job_id, manufacturer="CAT", model="420D", vin_pin_serial="C31", asset_type="machine", make_primary=True)
        revision = get_basket(self.job_id)["work_revision"]
        self.need = create_requested_need(self.job_id, job_asset_id=self.asset["id"], wording="Starter",
                                          expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = get_basket(self.job_id)["work_revision"]
        self.session = start_one_time_research(
            self.job_id, self.asset["id"], "https://supplier.test/part",
            requested_need_id=self.need["id"], expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )

    def tearDown(self):
        self.db_patch.stop(); self.temp.cleanup()

    def context(self):
        revision = get_basket(self.job_id)["work_revision"]
        return dict(expected_revision_id=revision["id"], expected_version=revision["lock_version"],
                    job_asset_id=self.asset["id"], requested_need_id=self.need["id"],
                    verification_session_id=self.session["id"], require_capture_context=True)

    def payload(self, item, mode="PAGE"):
        return {"source_key":"one_time_website", "source_name":"One-time Website",
                "source_url":"https://supplier.test/part", "capture_mode":mode, "items":[item]}

    def count_items(self):
        with closing(legacy_app.get_connection()) as c:
            return c.execute("SELECT COUNT(*) FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)", (self.job_id,)).fetchone()[0]

    def test_all_identifier_forms_import_with_exact_lineage_and_visibility(self):
        cases = [
            ("manufacturer_part_number", "MPN-1", "MPN-1", ""),
            ("supplier_part_number", "DER-93592", "", "DER-93592"),
            ("sku", "SKU-1", "", "SKU-1"), ("asin", "ASIN-1", "", "ASIN-1"),
            ("listing_id", "LIST-1", "", "LIST-1"), ("item_id", "ITEM-1", "", "ITEM-1"),
        ]
        for field, value, expected_mpn, expected_supplier in cases:
            with self.subTest(field=field):
                result = import_cart(self.job_id, self.payload({"description":field, field:value}), **self.context())
                self.assertEqual(result["imported_count"], 1)
                with closing(legacy_app.get_connection()) as c:
                    row = c.execute("SELECT * FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?) ORDER BY id DESC LIMIT 1", (self.job_id,)).fetchone()
                self.assertEqual(row["manufacturer_part_number"], expected_mpn)
                self.assertEqual(row["supplier_part_number"], expected_supplier)
                self.assertEqual(row["job_asset_id"], self.asset["id"])
                self.assertEqual(row["primary_requested_need_id"], self.need["id"])
                self.assertEqual(row["research_session_id"], self.session["id"])
                self.assertEqual(row["research_state"], "RESEARCH_RESULT")

    def test_page_and_cart_use_the_same_strict_context(self):
        for mode in ("PAGE", "CART"):
            with self.subTest(mode=mode):
                import_cart(self.job_id, self.payload({"description":mode,"supplier_part_number":f"{mode}-1"}, mode), **self.context())
                with closing(legacy_app.get_connection()) as c:
                    row = c.execute("SELECT research_evidence FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?) ORDER BY id DESC LIMIT 1", (self.job_id,)).fetchone()
                self.assertIn(f"Capture mode: {mode}", row[0])

    def test_changed_active_context_returns_409_and_inserts_nothing(self):
        before = self.count_items(); context = self.context()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE active_source_import SET requested_need_id=NULL WHERE id=1"); c.commit()
        with self.assertRaises(HTTPException) as caught:
            import_cart(self.job_id, self.payload({"description":"Wrong","supplier_part_number":"WRONG"}), **context)
        self.assertEqual(caught.exception.status_code, 409); self.assertEqual(self.count_items(), before)

    def test_missing_active_context_returns_409_and_inserts_nothing(self):
        before = self.count_items(); context = self.context()
        with closing(legacy_app.get_connection()) as c:
            c.execute("DELETE FROM active_source_import WHERE id=1"); c.commit()
        with self.assertRaises(HTTPException) as caught:
            import_cart(self.job_id, self.payload({"description":"Missing","supplier_part_number":"MISS"}), **context)
        self.assertEqual(caught.exception.status_code, 409); self.assertEqual(self.count_items(), before)

    def test_asset_need_and_session_ownership_mismatch_each_return_409(self):
        for key in ("job_asset_id", "requested_need_id", "verification_session_id"):
            with self.subTest(key=key):
                context = self.context(); context[key] = int(context[key]) + 999999
                with self.assertRaises(HTTPException) as caught:
                    import_cart(self.job_id, self.payload({"description":"Mismatch","supplier_part_number":"MISMATCH"}), **context)
                self.assertEqual(caught.exception.status_code, 409)

    def test_zero_accepted_items_is_error_and_rolls_back_source_and_items(self):
        before = self.count_items()
        with closing(legacy_app.get_connection()) as c:
            sources_before = c.execute("SELECT COUNT(*) FROM basket_sources WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)", (self.job_id,)).fetchone()[0]
        with self.assertRaises(HTTPException) as caught:
            import_cart(self.job_id, self.payload({"description":"No identifier"}), **self.context())
        self.assertEqual(caught.exception.status_code, 400); self.assertEqual(self.count_items(), before)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_sources WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)", (self.job_id,)).fetchone()[0], sources_before)

    def test_extension_endpoint_requires_and_passes_complete_context(self):
        revision = get_basket(self.job_id)["work_revision"]
        body = self.payload({"description":"Endpoint","supplier_part_number":"DER-93592"}) | {
            "job_id":self.job_id, "job_asset_id":self.asset["id"], "requested_need_id":self.need["id"],
            "verification_session_id":self.session["id"], "expected_revision_id":revision["id"],
            "expected_version":revision["lock_version"],
        }
        import json
        encoded = json.dumps(body).encode(); sent = False
        async def receive():
            nonlocal sent
            if sent: return {"type":"http.disconnect"}
            sent=True; return {"type":"http.request","body":encoded,"more_body":False}
        request = Request({"type":"http","method":"POST","path":"/api/basket/import-source-cart",
                           "headers":[(b"content-type",b"application/json")],"query_string":b""}, receive)
        result = asyncio.run(import_source_cart(request))
        self.assertEqual(result["imported_count"], 1)


if __name__ == "__main__": unittest.main()
