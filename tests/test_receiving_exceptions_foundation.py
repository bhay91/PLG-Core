from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.documents.integrity import verified_receiving_document
from plg_core.supply.models import (
    BackorderEventCreate,
    ReceiptCreate,
    ReceiptExceptionItem,
    ReceiptItem,
    ReceivingExceptionResolutionCreate,
)
from plg_core.supply.service import (
    clear_quarantined_exception,
    create_orders_from_paid_invoice,
    get_current_backorder,
    get_delivery_workspace,
    get_order,
    place_order,
    record_backorder_event,
    record_receipt,
    resolve_receiving_exception,
)


ROOT = Path(__file__).resolve().parents[1]


class ReceivingExceptionMigrationTests(unittest.TestCase):
    def test_additive_repeatable_migration_preserves_historical_quantities(self):
        with tempfile.TemporaryDirectory(prefix="pps-exception-migration-") as temp:
            database = Path(temp) / "test.db"
            shutil.copy2(ROOT / "data" / "plg_core.db", database)
            with sqlite3.connect(database) as connection:
                before = (
                    connection.execute("SELECT COUNT(*),COALESCE(SUM(quantity_received),0) FROM receiving_event_items").fetchone(),
                    connection.execute("SELECT COUNT(*),COALESCE(SUM(quantity_received),0) FROM supplier_order_items").fetchone(),
                )
            with patch.object(legacy_app, "DB_PATH", database):
                run_migrations()
                run_migrations()
            with sqlite3.connect(database) as connection:
                after = (
                    connection.execute("SELECT COUNT(*),COALESCE(SUM(quantity_received),0) FROM receiving_event_items").fetchone(),
                    connection.execute("SELECT COUNT(*),COALESCE(SUM(quantity_received),0) FROM supplier_order_items").fetchone(),
                )
                columns = {row[1] for row in connection.execute("PRAGMA table_info(receiving_events)")}
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE migration_id='0050_structured_receiving_exceptions'").fetchone()[0], 1)
            self.assertEqual(before, after)
            self.assertTrue({"event_kind", "request_fingerprint"}.issubset(columns))


class ReceivingExceptionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-exception-service-")
        root = Path(self.temp.name)
        self.database = root / "test.db"
        self.document_root = root / "documents"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.database)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.database)
        self.env_patch = patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)})
        self.db_patch.start()
        self.env_patch.start()
        run_migrations()
        self._fixture()

    def tearDown(self):
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def _fixture(self):
        with closing(legacy_app.get_connection()) as connection:
            customer = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('EX-C','Exception Customer',1)"
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('EX-J','2026-08-26',?,?,'CONFIRMED')",
                (customer, "Exception Customer"),
            ).lastrowid
            quote = connection.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total) VALUES ('EX-Q',?,'2026-08-26','CONVERTED',100)",
                (self.job_id,),
            ).lastrowid
            self.invoice_id = connection.execute(
                "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) VALUES ('EX-I',?,?,'2026-08-26','PAID',100,0)",
                (quote, self.job_id),
            ).lastrowid
            connection.execute(
                """INSERT INTO invoice_items(
                    invoice_id,quantity,description,supplier_name,supplier_part_number,
                    supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total
                ) VALUES (?,5,'Exception Item','Exception Supplier','EX-5',10,50,20,100)""",
                (self.invoice_id,),
            )
            connection.execute(
                """INSERT INTO invoice_items(
                    invoice_id,quantity,description,supplier_name,supplier_part_number,
                    supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total
                ) VALUES (?,1,'Exception Item B','Exception Supplier','EX-B',20,20,30,30)""",
                (self.invoice_id,),
            )
            connection.commit()
        self.order = create_orders_from_paid_invoice(self.invoice_id)[0]
        place_order(self.order["id"])
        self.order = get_order(self.order["id"])
        self.item_id = self.order["items"][0]["id"]
        self.other_item_id = self.order["items"][1]["id"]

    def _exception(self, disposition, quantity=1, key="exception", accepted=0):
        return record_receipt(
            self.order["id"],
            ReceiptCreate(
                items=[ReceiptItem(order_item_id=self.item_id, quantity_received=accepted)] if accepted else [],
                exceptions=[ReceiptExceptionItem(
                    order_item_id=self.item_id,
                    disposition=disposition,
                    quantity=quantity,
                    reason="Supplier shipment variance",
                    supplier_reference="PACK-1",
                )],
                receiver="Receiver",
                notes="Structured receipt",
                idempotency_key=key,
            ),
            actor="receiving.operator",
            request_id="request-exception",
        )

    def test_accepted_compatibility_status_delivery_and_accounting(self):
        with closing(legacy_app.get_connection()) as connection:
            financial_before = tuple(connection.execute(
                "SELECT customer_total,balance_due,status FROM invoices WHERE id=?", (self.invoice_id,)
            ).fetchone())
        receipt = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=2)],
            idempotency_key="accepted",
        ))
        self.assertEqual(receipt["status_after"], "PARTIAL")
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 2)
        self.assertEqual(get_delivery_workspace(self.job_id)["available_total"], 2)
        with closing(legacy_app.get_connection()) as connection:
            financial_after = tuple(connection.execute(
                "SELECT customer_total,balance_due,status FROM invoices WHERE id=?", (self.invoice_id,)
            ).fetchone())
        self.assertEqual(financial_before, financial_after)

    def test_physical_exception_dispositions_do_not_become_inventory(self):
        for index, disposition in enumerate(("DAMAGED", "WRONG_ITEM", "REJECTED", "QUARANTINED")):
            receipt = self._exception(disposition, key=f"physical-{index}", accepted=1 if index == 0 else 0)
            self.assertEqual(receipt["exceptions"][0]["disposition"], disposition)
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 1)
        self.assertEqual(get_delivery_workspace(self.job_id)["available_total"], 1)

    def test_zero_and_negative_exception_quantities_are_rejected(self):
        for quantity in (0, -1):
            with self.assertRaises(ValidationError):
                ReceiptExceptionItem(
                    order_item_id=self.item_id,
                    disposition="DAMAGED",
                    quantity=quantity,
                    reason="Invalid quantity",
                )

    def test_shortage_only_and_mixed_shortage_preserve_accepted_semantics(self):
        shortage = self._exception("SHORT", quantity=2, key="short-only")
        self.assertEqual(shortage["status_after"], "ORDERED")
        self.assertEqual(get_order(self.order["id"])["status"], "ORDERED")
        mixed = self._exception("SHORT", quantity=1, key="short-mixed", accepted=2)
        self.assertEqual(mixed["status_after"], "PARTIAL")
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 2)
        with self.assertRaises(HTTPException):
            self._exception("SHORT", quantity=4, key="short-too-large")

    def test_exception_documents_are_complete_and_manifest_verified(self):
        mixed = self._exception("DAMAGED", key="pdf-mixed", accepted=1)
        short = self._exception("SHORT", key="pdf-short")
        for receipt, expected in (
            (mixed, ("EXCEPTION ITEM", "DAMAGED", "NON-DELIVERABLE EXCEPTION")),
            (short, ("SHORT", "NOT PHYSICALLY RECEIVED", "SUPPLIER SHIPMENT VARIANCE")),
        ):
            with closing(legacy_app.get_connection()) as connection:
                path = verified_receiving_document(connection, receipt["id"])
                manifest = connection.execute(
                    "SELECT sha256 FROM receiving_documents_manifest WHERE receipt_id=?",
                    (receipt["id"],),
                ).fetchone()
            text = subprocess.run(
                ["pdftotext", str(path), "-"], check=True, capture_output=True, text=True
            ).stdout.upper()
            text = " ".join(text.split())
            for value in expected:
                self.assertIn(value, text)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest["sha256"])

    def test_cross_order_mixed_receipt_rolls_back_all_rows(self):
        other_item = self.order["items"][0]["id"] + 999999
        with self.assertRaises(HTTPException):
            record_receipt(self.order["id"], ReceiptCreate(
                items=[ReceiptItem(order_item_id=self.item_id, quantity_received=1)],
                exceptions=[ReceiptExceptionItem(
                    order_item_id=other_item, disposition="DAMAGED", quantity=1, reason="Wrong order"
                )],
                idempotency_key="atomic-failure",
            ))
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receiving_events WHERE idempotency_key='atomic-failure'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT quantity_received FROM supplier_order_items WHERE id=?", (self.item_id,)).fetchone()[0], 0)

    def test_receipt_fingerprint_replay_and_changed_content_conflict(self):
        first = self._exception("DAMAGED", key="fingerprint")
        replay = self._exception("DAMAGED", key="fingerprint")
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(HTTPException) as failure:
            self._exception("DAMAGED", quantity=2, key="fingerprint")
        self.assertEqual(failure.exception.status_code, 409)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receiving_exception_items WHERE receipt_id=?", (first["id"],)).fetchone()[0], 1)

    def test_receipt_replay_repairs_missing_document_without_duplicate_operation(self):
        payload = ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=1)],
            idempotency_key="receipt-document-repair",
        )
        with patch(
            "plg_core.supply.service._ensure_receiving_document",
            side_effect=RuntimeError("synthetic document failure"),
        ), self.assertRaises(RuntimeError):
            record_receipt(self.order["id"], payload)
        replay = record_receipt(self.order["id"], payload)
        self.assertTrue(replay["replayed"])
        with closing(legacy_app.get_connection()) as connection:
            verified_receiving_document(connection, replay["id"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receiving_events WHERE id=?", (replay["id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receiving_event_items WHERE receipt_id=?", (replay["id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receiving_documents_manifest WHERE receipt_id=?", (replay["id"],)).fetchone()[0], 1)

    def test_exception_fingerprint_is_independent_of_disposition_list_order(self):
        exceptions = [
            ReceiptExceptionItem(
                order_item_id=self.item_id, disposition="DAMAGED", quantity=1,
                reason="Damage",
            ),
            ReceiptExceptionItem(
                order_item_id=self.item_id, disposition="WRONG_ITEM", quantity=1,
                reason="Wrong item",
            ),
        ]
        first = record_receipt(self.order["id"], ReceiptCreate(
            exceptions=exceptions, idempotency_key="ordered-exceptions"
        ))
        replay = record_receipt(self.order["id"], ReceiptCreate(
            exceptions=list(reversed(exceptions)), idempotency_key="ordered-exceptions"
        ))
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])

    def test_partial_append_only_resolution_and_idempotency(self):
        receipt = self._exception("DAMAGED", quantity=3, key="resolve-source")
        exception_id = receipt["exceptions"][0]["id"]
        payload = ReceivingExceptionResolutionCreate(
            quantity=1, resolution="REPLACEMENT_EXPECTED", reason="Supplier approved",
            idempotency_key="resolution-1",
        )
        first = resolve_receiving_exception(exception_id, payload)
        replay = resolve_receiving_exception(exception_id, payload)
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        resolve_receiving_exception(exception_id, ReceivingExceptionResolutionCreate(
            quantity=2, resolution="DISPOSED", reason="Disposed", idempotency_key="resolution-2",
        ))
        with self.assertRaises(HTTPException):
            resolve_receiving_exception(exception_id, ReceivingExceptionResolutionCreate(
                quantity=2, resolution="CLOSED", reason="Too much", idempotency_key="resolution-3",
            ))
        with closing(legacy_app.get_connection()) as connection:
            original = connection.execute("SELECT quantity,disposition FROM receiving_exception_items WHERE id=?", (exception_id,)).fetchone()
            self.assertEqual(tuple(original), (3, "DAMAGED"))

    def test_interim_replacement_then_terminal_replacement_receipt(self):
        exception_id = self._exception("DAMAGED", key="replacement-source")["exceptions"][0]["id"]
        resolve_receiving_exception(exception_id, ReceivingExceptionResolutionCreate(
            quantity=1, resolution="REPLACEMENT_EXPECTED", reason="Supplier approved",
            idempotency_key="replacement-expected",
        ))
        replacement = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=1)],
            idempotency_key="replacement-receipt",
        ))
        resolved = resolve_receiving_exception(exception_id, ReceivingExceptionResolutionCreate(
            quantity=1, resolution="REPLACED_BY_RECEIPT", reason="Replacement accepted",
            related_receipt_id=replacement["id"], idempotency_key="replacement-complete",
        ))
        self.assertEqual(resolved["resolution"], "REPLACED_BY_RECEIPT")

    def test_replacement_receipt_relationship_is_same_line_sufficient_and_later(self):
        pre = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=1)],
            idempotency_key="replacement-pre",
        ))
        exception_id = self._exception(
            "DAMAGED", quantity=2, key="replacement-validation-source"
        )["exceptions"][0]["id"]
        wrong_line = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.other_item_id, quantity_received=1)],
            idempotency_key="replacement-wrong-line",
        ))
        insufficient = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=1)],
            idempotency_key="replacement-insufficient",
        ))
        payload = dict(
            quantity=2, resolution="REPLACED_BY_RECEIPT", reason="Replacement",
        )
        for index, related_receipt_id in enumerate(
            (None, pre["id"], wrong_line["id"], insufficient["id"])
        ):
            with self.assertRaises(HTTPException):
                resolve_receiving_exception(
                    exception_id,
                    ReceivingExceptionResolutionCreate(
                        **payload, related_receipt_id=related_receipt_id,
                        idempotency_key=f"replacement-invalid-{index}",
                    ),
                )
        valid = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=2)],
            idempotency_key="replacement-valid",
        ))
        result = resolve_receiving_exception(
            exception_id,
            ReceivingExceptionResolutionCreate(
                **payload, related_receipt_id=valid["id"],
                idempotency_key="replacement-valid-resolution",
            ),
        )
        self.assertEqual(result["related_receipt_id"], valid["id"])

    def test_replacement_receipt_capacity_cannot_be_claimed_twice(self):
        first_exception = self._exception(
            "DAMAGED", key="allocation-first-source"
        )["exceptions"][0]["id"]
        second_exception = self._exception(
            "REJECTED", key="allocation-second-source"
        )["exceptions"][0]["id"]
        replacement = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=1)],
            idempotency_key="allocation-one-unit",
        ))
        payload = ReceivingExceptionResolutionCreate(
            quantity=1, resolution="REPLACED_BY_RECEIPT", reason="Replacement",
            related_receipt_id=replacement["id"], idempotency_key="allocation-first",
        )
        first = resolve_receiving_exception(first_exception, payload)
        self.assertTrue(resolve_receiving_exception(first_exception, payload)["replayed"])
        accepted_before = get_order(self.order["id"])["items"][0]["quantity_received"]
        with closing(legacy_app.get_connection()) as connection:
            documents_before = [tuple(row) for row in connection.execute(
                "SELECT receipt_id,sha256 FROM receiving_documents_manifest ORDER BY id"
            ).fetchall()]
        with self.assertRaises(HTTPException) as failure:
            resolve_receiving_exception(second_exception, ReceivingExceptionResolutionCreate(
                quantity=1, resolution="REPLACED_BY_RECEIPT", reason="Second claim",
                related_receipt_id=replacement["id"], idempotency_key="allocation-second",
            ))
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], accepted_before)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM receiving_exception_resolutions WHERE related_receipt_id=? AND resolution='REPLACED_BY_RECEIPT'",
                (replacement["id"],),
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='RECEIVING_EXCEPTION_RESOLVED' AND entity_id=?",
                (str(second_exception),),
            ).fetchone()[0], 0)
            documents_after = [tuple(row) for row in connection.execute(
                "SELECT receipt_id,sha256 FROM receiving_documents_manifest ORDER BY id"
            ).fetchall()]
        self.assertEqual(documents_before, documents_after)
        self.assertEqual(first["quantity"], 1)

    def test_replacement_receipt_capacity_supports_partial_exact_then_rejects_overage(self):
        exception_ids = [
            self._exception(
                disposition, quantity=quantity,
                key=f"allocation-three-source-{index}",
            )["exceptions"][0]["id"]
            for index, (disposition, quantity) in enumerate(
                (("DAMAGED", 1), ("REJECTED", 2), ("WRONG_ITEM", 1))
            )
        ]
        replacement = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=3)],
            idempotency_key="allocation-three-units",
        ))
        for index, quantity in enumerate((1, 2)):
            resolve_receiving_exception(exception_ids[index], ReceivingExceptionResolutionCreate(
                quantity=quantity, resolution="REPLACED_BY_RECEIPT", reason="Allocated",
                related_receipt_id=replacement["id"], idempotency_key=f"allocation-three-{index}",
            ))
        with self.assertRaises(HTTPException):
            resolve_receiving_exception(exception_ids[2], ReceivingExceptionResolutionCreate(
                quantity=1, resolution="REPLACED_BY_RECEIPT", reason="Over allocation",
                related_receipt_id=replacement["id"], idempotency_key="allocation-three-over",
            ))
        with closing(legacy_app.get_connection()) as connection:
            allocated = connection.execute(
                "SELECT SUM(quantity) FROM receiving_exception_resolutions WHERE related_receipt_id=? AND resolution='REPLACED_BY_RECEIPT'",
                (replacement["id"],),
            ).fetchone()[0]
        self.assertEqual(allocated, 3)

    def test_resolution_matrix_positive_and_negative_for_every_disposition(self):
        cases = {
            "DAMAGED": ("DISPOSED", "BACKORDER_CONFIRMED"),
            "WRONG_ITEM": ("RETURNED", "DISPOSED"),
            "QUARANTINED": ("CLOSED", "RETURNED"),
            "REJECTED": ("DISPOSED", "BACKORDER_CONFIRMED"),
            "SHORT": ("CLOSED", "RETURNED"),
        }
        for index, (disposition, (valid, invalid)) in enumerate(cases.items()):
            exception_id = self._exception(
                disposition, key=f"matrix-source-{index}"
            )["exceptions"][0]["id"]
            resolve_receiving_exception(exception_id, ReceivingExceptionResolutionCreate(
                quantity=1, resolution=valid, reason="Valid outcome",
                idempotency_key=f"matrix-valid-{index}",
            ))
            with self.assertRaises(HTTPException):
                resolve_receiving_exception(exception_id, ReceivingExceptionResolutionCreate(
                    quantity=1, resolution=invalid, reason="Invalid outcome",
                    idempotency_key=f"matrix-invalid-{index}",
                ))

    def test_changed_resolution_content_conflicts_without_duplicate_audit(self):
        exception_id = self._exception("DAMAGED", key="resolution-change-source")["exceptions"][0]["id"]
        original = ReceivingExceptionResolutionCreate(
            quantity=1, resolution="CLOSED", reason="Resolved", notes="Original",
            supplier_reference="SUP-1", evidence_reference="E-1",
            idempotency_key="resolution-change",
        )
        resolve_receiving_exception(exception_id, original)
        resolve_receiving_exception(exception_id, original)
        changed = original.model_copy(update={"notes": "Changed"})
        with self.assertRaises(HTTPException):
            resolve_receiving_exception(exception_id, changed)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM receiving_exception_resolutions WHERE exception_item_id=?",
                (exception_id,),
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='RECEIVING_EXCEPTION_RESOLVED' AND entity_id=?",
                (str(exception_id),),
            ).fetchone()[0], 1)

    def test_concurrent_resolution_cannot_over_resolve(self):
        exception_id = self._exception("WRONG_ITEM", quantity=5, key="race-source")["exceptions"][0]["id"]
        def resolve(key):
            try:
                resolve_receiving_exception(exception_id, ReceivingExceptionResolutionCreate(
                    quantity=4, resolution="RETURNED", reason="Concurrent return", idempotency_key=key,
                ))
                return "ok"
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(resolve, ("race-a", "race-b")))
        self.assertEqual(sorted(results, key=str), [409, "ok"])

    def test_quarantine_clearance_is_atomic_idempotent_and_accepted_once(self):
        exception_id = self._exception("QUARANTINED", quantity=2, key="quarantine")["exceptions"][0]["id"]
        first = clear_quarantined_exception(
            exception_id, quantity=2, reason="Inspection passed", idempotency_key="clear-1"
        )
        replay = clear_quarantined_exception(
            exception_id, quantity=2, reason="Inspection passed", idempotency_key="clear-1"
        )
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 2)
        self.assertEqual(get_delivery_workspace(self.job_id)["available_total"], 2)

    def test_clearance_changed_business_content_conflicts(self):
        exception_id = self._exception(
            "QUARANTINED", quantity=2, key="clearance-fingerprint-source"
        )["exceptions"][0]["id"]
        original = dict(
            quantity=1, reason="Inspection passed", receiver="Alex", notes="Bay one",
            idempotency_key="clearance-fingerprint",
        )
        first = clear_quarantined_exception(exception_id, **original)
        self.assertTrue(clear_quarantined_exception(exception_id, **original)["replayed"])
        for field, value in (
            ("quantity", 2), ("reason", "Different"),
            ("receiver", "Jordan"), ("notes", "Bay two"),
        ):
            changed = {**original, field: value}
            with self.assertRaises(HTTPException):
                clear_quarantined_exception(exception_id, **changed)
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 1)
        self.assertEqual(first["event_kind"], "EXCEPTION_CLEARANCE")

    def test_clearance_completes_line_order_and_job_without_replay_duplicates(self):
        exception_id = self._exception(
            "QUARANTINED", quantity=5, key="clearance-completion-source"
        )["exceptions"][0]["id"]
        record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.other_item_id, quantity_received=1)],
            idempotency_key="clearance-other-line",
        ))
        result = clear_quarantined_exception(
            exception_id, quantity=5, reason="All units passed",
            idempotency_key="clearance-completion",
        )
        self.assertEqual(result["status_after"], "RECEIVED")
        self.assertEqual(get_order(self.order["id"])["status"], "RECEIVED")
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT status FROM jobs WHERE id=?", (self.job_id,)).fetchone()[0], "RECEIVED")
            before = (
                connection.execute("SELECT COUNT(*) FROM job_timeline WHERE job_id=? AND event_type='RECEIVING_COMPLETE'", (self.job_id,)).fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action='RECEIVING_EXCEPTION_RESOLVED' AND entity_id=?", (str(exception_id),)).fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action='PARTS_RECEIVED' AND metadata_json LIKE ?", (f'%\"receipt_id\": {result["id"]}%',)).fetchone()[0],
            )
        self.assertTrue(clear_quarantined_exception(
            exception_id, quantity=5, reason="All units passed",
            idempotency_key="clearance-completion",
        )["replayed"])
        with closing(legacy_app.get_connection()) as connection:
            after = (
                connection.execute("SELECT COUNT(*) FROM job_timeline WHERE job_id=? AND event_type='RECEIVING_COMPLETE'", (self.job_id,)).fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action='RECEIVING_EXCEPTION_RESOLVED' AND entity_id=?", (str(exception_id),)).fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action='PARTS_RECEIVED' AND metadata_json LIKE ?", (f'%\"receipt_id\": {result["id"]}%',)).fetchone()[0],
            )
        self.assertEqual(before, after)
        self.assertEqual(before, (1, 1, 1))

    def test_clearance_replay_repairs_missing_document_after_postcommit_failure(self):
        exception_id = self._exception(
            "QUARANTINED", quantity=1, key="clearance-repair-source"
        )["exceptions"][0]["id"]
        with patch(
            "plg_core.supply.service._ensure_receiving_document",
            side_effect=RuntimeError("synthetic document failure"),
        ), self.assertRaises(RuntimeError):
            clear_quarantined_exception(
                exception_id, quantity=1, reason="Passed",
                idempotency_key="clearance-repair",
            )
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 1)
        repaired = clear_quarantined_exception(
            exception_id, quantity=1, reason="Passed",
            idempotency_key="clearance-repair",
        )
        self.assertTrue(repaired["replayed"])
        with closing(legacy_app.get_connection()) as connection:
            verified_receiving_document(connection, repaired["id"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receiving_events WHERE id=?", (repaired["id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receiving_exception_resolutions WHERE exception_item_id=?", (exception_id,)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM receiving_documents_manifest WHERE receipt_id=?", (repaired["id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action='RECEIVING_EXCEPTION_RESOLVED' AND entity_id=?", (str(exception_id),)).fetchone()[0], 1)

    def test_concurrent_clearance_and_receiving_never_over_accept(self):
        exception_id = self._exception(
            "QUARANTINED", quantity=2, key="clearance-receive-race-source"
        )["exceptions"][0]["id"]
        def clearance():
            try:
                clear_quarantined_exception(
                    exception_id, quantity=2, reason="Passed",
                    idempotency_key="clearance-race",
                )
                return "clearance"
            except HTTPException:
                return "conflict"
        def receiving():
            try:
                record_receipt(self.order["id"], ReceiptCreate(
                    items=[ReceiptItem(order_item_id=self.item_id, quantity_received=4)],
                    idempotency_key="receiving-race",
                ))
                return "receiving"
            except HTTPException:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [executor.submit(clearance), executor.submit(receiving)]
            results = [future.result() for future in results]
        self.assertEqual(results.count("conflict"), 1)
        self.assertLessEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 5)

    def test_two_concurrent_clearances_cannot_double_accept(self):
        exception_id = self._exception(
            "QUARANTINED", quantity=3, key="double-clear-source"
        )["exceptions"][0]["id"]
        def clear(key):
            try:
                clear_quarantined_exception(
                    exception_id, quantity=2, reason="Passed", idempotency_key=key
                )
                return "ok"
            except HTTPException:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(clear, ("double-clear-a", "double-clear-b")))
        self.assertEqual(sorted(results), ["conflict", "ok"])
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 2)

    def test_backorder_lifecycle_is_append_only_idempotent_and_non_inventory(self):
        events = [
            BackorderEventCreate(event_kind="DECLARED", backordered_quantity=4, reason="Supplier confirmed", idempotency_key="bo-1"),
            BackorderEventCreate(event_kind="UPDATED", backordered_quantity=2, reason="Partial shipment planned", idempotency_key="bo-2"),
            BackorderEventCreate(event_kind="RESOLVED", backordered_quantity=0, reason="Commitment fulfilled", idempotency_key="bo-3"),
        ]
        for payload in events:
            record_backorder_event(self.item_id, payload)
        replay = record_backorder_event(self.item_id, events[-1])
        self.assertTrue(replay["replayed"])
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], 0)
        with self.assertRaises(HTTPException):
            record_backorder_event(self.item_id, BackorderEventCreate(
                event_kind="DECLARED", backordered_quantity=6, reason="Too many", idempotency_key="bo-invalid"
            ))
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM supplier_order_item_backorder_events WHERE order_item_id=?", (self.item_id,)).fetchone()[0], 3)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_backorder_effective_state_tracks_later_accepted_receiving(self):
        record_backorder_event(self.item_id, BackorderEventCreate(
            event_kind="DECLARED", backordered_quantity=4,
            reason="Supplier confirmed", idempotency_key="backorder-effective",
        ))
        record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=3)],
            idempotency_key="backorder-receive-three",
        ))
        partial = get_current_backorder(self.item_id)
        self.assertEqual((partial["declared_quantity"], partial["active_quantity"]), (4, 2))
        self.assertTrue(partial["partially_satisfied_by_receiving"])
        record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=2)],
            idempotency_key="backorder-receive-final",
        ))
        complete = get_current_backorder(self.item_id)
        self.assertEqual((complete["remaining_quantity"], complete["active_quantity"]), (0, 0))

    def test_backorder_receipt_fulfillment_requires_later_sufficient_same_line_receipt(self):
        pre = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=1)],
            idempotency_key="backorder-pre-receipt",
        ))
        declaration = record_backorder_event(self.item_id, BackorderEventCreate(
            event_kind="DECLARED", backordered_quantity=3, reason="Supplier commitment",
            idempotency_key="backorder-reference-declare",
        ))
        insufficient = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=1)],
            idempotency_key="backorder-insufficient-receipt",
        ))
        wrong_line = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.other_item_id, quantity_received=1)],
            idempotency_key="backorder-wrong-line-receipt",
        ))
        for index, receipt_id in enumerate((pre["id"], insufficient["id"], wrong_line["id"])):
            with self.assertRaises(HTTPException):
                record_backorder_event(self.item_id, BackorderEventCreate(
                    event_kind="RESOLVED", backordered_quantity=0,
                    reason="Fulfilled", related_receipt_id=receipt_id,
                    idempotency_key=f"backorder-reference-invalid-{index}",
                ))
        sufficient = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=3)],
            idempotency_key="backorder-sufficient-receipt",
        ))
        resolved = record_backorder_event(self.item_id, BackorderEventCreate(
            event_kind="RESOLVED", backordered_quantity=0, reason="Fulfilled",
            related_receipt_id=sufficient["id"], idempotency_key="backorder-reference-valid",
        ))
        self.assertEqual(resolved["fulfilled_quantity"], 3)
        self.assertEqual(resolved["related_receipt_id"], sufficient["id"])
        with closing(legacy_app.get_connection()) as connection:
            preserved = connection.execute(
                "SELECT event_kind,backordered_quantity,receipt_id_cutoff FROM supplier_order_item_backorder_events WHERE id=?",
                (declaration["id"],),
            ).fetchone()
            self.assertEqual(tuple(preserved)[:2], ("DECLARED", 3))

    def test_backorder_nonreceipt_closure_remains_append_only(self):
        record_backorder_event(self.item_id, BackorderEventCreate(
            event_kind="DECLARED", backordered_quantity=2, reason="Supplier commitment",
            idempotency_key="backorder-close-declare",
        ))
        closed = record_backorder_event(self.item_id, BackorderEventCreate(
            event_kind="RESOLVED", backordered_quantity=0,
            reason="Supplier withdrew commitment", idempotency_key="backorder-close",
        ))
        self.assertIsNone(closed["related_receipt_id"])
        self.assertEqual(closed["fulfilled_quantity"], 0)
        self.assertEqual(get_current_backorder(self.item_id)["active_quantity"], 0)

    def test_shared_receipt_capacity_prevents_replacement_and_backorder_overclaim(self):
        exception_id = self._exception(
            "DAMAGED", key="shared-capacity-exception"
        )["exceptions"][0]["id"]
        declaration = record_backorder_event(self.item_id, BackorderEventCreate(
            event_kind="DECLARED", backordered_quantity=2, reason="Supplier commitment",
            idempotency_key="shared-capacity-declare",
        ))
        receipt = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=2)],
            idempotency_key="shared-capacity-receipt",
        ))
        resolve_receiving_exception(exception_id, ReceivingExceptionResolutionCreate(
            quantity=1, resolution="REPLACED_BY_RECEIPT", reason="Replacement",
            related_receipt_id=receipt["id"], idempotency_key="shared-capacity-replacement",
        ))
        accepted_before = get_order(self.order["id"])["items"][0]["quantity_received"]
        with closing(legacy_app.get_connection()) as connection:
            documents_before = [tuple(row) for row in connection.execute(
                "SELECT receipt_id,sha256 FROM receiving_documents_manifest ORDER BY id"
            ).fetchall()]
        with self.assertRaises(HTTPException):
            record_backorder_event(self.item_id, BackorderEventCreate(
                event_kind="RESOLVED", backordered_quantity=0, reason="Fulfilled",
                related_receipt_id=receipt["id"], idempotency_key="shared-capacity-backorder",
            ))
        self.assertEqual(get_order(self.order["id"])["items"][0]["quantity_received"], accepted_before)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM supplier_order_item_backorder_events WHERE order_item_id=?",
                (self.item_id,),
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='SUPPLIER_BACKORDER_UPDATED' AND entity_id=?",
                (str(self.item_id),),
            ).fetchone()[0], 1)
            documents_after = [tuple(row) for row in connection.execute(
                "SELECT receipt_id,sha256 FROM receiving_documents_manifest ORDER BY id"
            ).fetchall()]
        self.assertEqual(documents_before, documents_after)
        self.assertEqual(declaration["backordered_quantity"], 2)

    def test_shared_receipt_capacity_is_safe_when_backorder_allocates_first(self):
        first_exception = self._exception(
            "DAMAGED", key="reverse-capacity-exception-one"
        )["exceptions"][0]["id"]
        second_exception = self._exception(
            "DAMAGED", key="reverse-capacity-exception-two"
        )["exceptions"][0]["id"]
        record_backorder_event(self.item_id, BackorderEventCreate(
            event_kind="DECLARED", backordered_quantity=2, reason="Supplier commitment",
            idempotency_key="reverse-capacity-declare",
        ))
        receipt = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=3)],
            idempotency_key="reverse-capacity-receipt",
        ))
        fulfilled = record_backorder_event(self.item_id, BackorderEventCreate(
            event_kind="RESOLVED", backordered_quantity=0, reason="Fulfilled",
            related_receipt_id=receipt["id"], idempotency_key="reverse-capacity-backorder",
        ))
        self.assertEqual(fulfilled["fulfilled_quantity"], 2)
        resolve_receiving_exception(first_exception, ReceivingExceptionResolutionCreate(
            quantity=1, resolution="REPLACED_BY_RECEIPT", reason="Replacement",
            related_receipt_id=receipt["id"], idempotency_key="reverse-capacity-replacement",
        ))
        with self.assertRaises(HTTPException):
            resolve_receiving_exception(second_exception, ReceivingExceptionResolutionCreate(
                quantity=1, resolution="REPLACED_BY_RECEIPT", reason="No capacity",
                related_receipt_id=receipt["id"], idempotency_key="reverse-capacity-overclaim",
            ))
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM receiving_exception_resolutions WHERE related_receipt_id=?",
                (receipt["id"],),
            ).fetchone()[0], 1)

    def test_changed_backorder_content_conflicts_without_duplicate_audit(self):
        original = BackorderEventCreate(
            event_kind="DECLARED", backordered_quantity=2, reason="Supplier confirmed",
            notes="Original", supplier_reference="BO-1", idempotency_key="backorder-change",
        )
        record_backorder_event(self.item_id, original)
        self.assertTrue(record_backorder_event(self.item_id, original)["replayed"])
        with self.assertRaises(HTTPException):
            record_backorder_event(
                self.item_id, original.model_copy(update={"notes": "Changed"})
            )
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM supplier_order_item_backorder_events WHERE order_item_id=?", (self.item_id,)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_logs WHERE action='SUPPLIER_BACKORDER_UPDATED' AND entity_id=?", (str(self.item_id),)).fetchone()[0], 1)

    def test_multiline_order_status_uses_all_accepted_lines(self):
        exception = self._exception("DAMAGED", key="multiline-exception", accepted=1)
        self.assertEqual(exception["status_after"], "PARTIAL")
        record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.item_id, quantity_received=4)],
            idempotency_key="multiline-first-complete",
        ))
        self.assertEqual(get_order(self.order["id"])["status"], "PARTIAL")
        final = record_receipt(self.order["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.other_item_id, quantity_received=1)],
            idempotency_key="multiline-final",
        ))
        self.assertEqual(final["status_after"], "RECEIVED")

    def test_whitespace_only_reasons_are_rejected(self):
        for model, values in (
            (ReceiptExceptionItem, dict(order_item_id=self.item_id, disposition="DAMAGED", quantity=1)),
            (ReceivingExceptionResolutionCreate, dict(quantity=1, resolution="CLOSED", idempotency_key="blank-resolution")),
            (BackorderEventCreate, dict(event_kind="DECLARED", backordered_quantity=1, idempotency_key="blank-backorder")),
        ):
            with self.assertRaises(ValidationError):
                model(reason="   ", **values)


if __name__ == "__main__":
    unittest.main()
