from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.assets.service import add_job_asset
from plg_core.basket.service import get_basket, import_cart
from plg_core.basket.routes import clone_supplier_quote
from plg_core.database.migrations import MIGRATIONS, run_migrations
from plg_core.commercial.service import create_selective_draft_quote
from plg_core.revisions.quote_workflow import start_quote_revision
from plg_core.research.branding import manufacturer_brand
from plg_core.research.service import (
    create_manual_research_result,
    create_requested_need,
    save_shipping_data,
    set_quote_candidate,
    update_requested_need,
)
from plg_core.verification.service import start_asset_research


ROOT = Path(__file__).resolve().parents[1]


class MachineFirstResearchBatch3CTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-batch3c-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()
        self._clear()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def _clear(self):
        with closing(self.connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "part_shipping_data", "consolidated_shipments",
                "basket_item_need_links", "work_revision_item_need_links",
                "requested_needs", "verification_sessions", "active_source_import",
                "work_revision_items", "work_revision_sources", "work_revisions",
                "invoice_items", "invoices", "quote_items", "quotes", "part_sources",
                "job_parts", "basket_activity", "basket_items", "basket_sources",
                "baskets", "job_follow_ups", "customer_requests", "job_timeline",
                "audit_logs", "job_assets", "machines", "customers", "jobs",
            ):
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(f'DELETE FROM "{table}"')
            c.execute("PRAGMA foreign_keys=ON")
            c.commit()

    def job(self):
        with closing(self.connection()) as c:
            customer_id = c.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('3C-C','Norman Frater',1)"
            ).lastrowid
            job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) "
                "VALUES ('PPS-J-3C','2026-08-11',?,'Norman Frater','REQUESTED')",
                (customer_id,),
            ).lastrowid
            c.commit()
        return int(job_id)

    def assets(self, job_id):
        return [
            add_job_asset(job_id, manufacturer="John Deere", model="350D", vin_pin_serial="PIN-DEERE", make_primary=True),
            add_job_asset(job_id, manufacturer="JCB", model="3CX", vin_pin_serial="PIN-JCB"),
            add_job_asset(job_id, manufacturer="Toyota", model="Hilux", vin_pin_serial="VIN-TOYOTA"),
        ]

    def revision(self, job_id):
        return get_basket(job_id)["work_revision"]

    def test_requested_need_is_machine_scoped_and_not_quote_ready(self):
        job_id = self.job(); deere, jcb, _ = self.assets(job_id)
        revision = self.revision(job_id)
        need = create_requested_need(
            job_id, job_asset_id=deere["id"], wording="Fuel Filter Kit",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        with closing(self.connection()) as c:
            self.assertEqual(need["job_asset_id"], deere["id"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_items").fetchone()[0], 0)
            self.assertNotEqual(need["job_asset_id"], jcb["id"])

    def test_manual_result_inherits_machine_and_requires_promotion(self):
        job_id = self.job(); deere, _, _ = self.assets(job_id)
        revision = self.revision(job_id)
        need = create_requested_need(job_id, job_asset_id=deere["id"], wording="Fuel Filter Kit",
                                     expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = self.revision(job_id)
        basket = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Primary Fuel Filter", manufacturer_part_number="RE123456",
            supplier_unit_cost=100, expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        item = basket["items"][-1]
        self.assertEqual(item["research_state"], "RESEARCH_RESULT")
        self.assertFalse(item["selected"])
        self.assertEqual(item["job_asset_id"], deere["id"])
        revision = self.revision(job_id)
        promoted = set_quote_candidate(
            job_id, item["id"], candidate=True, requested_need_ids=[need["id"]],
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertEqual(promoted["research_state"], "QUOTE_CANDIDATE")
        self.assertEqual(promoted["selected"], 1)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_item_need_links WHERE basket_item_id=?", (item["id"],)).fetchone()[0], 1)

    def test_cross_machine_need_link_is_blocked(self):
        job_id = self.job(); deere, jcb, _ = self.assets(job_id)
        revision = self.revision(job_id)
        need = create_requested_need(job_id, job_asset_id=jcb["id"], wording="Seal Kit",
                                     expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = self.revision(job_id)
        item = create_manual_research_result(job_id, job_asset_id=deere["id"], requested_need_id=None,
                                             description="Filter", manufacturer_part_number="RE1",
                                             expected_revision_id=revision["id"], expected_version=revision["lock_version"])["items"][-1]
        revision = self.revision(job_id)
        with self.assertRaises(HTTPException):
            set_quote_candidate(job_id, item["id"], candidate=True, requested_need_ids=[need["id"]],
                                expected_revision_id=revision["id"], expected_version=revision["lock_version"])

    def test_supplier_quote_copy_stays_in_selected_machine_research_results(self):
        job_id = self.job(); deere, jcb, _ = self.assets(job_id)
        revision = self.revision(job_id)
        item = create_manual_research_result(job_id, job_asset_id=deere["id"], requested_need_id=None,
                                             description="Fuel Filter", manufacturer_part_number="RE5",
                                             expected_revision_id=revision["id"], expected_version=revision["lock_version"])["items"][-1]
        revision = self.revision(job_id)
        set_quote_candidate(job_id, item["id"], candidate=True, requested_need_ids=[],
                            expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = self.revision(job_id)
        clone_supplier_quote(job_id, vendor_name="Dealer B", source_type="OEM", job_asset_id=deere["id"],
                             expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        with closing(self.connection()) as c:
            copied = c.execute("SELECT * FROM basket_items WHERE supplier_name='Dealer B'").fetchone()
        self.assertIsNotNone(copied)
        self.assertEqual((copied["job_asset_id"], copied["research_state"], copied["selected"]),
                         (deere["id"], "RESEARCH_RESULT", 0))
        self.assertNotEqual(copied["job_asset_id"], jcb["id"])

    def test_need_resolution_preserves_wording_and_unresolved_needs(self):
        job_id = self.job(); deere, _, _ = self.assets(job_id)
        revision = self.revision(job_id)
        first = create_requested_need(job_id, job_asset_id=deere["id"], wording="Fuel Filter Kit",
                                      expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = self.revision(job_id)
        second = create_requested_need(job_id, job_asset_id=deere["id"], wording="Hydraulic Filter",
                                       expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = self.revision(job_id)
        update_requested_need(job_id, first["id"], state="SATISFIED", wording=first["wording"],
                              expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        with closing(self.connection()) as c:
            states = {r["wording"]: r["state"] for r in c.execute("SELECT * FROM requested_needs WHERE id IN (?,?)", (first["id"], second["id"]))}
        self.assertEqual(states, {"Fuel Filter Kit": "SATISFIED", "Hydraulic Filter": "OPEN"})

    def test_shipping_optional_and_quality_precedence(self):
        job_id = self.job(); deere, _, _ = self.assets(job_id)
        revision = self.revision(job_id)
        item = create_manual_research_result(job_id, job_asset_id=deere["id"], requested_need_id=None,
                                             description="Filter", manufacturer_part_number="RE2",
                                             expected_revision_id=revision["id"], expected_version=revision["lock_version"])["items"][-1]
        verified = save_shipping_data(job_id, item["id"], quality="VERIFIED", unit_weight=2.2,
                                      length=7, width=5, height=5, provenance="Manufacturer")
        self.assertEqual(verified["quality"], "VERIFIED")
        with self.assertRaises(HTTPException):
            save_shipping_data(job_id, item["id"], quality="ESTIMATED_LOW", unit_weight=1.5)
        actual = save_shipping_data(job_id, item["id"], quality="ACTUAL", unit_weight=2.15,
                                    length=7, width=5, height=5, provenance="Warehouse scale")
        self.assertIsNotNone(actual["measured_at"])
        with closing(self.connection()) as c:
            current = c.execute("SELECT * FROM part_shipping_data WHERE basket_item_id=? AND is_current=1", (item["id"],)).fetchone()
            self.assertEqual((current["quality"], current["unit_weight"]), ("ACTUAL", 2.15))

    def test_asset_verification_and_future_cart_return_share_exact_context(self):
        job_id = self.job(); deere, _, _ = self.assets(job_id)
        revision = self.revision(job_id)
        need = create_requested_need(job_id, job_asset_id=deere["id"], wording="Fuel Filter",
                                     expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = self.revision(job_id)
        with closing(self.connection()) as c:
            connector = c.execute("SELECT id FROM connector_profiles WHERE is_enabled=1 ORDER BY id LIMIT 1").fetchone()
        session = start_asset_research(job_id, deere["id"], connector["id"], requested_need_id=need["id"],
                                       expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        self.assertEqual(session["requested_need_id"], need["id"])
        imported = import_cart(job_id, {"source_key": "test", "source_name": "Catalog", "items": [
            {"description": "Primary Filter", "manufacturer_part_number": "RE3", "quantity": 1}
        ]})
        item = imported["items"][-1]
        self.assertEqual((item["job_asset_id"], item["primary_requested_need_id"], item["research_state"], item["selected"]),
                         (deere["id"], need["id"], "RESEARCH_RESULT", 0))

    def test_brand_registry_uses_fallback_without_approved_local_asset(self):
        job_id = self.job(); self.assets(job_id)
        with closing(self.connection()) as c:
            deere = manufacturer_brand(c, "Deere")
            unknown = manufacturer_brand(c, "Acme Equipment")
        self.assertEqual(deere["canonical_name"], "John Deere")
        self.assertFalse(deere["logo_url"])
        self.assertEqual(unknown["key"], "generic")
        self.assertEqual(unknown["initials"], "AE")

    @patch("plg_core.commercial.service._write_documents")
    def test_need_and_shipping_lineage_survive_quote_revision_clone(self, _write_documents):
        job_id = self.job(); deere, _, _ = self.assets(job_id)
        revision = self.revision(job_id)
        need = create_requested_need(job_id, job_asset_id=deere["id"], wording="Fuel Filter Kit",
                                     expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        revision = self.revision(job_id)
        item = create_manual_research_result(job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
                                             description="Primary Filter", manufacturer_part_number="RE4",
                                             supplier_unit_cost=100, expected_revision_id=revision["id"],
                                             expected_version=revision["lock_version"])["items"][-1]
        save_shipping_data(job_id, item["id"], quality="VERIFIED", unit_weight=2.2, provenance="Manufacturer")
        revision = self.revision(job_id)
        set_quote_candidate(job_id, item["id"], candidate=True, requested_need_ids=[need["id"]],
                            expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        with closing(self.connection()) as c:
            c.execute("UPDATE basket_items SET verification_status='VERIFIED' WHERE id=?", (item["id"],))
            c.commit()
        revision = self.revision(job_id)
        quote = create_selective_draft_quote(job_id, basket_item_ids=[item["id"]], bill_to_kind="CONTACT",
                                             expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        with closing(self.connection()) as c:
            c.execute("UPDATE quotes SET status='SENT',issued_at=CURRENT_TIMESTAMP WHERE id=?", (quote["id"],))
            c.commit()
        started = start_quote_revision(quote["id"], "Customer requested a correction")
        with closing(self.connection()) as c:
            cloned = c.execute("SELECT bi.* FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id WHERE b.job_id=?", (job_id,)).fetchone()
            links = c.execute("SELECT requested_need_id FROM basket_item_need_links WHERE basket_item_id=?", (cloned["id"],)).fetchall()
            shipping = c.execute("SELECT quality,unit_weight FROM part_shipping_data WHERE basket_item_id=? AND is_current=1", (cloned["id"],)).fetchone()
        self.assertEqual(cloned["primary_requested_need_id"], need["id"])
        self.assertEqual(cloned["research_state"], "QUOTE_CANDIDATE")
        self.assertEqual([row[0] for row in links], [need["id"]])
        self.assertEqual(tuple(shipping), ("VERIFIED", 2.2))

    def test_migration_direct_rerun_does_not_consume_brand_ids(self):
        migration = dict(MIGRATIONS)["0037_machine_first_research"]
        with closing(self.connection()) as c:
            before = c.execute("SELECT seq FROM sqlite_sequence WHERE name='manufacturer_brands'").fetchone()[0]
            migration(c); c.commit()
            after = c.execute("SELECT seq FROM sqlite_sequence WHERE name='manufacturer_brands'").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
