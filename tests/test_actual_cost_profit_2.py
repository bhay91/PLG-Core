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
from plg_core.admin.service import accounting_snapshot
from plg_core.database.migrations import run_migrations
from plg_core.documents import invoice_pdf
from plg_core.documents.integrity import issue_invoice_documents, verified_invoice_document
from plg_core.documents.paths import resolve_manifest_path
from plg_core.supply.models import DeliveryCreate, DeliveryItemCreate, ReceiptCreate, ReceiptItem
from plg_core.supply.service import (
    complete_delivery, create_delivery, create_orders_from_paid_invoice,
    get_order, place_order, record_actual_cost_adjustment, record_receipt,
)


ROOT = Path(__file__).resolve().parents[1]


class ActualCostProfit2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-actual-cost-2-")
        self.db_path = Path(self.temp.name) / "test.db"
        self.document_root = Path(self.temp.name) / "documents"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.doc_patch = patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)})
        self.invoice_doc_patch = patch.object(
            invoice_pdf, "DOCUMENT_ROOT", self.document_root / "Customers"
        )
        self.db_patch.start(); self.doc_patch.start(); self.invoice_doc_patch.start(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "supplier_cost_adjustments", "supplier_order_documents_manifest",
                "delivery_documents_manifest", "delivery_items", "deliveries",
                "receiving_documents_manifest", "receiving_event_items", "receiving_events",
                "supplier_order_items", "supplier_orders", "invoice_documents_manifest",
                "invoice_events", "invoice_items", "invoices", "quote_items", "quotes",
                "customer_transactions", "job_timeline", "audit_logs", "suppliers",
                "job_assets", "requested_needs", "machines", "jobs", "customers",
            ):
                c.execute(f'DELETE FROM "{table}"')
            c.execute("PRAGMA foreign_keys=ON"); c.commit()
        self.job_id, self.invoice_id = self._invoice()

    def tearDown(self):
        self.invoice_doc_patch.stop(); self.db_patch.stop(); self.doc_patch.stop(); self.temp.cleanup()

    def _invoice(self):
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,active) VALUES ('CAM-C','Synthetic Cost Customer',1)").lastrowid
            job = c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('CAM-J','2026-08-15',?,'Synthetic Cost Customer','CONFIRMED')", (customer,)).lastrowid
            quote = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,supplier_total,profit_total) VALUES ('CAM-Q',?,'2026-08-15','CONVERTED',150,90,60)", (job,)).lastrowid
            invoice = c.execute("INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,supplier_total,profit_total,balance_due) VALUES ('CAM-INV',?,?,'2026-08-15','PAID',150,90,60,0)", (quote, job)).lastrowid
            for supplier, description, cost in (("Supplier A", "Synthetic Cost Part", 90),):
                c.execute("INSERT INTO invoice_items(invoice_id,quantity,description,supplier_name,supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total,line_profit) VALUES (?,1,?,?,?,?,150,150,60)", (invoice, description, supplier, cost, cost))
            c.commit()
        return int(job), int(invoice)

    def _placed(self, invoice_id=None):
        order = create_orders_from_paid_invoice(invoice_id or self.invoice_id)[0]
        return get_order(place_order(order["id"], actor="Tester", request_id=f"place-{order['id']}")["id"])

    def _adjust(self, order, amount, request_id="actual-1", **overrides):
        values = dict(
            cost_kind="ITEM", new_amount=amount,
            supplier_order_item_id=order["items"][0]["id"], reason="Supplier invoice",
            actor="Synthetic Tester", request_id=request_id, supplier_reference="SUP-REF-1",
        )
        values.update(overrides)
        return record_actual_cost_adjustment(order["id"], **values)

    def test_01_migration_is_additive_repeatable_and_clean(self):
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            self.assertIn("actual_unit_cost", {r[1] for r in c.execute("PRAGMA table_info(supplier_order_items)")})
            self.assertIn("actual_shipping_total", {r[1] for r in c.execute("PRAGMA table_info(supplier_orders)")})
            self.assertEqual(c.execute("SELECT COUNT(*) FROM schema_migrations WHERE migration_id='0048_actual_supplier_costs'").fetchone()[0], 1)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM supplier_cost_adjustments a "
                    "LEFT JOIN supplier_orders o ON o.id=a.supplier_order_id "
                    "WHERE o.id IS NULL"
                ).fetchone()[0], 0
            )

    def test_02_synthetic_higher_and_lower_actual_cost_math(self):
        order = self._placed(); self._adjust(order, 115)
        row = accounting_snapshot()["invoice_reconciliation"][0]
        self.assertEqual((row["booked_supplier_cost"], row["placed_supplier_cost"], row["actual_supplier_cost"]), (90, 90, 115))
        self.assertEqual((row["cost_variance"], row["expected_profit"], row["placed_cost_profit"], row["actual_profit"], row["profit_variance"]), (25, 60, 60, 35, -25))
        self._adjust(get_order(order["id"]), 80, request_id="actual-2")
        row = accounting_snapshot()["invoice_reconciliation"][0]
        self.assertEqual((row["cost_variance"], row["actual_profit"], row["profit_variance"]), (-10, 70, 10))

    def test_03_adjustment_attribution_history_and_idempotency(self):
        order = self._placed(); self._adjust(order, 115)
        self._adjust(get_order(order["id"]), 115)
        with closing(legacy_app.get_connection()) as c:
            rows = c.execute("SELECT * FROM supplier_cost_adjustments WHERE supplier_order_id=?", (order["id"],)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["reason"], rows[0]["actor"], rows[0]["request_id"], rows[0]["supplier_reference"]), ("Supplier invoice", "Synthetic Tester", "actual-1", "SUP-REF-1"))
            audit = c.execute("SELECT * FROM audit_logs WHERE action='SUPPLIER_ACTUAL_COST_ADJUSTED'").fetchone()
            self.assertEqual((audit["actor"], audit["request_id"]), ("Synthetic Tester", "actual-1"))

    def test_04_guards_and_post_placement_statuses(self):
        draft = create_orders_from_paid_invoice(self.invoice_id)[0]; order = get_order(draft["id"])
        with self.assertRaises(HTTPException): self._adjust(order, 115)
        place_order(order["id"], actor="Tester", request_id="place")
        for missing in ("reason", "actor", "request_id"):
            overrides={missing: ""}
            if missing != "request_id":
                overrides["request_id"] = f"missing-{missing}"
            with self.assertRaises(HTTPException): self._adjust(get_order(order["id"]), 115, **overrides)
        self._adjust(get_order(order["id"]), 115, request_id="ordered")
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE supplier_orders SET status='PARTIAL' WHERE id=?", (order["id"],)); c.commit()
        self._adjust(get_order(order["id"]), 114, request_id="partial")
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE supplier_orders SET status='RECEIVED' WHERE id=?", (order["id"],)); c.commit()
        self._adjust(get_order(order["id"]), 113, request_id="received")
        self.assertEqual(get_order(order["id"])["items"][0]["actual_unit_cost"], 113)

    def test_05_shipping_and_partial_confirmation(self):
        order = self._placed()
        self._adjust(order, 115)
        row = accounting_snapshot()["invoice_reconciliation"][0]
        self.assertEqual(row["actual_cost_state"], "PARTIALLY_CONFIRMED")
        record_actual_cost_adjustment(order["id"], cost_kind="SHIPPING", new_amount=5, reason="Freight invoice", actor="Synthetic Tester", request_id="ship-1", supplier_reference="FRT-1")
        order = get_order(order["id"]); row = accounting_snapshot()["invoice_reconciliation"][0]
        self.assertTrue(order["actual_shipping_confirmed"])
        self.assertEqual((order["shipping_total"], order["actual_shipping_total"], row["actual_supplier_cost"], row["actual_cost_state"]), (0, 5, 120, "CONFIRMED"))

    def test_06_multi_supplier_independence(self):
        with closing(legacy_app.get_connection()) as c:
            c.execute("INSERT INTO invoice_items(invoice_id,quantity,description,supplier_name,supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total,line_profit) VALUES (?,1,'Second','Supplier B',10,10,20,20,10)", (self.invoice_id,)); c.execute("UPDATE invoices SET customer_total=170,supplier_total=100,profit_total=70 WHERE id=?", (self.invoice_id,)); c.commit()
        orders = create_orders_from_paid_invoice(self.invoice_id)
        for order in orders: place_order(order["id"], actor="Tester", request_id=f"place-{order['id']}")
        first = get_order(orders[0]["id"]); self._adjust(first, 115)
        second = get_order(orders[1]["id"])
        self.assertTrue(get_order(first["id"])["items"][0]["actual_confirmed"])
        self.assertFalse(second["items"][0]["actual_confirmed"])
        self.assertEqual(accounting_snapshot()["invoice_reconciliation"][0]["actual_cost_state"], "PARTIALLY_CONFIRMED")

    def test_07_invoice_and_supplier_po_are_preserved(self):
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE invoices SET customer_total=1000,supplier_total=600,profit_total=400 WHERE id=?", (self.invoice_id,))
            c.execute("UPDATE invoice_items SET supplier_unit_cost=500,supplier_line_total=500,customer_unit_price=1000,customer_line_total=1000,line_profit=500 WHERE invoice_id=?", (self.invoice_id,))
            invoice, items = legacy_app.load_invoice(c, self.invoice_id)
            issued = issue_invoice_documents(c, invoice, items, variant="PAID")
            c.commit()
        order = self._placed()
        with closing(legacy_app.get_connection()) as c:
            invoice_before = tuple(c.execute("SELECT customer_total,supplier_total,profit_total,status,balance_due FROM invoices WHERE id=?", (self.invoice_id,)).fetchone())
            po_before = tuple(c.execute("SELECT unit_cost,line_cost FROM supplier_order_items WHERE id=?", (order["items"][0]["id"],)).fetchone())
            manifest = c.execute("SELECT * FROM supplier_order_documents_manifest WHERE supplier_order_id=?", (order["id"],)).fetchone(); manifest_before = dict(manifest)
            file_before = hashlib.sha256(resolve_manifest_path(manifest["file_path"]).read_bytes()).hexdigest()
            customer_manifests_before = [dict(r) for r in c.execute("SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND audience='CUSTOMER' ORDER BY id", (self.invoice_id,))]
            customer_hashes_before = [hashlib.sha256(resolve_manifest_path(r["file_path"]).read_bytes()).hexdigest() for r in customer_manifests_before]
            original_internal = dict(c.execute("SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND audience='INTERNAL' AND is_current=1", (self.invoice_id,)).fetchone())
        record_actual_cost_adjustment(order["id"], cost_kind="SHIPPING", new_amount=0, reason="No freight", actor="Synthetic Tester", request_id="ship-confirmed")
        self._adjust(get_order(order["id"]), 550)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(tuple(c.execute("SELECT customer_total,supplier_total,profit_total,status,balance_due FROM invoices WHERE id=?", (self.invoice_id,)).fetchone()), invoice_before)
            self.assertEqual(tuple(c.execute("SELECT unit_cost,line_cost FROM supplier_order_items WHERE id=?", (order["items"][0]["id"],)).fetchone()), po_before)
            self.assertEqual(dict(c.execute("SELECT * FROM supplier_order_documents_manifest WHERE supplier_order_id=?", (order["id"],)).fetchone()), manifest_before)
            self.assertEqual(hashlib.sha256(resolve_manifest_path(manifest_before["file_path"]).read_bytes()).hexdigest(), file_before)
            self.assertEqual([dict(r) for r in c.execute("SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND audience='CUSTOMER' ORDER BY id", (self.invoice_id,))], customer_manifests_before)
            self.assertEqual([hashlib.sha256(resolve_manifest_path(r["file_path"]).read_bytes()).hexdigest() for r in customer_manifests_before], customer_hashes_before)
            internal = [dict(r) for r in c.execute("SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND audience='INTERNAL' ORDER BY version", (self.invoice_id,))]
            self.assertEqual([r["version"] for r in internal], [1, 2, 3])
            self.assertEqual([r["is_current"] for r in internal], [0, 0, 1])
            self.assertEqual(internal[0]["file_path"], original_internal["file_path"])
            current_path = verified_invoice_document(c, self.invoice_id, "INTERNAL_INVOICE_PAID", "INTERNAL")
            self.assertEqual(current_path, resolve_manifest_path(internal[-1]["file_path"]))
            self.assertEqual(hashlib.sha256(current_path.read_bytes()).hexdigest(), internal[-1]["sha256"])
        text = subprocess.run(["pdftotext", str(current_path), "-"], check=True, capture_output=True, text=True).stdout
        self.assertIn("$550.00", text)
        self.assertIn("$450.00", text)
        customer_before = Path(issued["customer"]).read_bytes()
        self._adjust(get_order(order["id"]), 570, request_id="actual-2")
        with closing(legacy_app.get_connection()) as c:
            versions = c.execute("SELECT version,is_current,file_path,sha256 FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind='INTERNAL_INVOICE_PAID' AND audience='INTERNAL' ORDER BY version", (self.invoice_id,)).fetchall()
            self.assertEqual([r["version"] for r in versions], [1, 2, 3, 4])
            self.assertEqual([r["is_current"] for r in versions], [0, 0, 0, 1])
            self.assertEqual(len({r["file_path"] for r in versions}), 4)
            for row in versions:
                self.assertEqual(hashlib.sha256(resolve_manifest_path(row["file_path"]).read_bytes()).hexdigest(), row["sha256"])
        self.assertEqual(Path(issued["customer"]).read_bytes(), customer_before)

    def test_07b_document_failure_rolls_back_actual_cost(self):
        order = self._placed()
        with patch("plg_core.documents.invoice_pdf.build_invoice_pdf", side_effect=RuntimeError("render failed")):
            with self.assertRaisesRegex(HTTPException, "could not be generated"):
                self._adjust(order, 115)
        with closing(legacy_app.get_connection()) as c:
            self.assertIsNone(c.execute("SELECT actual_unit_cost FROM supplier_order_items WHERE id=?", (order["items"][0]["id"],)).fetchone()[0])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM supplier_cost_adjustments WHERE supplier_order_id=?", (order["id"],)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoice_documents_manifest WHERE invoice_id=? AND audience='INTERNAL'", (self.invoice_id,)).fetchone()[0], 0)

    def test_08_receiving_and_delivery_do_not_change_actual_cost(self):
        order = self._placed(); self._adjust(order, 115); order = get_order(order["id"])
        before = order["items"][0]["actual_unit_cost"]
        record_receipt(order["id"], ReceiptCreate(items=[ReceiptItem(order_item_id=order["items"][0]["id"], quantity_received=1)], receiver="Receiver", idempotency_key="receipt"))
        delivery = create_delivery(self.job_id, DeliveryCreate(items=[DeliveryItemCreate(order_item_id=order["items"][0]["id"], quantity=1)], recipient="Customer", idempotency_key="delivery"))
        complete_delivery(delivery["id"], actor="Tester", request_id="deliver")
        self.assertEqual(get_order(order["id"])["items"][0]["actual_unit_cost"], before)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM supplier_cost_adjustments WHERE supplier_order_id=?", (order["id"],)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
