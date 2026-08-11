from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.assets.service import add_job_asset, assign_basket_item_asset, edit_job_asset
from plg_core.basket.models import BasketItemCreate
from plg_core.basket.service import add_item, get_basket
from plg_core.commercial.service import (
    create_selective_draft_quote,
    record_quote_item_decisions,
    split_issued_quote,
    update_draft_bill_to,
)
from plg_core.database.migrations import run_migrations
from plg_core.documents import quote_pdf
from plg_core.verification.service import start_part_verification
from plg_core.revisions.quote_workflow import start_quote_revision


ROOT = Path(__file__).resolve().parents[1]


class MultiMachineBatch3BTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-batch3b-")
        self.db_path = Path(self.temp.name) / "test.db"
        self.document_root = Path(self.temp.name) / "documents" / "Customers"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.doc_patch = patch.object(quote_pdf, "DOCUMENT_ROOT", self.document_root)
        self.db_patch.start(); self.doc_patch.start()
        run_migrations(); self._clear()

    def tearDown(self):
        self.doc_patch.stop(); self.db_patch.stop(); self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def _clear(self):
        tables = (
            "quote_item_lineage", "quote_split_successors", "quote_splits",
            "quote_item_decisions", "verification_sessions", "quote_documents_manifest",
            "work_revision_attachments", "work_revision_items", "work_revision_sources",
            "work_revisions", "delivery_items", "deliveries", "receiving_event_items",
            "receiving_events", "supplier_order_items", "supplier_orders", "invoice_events",
            "invoice_items", "invoices", "quote_events", "quote_items", "quotes",
            "quote_tracks", "customer_transactions", "part_sources", "job_parts",
            "basket_activity", "basket_attachments", "basket_items", "basket_sources",
            "baskets", "job_follow_ups", "customer_request_attachments", "customer_requests",
            "job_timeline", "audit_logs", "job_assets", "machines", "customers", "jobs",
        )
        with closing(self.connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in tables:
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(f'DELETE FROM "{table}"')
            c.execute("DELETE FROM active_source_import")
            c.execute("PRAGMA foreign_keys=ON"); c.commit()

    def job(self):
        with closing(self.connection()) as c:
            customer_id = c.execute(
                "INSERT INTO customers(customer_number,name,company,active) VALUES ('3B-C','Norman Frater','Tropical Real Estate Development',1)"
            ).lastrowid
            job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,company,status) VALUES ('PPS-J-3B','2026-08-11',?,'Norman Frater','Tropical Real Estate Development','REQUESTED')",
                (customer_id,),
            ).lastrowid
            c.commit(); return int(job_id)

    def assets(self, job_id):
        return [
            add_job_asset(job_id, manufacturer="John Deere", model="350D", vin_pin_serial="1FF350DXTA0806941", asset_type="Excavator", make_primary=True),
            add_job_asset(job_id, manufacturer="JCB", model="3CX", vin_pin_serial="JCB-PIN", asset_type="Backhoe"),
            add_job_asset(job_id, manufacturer="Toyota", model="Hilux", year="2020", vin_pin_serial="VIN-HILUX", asset_type="Vehicle"),
        ]

    def part(self, job_id, asset_id, description, *, override=None):
        basket = add_item(job_id, BasketItemCreate(
            job_asset_id=asset_id, requested_description=description,
            supplier_name="Test Supplier", supplier_part_number=description.upper(),
            quantity=1, supplier_unit_cost=100, markup_percent=30,
            customer_unit_price_override=override, verification_status="VERIFIED", selected=True,
        ))
        return basket["items"][-1]

    def test_legacy_job_and_zero_asset_job_remain_valid(self):
        job_id = self.job()
        item = self.part(job_id, None, "General shop supply")
        self.assertIsNone(item["job_asset_id"])
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM job_assets WHERE job_id=?", (job_id,)).fetchone()[0], 0)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_multi_asset_assignment_mutability_and_historical_snapshot_protection(self):
        job_id = self.job(); deere, jcb, _ = self.assets(job_id)
        item = self.part(job_id, deere["id"], "Fuel Filter Kit")
        basket = get_basket(job_id); revision = basket["work_revision"]
        assign_basket_item_asset(job_id, item["id"], jcb["id"], expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT job_asset_id FROM basket_items WHERE id=?", (item["id"],)).fetchone()[0], jcb["id"])
            part_id = c.execute("INSERT INTO job_parts(job_id,requested_description,job_asset_id) VALUES (?,'Fuel Filter',?)", (job_id, jcb["id"])).lastrowid
            source_id = c.execute("INSERT INTO part_sources(part_id,supplier_name) VALUES (?,'Supplier')", (part_id,)).lastrowid
            c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,is_current) VALUES ('PPS-Q-HIST',?,'2026-08-11','SENT',1)", (job_id,))
            quote_id = c.execute("SELECT id FROM quotes WHERE quote_number='PPS-Q-HIST'").fetchone()[0]
            c.execute("INSERT INTO quote_items(quote_id,part_id,source_id,job_asset_id,quantity,description,supplier_name,source_type,asset_manufacturer_snapshot,asset_model_snapshot) VALUES (?,?,?,?,1,'Fuel Filter','Supplier','AFTERMARKET','JCB','3CX')", (quote_id, part_id, source_id, jcb["id"]))
            c.commit()
        with self.assertRaises(HTTPException):
            edit_job_asset(job_id, jcb["id"], manufacturer="Changed")
        with closing(self.connection()) as c:
            row = c.execute("SELECT asset_manufacturer_snapshot,asset_model_snapshot FROM quote_items WHERE quote_id=?", (quote_id,)).fetchone()
            self.assertEqual(tuple(row), ("JCB", "3CX"))

    @patch("plg_core.commercial.service._write_documents")
    def test_selective_multi_machine_quotes_leave_unquoted_work_available(self, _write):
        job_id = self.job(); deere, jcb, toyota = self.assets(job_id)
        first = self.part(job_id, deere["id"], "Fuel Filter Kit", override=0)
        second = self.part(job_id, jcb["id"], "Seal Kit")
        third = self.part(job_id, toyota["id"], "Headlight")
        revision = get_basket(job_id)["work_revision"]
        quote = create_selective_draft_quote(
            job_id, basket_item_ids=[first["id"], second["id"]],
            bill_to_kind="CONTACT", expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        with closing(self.connection()) as c:
            rows = c.execute("SELECT description,job_asset_id,customer_unit_price FROM quote_items WHERE quote_id=? ORDER BY id", (quote["id"],)).fetchall()
            self.assertEqual(len(rows), 2); self.assertEqual(rows[0][2], 0)
            self.assertEqual({r[1] for r in rows}, {deere["id"], jcb["id"]})
            remaining = c.execute("SELECT requested_description FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id WHERE b.job_id=?", (job_id,)).fetchall()
            self.assertEqual([r[0] for r in remaining], ["Headlight"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (job_id,)).fetchone()[0], 1)
        continuation = get_basket(job_id)["work_revision"]
        second_quote = create_selective_draft_quote(
            job_id, basket_item_ids=[get_basket(job_id)["items"][0]["id"]],
            bill_to_kind="COMPANY", expected_revision_id=continuation["id"],
            expected_version=continuation["lock_version"],
        )
        self.assertNotEqual(quote["quote_number"], second_quote["quote_number"])

    @patch("plg_core.commercial.service._write_documents")
    def test_draft_edit_cannot_mix_with_unquoted_continuation_workspace(self, _write):
        job_id = self.job(); deere, jcb, _ = self.assets(job_id)
        quoted = self.part(job_id, deere["id"], "Quoted Filter")
        self.part(job_id, jcb["id"], "Still Unquoted")
        revision = get_basket(job_id)["work_revision"]
        quote = create_selective_draft_quote(
            job_id, basket_item_ids=[quoted["id"]], bill_to_kind="CONTACT",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        continuation = get_basket(job_id)["work_revision"]
        self.assertFalse(continuation["is_synthetic"])
        with self.assertRaises(HTTPException) as blocked:
            start_quote_revision(quote["id"], "Correct draft")
        self.assertEqual(blocked.exception.status_code, 409)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_revisions WHERE job_id=? AND state='EDITABLE'", (job_id,)).fetchone()[0], 1)

    @patch("plg_core.commercial.service._write_documents")
    def test_bill_to_split_lineage_partial_decisions_and_idempotency(self, _write):
        job_id = self.job(); deere, jcb, _ = self.assets(job_id)
        first = self.part(job_id, deere["id"], "Filter"); second = self.part(job_id, jcb["id"], "Seal")
        revision = get_basket(job_id)["work_revision"]
        quote = create_selective_draft_quote(job_id, basket_item_ids=[first["id"], second["id"]], bill_to_kind="CONTACT", expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        update_draft_bill_to(quote["id"], kind="COMPANY")
        with closing(self.connection()) as c:
            c.execute("UPDATE quotes SET status='SENT',issued_at=CURRENT_TIMESTAMP WHERE id=?", (quote["id"],))
            item_ids = [r[0] for r in c.execute("SELECT id FROM quote_items WHERE quote_id=? ORDER BY id", (quote["id"],))]
            c.commit()
        record_quote_item_decisions(quote["id"], {item_ids[0]: "ACCEPTED", item_ids[1]: "DECLINED"}, reason="Customer selected filters")
        successors = split_issued_quote(quote["id"], groups=[
            {"quote_item_ids": [item_ids[0]], "bill_to_kind": "CONTACT"},
            {"quote_item_ids": [item_ids[1]], "bill_to_kind": "COMPANY"},
        ], reason="Separate personal and company billing")
        retry = split_issued_quote(quote["id"], groups=[], reason="retry")
        self.assertEqual([q["id"] for q in successors], [q["id"] for q in retry])
        with closing(self.connection()) as c:
            source = c.execute("SELECT status,is_current FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertEqual(tuple(source), ("SUPERSEDED", 0))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_item_lineage WHERE split_id=(SELECT id FROM quote_splits WHERE source_quote_id=?)", (quote["id"],)).fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(DISTINCT quote_number) FROM quotes WHERE job_id=?", (job_id,)).fetchone()[0], 3)

    def test_machine_aware_verification_context_and_missing_url(self):
        job_id = self.job(); deere, jcb, _ = self.assets(job_id)
        assigned = self.part(job_id, deere["id"], "Filter")
        unassigned = self.part(job_id, None, "Unknown Part")
        with closing(self.connection()) as c:
            connector = c.execute("INSERT INTO connector_profiles(connector_key,display_name,category,trust_level,launch_url,connector_type,is_enabled,is_archived,manufacturer_applicability) VALUES ('3b-source','Dealer Catalog','OEM','OEM_VERIFIED','','LINK',1,0,'John Deere')").lastrowid
            c.commit()
        revision = get_basket(job_id)["work_revision"]
        session = start_part_verification(job_id, assigned["id"], connector, expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        self.assertEqual(session["job_asset_id"], deere["id"])
        with closing(self.connection()) as c:
            active = c.execute("SELECT job_asset_id,basket_item_id,verification_session_id FROM active_source_import WHERE id=1").fetchone()
            self.assertEqual(tuple(active), (deere["id"], assigned["id"], session["id"]))
            self.assertEqual(c.execute("SELECT launch_url FROM connector_profiles WHERE id=?", (connector,)).fetchone()[0], "")
        with self.assertRaises(HTTPException):
            start_part_verification(job_id, unassigned["id"], connector, expected_revision_id=revision["id"], expected_version=revision["lock_version"])

    @patch("plg_core.commercial.service._write_documents")
    def test_multi_asset_customer_pdf_grouping_and_confidentiality(self, _write):
        job_id = self.job(); deere, jcb, _ = self.assets(job_id)
        first = self.part(job_id, deere["id"], "Fuel Filter"); second = self.part(job_id, jcb["id"], "Seal Kit")
        revision = get_basket(job_id)["work_revision"]
        quote = create_selective_draft_quote(job_id, basket_item_ids=[first["id"], second["id"]], bill_to_kind="CONTACT", expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        with closing(self.connection()) as c:
            q, items = legacy_app.load_quote(c, quote["id"])
        paths = quote_pdf.generate_quote_pdfs(q, items)
        customer = Path(paths["customer"]); original = customer.read_bytes()
        self.assertGreater(len(original), 500)
        strings = original.decode("latin1", errors="ignore").lower()
        self.assertNotIn("supplier cost", strings); self.assertNotIn("internal profit", strings)
        self.assertTrue(Path(paths["internal"]).exists())


if __name__ == "__main__":
    unittest.main()
