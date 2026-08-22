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
from starlette.requests import Request

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.documents.integrity import (
    preview_supplier_order_document,
    verified_supplier_order_document,
)
from plg_core.supply.models import ReceiptCreate, ReceiptItem
from plg_core.supply.service import (
    create_orders_from_paid_invoice,
    get_order,
    place_order,
    record_receipt,
    update_order,
    update_order_item_cost,
)


ROOT = Path(__file__).resolve().parents[1]


def pdf_text(path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class SupplierPODocumentIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-supplier-po-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        self.document_root = root / "documents"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.env_patch = patch.dict(
            os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)}
        )
        self.db_patch.start()
        self.env_patch.start()
        run_migrations()
        self._create_invoice()

    def tearDown(self):
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def _create_invoice(self):
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,company,active) "
                "VALUES ('PO-C','PO Customer','Customer Co',1)"
            ).lastrowid
            machine_id = connection.execute(
                "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial) "
                "VALUES (?,'PO-M','Loader','Caterpillar','420D','PIN-PO-420')",
                (customer_id,),
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,machine_id,customer,company,status) "
                "VALUES ('PO-J','2026-08-14',?,?,?,'Customer Co','CONFIRMED')",
                (customer_id, machine_id, "PO Customer"),
            ).lastrowid
            self.asset_id = connection.execute(
                "INSERT INTO job_assets(job_id,machine_id,customer_id,name,manufacturer,model,vin_pin_serial,is_primary) "
                "VALUES (?,?,?,'Loader','Caterpillar','420D','PIN-PO-420',1)",
                (self.job_id, machine_id, customer_id),
            ).lastrowid
            quote_id = connection.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total) "
                "VALUES ('PO-Q',?,'2026-08-14','CONVERTED',999.99)",
                (self.job_id,),
            ).lastrowid
            self.invoice_id = connection.execute(
                "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) "
                "VALUES ('PO-INV',?,?,'2026-08-14','PAID',999.99,0)",
                (quote_id, self.job_id),
            ).lastrowid
            connection.execute(
                "INSERT INTO suppliers(name,contact_person,phone,email,status) "
                "VALUES ('A Synthetic Supplier','Synthetic Buyer','555-0100','orders@example.test','ACTIVE') "
                "ON CONFLICT(name) DO UPDATE SET contact_person=excluded.contact_person,"
                "phone=excluded.phone,email=excluded.email,status='ACTIVE'"
            )
            supplier_a = connection.execute(
                "SELECT id FROM suppliers WHERE name='A Synthetic Supplier'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO suppliers(name,status) VALUES ('Supplier B','ACTIVE') "
                "ON CONFLICT(name) DO UPDATE SET status='ACTIVE'"
            )
            for supplier, part, quantity, cost in (
                ("A Synthetic Supplier", "TEST-PART-A", 3, 10.00),
                ("A Synthetic Supplier", "TEST-PART-B", 1, 20.00),
                ("Supplier B", "B-1", 2, 15.00),
            ):
                connection.execute(
                    """
                    INSERT INTO invoice_items(
                        invoice_id,quantity,description,supplier_name,
                        supplier_part_number,supplier_unit_cost,
                        supplier_line_total,customer_unit_price,
                        customer_line_total,job_asset_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        self.invoice_id, quantity, f"Part {part}", supplier,
                        part, cost, quantity * cost, 321.23,
                        quantity * 321.23, self.asset_id,
                    ),
                )
            connection.commit()
        self.assertGreater(supplier_a, 0)

    def _orders(self):
        return create_orders_from_paid_invoice(self.invoice_id)

    @staticmethod
    def _placement_request(
        token="", *, order_id=1, request_id="placement-request-1"
    ):
        headers = []
        if token:
            headers.append(
                (b"cookie", f"pps_csrf_token={token}".encode("ascii"))
            )
        user = type(
            "AuthenticatedUser",
            (),
            {
                "is_authenticated": True,
                "username": "purchasing.operator",
            },
        )()
        request = Request({
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": f"/purchasing/orders/{order_id}/place",
            "raw_path": f"/purchasing/orders/{order_id}/place".encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
            "user": user,
        })
        request.state.request_id = request_id
        return request

    def test_paid_invoice_grouping_and_idempotency(self):
        orders = self._orders()
        self.assertEqual([row["supplier_name"] for row in orders], ["A Synthetic Supplier", "Supplier B"])
        self.assertEqual([row["id"] for row in self._orders()], [row["id"] for row in orders])

    def test_draft_preview_uses_supplier_order_values_and_is_supplier_safe(self):
        order_id = self._orders()[0]["id"]
        item = get_order(order_id)["items"][0]
        update_order_item_cost(order_id, item["id"], 12.34)
        update_order(
            order_id, shipping_total=17.25,
            expected_at="2026-08-20", notes="Supplier confirmation CONF-77",
        )
        with closing(legacy_app.get_connection()) as connection:
            path = Path(preview_supplier_order_document(connection, order_id))
            manifest_count = connection.execute(
                "SELECT COUNT(*) FROM supplier_order_documents_manifest "
                "WHERE supplier_order_id=?",
                (order_id,),
            ).fetchone()[0]
        text = pdf_text(path)
        for expected in (
            "DRAFT", "A Synthetic Supplier", "TEST-PART-A", "$12.34", "$17.25",
            "2026", "CONF-77", "PO-J", "PO-INV", "PIN-PO-420",
        ):
            self.assertIn(expected, text)
        for forbidden in ("$321.23", "MARKUP", "MARGIN", "PROFIT", "SELL PRICE"):
            self.assertNotIn(forbidden, text.upper())
        self.assertEqual(manifest_count, 0)

    def test_ordered_issue_is_immutable_hashed_and_route_compatible(self):
        order_id = self._orders()[0]["id"]
        update_order(order_id, shipping_total=9.50, expected_at="2026-08-21", notes="REF-9")
        placed = place_order(order_id)
        self.assertEqual(placed["status"], "ORDERED")
        with closing(legacy_app.get_connection()) as connection:
            row = connection.execute(
                "SELECT * FROM supplier_order_documents_manifest WHERE supplier_order_id=?",
                (order_id,),
            ).fetchone()
            path = verified_supplier_order_document(connection, order_id)
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["supplier_order_status"], "ORDERED")
        self.assertEqual(row["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        place_order(order_id)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        text = pdf_text(path)
        for expected in (placed["po_number"], "ORDERED", "A Synthetic Supplier", "TEST-PART-A", "$9.50", "REF-9"):
            self.assertIn(expected, text)
        response = legacy_app.supplier_purchase_order_pdf(order_id, download=0)
        download = legacy_app.supplier_purchase_order_pdf(order_id, download=1)
        self.assertEqual(Path(response.path), path)
        self.assertEqual(Path(download.path), path)

    def test_web_placement_requires_csrf_and_records_request_attribution(self):
        order_id = self._orders()[0]["id"]
        with self.assertRaises(HTTPException) as missing:
            legacy_app.place_supplier_order_web(
                self._placement_request(order_id=order_id), order_id, ""
            )
        self.assertEqual(missing.exception.status_code, 403)
        with self.assertRaises(HTTPException) as invalid:
            legacy_app.place_supplier_order_web(
                self._placement_request("cookie-token", order_id=order_id),
                order_id,
                "different-form-token",
            )
        self.assertEqual(invalid.exception.status_code, 403)
        self.assertEqual(get_order(order_id)["status"], "DRAFT")

        token = "valid-placement-csrf-token"
        request = self._placement_request(token, order_id=order_id)
        legacy_app.place_supplier_order_web(request, order_id, token)
        legacy_app.place_supplier_order_web(request, order_id, token)

        with closing(legacy_app.get_connection()) as connection:
            audits = connection.execute(
                "SELECT * FROM audit_logs WHERE action='SUPPLIER_ORDER_PLACED' "
                "AND entity_type='SUPPLIER_ORDER' AND entity_id=?",
                (str(order_id),),
            ).fetchall()
            manifests = connection.execute(
                "SELECT * FROM supplier_order_documents_manifest "
                "WHERE supplier_order_id=?",
                (order_id,),
            ).fetchall()
        self.assertEqual(get_order(order_id)["status"], "ORDERED")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["actor"], "purchasing.operator")
        self.assertEqual(audits[0]["request_id"], "placement-request-1")
        self.assertIn(
            f'"source_path": "/purchasing/orders/{order_id}/place"',
            audits[0]["metadata_json"],
        )
        self.assertEqual(len(manifests), 1)

    def test_missing_and_corrupt_issued_documents_fail_closed(self):
        order_id = self._orders()[0]["id"]
        place_order(order_id)
        with closing(legacy_app.get_connection()) as connection:
            path = verified_supplier_order_document(connection, order_id)
        original = path.read_bytes()
        path.write_bytes(original + b"corrupt")
        with closing(legacy_app.get_connection()) as connection:
            with self.assertRaises(HTTPException) as error:
                verified_supplier_order_document(connection, order_id)
        self.assertEqual(error.exception.status_code, 409)
        path.unlink()
        with closing(legacy_app.get_connection()) as connection:
            with self.assertRaises(HTTPException) as error:
                verified_supplier_order_document(connection, order_id)
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(path.exists())

    def test_receiving_does_not_change_issued_document(self):
        order_id = self._orders()[0]["id"]
        place_order(order_id)
        with closing(legacy_app.get_connection()) as connection:
            path = verified_supplier_order_document(connection, order_id)
            manifest = dict(connection.execute(
                "SELECT * FROM supplier_order_documents_manifest WHERE supplier_order_id=?",
                (order_id,),
            ).fetchone())
        original = path.read_bytes()
        items = get_order(order_id)["items"]
        partial = record_receipt(order_id, ReceiptCreate(items=[
            ReceiptItem(order_item_id=items[0]["id"], quantity_received=1)
        ]))
        self.assertEqual(partial["status"], "PARTIAL")
        final = record_receipt(order_id, ReceiptCreate(items=[
            ReceiptItem(order_item_id=items[0]["id"], quantity_received=2),
            ReceiptItem(order_item_id=items[1]["id"], quantity_received=1),
        ]))
        self.assertEqual(final["status"], "RECEIVED")
        with closing(legacy_app.get_connection()) as connection:
            after = dict(connection.execute(
                "SELECT * FROM supplier_order_documents_manifest WHERE supplier_order_id=?",
                (order_id,),
            ).fetchone())
        self.assertEqual(manifest["sha256"], after["sha256"])
        self.assertEqual(original, path.read_bytes())

    def test_accounting_placed_cost_counts_orders_and_shipping_once(self):
        orders = self._orders()
        update_order(orders[0]["id"], shipping_total=7)
        update_order(orders[1]["id"], shipping_total=11)
        place_order(orders[0]["id"])
        with closing(legacy_app.get_connection()) as connection:
            placed = connection.execute(
                "SELECT COALESCE(SUM(order_total),0) FROM supplier_orders "
                "WHERE invoice_id=? AND status IN ('ORDERED','PARTIAL','RECEIVED')",
                (self.invoice_id,),
            ).fetchone()[0]
            draft = connection.execute(
                "SELECT order_total FROM supplier_orders WHERE id=?",
                (orders[1]["id"],),
            ).fetchone()[0]
        self.assertEqual(placed, 57)
        self.assertEqual(draft, 41)
        place_order(orders[1]["id"])
        with closing(legacy_app.get_connection()) as connection:
            placed = connection.execute(
                "SELECT SUM(order_total) FROM supplier_orders WHERE invoice_id=? "
                "AND status IN ('ORDERED','PARTIAL','RECEIVED')",
                (self.invoice_id,),
            ).fetchone()[0]
        self.assertEqual(placed, 98)

    def test_schema_preserves_existing_manifests_and_integrity(self):
        with closing(legacy_app.get_connection()) as connection:
            quote_rows = [tuple(row) for row in connection.execute(
                "SELECT id,sha256 FROM quote_documents_manifest ORDER BY id"
            )]
            invoice_rows = [tuple(row) for row in connection.execute(
                "SELECT id,sha256 FROM invoice_documents_manifest ORDER BY id"
            )]
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='supplier_order_documents_manifest'"
            ).fetchone())
            self.assertEqual(quote_rows, [tuple(row) for row in connection.execute(
                "SELECT id,sha256 FROM quote_documents_manifest ORDER BY id"
            )])
            self.assertEqual(invoice_rows, [tuple(row) for row in connection.execute(
                "SELECT id,sha256 FROM invoice_documents_manifest ORDER BY id"
            )])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


class SupplierPODocumentPresentationTests(unittest.TestCase):
    def test_actions_and_responsive_layout_contract(self):
        template = (ROOT / "templates" / "supplier_order_detail.html").read_text()
        css = (ROOT / "static" / "app.css").read_text()
        for label in ("Open DRAFT PO Preview", "Open Purchase Order", "Download Purchase Order"):
            self.assertIn(label, template)
        self.assertIn("supplier-order-heading-actions", template)
        self.assertIn("flex-wrap: wrap", css)
        self.assertIn("@media (max-width: 680px)", css)

    def test_parts_order_sheet_is_explicitly_non_authoritative(self):
        generator = (ROOT / "plg_core" / "documents" / "parts_order_pdf.py").read_text()
        invoice_template = (ROOT / "templates" / "invoice_documents.html").read_text()
        self.assertIn("INTERNAL PARTS ORDER WORKSHEET", generator)
        self.assertIn("Internal Parts Order Worksheet", invoice_template)


if __name__ == "__main__":
    unittest.main()
