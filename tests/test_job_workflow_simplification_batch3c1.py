from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request

import legacy_app
from plg_core.application import app
from plg_core.assets.service import add_job_asset
from plg_core.basket.routes import basket_page
from plg_core.basket.service import get_basket
from plg_core.database.migrations import run_migrations
from plg_core.jobs.workflow import derive_machine_work_status, summarize_job_work
from plg_core.requests.routes import create_job_from_request
from plg_core.research.service import create_manual_research_result, create_requested_need


ROOT = Path(__file__).resolve().parents[1]


class JobWorkflowSimplificationBatch3C1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-batch3c1-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start(); run_migrations(); self._clear()

    def tearDown(self):
        self.db_patch.stop(); self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def _clear(self):
        with closing(self.connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "part_shipping_data", "basket_item_need_links", "work_revision_item_need_links",
                "requested_needs", "verification_sessions", "active_source_import",
                "work_revision_items", "work_revision_sources", "work_revisions",
                "invoice_items", "invoices", "quote_items", "quotes", "part_sources",
                "job_parts", "basket_activity", "basket_items", "basket_sources", "baskets",
                "job_follow_ups", "customer_request_attachments", "customer_requests",
                "job_timeline", "audit_logs", "job_assets", "machines", "customers", "jobs",
            ):
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(f'DELETE FROM "{table}"')
            c.execute("PRAGMA foreign_keys=ON"); c.commit()

    def _request(self, path: str, query: bytes = b""):
        return Request({"type":"http", "http_version":"1.1", "method":"GET",
                        "scheme":"http", "path":path, "raw_path":path.encode(),
                        "query_string":query, "headers":[], "client":("test",1),
                        "server":("test",80), "app":app, "router":app.router})

    def job(self):
        with closing(self.connection()) as c:
            customer_id = c.execute(
                "INSERT INTO customers(customer_number,name,company,active) "
                "VALUES ('3C1-C','Test Customer','Test Company',1)"
            ).lastrowid
            job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,company,status) "
                "VALUES ('PPS-J-3C1','2026-08-11',?,'Test Customer','Test Company','REQUESTED')",
                (customer_id,),
            ).lastrowid
            c.commit()
        return int(job_id), int(customer_id)

    def test_summary_and_machine_status_are_derived(self):
        assets = [
            {"need_count":2, "open_need_count":2, "parts_found_count":1},
            {"need_count":1, "open_need_count":0, "parts_found_count":2},
        ]
        self.assertEqual(summarize_job_work(assets, 1), {
            "machine_count":2, "need_count":3, "open_need_count":2,
            "parts_found_count":3, "quote_candidate_count":1,
        })
        self.assertEqual(summarize_job_work([], 0, 2)["parts_found_count"], 2)
        self.assertEqual(derive_machine_work_status(need_count=2, parts_found_count=0,
            quote_candidate_count=0, covered_need_count=0, active_research=False)[0], "RESEARCH_NEEDED")
        self.assertEqual(derive_machine_work_status(need_count=2, parts_found_count=0,
            quote_candidate_count=0, covered_need_count=0, active_research=True)[0], "IN_RESEARCH")
        self.assertEqual(derive_machine_work_status(need_count=2, parts_found_count=1,
            quote_candidate_count=0, covered_need_count=0, active_research=True)[0], "PARTS_FOUND")
        self.assertEqual(derive_machine_work_status(need_count=2, parts_found_count=2,
            quote_candidate_count=2, covered_need_count=2, active_research=False)[0], "READY")

    def test_request_conversion_creates_asset_and_needs_not_quote_candidates(self):
        _, customer_id = self.job()
        with closing(self.connection()) as c:
            machine_id = c.execute(
                "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,year,vin_pin_serial,active) "
                "VALUES (?,'PPS-M-3C1','350D','John Deere','350D','2022','PIN-3C1',1)", (customer_id,)
            ).lastrowid
            request_id = c.execute(
                "INSERT INTO customer_requests(request_number,customer_id,machine_id,individual_name,"
                "company_name,request_text,requested_parts,status,is_archived) "
                "VALUES ('PPS-R-3C1',?,?,'Test Customer','Test Company','Original message',"
                "'Fuel Filter Kit\nHydraulic Filter','READY',0)", (customer_id,machine_id)
            ).lastrowid
            c.commit()
        response = create_job_from_request(int(request_id))
        self.assertEqual(response.status_code, 303)
        with closing(self.connection()) as c:
            request = c.execute("SELECT * FROM customer_requests WHERE id=?", (request_id,)).fetchone()
            asset = c.execute("SELECT * FROM job_assets WHERE job_id=?", (request["job_id"],)).fetchone()
            needs = c.execute("SELECT wording,job_asset_id FROM requested_needs WHERE job_id=? ORDER BY id", (request["job_id"],)).fetchall()
            self.assertEqual((asset["manufacturer"],asset["model"],asset["vin_pin_serial"]), ("John Deere","350D","PIN-3C1"))
            self.assertEqual([row["wording"] for row in needs], ["Fuel Filter Kit","Hydraulic Filter"])
            self.assertTrue(all(row["job_asset_id"] == asset["id"] for row in needs))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id WHERE b.job_id=?", (request["job_id"],)).fetchone()[0], 0)
            self.assertEqual(request["request_text"], "Original message")

    def test_selected_machine_context_navigation_and_saved_work(self):
        job_id, _ = self.job()
        deere = add_job_asset(job_id, manufacturer="John Deere", model="350D", make_primary=True)
        jcb = add_job_asset(job_id, manufacturer="JCB", model="3CX")
        toyota = add_job_asset(job_id, manufacturer="Toyota", model="Hilux")
        revision = get_basket(job_id)["work_revision"]
        create_requested_need(job_id, job_asset_id=deere["id"], wording="Fuel Filter",
                              expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = get_basket(job_id)["work_revision"]
        create_requested_need(job_id, job_asset_id=jcb["id"], wording="Seal Kit",
                              expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = get_basket(job_id)["work_revision"]
        create_manual_research_result(job_id, job_asset_id=deere["id"], requested_need_id=None,
                                      description="Primary Filter", manufacturer_part_number="RE-1",
                                      expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        deere_response = basket_page(self._request(f"/jobs/{job_id}/basket", f"asset_id={deere['id']}".encode()), job_id, deere["id"])
        jcb_response = basket_page(self._request(f"/jobs/{job_id}/basket", f"asset_id={jcb['id']}".encode()), job_id, jcb["id"])
        self.assertEqual([need["wording"] for need in deere_response.context["requested_needs"]], ["Fuel Filter"])
        self.assertEqual([need["wording"] for need in jcb_response.context["requested_needs"]], ["Seal Kit"])
        self.assertEqual(deere_response.context["next_asset"]["id"], jcb["id"])
        self.assertEqual(jcb_response.context["previous_asset"]["id"], deere["id"])
        self.assertEqual(jcb_response.context["next_asset"]["id"], toyota["id"])
        with closing(self.connection()) as c:
            saved = c.execute("SELECT COUNT(*) FROM basket_items WHERE job_asset_id=? AND requested_description='Primary Filter'", (deere["id"],)).fetchone()[0]
        self.assertEqual(saved, 1)

    def test_single_and_zero_machine_contexts_are_safe(self):
        single_job, _ = self.job()
        deere = add_job_asset(single_job, manufacturer="John Deere", model="350D", make_primary=True)
        single = basket_page(self._request(f"/jobs/{single_job}/basket"), single_job)
        self.assertEqual(single.context["selected_asset_id"], deere["id"])
        self.assertIsNone(single.context["previous_asset"]); self.assertIsNone(single.context["next_asset"])
        with closing(self.connection()) as c:
            customer_id = c.execute("SELECT id FROM customers LIMIT 1").fetchone()[0]
            zero_job = c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('PPS-J-ZERO','2026-08-11',?,'Test Customer','REQUESTED')", (customer_id,)).lastrowid
            c.commit()
        zero = basket_page(self._request(f"/jobs/{zero_job}/basket"), int(zero_job))
        self.assertIsNone(zero.context["selected_asset"])
        self.assertEqual(zero.context["job_summary"]["machine_count"], 0)

    def test_template_keeps_original_request_secondary_and_guides_sourcing(self):
        source = (ROOT / "templates" / "job_command_center.html").read_text()
        self.assertIn("View Original Customer Request", source)
        self.assertIn("machine-need-list", source)
        self.assertIn("RESEARCH SOURCE", source)
        self.assertIn("Open Source", source)
        self.assertIn("PARTS FOUND", source)
        self.assertIn("Next Machine ·", source)
        self.assertNotIn("<h2>What the customer needs</h2>", source)


if __name__ == "__main__":
    unittest.main()
