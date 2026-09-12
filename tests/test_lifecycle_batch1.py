from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.documents import invoice_pdf, quote_pdf
from plg_core.database.migrations import run_migrations
from plg_core.database.migrations import (
    _migration_0026_lifecycle_safety,
    _migration_0027_request_cancellation_flag,
    _migration_0028_one_active_quote_per_job,
)
from plg_core.jobs.engine import JobEngine
from plg_core.lifecycle import (
    archive_job,
    cancel_job,
    delete_job_safely,
    ensure_job_pre_document_work,
    reopen_job,
    restore_job,
    transition_quote,
)
from plg_core.pricing import pricing_assessment
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.basket.service import add_item, get_basket, update_item


ROOT = Path(__file__).resolve().parents[1]


class LifecycleBatch1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-lifecycle-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        document_root = Path(self.temp.name) / "documents" / "Customers"
        self.quote_pdf_patch = patch.object(quote_pdf, "DOCUMENT_ROOT", document_root)
        self.invoice_pdf_patch = patch.object(invoice_pdf, "DOCUMENT_ROOT", document_root)
        self.db_patch.start()
        self.quote_pdf_patch.start()
        self.invoice_pdf_patch.start()
        run_migrations()
        self._clear_business_data()

    def tearDown(self):
        self.invoice_pdf_patch.stop()
        self.quote_pdf_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def _clear_business_data(self):
        with closing(self.connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "quote_documents_manifest", "work_revision_attachments",
                "work_revision_items", "work_revision_sources", "work_revisions",
                "delivery_items", "deliveries", "receiving_event_items", "receiving_events",
                "supplier_order_items", "supplier_orders", "invoice_documents_manifest",
                "invoice_events", "invoice_items",
                "invoices", "quote_events", "quote_items", "quotes", "customer_transactions",
                "part_sources", "job_parts", "basket_activity", "basket_attachments",
                "basket_items", "basket_sources", "baskets", "customer_request_attachments",
                "customer_requests", "job_timeline", "audit_logs", "deletion_tombstones",
                "machines", "customers", "jobs",
            ):
                c.execute(f"DELETE FROM {table}")
            c.commit()

    def customer(self, name="Customer A"):
        with closing(self.connection()) as c:
            cur = c.execute("INSERT INTO customers (customer_number,name,active) VALUES (?,?,1)",
                            (f"TEST-C-{name[-1]}", name))
            c.commit()
            return cur.lastrowid

    def machine(self, customer_id, serial="SERIAL-A", model="Model A"):
        with closing(self.connection()) as c:
            cur = c.execute(
                "INSERT INTO machines (customer_id,machine_number,name,manufacturer,model,vin_pin_serial) "
                "VALUES (?,?,?,?,?,?)", (customer_id, f"TEST-M-{serial}", model, "CAT", model, serial)
            )
            c.commit()
            return cur.lastrowid

    def job(self, customer_id=None, machine_id=None, status="REQUESTED"):
        with closing(self.connection()) as c:
            number = legacy_app.next_job_number(c)
            customer = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone() if customer_id else None
            machine = c.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone() if machine_id else None
            cur = c.execute(
                "INSERT INTO jobs (job_number,created_date,customer_id,machine_id,customer,company,"
                "phone,email,address,manufacturer,machine,pin_serial,status,notes) "
                "VALUES (?,'2026-08-11',?,?,?,?,?,?,?,?,?,?,?,?)",
                (number, customer_id, machine_id, customer["name"] if customer else "Test",
                 "", "", "", "", machine["manufacturer"] if machine else "",
                 (machine["model"] or machine["name"]) if machine else "",
                 machine["vin_pin_serial"] if machine else "", status, ""),
            )
            c.commit()
            return cur.lastrowid

    def part_and_quote(self, job_id, quote_status="DRAFT"):
        with closing(self.connection()) as c:
            part = c.execute(
                "INSERT INTO job_parts (job_id,requested_description,quantity,verification_status,customer_unit_price) "
                "VALUES (?,'Filter',1,'VERIFIED',130)", (job_id,)
            ).lastrowid
            source = c.execute(
                "INSERT INTO part_sources (part_id,supplier_name,supplier_cost,selected_for_quote) "
                "VALUES (?,'Supplier',100,1)", (part,)
            ).lastrowid
            quote = c.execute(
                "INSERT INTO quotes (quote_number,job_id,quote_date,status,parts_subtotal,customer_total,"
                "supplier_total,profit_total) VALUES (?,?, '2026-08-11',?,130,130,100,30)",
                (legacy_app.next_quote_number(c), job_id, quote_status),
            ).lastrowid
            c.execute(
                "INSERT INTO quote_items (quote_id,part_id,source_id,quantity,description,supplier_name,"
                "source_type,supplier_unit_cost,customer_unit_price,supplier_line_total,customer_line_total,line_profit) "
                "VALUES (?,?,?,1,'Filter','Supplier','AFTERMARKET',100,130,100,130,30)",
                (quote, part, source),
            )
            c.commit()
            return part, source, quote

    def test_job_engine_approved_quote_uses_real_conversion_route(self):
        info = JobEngine.evaluate(
            {"id": 7, "customer_id": 1, "machine_id": 1, "status": "CONFIRMED"},
            selected_items=1, basket_status="COMMITTED",
            quote={"id": 11, "status": "APPROVED"}, invoice=None,
        )
        self.assertEqual(info.workflow_stage, "READY_TO_INVOICE")
        self.assertEqual(info.workflow_label, "Ready to Invoice")
        self.assertEqual(info.next_action, "Create Invoice for Payment")
        self.assertEqual(info.action_url, "/quotes/11/convert-to-invoice")
        self.assertEqual(info.action_method, "POST")
        existing = JobEngine.evaluate(
            {"id": 7, "customer_id": 1, "machine_id": 1}, selected_items=1,
            basket_status="COMMITTED", quote={"id": 11, "status": "APPROVED"},
            invoice={"id": 4, "status": "UNPAID"},
        )
        self.assertEqual(existing.workflow_stage, "WAITING_PAYMENT")
        self.assertEqual(existing.next_action, "Waiting for Payment")
        self.assertEqual(existing.action_url, "/invoices/4/documents")

    def test_quote_transition_matrix_and_downstream_lock(self):
        cid = self.customer(); mid = self.machine(cid); jid = self.job(cid, mid)
        _, _, qid = self.part_and_quote(jid)
        transition_quote(qid, "SENT")
        transition_quote(qid, "APPROVED")
        with self.assertRaises(HTTPException): transition_quote(qid, "REJECTED")
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT status FROM quotes WHERE id=?", (qid,)).fetchone()[0], "APPROVED")
            c.execute("INSERT INTO invoices (invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) VALUES ('TEST-I',?,?,'2026-08-11','UNPAID',130,130)", (qid, jid))
            c.commit()
        with self.assertRaises(HTTPException): transition_quote(qid, "SENT")

    def test_revision_required_is_terminal_until_batch2(self):
        cid = self.customer(); jid = self.job(cid)
        _, _, qid = self.part_and_quote(jid, "SENT")
        transition_quote(qid, "REVISION_REQUIRED")
        with self.assertRaisesRegex(HTTPException, "Batch 2"):
            transition_quote(qid, "DRAFT")

    def test_legacy_part_and_source_deletion_blocked_after_quote(self):
        cid = self.customer(); jid = self.job(cid)
        part, source, _ = self.part_and_quote(jid)
        with self.assertRaises(HTTPException): legacy_app.delete_job_part(part)
        with self.assertRaises(HTTPException): legacy_app.delete_part_source(part, source)
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM job_parts WHERE id=?", (part,)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM part_sources WHERE id=?", (source,)).fetchone())

    def test_pre_document_part_can_be_deleted_with_audit(self):
        cid = self.customer(); jid = self.job(cid)
        with closing(self.connection()) as c:
            part = c.execute("INSERT INTO job_parts (job_id,requested_description) VALUES (?,'Wrong part')", (jid,)).lastrowid
            c.commit()
        legacy_app.delete_job_part(part)
        with closing(self.connection()) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM job_parts WHERE id=?", (part,)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM audit_logs WHERE action='JOB_PART_DELETED'").fetchone())

    def test_job_relationship_correction_and_ownership_conflict(self):
        a = self.customer("Customer A"); b = self.customer("Customer B")
        ma = self.machine(a, "A"); mb = self.machine(b, "B")
        jid = self.job(a, ma)
        legacy_app.update_job(jid, customer_id=b, machine_id=mb, notes="corrected")
        with closing(self.connection()) as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            self.assertEqual((row["customer_id"], row["machine_id"], row["customer"]), (b, mb, "Customer B"))
        with self.assertRaises(HTTPException):
            legacy_app.update_job(jid, customer_id=a, machine_id=mb, notes="bad owner")

    def test_historical_job_identity_cannot_be_edited(self):
        a = self.customer("Customer A"); b = self.customer("Customer B")
        ma = self.machine(a, "A"); mb = self.machine(b, "B")
        jid = self.job(a, ma); self.part_and_quote(jid)
        with self.assertRaises(HTTPException):
            legacy_app.update_job(jid, customer_id=b, machine_id=mb, notes="attempt")
        with closing(self.connection()) as c:
            row = c.execute("SELECT customer_id,machine_id FROM jobs WHERE id=?", (jid,)).fetchone()
            self.assertEqual(tuple(row), (a, ma))

    def test_customer_master_edit_syncs_active_but_not_historical_job(self):
        cid = self.customer(); mid = self.machine(cid)
        active = self.job(cid, mid); historical = self.job(cid, mid)
        self.part_and_quote(historical)
        legacy_app.update_customer(
            cid, "Renamed Customer", "New Company", "555-0100",
            "new@example.com", "New Address",
        )
        with closing(self.connection()) as c:
            master = c.execute("SELECT name FROM customers WHERE id=?", (cid,)).fetchone()[0]
            active_name = c.execute("SELECT customer FROM jobs WHERE id=?", (active,)).fetchone()[0]
            historical_row = c.execute(
                "SELECT customer,company,phone,email,address FROM jobs WHERE id=?", (historical,)
            ).fetchone()
            self.assertEqual(master, "Renamed Customer")
            self.assertEqual(active_name, "Renamed Customer")
            self.assertEqual(tuple(historical_row), ("Customer A", "", "", "", ""))

    def test_master_edits_do_not_refresh_cancelled_job_snapshots(self):
        cid = self.customer(); mid = self.machine(cid); jid = self.job(cid, mid)
        cancel_job(jid, "not proceeding")
        legacy_app.update_customer(cid, "Changed Customer")
        from plg_core.machines.routes import update_machine
        update_machine(mid, cid, name="Changed Machine", manufacturer="Changed", model="Changed",
                       vin_pin_serial="CHANGED")
        with closing(self.connection()) as c:
            row = c.execute(
                "SELECT customer,manufacturer,machine,pin_serial FROM jobs WHERE id=?", (jid,)
            ).fetchone()
            self.assertEqual(tuple(row), ("Customer A", "CAT", "Model A", "SERIAL-A"))

    def test_notes_only_historical_edit_preserves_all_identity_snapshots(self):
        cid = self.customer(); mid = self.machine(cid)
        jid = self.job(cid, mid)
        with closing(self.connection()) as c:
            original = tuple(c.execute(
                "SELECT customer,company,phone,email,address,manufacturer,machine,pin_serial "
                "FROM jobs WHERE id=?", (jid,),
            ).fetchone())
        self.part_and_quote(jid)
        with closing(self.connection()) as c:
            c.execute("UPDATE customers SET name='Changed Master',company='Changed Co',address='Changed' WHERE id=?", (cid,))
            c.execute("UPDATE machines SET manufacturer='Changed Make',model='Changed Model',vin_pin_serial='Changed Serial' WHERE id=?", (mid,))
            c.commit()
        legacy_app.update_job(jid, customer_id=cid, machine_id=mid, notes="Historical note only")
        with closing(self.connection()) as c:
            row = c.execute(
                "SELECT customer,company,phone,email,address,manufacturer,machine,pin_serial,notes "
                "FROM jobs WHERE id=?", (jid,),
            ).fetchone()
            self.assertEqual(tuple(row[:8]), original)
            self.assertEqual(row["notes"], "Historical note only")

    def test_machine_edit_syncs_active_but_not_historical_job(self):
        cid = self.customer(); mid = self.machine(cid)
        active = self.job(cid, mid); historical = self.job(cid, mid); self.part_and_quote(historical)
        from plg_core.machines.routes import update_machine
        update_machine(mid, cid, name="New", manufacturer="CAT", model="New Model",
                       vin_pin_serial="NEW-SERIAL")
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT pin_serial FROM jobs WHERE id=?", (active,)).fetchone()[0], "NEW-SERIAL")
            self.assertEqual(c.execute("SELECT pin_serial FROM jobs WHERE id=?", (historical,)).fetchone()[0], "SERIAL-A")

    def test_cancel_archive_restore_reopen_and_history_block(self):
        cid = self.customer(); jid = self.job(cid)
        cancel_job(jid, "duplicate entry")
        archive_job(jid); restore_job(jid)
        reopen_job(jid, "customer called back")
        cancel_job(jid, "stopped again")
        self.part_and_quote(jid, "REJECTED")
        with self.assertRaisesRegex(HTTPException, "Batch 2"):
            reopen_job(jid, "needs revision")
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0], "CANCELLED")
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM audit_logs WHERE entity_type='JOB'").fetchone()[0], 4)

    def test_safe_delete_tombstone_and_number_nonreuse(self):
        cid = self.customer(); jid = self.job(cid)
        with closing(self.connection()) as c: number = c.execute("SELECT job_number FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        delete_job_safely(jid, "accidental click", number)
        new_job = self.job(cid)
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM deletion_tombstones WHERE entity_number=?", (number,)).fetchone())
            new_number = c.execute("SELECT job_number FROM jobs WHERE id=?", (new_job,)).fetchone()[0]
            self.assertNotEqual(number, new_number)

    def test_safe_delete_blocked_by_meaningful_basket(self):
        cid = self.customer(); jid = self.job(cid)
        with closing(self.connection()) as c:
            basket = c.execute("INSERT INTO baskets (job_id) VALUES (?)", (jid,)).lastrowid
            c.execute("INSERT INTO basket_items (basket_id,requested_description) VALUES (?,'Research')", (basket,))
            number = c.execute("SELECT job_number FROM jobs WHERE id=?", (jid,)).fetchone()[0]
            c.commit()
        with self.assertRaises(HTTPException): delete_job_safely(jid, "cleanup", number)

    def test_actual_markup_override_and_zero_cost(self):
        self.assertEqual(pricing_assessment(100, None, 130)["actual_markup_percent"], 30)
        self.assertEqual(pricing_assessment(100, None, 175)["actual_markup_percent"], 75)
        self.assertEqual(pricing_assessment(100, None, 80)["actual_markup_percent"], -20)
        self.assertEqual(pricing_assessment(100, None, 0)["actual_markup_percent"], -100)
        self.assertIsNone(pricing_assessment(0, None, 10)["actual_markup_percent"])
        self.assertEqual(pricing_assessment(100, None, None)["current_unit_price"], 130)

    def test_customer_override_survives_cost_and_quantity_changes_and_blank_restore(self):
        cid = self.customer(); jid = self.job(cid)
        result = add_item(jid, BasketItemCreate(
            requested_description="Pump", quantity=1, supplier_unit_cost=100,
            customer_unit_price_override=175,
        ))
        item_id = result["items"][0]["id"]
        update_item(item_id, BasketItemUpdate(supplier_unit_cost=140, quantity=3), jid)
        basket = get_basket(jid)
        item = basket["items"][0]
        self.assertEqual(item["customer_unit_price_override"], 175)
        self.assertEqual(basket["totals"]["customer_parts_total"], 525)
        update_item(item_id, BasketItemUpdate(customer_unit_price_override=None), jid)
        restored = get_basket(jid)
        self.assertIsNone(restored["items"][0]["customer_unit_price_override"])
        self.assertNotEqual(restored["totals"]["customer_parts_total"], 525)

    def test_invoice_conversion_is_idempotent_and_preserves_snapshot(self):
        cid = self.customer(); mid = self.machine(cid); jid = self.job(cid, mid)
        _, _, qid = self.part_and_quote(jid, "APPROVED")
        with patch("plg_core.documents.integrity.issue_invoice_documents", lambda *_args, **_kwargs: None):
            legacy_app.convert_quote_to_invoice(qid)
            legacy_app.convert_quote_to_invoice(qid)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices WHERE quote_id=?", (qid,)).fetchone()[0], 1)
            row = c.execute("SELECT * FROM invoice_items").fetchone()
            self.assertEqual((row["supplier_unit_cost"], row["customer_unit_price"], row["line_profit"]), (100, 130, 30))

    def test_concurrent_quote_generation_creates_one_active_quote_and_consumes_one_number(self):
        cid = self.customer(); jid = self.job(cid)
        add_item(jid, BasketItemCreate(
            requested_description="Concurrent filter", quantity=1,
            supplier_name="Supplier", supplier_unit_cost=100, selected=True,
            verification_status="VERIFIED",
        ))
        with closing(self.connection()) as c:
            before = c.execute(
                "SELECT last_number FROM pps_number_sequences WHERE entity_type='QUOTE'"
            ).fetchone()[0]
        from plg_core.basket import service as basket_service
        original_commit = basket_service.commit_basket
        barrier = Barrier(2)

        def synchronized_commit(job_id):
            result = original_commit(job_id)
            barrier.wait(timeout=5)
            return result

        with patch.object(basket_service, "commit_basket", synchronized_commit), \
             patch.object(legacy_app, "generate_quote_pdfs", lambda *_: None):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: legacy_app.generate_quote(jid), range(2)))

        with closing(self.connection()) as c:
            quotes = c.execute(
                "SELECT id,quote_number FROM quotes WHERE job_id=? AND is_archived=0", (jid,)
            ).fetchall()
            after = c.execute(
                "SELECT last_number FROM pps_number_sequences WHERE entity_type='QUOTE'"
            ).fetchone()[0]
            self.assertEqual(len(quotes), 1)
            self.assertEqual(after, before + 1)
            self.assertEqual(
                {response.headers["location"] for response in responses},
                {f"/quotes/{quotes[0]['id']}/documents"},
            )

    def test_concurrent_invoice_conversion_is_cleanly_idempotent(self):
        cid = self.customer(); mid = self.machine(cid); jid = self.job(cid, mid)
        _, _, qid = self.part_and_quote(jid, "APPROVED")
        barrier = Barrier(2)

        def convert(_):
            barrier.wait(timeout=5)
            return legacy_app.convert_quote_to_invoice(qid)

        with patch("plg_core.documents.integrity.issue_invoice_documents", lambda *_args, **_kwargs: None):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(convert, range(2)))
        with closing(self.connection()) as c:
            invoices = c.execute("SELECT id FROM invoices WHERE quote_id=?", (qid,)).fetchall()
            self.assertEqual(len(invoices), 1)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM customer_transactions WHERE invoice_id=? AND transaction_type='INVOICE'",
                (invoices[0]["id"],),
            ).fetchone()[0], 1)
            self.assertEqual(
                {response.headers["location"] for response in responses},
                {f"/invoices/{invoices[0]['id']}/documents"},
            )

    def test_nonapproved_and_forced_invoice_conversion_are_blocked(self):
        cid = self.customer(); jid = self.job(cid)
        _, _, qid = self.part_and_quote(jid, "SENT")
        with self.assertRaises(HTTPException): legacy_app.convert_quote_to_invoice(qid)
        from plg_core.sales.routes import convert_quote
        from plg_core.sales.models import ConversionRequest
        with self.assertRaises(HTTPException): convert_quote(qid, ConversionRequest(force=True))
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 0)

    def test_cancelled_job_blocks_basket_and_quote_work(self):
        cid = self.customer(); jid = self.job(cid)
        cancel_job(jid, "not proceeding")
        with self.assertRaises(HTTPException):
            add_item(jid, BasketItemCreate(requested_description="Blocked"))
        with self.assertRaises(HTTPException): legacy_app.generate_quote(jid)

    def test_cancellation_is_blocked_by_each_major_obligation(self):
        cid = self.customer()

        approved_job = self.job(cid)
        self.part_and_quote(approved_job, "APPROVED")

        invoice_job = self.job(cid)
        _, _, invoice_quote = self.part_and_quote(invoice_job, "APPROVED")
        with closing(self.connection()) as c:
            c.execute(
                "INSERT INTO invoices (invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) "
                "VALUES ('TEST-CANCEL-I',?,?,'2026-08-11','UNPAID',10,10)",
                (invoice_quote, invoice_job),
            )
            c.commit()

        order_job = self.job(cid)
        receipt_job = self.job(cid)
        delivery_job = self.job(cid)
        with closing(self.connection()) as c:
            c.execute("INSERT INTO supplier_orders (job_id,supplier_name) VALUES (?,'Supplier')", (order_job,))
            receipt_order = c.execute(
                "INSERT INTO supplier_orders (job_id,supplier_name) VALUES (?,'Supplier')", (receipt_job,)
            ).lastrowid
            c.execute("INSERT INTO receiving_events (order_id,receipt_number) VALUES (?,'TEST-RCV')", (receipt_order,))
            c.execute("INSERT INTO deliveries (job_id,status) VALUES (?,'READY')", (delivery_job,))
            c.commit()

        for label, job_id in {
            "approved quote": approved_job,
            "invoice": invoice_job,
            "supplier order": order_job,
            "receipt": receipt_job,
            "delivery": delivery_job,
        }.items():
            with self.subTest(label=label), self.assertRaises(HTTPException):
                cancel_job(job_id, "must be blocked")
            with closing(self.connection()) as c:
                self.assertNotEqual(
                    c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0],
                    "CANCELLED",
                )

    def test_safe_delete_is_blocked_by_major_durable_history(self):
        cid = self.customer()
        jobs = {}
        jobs["quote"] = self.job(cid); self.part_and_quote(jobs["quote"])
        jobs["payment"] = self.job(cid)
        jobs["order"] = self.job(cid)
        jobs["delivery"] = self.job(cid)
        with closing(self.connection()) as c:
            c.execute(
                "INSERT INTO customer_transactions (customer_id,transaction_date,transaction_type,amount,job_id) "
                "VALUES (?,'2026-08-11','PAYMENT',10,?)", (cid, jobs["payment"]),
            )
            c.execute("INSERT INTO supplier_orders (job_id,supplier_name) VALUES (?,'Supplier')", (jobs["order"],))
            c.execute("INSERT INTO deliveries (job_id,status) VALUES (?,'READY')", (jobs["delivery"],))
            numbers = {name: c.execute("SELECT job_number FROM jobs WHERE id=?", (jid,)).fetchone()[0]
                       for name, jid in jobs.items()}
            c.commit()
        for name, jid in jobs.items():
            with self.subTest(history=name), self.assertRaises(HTTPException):
                delete_job_safely(jid, "accidental", numbers[name])
            with closing(self.connection()) as c:
                self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone())

    def test_lifecycle_reasons_and_delete_confirmation_are_required(self):
        cid = self.customer(); jid = self.job(cid)
        with closing(self.connection()) as c:
            number = c.execute("SELECT job_number FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        with self.assertRaises(HTTPException): cancel_job(jid, "")
        with self.assertRaises(HTTPException): delete_job_safely(jid, "", number)
        with self.assertRaises(HTTPException): delete_job_safely(jid, "duplicate", "WRONG")
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone())

    def test_legacy_add_and_import_work_are_locked_after_quote(self):
        cid = self.customer(); jid = self.job(cid)
        self.part_and_quote(jid, "DRAFT")
        with closing(self.connection()) as c:
            with self.assertRaises(HTTPException):
                ensure_job_pre_document_work(c, jid, "add parts")
        before = None
        with closing(self.connection()) as c:
            before = c.execute("SELECT COUNT(*) FROM job_parts WHERE job_id=?", (jid,)).fetchone()[0]
        with self.assertRaises(HTTPException):
            legacy_app.add_job_part(jid, "Late part", 1)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM job_parts WHERE job_id=?", (jid,)).fetchone()[0], before)

    def test_revenue_adjustment_route_cannot_bypass_document_lock(self):
        from plg_core.basket.routes import update_revenue_adjustments
        cid = self.customer(); jid = self.job(cid)
        self.part_and_quote(jid, "DRAFT")
        with self.assertRaises(HTTPException):
            update_revenue_adjustments(jid, "75", "Service", "0", "")
        with closing(self.connection()) as c:
            row = c.execute("SELECT service_charge,sourcing_fee FROM jobs WHERE id=?", (jid,)).fetchone()
            self.assertEqual(tuple(row), (0, 0))

    def test_arbitrary_job_status_endpoint_is_blocked(self):
        cid = self.customer(); jid = self.job(cid)
        with self.assertRaises(HTTPException): legacy_app.update_job_status(jid, "DELIVERED")
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()[0], "REQUESTED")

    def test_request_cancel_and_safe_delete_tombstone(self):
        from plg_core.requests.routes import cancel_request, delete_request
        with closing(self.connection()) as c:
            rid = c.execute(
                "INSERT INTO customer_requests (request_number,request_text,status) VALUES ('PPS-R-9001','','NEW')"
            ).lastrowid
            c.commit()
        cancel_request(rid, "duplicate request")
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT is_cancelled FROM customer_requests WHERE id=?", (rid,)).fetchone()[0], 1)
            c.execute("UPDATE customer_requests SET is_cancelled=0 WHERE id=?", (rid,)); c.commit()
        delete_request(rid, "accidental", "PPS-R-9001")
        with closing(self.connection()) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM customer_requests WHERE id=?", (rid,)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM deletion_tombstones WHERE entity_number='PPS-R-9001'").fetchone())

    def test_meaningful_request_delete_is_blocked(self):
        from plg_core.requests.routes import delete_request
        with closing(self.connection()) as c:
            rid = c.execute(
                "INSERT INTO customer_requests (request_number,request_text,status) VALUES ('PPS-R-9002','Need a pump','NEW')"
            ).lastrowid
            c.commit()
        with self.assertRaises(HTTPException): delete_request(rid, "cleanup", "PPS-R-9002")
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM customer_requests WHERE id=?", (rid,)).fetchone())

    def test_request_number_is_not_reused_and_cancelled_request_cannot_advance(self):
        from plg_core.requests.routes import cancel_request, create_job_from_request, delete_request, update_status
        with closing(self.connection()) as c:
            first_number = legacy_app.next_request_number(c)
            rid = c.execute(
                "INSERT INTO customer_requests (request_number,request_text,status) VALUES (?,'','NEW')",
                (first_number,),
            ).lastrowid
            c.commit()
        delete_request(rid, "accidental", first_number)
        with closing(self.connection()) as c:
            second_number = legacy_app.next_request_number(c)
            cancelled_id = c.execute(
                "INSERT INTO customer_requests (request_number,request_text,status) VALUES (?,'','NEW')",
                (second_number,),
            ).lastrowid
            c.commit()
        self.assertNotEqual(first_number, second_number)
        cancel_request(cancelled_id, "not proceeding")
        with self.assertRaises(HTTPException): create_job_from_request(cancelled_id)
        with self.assertRaises(HTTPException): update_status(cancelled_id, "READY")
        with closing(self.connection()) as c:
            row = c.execute("SELECT status,job_id FROM customer_requests WHERE id=?", (cancelled_id,)).fetchone()
            self.assertEqual(tuple(row), ("NEW", None))

    def test_job_archive_filters_active_and_archived_views(self):
        cid = self.customer(); active = self.job(cid); archived = self.job(cid)
        archive_job(archived)
        request = Request({"type": "http", "method": "GET", "path": "/jobs", "headers": []})

        def context_response(*, request, name, context):
            return context

        with patch.object(legacy_app.templates, "TemplateResponse", side_effect=context_response):
            active_context = legacy_app.list_jobs(request, "active")
            archived_context = legacy_app.list_jobs(request, "archived")
        self.assertIn(active, {row["id"] for row in active_context["jobs"]})
        self.assertNotIn(archived, {row["id"] for row in active_context["jobs"]})
        self.assertEqual({row["id"] for row in archived_context["jobs"]}, {archived})

    def test_jobs_directory_uses_durable_jcc_stage_and_action(self):
        cid = self.customer()
        job_id = self.job(cid)
        request = Request({"type": "http", "method": "GET", "path": "/jobs", "headers": []})
        workflow = {
            "stage": "Ready to Order",
            "next_action": "Mark Order Placed",
            "next_url": f"/jobs/{job_id}/fulfillment/order",
            "action_method": "POST",
        }

        def context_response(*, request, name, context):
            return context

        with patch(
            "plg_core.jobs.service.get_job_operational_snapshot",
            return_value={"workflow": workflow},
        ), patch.object(
            legacy_app.templates,
            "TemplateResponse",
            side_effect=context_response,
        ):
            context = legacy_app.list_jobs(request, "active")

        job = next(row for row in context["jobs"] if row["id"] == job_id)
        self.assertEqual(job["intelligence"]["workflow_label"], "Ready to Order")
        self.assertEqual(job["intelligence"]["next_action"], "Mark Order Placed")
        self.assertEqual(job["intelligence"]["action_method"], "POST")

    def test_legacy_job_detail_uses_durable_state_and_suppresses_stale_quote_action(self):
        cid = self.customer()
        job_id = self.job(cid)
        self.part_and_quote(job_id, "CONVERTED")
        request = Request({"type": "http", "method": "GET", "path": f"/jobs/{job_id}", "headers": []})
        workflow = {
            "stage": "Ready to Order",
            "next_action": "Mark Order Placed",
            "next_url": f"/jobs/{job_id}/fulfillment/order",
            "action_method": "POST",
        }

        class CapturedResponse:
            def __init__(self, context):
                self.context = context

            def set_cookie(self, *args, **kwargs):
                return None

        def context_response(*, request, name, context):
            return CapturedResponse(context)

        with patch(
            "plg_core.jobs.service.get_job_operational_snapshot",
            return_value={"workflow": workflow},
        ), patch.object(
            legacy_app.templates,
            "TemplateResponse",
            side_effect=context_response,
        ):
            response = legacy_app.job_detail(request, job_id)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/jobs/{job_id}/basket?view=advanced")

        template = (ROOT / "templates" / "job_detail.html").read_text()
        self.assertIn("{{ operational_snapshot.workflow.stage }}", template)
        self.assertIn('{% if operational_snapshot.workflow.action_method == "POST" %}', template)
        self.assertIn('method="post" action="{{ operational_snapshot.workflow.next_url }}"', template)
        self.assertIn('name="csrf_token" value="{{ csrf_token }}"', template)
        self.assertIn("{% if quote_is_next_action %}", template)

    def test_startup_defaults_do_not_advance_internal_sequences(self):
        legacy_app.initialize_database()
        with closing(self.connection()) as c:
            before = dict(c.execute(
                "SELECT name,seq FROM sqlite_sequence WHERE name IN ('connector_profiles','suppliers')"
            ).fetchall())
        legacy_app.initialize_database()
        with closing(self.connection()) as c:
            after = dict(c.execute(
                "SELECT name,seq FROM sqlite_sequence WHERE name IN ('connector_profiles','suppliers')"
            ).fetchall())
        self.assertEqual(after, before)

    def test_startup_does_not_relink_historical_job_after_customer_master_rename(self):
        cid = self.customer(); mid = self.machine(cid); jid = self.job(cid, mid)
        self.part_and_quote(jid)
        with closing(self.connection()) as c:
            original_snapshot = c.execute("SELECT customer FROM jobs WHERE id=?", (jid,)).fetchone()[0]
            customer_count = c.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            c.execute("UPDATE customers SET name='Renamed Master' WHERE id=?", (cid,))
            c.commit()
        legacy_app.initialize_database()
        with closing(self.connection()) as c:
            row = c.execute("SELECT customer_id,customer FROM jobs WHERE id=?", (jid,)).fetchone()
            self.assertEqual(tuple(row), (cid, original_snapshot))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM customers").fetchone()[0], customer_count)

    def test_lifecycle_migrations_are_idempotent(self):
        with closing(self.connection()) as c:
            before_columns = {
                table: tuple(row[1] for row in c.execute(f"PRAGMA table_info({table})"))
                for table in ("jobs", "customer_requests", "deletion_tombstones")
            }
            for _ in range(2):
                _migration_0026_lifecycle_safety(c)
                _migration_0027_request_cancellation_flag(c)
                _migration_0028_one_active_quote_per_job(c)
            after_columns = {
                table: tuple(row[1] for row in c.execute(f"PRAGMA table_info({table})"))
                for table in ("jobs", "customer_requests", "deletion_tombstones")
            }
            self.assertEqual(after_columns, before_columns)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
                "AND name='uq_quotes_one_active_per_job'"
            ).fetchone()[0], 1)

    def test_customer_quote_and_invoice_pdfs_hide_internal_pricing(self):
        import plg_core.documents.quote_pdf as quote_pdf
        import plg_core.documents.invoice_pdf as invoice_pdf
        root = Path(self.temp.name) / "documents"
        quote = {
            "quote_number": "TEST-Q-PDF", "quote_date": "2026-08-11", "customer": "PDF Customer",
            "job_number": "TEST-J", "customer_total": 150, "parts_subtotal": 150,
            "supplier_total": 73.21, "profit_total": 76.79, "shipping_total": 0,
            "service_charge": 0, "sourcing_fee": 0,
        }
        invoice = dict(quote, invoice_number="TEST-I-PDF", invoice_date="2026-08-11",
                       status="UNPAID", balance_due=150, credit_applied=0)
        item = {
            "description": "Filter", "quantity": 1, "customer_unit_price": 150,
            "customer_line_total": 150, "supplier_unit_cost": 73.21,
            "supplier_line_total": 73.21, "line_profit": 76.79,
            "supplier_name": "CONFIDENTIAL SUPPLIER", "supplier_part_number": "SECRET-PART",
        }
        with patch.object(quote_pdf, "DOCUMENT_ROOT", root), patch.object(invoice_pdf, "DOCUMENT_ROOT", root):
            quote_paths = quote_pdf.generate_quote_pdfs(quote, [item])
            invoice_paths = invoice_pdf.generate_invoice_pdfs(invoice, [item])
        for path in (quote_paths["customer"], invoice_paths["customer"]):
            result = subprocess.run(["pdftotext", path, "-"], check=True, capture_output=True, text=True)
            text = result.stdout.upper()
            self.assertNotIn("$73.21", text)
            self.assertNotIn("$76.79", text)
            self.assertNotIn("CONFIDENTIAL SUPPLIER", text)
            self.assertNotIn("INTERNAL COST", text)
            self.assertNotIn("MARKUP", text)
            self.assertNotIn("PROFIT", text)


if __name__ == "__main__":
    unittest.main()
