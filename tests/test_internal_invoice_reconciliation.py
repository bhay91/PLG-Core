from contextlib import closing
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.documents import invoice_pdf
from plg_core.documents.integrity import issue_invoice_documents, verified_invoice_document
from plg_core.documents.reconciliation import (
    find_stale_internal_invoice_documents,
    internal_invoice_reconciliation_status,
    reconcile_stale_internal_invoice_documents,
)
from plg_core.supply.service import get_order, record_actual_cost_adjustment


ROOT = Path(__file__).resolve().parents[1]


class InternalInvoiceReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-internal-reconcile-")
        self.db_path = Path(self.temp.name) / "test.db"
        self.document_root = Path(self.temp.name) / "documents"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.env_patch = patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)})
        self.pdf_patch = patch.object(
            invoice_pdf, "DOCUMENT_ROOT", self.document_root / "Customers"
        )
        self.db_patch.start(); self.env_patch.start(); self.pdf_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "supplier_cost_adjustments", "supplier_order_documents_manifest",
                "supplier_order_items", "supplier_orders", "invoice_documents_manifest",
                "invoice_events", "invoice_items", "invoices", "quote_items", "quotes",
                "customer_transactions", "job_timeline", "audit_logs", "suppliers",
                "job_assets", "requested_needs", "machines", "jobs", "customers",
            ):
                connection.execute(f'DELETE FROM "{table}"')
            connection.execute("PRAGMA foreign_keys=ON")
            connection.commit()

    def tearDown(self):
        self.pdf_patch.stop(); self.env_patch.stop(); self.db_patch.stop()
        self.temp.cleanup()

    def _stale_fixture(self, status="PAID"):
        suffix = status.lower()
        balance = 0 if status == "PAID" else 1000
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES (?,?,1)",
                (f"REC-{suffix}", f"Reconcile {status}"),
            ).lastrowid
            job_id = connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES (?,?,?,?,?)",
                (f"REC-J-{suffix}", "2026-08-16", customer_id, f"Reconcile {status}", "CONFIRMED"),
            ).lastrowid
            quote_id = connection.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,supplier_total,profit_total) VALUES (?,?,?,?,?,?,?)",
                (f"REC-Q-{suffix}", job_id, "2026-08-16", "CONVERTED", 1000, 600, 400),
            ).lastrowid
            invoice_id = connection.execute(
                "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,supplier_total,profit_total,balance_due) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"REC-INV-{suffix}", quote_id, job_id, "2026-08-16", status, 1000, 600, 400, balance),
            ).lastrowid
            invoice_item_id = connection.execute(
                "INSERT INTO invoice_items(invoice_id,quantity,description,supplier_name,supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total,line_profit) VALUES (?,1,'Synthetic Reconciliation Part','Fixture Supplier',500,500,1000,1000,500)",
                (invoice_id,),
            ).lastrowid
            order_id = connection.execute(
                "INSERT INTO supplier_orders(job_id,invoice_id,supplier_name,status,parts_total,shipping_total,order_total,actual_shipping_total,po_number) VALUES (?,?,?,'ORDERED',500,0,500,0,?)",
                (job_id, invoice_id, "Fixture Supplier", f"REC-PO-{suffix}"),
            ).lastrowid
            order_item_id = connection.execute(
                "INSERT INTO supplier_order_items(order_id,invoice_item_id,description,quantity_ordered,unit_cost,line_cost,actual_unit_cost) VALUES (?,?,'Synthetic Reconciliation Part',1,500,500,NULL)",
                (order_id, invoice_item_id),
            ).lastrowid
            invoice, items = legacy_app.load_invoice(connection, invoice_id)
            paths = issue_invoice_documents(
                connection, invoice, items,
                variant="PAID" if status == "PAID" else "ISSUED",
            )
            connection.execute(
                "UPDATE invoice_documents_manifest SET generated_at='2020-01-01 00:00:00',issued_at='2020-01-01 00:00:00' WHERE invoice_id=?",
                (invoice_id,),
            )
            connection.execute(
                "UPDATE supplier_order_items SET actual_unit_cost=550 WHERE id=?",
                (order_item_id,),
            )
            connection.execute(
                "INSERT INTO supplier_cost_adjustments(supplier_order_id,supplier_order_item_id,cost_kind,old_amount,new_amount,reason,actor,request_id,created_at) VALUES (?,?,'ITEM',NULL,550,'Historical actual','Fixture','historical-item','2021-01-01 00:00:00')",
                (order_id, order_item_id),
            )
            connection.execute(
                "INSERT INTO supplier_cost_adjustments(supplier_order_id,supplier_order_item_id,cost_kind,old_amount,new_amount,reason,actor,request_id,created_at) VALUES (?,NULL,'SHIPPING',NULL,0,'Historical freight','Fixture','historical-shipping','2021-01-01 00:00:01')",
                (order_id,),
            )
            connection.commit()
        return {
            "invoice_id": int(invoice_id), "order_id": int(order_id),
            "order_item_id": int(order_item_id), "paths": paths,
            "kind": "INTERNAL_INVOICE_PAID" if status == "PAID" else "INTERNAL_INVOICE",
        }

    def _apply(self, invoice_id):
        return reconcile_stale_internal_invoice_documents(
            invoice_id=invoice_id, dry_run=False,
            connection_factory=legacy_app.get_connection,
        )

    def test_stale_paid_document_is_detected(self):
        fixture = self._stale_fixture("PAID")
        with closing(legacy_app.get_connection()) as connection:
            status = internal_invoice_reconciliation_status(connection, fixture["invoice_id"])
            found = find_stale_internal_invoice_documents(connection, fixture["invoice_id"])
        self.assertTrue(status["stale"])
        self.assertEqual(status["document_kind"], "INTERNAL_INVOICE_PAID")
        self.assertEqual(status["current_version"], 1)
        self.assertEqual(len(found), 1)

    def test_placed_order_timestamp_is_a_relevant_financial_change(self):
        fixture = self._stale_fixture("PAID")
        with closing(legacy_app.get_connection()) as connection:
            connection.execute(
                "DELETE FROM supplier_cost_adjustments WHERE supplier_order_id=?",
                (fixture["order_id"],),
            )
            connection.execute(
                "UPDATE supplier_order_items SET actual_unit_cost=NULL WHERE id=?",
                (fixture["order_item_id"],),
            )
            connection.execute(
                "UPDATE supplier_orders SET actual_shipping_total=NULL WHERE id=?",
                (fixture["order_id"],),
            )
            connection.commit()
            status = internal_invoice_reconciliation_status(
                connection, fixture["invoice_id"]
            )
        self.assertTrue(status["stale"])
        self.assertIsNotNone(status["latest_financial_change_at"])

    def test_paid_reconciliation_versions_hashes_content_and_preserves_customer(self):
        fixture = self._stale_fixture("PAID")
        customer_bytes = Path(fixture["paths"]["customer"]).read_bytes()
        with closing(legacy_app.get_connection()) as connection:
            old = dict(connection.execute(
                "SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind=? AND audience='INTERNAL' AND is_current=1",
                (fixture["invoice_id"], fixture["kind"]),
            ).fetchone())
        old_bytes = Path(old["file_path"]).read_bytes()
        result = self._apply(fixture["invoice_id"])
        self.assertEqual(len(result), 1)
        with closing(legacy_app.get_connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind=? AND audience='INTERNAL' ORDER BY version",
                (fixture["invoice_id"], fixture["kind"]),
            ).fetchall()
            current_path = verified_invoice_document(
                connection, fixture["invoice_id"], fixture["kind"], "INTERNAL"
            )
        self.assertEqual([row["version"] for row in rows], [1, 2])
        self.assertEqual([row["is_current"] for row in rows], [0, 1])
        self.assertEqual(Path(old["file_path"]).read_bytes(), old_bytes)
        self.assertEqual(Path(fixture["paths"]["customer"]).read_bytes(), customer_bytes)
        self.assertEqual(str(current_path), rows[-1]["file_path"])
        self.assertEqual(hashlib.sha256(current_path.read_bytes()).hexdigest(), rows[-1]["sha256"])
        text = subprocess.run(
            ["pdftotext", str(current_path), "-"], check=True,
            capture_output=True, text=True,
        ).stdout
        self.assertIn("$550.00", text)
        self.assertIn("$450.00", text)

    def test_rerun_is_idempotent_and_future_adjustment_creates_v3(self):
        fixture = self._stale_fixture("PAID")
        self._apply(fixture["invoice_id"])
        self.assertEqual(self._apply(fixture["invoice_id"]), [])
        order = get_order(fixture["order_id"])
        record_actual_cost_adjustment(
            fixture["order_id"], cost_kind="ITEM", new_amount=570,
            supplier_order_item_id=order["items"][0]["id"], reason="Later invoice",
            actor="Fixture", request_id="later-adjustment",
        )
        with closing(legacy_app.get_connection()) as connection:
            rows = connection.execute(
                "SELECT version,is_current FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind=? AND audience='INTERNAL' ORDER BY version",
                (fixture["invoice_id"], fixture["kind"]),
            ).fetchall()
        self.assertEqual([row["version"] for row in rows], [1, 2, 3])
        self.assertEqual([row["is_current"] for row in rows], [0, 0, 1])

    def test_unpaid_reconciliation_uses_issued_internal_family(self):
        fixture = self._stale_fixture("UNPAID")
        self._apply(fixture["invoice_id"])
        with closing(legacy_app.get_connection()) as connection:
            rows = connection.execute(
                "SELECT document_kind,version,is_current FROM invoice_documents_manifest WHERE invoice_id=? AND audience='INTERNAL' ORDER BY version",
                (fixture["invoice_id"],),
            ).fetchall()
        self.assertEqual([(r["document_kind"], r["version"], r["is_current"]) for r in rows], [
            ("INTERNAL_INVOICE", 1, 0), ("INTERNAL_INVOICE", 2, 1),
        ])

    def test_generation_failure_preserves_old_current_and_financial_data(self):
        fixture = self._stale_fixture("PAID")
        with patch("plg_core.documents.invoice_pdf.build_invoice_pdf", side_effect=RuntimeError("render failed")):
            with self.assertRaisesRegex(HTTPException, "reconciliation failed"):
                self._apply(fixture["invoice_id"])
        with closing(legacy_app.get_connection()) as connection:
            manifests = connection.execute(
                "SELECT version,is_current FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind=? AND audience='INTERNAL'",
                (fixture["invoice_id"], fixture["kind"]),
            ).fetchall()
            actual = connection.execute(
                "SELECT actual_unit_cost FROM supplier_order_items WHERE id=?",
                (fixture["order_item_id"],),
            ).fetchone()[0]
        self.assertEqual([(r["version"], r["is_current"]) for r in manifests], [(1, 1)])
        self.assertEqual(actual, 550)

    def test_dry_run_does_not_create_a_version(self):
        fixture = self._stale_fixture("PAID")
        result = reconcile_stale_internal_invoice_documents(
            invoice_id=fixture["invoice_id"], dry_run=True,
            connection_factory=legacy_app.get_connection,
        )
        self.assertEqual(len(result), 1)
        with closing(legacy_app.get_connection()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind=? AND audience='INTERNAL'",
                (fixture["invoice_id"], fixture["kind"]),
            ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
