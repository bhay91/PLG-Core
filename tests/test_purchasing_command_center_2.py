from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Request

import legacy_app
from plg_core.supply.service import (
    get_purchasing_operational_snapshot,
    list_purchasing_operational_snapshots,
)


ROOT = Path(__file__).resolve().parents[1]


class PurchasingCommandCenter2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-purchasing-command-2-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        self.document_root = root / "documents"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db_path),
            patch.object(legacy_app, "DOCUMENTS_DIR", self.document_root),
            patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)}),
        ]
        for item in self.patches:
            item.start()
        self.configure_fixture()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def snapshot(self, order_id=9001):
        return get_purchasing_operational_snapshot(
            order_id, document_root=self.document_root
        )

    def configure_fixture(self):
        purchase_document = (
            self.document_root / "Supplier Orders" / "Synthetic Supplier" / "PPS-PO-9001.pdf"
        )
        purchase_document.parent.mkdir(parents=True)
        purchase_document.write_bytes(b"%PDF-1.4\n% Hermetic supplier purchase fixture\n%%EOF\n")
        purchase_digest = hashlib.sha256(purchase_document.read_bytes()).hexdigest()

        with closing(self.connection()) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript(
                """
                DELETE FROM delivery_items;
                DELETE FROM receiving_event_items;
                DELETE FROM receiving_documents_manifest;
                DELETE FROM receiving_events;
                DELETE FROM supplier_order_documents_manifest;
                DELETE FROM supplier_cost_adjustments;
                DELETE FROM supplier_order_items;
                DELETE FROM supplier_orders;
                DELETE FROM invoice_items WHERE invoice_id=9001 OR id BETWEEN 9001 AND 9007;
                DELETE FROM invoices WHERE id=9001 OR invoice_number='PPS-INV-9001';
                DELETE FROM quotes WHERE id=9001 OR quote_number='PPS-Q-9001';
                DELETE FROM jobs WHERE id=9001 OR job_number='PPS-J-9001';

                INSERT INTO jobs (id,job_number,created_date,customer,company,status)
                VALUES (9001,'PPS-J-9001','2026-08-14','Synthetic Purchasing Customer','','INVOICED');

                INSERT INTO quotes (
                    id,quote_number,job_id,quote_date,status,
                    customer_total,supplier_total,profit_total
                ) VALUES (9001,'PPS-Q-9001',9001,'2026-08-14','CONVERTED',900,550,350);

                INSERT INTO invoices (
                    id,invoice_number,quote_id,job_id,invoice_date,status,
                    customer_total,supplier_total,profit_total,balance_due,
                    bill_to_name_snapshot
                ) VALUES (
                    9001,'PPS-INV-9001',9001,9001,'2026-08-14','PAID',
                    900,550,350,0,'Synthetic Purchasing Customer'
                );

                INSERT INTO invoice_items (
                    id,invoice_id,quantity,description,supplier_name,supplier_part_number,
                    supplier_unit_cost,customer_unit_price,supplier_line_total,
                    customer_line_total,line_profit
                ) VALUES
                    (9001,9001,2,'Synthetic Test Part A','Synthetic Supplier','TEST-PART-A',100,150,200,300,100),
                    (9002,9001,1,'Synthetic Test Part B','Synthetic Supplier','TEST-PART-B',50,100,50,100,50),
                    (9003,9001,1,'Synthetic Test Part C','Synthetic Supplier B','TEST-PART-C',60,100,60,100,40),
                    (9004,9001,1,'Synthetic Test Part D','Synthetic Supplier B','TEST-PART-D',60,100,60,100,40),
                    (9005,9001,1,'Synthetic Test Part E','Synthetic Supplier B','TEST-PART-E',60,100,60,100,40),
                    (9006,9001,1,'Synthetic Test Part F','Synthetic Supplier B','TEST-PART-F',60,100,60,100,40),
                    (9007,9001,1,'Synthetic Test Part G','Synthetic Supplier B','TEST-PART-G',60,100,60,100,40);

                INSERT INTO supplier_orders (
                    id,po_number,job_id,invoice_id,supplier_name,status,
                    parts_total,shipping_total,order_total,ordered_at,actual_shipping_total
                ) VALUES
                    (9001,'PPS-PO-9001',9001,9001,'Synthetic Supplier','ORDERED',250,0,250,'2026-08-14 19:35:31',0),
                    (9002,'PPS-PO-9002',9001,9001,'Synthetic Supplier B','ORDERED',200,0,200,'2026-08-14 17:29:37',0);

                INSERT INTO supplier_order_items (
                    id,order_id,invoice_item_id,description,supplier_part_number,
                    quantity_ordered,quantity_received,unit_cost,line_cost,actual_unit_cost
                ) VALUES
                    (9001,9001,9001,'Synthetic Test Part A','TEST-PART-A',2,0,100,200,110),
                    (9002,9001,9002,'Synthetic Test Part B','TEST-PART-B',1,0,50,50,60),
                    (9003,9002,9003,'Synthetic Test Part C','TEST-PART-C',1,0,40,40,40),
                    (9004,9002,9004,'Synthetic Test Part D','TEST-PART-D',1,0,40,40,40),
                    (9005,9002,9005,'Synthetic Test Part E','TEST-PART-E',1,0,40,40,40),
                    (9006,9002,9006,'Synthetic Test Part F','TEST-PART-F',1,0,40,40,40),
                    (9007,9002,9007,'Synthetic Test Part G','TEST-PART-G',1,0,40,40,40);

                INSERT INTO supplier_cost_adjustments (
                    supplier_order_id,supplier_order_item_id,cost_kind,
                    old_amount,new_amount,reason,actor,request_id
                ) VALUES (9001,9001,'ITEM',100,110,'Hermetic actual-cost fixture','test','fixture-adjustment');
                """
            )
            connection.execute(
                """
                INSERT INTO supplier_order_documents_manifest (
                    supplier_order_id,document_kind,audience,version,
                    supplier_order_status,file_path,sha256,is_current
                ) VALUES (9001,'SUPPLIER_PURCHASE_ORDER','SUPPLIER',1,'ORDERED',?,?,1)
                """,
                (str(purchase_document), purchase_digest),
            )
            connection.commit()

    def set_status(self, status, received=0):
        with closing(self.connection()) as connection:
            connection.execute("UPDATE supplier_orders SET status=? WHERE id=9001", (status,))
            connection.execute("UPDATE supplier_order_items SET quantity_received=? WHERE order_id=9001", (received,))
            connection.commit()

    @staticmethod
    def request(cookie=""):
        headers = [(b"cookie", f"pps_csrf_token={cookie}".encode())] if cookie else []
        return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})

    def test_01_operational_snapshot_aggregation(self):
        self.assertEqual(self.snapshot()["identity"]["po_number"], "PPS-PO-9001")

    def test_02_draft_state(self):
        self.set_status("DRAFT")
        result = self.snapshot()
        self.assertEqual(result["status"]["current"], "DRAFT")
        self.assertEqual(result["costs"]["draft_total"], 250.0)

    def test_03_ordered_state(self):
        self.assertEqual(self.snapshot()["status"]["current"], "ORDERED")

    def test_04_partial_state(self):
        self.set_status("PARTIAL", 1)
        self.assertEqual(self.snapshot()["status"]["current"], "PARTIAL")

    def test_05_received_state(self):
        with closing(self.connection()) as connection:
            connection.execute("UPDATE supplier_orders SET status='RECEIVED' WHERE id=9001")
            connection.execute("UPDATE supplier_order_items SET quantity_received=quantity_ordered WHERE order_id=9001")
            connection.commit()
        self.assertEqual(self.snapshot()["movement"]["remaining"], 0)

    def test_06_next_action(self):
        self.assertEqual(self.snapshot()["status"]["next_action"], "Receive incoming supplier parts")

    def test_07_booked_cost(self):
        self.assertEqual(self.snapshot()["costs"]["booked_cost"], 250.0)

    def test_08_placed_cost(self):
        self.assertEqual(self.snapshot()["costs"]["placed_total"], 250.0)

    def test_09_final_actual_cost(self):
        self.assertEqual(self.snapshot()["costs"]["final_actual_cost"], 280.0)

    def test_10_actual_confirmation_state(self):
        self.assertEqual(self.snapshot()["costs"]["actual_cost_state"], "CONFIRMED")

    def test_11_variance(self):
        self.assertEqual(self.snapshot()["costs"]["variance_vs_booked"], 30.0)

    def test_12_ordered_received_remaining(self):
        self.assertEqual(self.snapshot()["movement"], {"ordered": 3, "received": 0, "remaining": 3, "delivered": 0, "available_to_deliver": 0})

    def test_13_delivered_and_available(self):
        result = self.snapshot()["movement"]
        self.assertEqual((result["delivered"], result["available_to_deliver"]), (0, 0))

    def test_14_document_integrity(self):
        self.assertEqual(self.snapshot()["documents"]["supplier_po"]["integrity"], "VALID")

    def test_15_receipt_history(self):
        self.assertIsInstance(self.snapshot()["receipts"], list)

    def test_16_actual_cost_history(self):
        self.assertGreaterEqual(len(self.snapshot()["actual_cost_adjustments"]), 1)

    def test_17_related_links(self):
        links = self.snapshot()["links"]
        self.assertEqual(links["job"], "/jobs/9001/basket")
        self.assertIn("PPS-INV-9001", links["accounting"])

    def test_18_multi_supplier_comparison(self):
        first, second = self.snapshot(9001), self.snapshot(9002)
        self.assertEqual((first["identity"]["supplier"], second["identity"]["supplier"]), ("Synthetic Supplier", "Synthetic Supplier B"))

    def test_19_invoice_filter(self):
        result = list_purchasing_operational_snapshots(invoice_id=9001)
        self.assertEqual({row["id"] for row in result["orders"]}, {9001, 9002})

    def test_20_supplier_filter(self):
        result = list_purchasing_operational_snapshots(supplier="Synthetic Supplier B")
        self.assertEqual([row["id"] for row in result["orders"]], [9002])

    def test_21_status_filter(self):
        self.assertTrue(all(row["status"]["current"] == "ORDERED" for row in list_purchasing_operational_snapshots(status="ORDERED")["orders"]))

    def test_22_customer_filter(self):
        result = list_purchasing_operational_snapshots(customer="Synthetic Purchasing")
        self.assertEqual({row["id"] for row in result["orders"]}, {9001, 9002})

    def test_23_job_filter(self):
        result = list_purchasing_operational_snapshots(job="PPS-J-9001")
        self.assertEqual({row["id"] for row in result["orders"]}, {9001, 9002})

    def test_24_csrf_draft_edit_protection(self):
        self.set_status("DRAFT")
        with closing(self.connection()) as connection:
            item = connection.execute("SELECT id FROM supplier_order_items WHERE order_id=9001 LIMIT 1").fetchone()[0]
        with self.assertRaises(HTTPException) as error:
            legacy_app.update_supplier_order_item_cost_web(self.request("good"), 9001, item, 1.0, "bad")
        self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as error:
            legacy_app.update_supplier_order_web(self.request("good"), 9001, 0, "", "", "bad")
        self.assertEqual(error.exception.status_code, 403)

    def test_25_valid_draft_edit_still_succeeds(self):
        self.set_status("DRAFT")
        with closing(self.connection()) as connection:
            item = connection.execute("SELECT id FROM supplier_order_items WHERE order_id=9001 LIMIT 1").fetchone()[0]
        token = "valid-purchasing-draft-csrf-token"
        response = legacy_app.update_supplier_order_item_cost_web(self.request(token), 9001, item, 123.45, token)
        self.assertEqual(response.status_code, 303)
        response = legacy_app.update_supplier_order_web(self.request(token), 9001, 4.25, "2026-08-20", "CSRF fixture", token)
        self.assertEqual(response.status_code, 303)
        with closing(self.connection()) as connection:
            self.assertEqual(connection.execute("SELECT unit_cost FROM supplier_order_items WHERE id=?", (item,)).fetchone()[0], 123.45)
            self.assertEqual(connection.execute("SELECT shipping_total FROM supplier_orders WHERE id=9001").fetchone()[0], 4.25)

    def test_26_synthetic_authoritative_values(self):
        first, second = self.snapshot(9001), self.snapshot(9002)
        self.assertEqual((first["costs"]["booked_cost"], first["costs"]["placed_total"], first["costs"]["final_actual_cost"], first["costs"]["variance_vs_booked"]), (250.0, 250.0, 280.0, 30.0))
        self.assertEqual((second["costs"]["booked_cost"], second["costs"]["placed_total"], second["costs"]["final_actual_cost"], second["costs"]["variance_vs_booked"]), (300.0, 200.0, 200.0, -100.0))

    def test_27_no_duplicate_state_creation(self):
        tables = ("supplier_orders", "supplier_order_items", "receiving_events", "supplier_cost_adjustments", "audit_logs", "job_timeline")
        with closing(self.connection()) as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        self.snapshot(); self.snapshot()
        with closing(self.connection()) as connection:
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
