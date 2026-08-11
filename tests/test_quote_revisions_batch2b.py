from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from pathlib import Path
from threading import Barrier
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.basket.routes import update_revenue_adjustments
from plg_core.basket.service import add_item, commit_basket, get_basket, update_item
from plg_core.database.migrations import run_migrations
from plg_core.documents import quote_pdf
from plg_core.revisions import (
    cancel_quote_revision,
    commit_work_revision,
    generate_quote_from_revision,
    get_revision_context,
    reopen_job_for_revision,
    start_quote_revision,
)


ROOT = Path(__file__).resolve().parents[1]


class QuoteRevisionBatch2BTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-batch2b-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        self.document_root = self.root / "documents" / "Customers"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.pdf_patch = patch.object(
            quote_pdf, "DOCUMENT_ROOT", self.document_root
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
                "quote_documents_manifest", "work_revision_attachments",
                "work_revision_items", "work_revision_sources", "work_revisions",
                "invoice_events", "invoice_items", "invoices", "quote_events",
                "quote_items", "quotes", "part_sources", "job_parts",
                "basket_activity", "basket_attachments", "basket_items",
                "basket_sources", "baskets", "job_timeline", "audit_logs",
                "machines", "customers", "jobs",
            ):
                c.execute(f'DELETE FROM "{table}"')
            c.execute(
                "UPDATE pps_number_sequences SET last_number=0 "
                "WHERE entity_type IN ('JOB','QUOTE')"
            )
            c.commit()

    def job(self):
        with closing(self.connection()) as c:
            customer_id = c.execute(
                "INSERT INTO customers (customer_number,name,active) "
                "VALUES ('B2-C','Batch 2 Customer',1)"
            ).lastrowid
            job_id = c.execute(
                """
                INSERT INTO jobs (
                    job_number,created_date,customer_id,customer,company,
                    address,manufacturer,machine,pin_serial,status
                ) VALUES ('PPS-J-0001','2026-08-11',?,'Batch 2 Customer',
                          'Customer Co','Kingston','CAT','320','SER-1','REQUESTED')
                """,
                (customer_id,),
            ).lastrowid
            c.commit()
            return int(job_id)

    def add_part(
        self,
        job_id,
        *,
        description="Filter",
        quantity=1,
        cost=100,
        markup=30,
        override=None,
        revision=None,
    ):
        return add_item(
            job_id,
            BasketItemCreate(
                requested_description=description,
                supplier_name="Supplier A",
                supplier_part_number=description.upper(),
                quantity=quantity,
                supplier_unit_cost=cost,
                markup_percent=markup,
                customer_unit_price_override=override,
                verification_status="VERIFIED",
                selected=True,
            ),
            expected_revision_id=revision["id"] if revision else None,
            expected_version=revision["lock_version"] if revision else None,
        )

    def original_quote(self, *, status="SENT", override=None):
        job_id = self.job()
        self.add_part(job_id, override=override)
        committed = commit_basket(job_id)
        with closing(self.connection()) as c:
            part = c.execute(
                "SELECT * FROM job_parts WHERE work_revision_id=?",
                (committed["revision_id"],),
            ).fetchone()
            source = c.execute(
                "SELECT * FROM part_sources WHERE part_id=?", (part["id"],)
            ).fetchone()
            number = legacy_app.next_quote_number(c)
            price = float(part["customer_unit_price"] or 0)
            cost = float(source["supplier_cost"] or 0)
            quote_id = c.execute(
                """
                INSERT INTO quotes (
                    quote_number,job_id,quote_date,status,parts_subtotal,
                    customer_total,supplier_total,profit_total,work_revision_id,
                    is_current,issued_at
                ) VALUES (?,?, '2026-08-11',?,?,?,?,?,?,1,
                          CASE WHEN ?='DRAFT' THEN NULL ELSE CURRENT_TIMESTAMP END)
                """,
                (
                    number, job_id, status, price, price, cost, price-cost,
                    committed["revision_id"], status,
                ),
            ).lastrowid
            c.execute(
                """
                INSERT INTO quote_items (
                    quote_id,part_id,source_id,quantity,description,supplier_name,
                    source_type,supplier_part_number,supplier_unit_cost,
                    customer_unit_price,supplier_line_total,customer_line_total,
                    line_profit,pricing_mode,customer_unit_price_override,
                    recommended_markup_percent
                ) VALUES (?,?,?,1,'Filter','Supplier A','AFTERMARKET','FILTER',
                          ?,?,?,?,?,'LEGACY_FIXED',?,30)
                """,
                (
                    quote_id, part["id"], source["id"], cost, price, cost,
                    price, price-cost, override,
                ),
            )
            c.commit()
            quote, items = legacy_app.load_quote(c, quote_id)
        paths = quote_pdf.generate_quote_pdfs(quote, items)
        return job_id, int(quote_id), paths

    def test_issued_quote_revision_new_number_lineage_history_and_pdf(self):
        job_id, old_quote_id, paths = self.original_quote(status="SENT")
        old_hash = hashlib.sha256(Path(paths["customer"]).read_bytes()).hexdigest()
        with closing(self.connection()) as c:
            old_quote = dict(c.execute(
                "SELECT * FROM quotes WHERE id=?", (old_quote_id,)
            ).fetchone())
            old_items = [dict(row) for row in c.execute(
                "SELECT * FROM quote_items WHERE quote_id=?", (old_quote_id,)
            )]
        revision = start_quote_revision(old_quote_id, "Customer added a hose")
        self.add_part(
            job_id, description="Hose",
            revision=get_revision_context(job_id),
        )
        current = get_revision_context(job_id)
        new_quote = generate_quote_from_revision(
            revision["id"], expected_version=current["lock_version"]
        )
        self.assertNotEqual(new_quote["quote_number"], old_quote["quote_number"])
        self.assertEqual(new_quote["supersedes_quote_id"], old_quote_id)
        with closing(self.connection()) as c:
            old_after = dict(c.execute(
                "SELECT * FROM quotes WHERE id=?", (old_quote_id,)
            ).fetchone())
            old_items_after = [dict(row) for row in c.execute(
                "SELECT * FROM quote_items WHERE quote_id=?", (old_quote_id,)
            )]
            new_items = c.execute(
                "SELECT * FROM quote_items WHERE quote_id=?", (new_quote["id"],)
            ).fetchall()
            self.assertEqual(old_after["status"], "SUPERSEDED")
            self.assertEqual(old_after["is_current"], 0)
            self.assertEqual(new_quote["is_current"], 1)
            self.assertEqual(len(new_items), 2)
            # Only lifecycle lineage fields changed on the old quote.
            for key, value in old_quote.items():
                if key not in {
                    "status", "is_current", "superseded_at",
                    "supersession_reason",
                }:
                    self.assertEqual(old_after[key], value)
            self.assertEqual(old_items_after, old_items)
        self.assertEqual(
            hashlib.sha256(Path(paths["customer"]).read_bytes()).hexdigest(),
            old_hash,
        )
        new_paths = quote_pdf.quote_paths(
            "Batch 2 Customer", new_quote["quote_number"]
        )
        self.assertTrue(new_paths["customer"].exists())
        with closing(self.connection()) as c:
            rendered_quote, _ = legacy_app.load_quote(c, new_quote["id"])
        self.assertEqual(
            rendered_quote["supersedes_quote_number"],
            old_quote["quote_number"],
        )

    def test_missing_issued_pdf_is_not_silently_regenerated(self):
        _, quote_id, paths = self.original_quote(status="SENT")
        Path(paths["customer"]).unlink()
        with self.assertRaises(HTTPException) as blocked:
            legacy_app.customer_quote_pdf(quote_id)
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertFalse(Path(paths["customer"]).exists())

    def test_quote_rendering_uses_immutable_identity_snapshot(self):
        job_id, quote_id, _ = self.original_quote(status="SENT")
        with closing(self.connection()) as c:
            c.execute(
                "UPDATE quotes SET customer_name_snapshot='Original Customer',"
                "machine_snapshot='Original Machine',pin_serial_snapshot='ORIG-PIN' "
                "WHERE id=?",
                (quote_id,),
            )
            c.execute(
                "UPDATE jobs SET customer='Renamed Master',machine='New Machine',"
                "pin_serial='NEW-PIN' WHERE id=?",
                (job_id,),
            )
            c.commit()
            quote, _ = legacy_app.load_quote(c, quote_id)
        self.assertEqual(quote["customer"], "Original Customer")
        self.assertEqual(quote["machine"], "Original Machine")
        self.assertEqual(quote["pin_serial"], "ORIG-PIN")

    def test_draft_correction_retains_number_and_row(self):
        job_id, quote_id, _ = self.original_quote(status="DRAFT")
        with closing(self.connection()) as c:
            number = c.execute(
                "SELECT quote_number FROM quotes WHERE id=?", (quote_id,)
            ).fetchone()[0]
        revision = start_quote_revision(quote_id, "Quantity correction")
        basket = get_basket(job_id)
        update_item(
            basket["items"][0]["id"], BasketItemUpdate(quantity=3),
            expected_job_id=job_id, expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        current = get_revision_context(job_id)
        corrected = generate_quote_from_revision(
            revision["id"], expected_version=current["lock_version"]
        )
        self.assertEqual(corrected["id"], quote_id)
        self.assertEqual(corrected["quote_number"], number)
        self.assertEqual(corrected["content_version"], 2)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM quotes WHERE job_id=?", (job_id,)
            ).fetchone()[0], 1)
            self.assertEqual(c.execute(
                "SELECT quantity FROM quote_items WHERE quote_id=?", (quote_id,)
            ).fetchone()[0], 3)

    def test_revision_remove_replace_supplier_quantity_cost_and_price(self):
        job_id, quote_id, _ = self.original_quote(status="SENT")
        revision = start_quote_revision(quote_id, "Replace wrong part")
        basket = get_basket(job_id)
        old_item = basket["items"][0]
        update_item(
            old_item["id"], BasketItemUpdate(selected=False),
            expected_job_id=job_id, expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        current = get_revision_context(job_id)
        self.add_part(
            job_id, description="Replacement", quantity=2, cost=80,
            override=90, revision=current,
        )
        current = get_revision_context(job_id)
        replacement = [
            item for item in get_basket(job_id)["items"]
            if item["requested_description"] == "Replacement"
        ][0]
        update_item(
            replacement["id"],
            BasketItemUpdate(
                supplier_name="Supplier B", supplier_unit_cost=70,
                quantity=4, customer_unit_price_override=85,
            ),
            expected_job_id=job_id, expected_revision_id=current["id"],
            expected_version=current["lock_version"],
        )
        current = get_revision_context(job_id)
        new_quote = generate_quote_from_revision(
            revision["id"], expected_version=current["lock_version"]
        )
        with closing(self.connection()) as c:
            item = c.execute(
                "SELECT * FROM quote_items WHERE quote_id=?", (new_quote["id"],)
            ).fetchone()
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM quote_items WHERE quote_id=?",
                (new_quote["id"],),
            ).fetchone()[0], 1)
            self.assertEqual(item["supplier_name"], "Supplier B")
            self.assertEqual(item["supplier_unit_cost"], 70)
            self.assertEqual(item["quantity"], 4)
            self.assertEqual(item["customer_unit_price"], 85)

    def test_pricing_provenance_survives_and_can_change_deliberately(self):
        for promised in (175.0, 0.0):
            with self.subTest(promised=promised):
                self._clear()
                job_id, quote_id, _ = self.original_quote(
                    status="SENT", override=promised
                )
                revision = start_quote_revision(quote_id, "Supplier cost changed")
                item = get_basket(job_id)["items"][0]
                self.assertEqual(item["customer_unit_price_override"], promised)
                self.assertEqual(item["pricing_mode"], "LEGACY_FIXED")
                update_item(
                    item["id"], BasketItemUpdate(supplier_unit_cost=140),
                    expected_job_id=job_id,
                    expected_revision_id=revision["id"],
                    expected_version=revision["lock_version"],
                )
                changed = get_basket(job_id)["items"][0]
                self.assertEqual(changed["customer_unit_price_override"], promised)

    def test_cancel_restores_previous_work_and_charges_and_can_revise_again(self):
        job_id, quote_id, _ = self.original_quote(status="REJECTED")
        revision = start_quote_revision(quote_id, "First attempt")
        with closing(self.connection()) as c:
            c.execute(
                "UPDATE jobs SET service_charge=100,sourcing_fee=25 WHERE id=?",
                (job_id,),
            )
            c.execute(
                "UPDATE work_revisions SET service_charge=100,sourcing_fee=25 "
                "WHERE id=?",
                (revision["id"],),
            )
            c.commit()
        cancelled = cancel_quote_revision(
            revision["id"], reason="Customer paused",
            expected_version=revision["lock_version"],
        )
        self.assertEqual(cancelled["state"], "CANCELLED")
        with closing(self.connection()) as c:
            job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            quote = c.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
            self.assertEqual(job["service_charge"], 0)
            self.assertEqual(job["sourcing_fee"], 0)
            self.assertEqual(quote["status"], "REJECTED")
            self.assertEqual(quote["is_current"], 1)
        again = start_quote_revision(quote_id, "Customer resumed")
        self.assertNotEqual(again["id"], revision["id"])

    def test_revision_start_and_cancel_are_idempotent_but_cancelled_cannot_generate(self):
        _, quote_id, _ = self.original_quote(status="SENT")
        first = start_quote_revision(quote_id, "Customer changed request")
        second = start_quote_revision(quote_id, "Duplicate click")
        self.assertEqual(first["id"], second["id"])
        cancelled = cancel_quote_revision(
            first["id"], reason="Abandon changes",
            expected_version=first["lock_version"],
        )
        repeated = cancel_quote_revision(
            first["id"], reason="Repeated click",
            expected_version=first["lock_version"],
        )
        self.assertEqual(cancelled["id"], repeated["id"])
        with self.assertRaises(HTTPException) as blocked:
            generate_quote_from_revision(
                first["id"], expected_version=first["lock_version"]
            )
        self.assertEqual(blocked.exception.status_code, 409)

    def test_rejected_and_approved_quotes_generate_audited_revisions(self):
        for status in ("REJECTED", "APPROVED"):
            with self.subTest(status=status):
                self._clear()
                _, quote_id, _ = self.original_quote(status=status)
                revision = start_quote_revision(quote_id, f"Revise {status}")
                new_quote = generate_quote_from_revision(
                    revision["id"], expected_version=revision["lock_version"]
                )
                with closing(self.connection()) as c:
                    event = c.execute(
                        "SELECT * FROM quote_events WHERE quote_id=? "
                        "AND event_type='QUOTE_SUPERSEDED'",
                        (quote_id,),
                    ).fetchone()
                    self.assertIsNotNone(event)
                    self.assertEqual(event["from_status"], status)
                    self.assertEqual(event["to_status"], "SUPERSEDED")
                    self.assertEqual(c.execute(
                        "SELECT is_current FROM quotes WHERE id=?",
                        (new_quote["id"],),
                    ).fetchone()[0], 1)

    def test_revision_service_and_sourcing_fees_flow_to_new_quote(self):
        job_id, quote_id, _ = self.original_quote(status="SENT")
        revision = start_quote_revision(quote_id, "Charges changed")
        update_revenue_adjustments(
            job_id,
            service_charge="75",
            service_charge_description="Field service",
            sourcing_fee="20",
            sourcing_fee_description="Special sourcing",
            expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        current = get_revision_context(job_id)
        quote = generate_quote_from_revision(
            revision["id"], expected_version=current["lock_version"]
        )
        self.assertEqual(quote["service_charge"], 75)
        self.assertEqual(quote["sourcing_fee"], 20)

    def test_approved_before_invoice_allowed_but_invoice_blocks(self):
        job_id, quote_id, _ = self.original_quote(status="APPROVED")
        revision = start_quote_revision(quote_id, "Approved correction")
        self.assertEqual(revision["purpose"], "QUOTE_REVISION")
        cancel_quote_revision(
            revision["id"], reason="No change",
            expected_version=revision["lock_version"],
        )
        with closing(self.connection()) as c:
            c.execute(
                """
                INSERT INTO invoices (
                    invoice_number,quote_id,job_id,invoice_date,
                    customer_total,balance_due
                ) VALUES ('PPS-INV-0001',?,?,'2026-08-11',130,130)
                """,
                (quote_id, job_id),
            )
            c.commit()
        with self.assertRaises(HTTPException):
            start_quote_revision(quote_id, "Too late")

    def test_order_receipt_and_delivery_each_block_ordinary_revision(self):
        for history in ("order", "receipt", "delivery"):
            with self.subTest(history=history):
                self._clear()
                job_id, quote_id, _ = self.original_quote(status="SENT")
                with closing(self.connection()) as c:
                    if history in {"order", "receipt"}:
                        order_id = c.execute(
                            "INSERT INTO supplier_orders(job_id,supplier_name,status) "
                            "VALUES (?,'Supplier A','DRAFT')",
                            (job_id,),
                        ).lastrowid
                        if history == "receipt":
                            c.execute(
                                "INSERT INTO receiving_events(order_id,notes) "
                                "VALUES (?,'Partial receipt')",
                                (order_id,),
                            )
                    else:
                        c.execute(
                            "INSERT INTO deliveries(job_id,status) VALUES (?,'READY')",
                            (job_id,),
                        )
                    c.commit()
                with self.assertRaises(HTTPException) as blocked:
                    start_quote_revision(quote_id, "Unsafe downstream change")
                self.assertEqual(blocked.exception.status_code, 409)

    def test_concurrent_generate_consumes_one_quote_number(self):
        job_id, quote_id, _ = self.original_quote(status="SENT")
        revision = start_quote_revision(quote_id, "Concurrent generate")
        barrier = Barrier(2)
        def generate():
            barrier.wait()
            return generate_quote_from_revision(
                revision["id"], expected_version=revision["lock_version"]
            )["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: generate(), range(2)))
        self.assertEqual(ids[0], ids[1])
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM quotes WHERE job_id=?", (job_id,)
            ).fetchone()[0], 2)

    def test_committed_pending_revision_is_retryable_and_blocks_invoice_race(self):
        job_id, quote_id, _ = self.original_quote(status="APPROVED")
        revision = start_quote_revision(quote_id, "Crash-window rehearsal")
        committed = commit_work_revision(
            job_id,
            expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        self.assertFalse(committed["already_committed"])
        with self.assertRaises(HTTPException) as invoice_blocked:
            legacy_app.convert_quote_to_invoice(quote_id)
        self.assertEqual(invoice_blocked.exception.status_code, 409)
        quote = generate_quote_from_revision(
            revision["id"], expected_version=revision["lock_version"]
        )
        self.assertEqual(quote["supersedes_quote_id"], quote_id)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM invoices WHERE job_id=?", (job_id,)
            ).fetchone()[0], 0)
            self.assertEqual(c.execute(
                "SELECT last_number FROM pps_number_sequences "
                "WHERE entity_type='QUOTE'"
            ).fetchone()[0], 2)

    def test_reopen_cancelled_job_enters_governed_revision(self):
        job_id, quote_id, _ = self.original_quote(status="SENT")
        with closing(self.connection()) as c:
            c.execute(
                "UPDATE jobs SET status='CANCELLED',status_before_cancel='QUOTED',"
                "cancellation_reason='Paused' WHERE id=?",
                (job_id,),
            )
            c.commit()
        revision = reopen_job_for_revision(job_id, "Customer returned")
        self.assertEqual(revision["state"], "EDITABLE")
        with closing(self.connection()) as c:
            job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.assertNotEqual(job["status"], "CANCELLED")
            self.assertEqual(job["active_work_revision_id"], revision["id"])


if __name__ == "__main__":
    unittest.main()
