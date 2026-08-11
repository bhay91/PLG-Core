from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import legacy_app
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.basket.routes import add_manual_item, update_basket_item_form
from plg_core.basket.service import add_item, commit_basket, get_basket, update_item
from plg_core.crm.routes import search_records
from plg_core.dashboard.service import get_follow_up_data
from plg_core.database.migrations import run_migrations
from plg_core.documents import quote_pdf
from plg_core.followups.routes import (
    create_job_follow_up,
    information_received,
    resolve_follow_up,
)
from plg_core.lifecycle import cancel_job, reopen_job


ROOT = Path(__file__).resolve().parents[1]


class WorkflowUIBatch3ATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-batch3a-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.pdf_patch = patch.object(
            quote_pdf, "DOCUMENT_ROOT", self.root / "documents" / "Customers"
        )
        self.db_patch.start()
        self.pdf_patch.start()
        run_migrations()
        self._clear()

    def tearDown(self):
        self.pdf_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def _clear(self):
        with closing(self.connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "job_follow_ups", "quote_documents_manifest",
                "work_revision_attachments", "work_revision_items",
                "work_revision_sources", "work_revisions", "invoice_events",
                "invoice_items", "invoices", "quote_events", "quote_items",
                "quotes", "receiving_event_items", "receiving_events",
                "supplier_order_items", "supplier_orders", "deliveries",
                "part_sources", "job_parts", "basket_activity",
                "basket_attachments", "basket_items", "basket_sources",
                "baskets", "customer_request_attachments", "customer_requests",
                "job_timeline", "audit_logs", "machines", "customers", "jobs",
            ):
                c.execute(f'DELETE FROM "{table}"')
            c.execute("UPDATE internal_part_number_sequence SET last_number=0")
            c.execute(
                "UPDATE pps_number_sequences SET last_number=0 "
                "WHERE entity_type IN ('JOB','QUOTE','REQUEST')"
            )
            c.commit()

    def job(self, *, request=False):
        with closing(self.connection()) as c:
            customer_id = c.execute(
                "INSERT INTO customers(customer_number,name,active) "
                "VALUES ('PPS-C-B3','Batch 3 Customer',1)"
            ).lastrowid
            job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,"
                "manufacturer,machine,pin_serial,status) "
                "VALUES ('PPS-J-0001','2026-08-11',?,'Batch 3 Customer',"
                "'John Deere','350D','PIN-B3','REQUESTED')",
                (customer_id,),
            ).lastrowid
            request_id = None
            if request:
                request_id = c.execute(
                    "INSERT INTO customer_requests(request_number,individual_name,"
                    "request_text,requested_parts,job_id,status,is_archived) "
                    "VALUES ('PPS-R-0001','Batch 3 Customer','Need filters',"
                    "'Fuel Filter Kit',?,'COMPLETED',0)",
                    (job_id,),
                ).lastrowid
            c.commit()
        return int(job_id), int(request_id) if request_id else None

    def ready_part(self, job_id):
        add_item(
            job_id,
            BasketItemCreate(
                requested_description="Fuel Filter Kit",
                quantity=1,
                supplier_name="Test Supplier",
                supplier_unit_cost=100,
                markup_percent=30,
                verification_status="VERIFIED",
                selected=True,
            ),
        )

    def test_cancel_archives_and_reopen_restores_active_visibility(self):
        job_id, _ = self.job()
        cancelled = cancel_job(job_id, "Customer cancelled")
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["is_archived"], 1)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM jobs WHERE id=? AND is_archived=0", (job_id,)
            ).fetchone()[0], 0)
        reopened = reopen_job(job_id, "Customer resumed")
        self.assertNotEqual(reopened["status"], "CANCELLED")
        self.assertEqual(reopened["is_archived"], 0)

    def test_draft_quote_archives_originating_request_without_deleting_it(self):
        job_id, request_id = self.job(request=True)
        self.ready_part(job_id)
        response = legacy_app.generate_quote(job_id)
        self.assertEqual(response.status_code, 303)
        with closing(self.connection()) as c:
            request = c.execute(
                "SELECT * FROM customer_requests WHERE id=?", (request_id,)
            ).fetchone()
            quote = c.execute(
                "SELECT * FROM quotes WHERE job_id=?", (job_id,)
            ).fetchone()
            self.assertEqual(request["is_archived"], 1)
            self.assertEqual(quote["status"], "DRAFT")
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM customer_requests WHERE id=?", (request_id,)
            ).fetchone()[0], 1)
        result = search_records("PPS-R-0001", limit=10)
        self.assertTrue(any(row["record_type"] == "REQUEST" for row in result["items"]))

    def test_customer_information_moves_to_attention_then_leaves_queue(self):
        job_id, _ = self.job()
        create_job_follow_up(
            job_id,
            summary="Photo of headlight connector",
            reason="Confirm halogen versus HID",
            category="CUSTOMER_INFORMATION",
        )
        with closing(self.connection()) as c:
            data = get_follow_up_data(c)
            self.assertEqual(data["summary"]["customer_information"], 1)
            follow_up_id = c.execute("SELECT id FROM job_follow_ups").fetchone()[0]
        information_received(follow_up_id, "Customer sent connector photo")
        with closing(self.connection()) as c:
            data = get_follow_up_data(c)
            self.assertEqual(data["summary"]["customer_information"], 0)
            self.assertEqual(data["summary"]["needs_attention"], 1)
        resolve_follow_up(follow_up_id, "Matched the correct lamp")
        with closing(self.connection()) as c:
            data = get_follow_up_data(c)
            self.assertEqual(data["summary"]["needs_attention"], 0)
            self.assertEqual(c.execute(
                "SELECT status FROM job_follow_ups WHERE id=?", (follow_up_id,)
            ).fetchone()[0], "RESOLVED")

    def test_payment_queue_remains_distinct_from_customer_information(self):
        job_id, _ = self.job()
        with closing(self.connection()) as c:
            quote_id = c.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total) "
                "VALUES ('PPS-Q-0001',?,'2026-08-11','APPROVED',500)",
                (job_id,),
            ).lastrowid
            c.execute(
                "INSERT INTO invoices(invoice_number,job_id,quote_id,invoice_date,status,"
                "customer_total,balance_due) VALUES "
                "('PPS-INV-0001',?,?,'2026-08-11','UNPAID',500,500)",
                (job_id, quote_id),
            )
            c.execute(
                "INSERT INTO job_follow_ups(job_id,category,summary) "
                "VALUES (?,'CUSTOMER_INFORMATION','Need engine serial')",
                (job_id,),
            )
            c.commit()
            data = get_follow_up_data(c)
        self.assertEqual(data["summary"]["payments"], 1)
        self.assertEqual(data["summary"]["customer_information"], 1)
        self.assertEqual(
            {row["category"] for row in data["items"]},
            {"PAYMENT", "CUSTOMER_INFORMATION"},
        )

    def test_manual_part_gets_stable_searchable_internal_identifier(self):
        job_id, _ = self.job()
        self.ready_part(job_id)
        basket = get_basket(job_id)
        item = basket["items"][0]
        self.assertEqual(item["internal_part_number"], "PPS-MAN-000001")
        update_item(
            item["id"],
            BasketItemUpdate(manufacturer_part_number="RE-60021"),
            expected_job_id=job_id,
        )
        self.assertEqual(
            get_basket(job_id)["items"][0]["internal_part_number"],
            "PPS-MAN-000001",
        )
        commit_basket(job_id)
        with closing(self.connection()) as c:
            part = c.execute("SELECT * FROM job_parts WHERE job_id=?", (job_id,)).fetchone()
            self.assertEqual(part["internal_part_number"], "PPS-MAN-000001")
            self.assertEqual(part["oem_part_number"], "")
        result = search_records("PPS-MAN-000001", limit=10)
        self.assertTrue(any(row["record_type"] == "JOB" for row in result["items"]))

    def test_manual_add_and_responsive_save_preserve_part_metadata(self):
        job_id, _ = self.job()
        response = add_manual_item(
            job_id,
            requested_description="Headlight assembly",
            manufacturer_part_number="HL-100",
            quantity=2,
            supplier_name="Lamp Supplier",
            supplier_part_number="LS-9",
            supplier_unit_cost=80,
            source_type="AFTERMARKET",
        )
        self.assertEqual(response.status_code, 303)
        item = get_basket(job_id)["items"][0]
        update_basket_item_form(
            job_id,
            item["id"],
            quantity=3,
            supplier_unit_cost=85,
            markup_percent=30,
            customer_unit_price_override="125.00",
            manufacturer_part_number="HL-100",
            alternate_part_number="HL-101",
            supplier_part_number="LS-9",
            part_status="QUOTED",
            verification_status="VERIFIED",
            verification_note="Catalog match retained",
            confidence=0.95,
            expected_revision_id=None,
            expected_version=None,
        )
        saved = get_basket(job_id)["items"][0]
        self.assertEqual(saved["requested_description"], "Headlight assembly")
        self.assertEqual(saved["supplier_name"], "Lamp Supplier")
        self.assertEqual(saved["quantity"], 3)
        self.assertEqual(saved["manufacturer_part_number"], "HL-100")
        self.assertEqual(saved["supplier_part_number"], "LS-9")
        self.assertEqual(saved["verification_status"], "VERIFIED")
        self.assertEqual(saved["verification_note"], "Catalog match retained")
        self.assertEqual(saved["customer_unit_price_override"], 125.0)

    def test_verification_source_start_accepts_current_revision_token(self):
        job_id, _ = self.job()
        self.ready_part(job_id)
        with closing(self.connection()) as c:
            c.execute(
                "INSERT OR REPLACE INTO connector_profiles("
                "connector_key,display_name,is_enabled,launch_url) "
                "VALUES ('batch3_catalog','Batch 3 Catalog',1,"
                "'https://example.invalid/catalog')"
            )
            c.commit()
        revision = get_basket(job_id)["work_revision"]
        response = legacy_app.start_source_import(
            job_id,
            source_key="batch3_catalog",
            expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        self.assertEqual(response.status_code, 303)
        with closing(self.connection()) as c:
            active = c.execute("SELECT * FROM active_source_import WHERE id=1").fetchone()
            self.assertEqual(active["job_id"], job_id)
            self.assertEqual(active["source_key"], "batch3_catalog")


if __name__ == "__main__":
    unittest.main()
