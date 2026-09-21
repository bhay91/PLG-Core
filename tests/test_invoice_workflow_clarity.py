from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request
from jinja2 import Environment, FileSystemLoader

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.research.branding import manufacturer_identity


ROOT = Path(__file__).resolve().parents[1]


class InvoiceWorkflowClarityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-invoice-workflow-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        self.document_root = root / "documents"
        self.document_root.mkdir()
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db_path),
            patch.object(legacy_app, "DOCUMENTS_DIR", self.document_root),
            patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)}),
        ]
        for item in self.patches:
            item.start()
        legacy_app.initialize_database()
        run_migrations()
        self._seed_fixture()

    def _seed_fixture(self):
        with legacy_app.get_connection() as connection:
            for job_id, number in ((7, "PPS-J-0007"), (12, "PPS-J-0012")):
                connection.execute("INSERT INTO jobs(id,job_number,created_date,customer,company,status) VALUES (?,?,?,?,?,'INVOICED')", (job_id, number, "2026-01-01", "Synthetic Invoice Customer", "Synthetic Invoice Co"))
            for quote_id, number, job_id in ((7, "SYN-Q-0007", 7), (12, "SYN-Q-0012", 12)):
                connection.execute("INSERT INTO quotes(id,quote_number,job_id,quote_date,status) VALUES (?,?,?,DATE('now'),'ISSUED')", (quote_id, number, job_id))
            for invoice_id, number, job_id, status in ((8, "PPS-INV-0009", 7, "PAID"), (9, "PPS-INV-0012", 12, "PAID")):
                connection.execute("INSERT INTO invoices(id,invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due,bill_to_name_snapshot,bill_to_company_snapshot) VALUES (?,?,?,?,DATE('now'),?,?,?, ?,?)", (invoice_id, number, job_id, job_id, status, 100.0, 0.0, "Synthetic Invoice Customer", "Synthetic Invoice Co"))
            connection.execute("INSERT INTO invoice_documents_manifest(invoice_id,document_kind,audience,invoice_status,file_path,sha256) VALUES (8,'CUSTOMER_INVOICE','CUSTOMER','PAID','missing-synthetic-invoice.pdf','bad')")
            for po_id in (1, 2):
                connection.execute("INSERT INTO supplier_orders(id,po_number,job_id,invoice_id,supplier_name,status,parts_total,order_total) VALUES (?,?,?,?,?,?,?,?)", (po_id, f"SYN-PO-{po_id}", 7, 8, "Synthetic Supplier", "PLACED", 50.0, 50.0))
            connection.commit()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_directory_paid_actions_follow_supplier_order_state(self):
        response = legacy_app.list_invoices(self._request("/invoices"), "paid")
        self.assertEqual(response.status_code, 200)
        html = response.body.decode()
        with_orders = re.search(
            r'<article[^>]*data-search="[^"]*PPS-INV-0009[\s\S]*?</article>', html
        ).group(0)
        without_orders = re.search(
            r'<article[^>]*data-search="[^"]*PPS-INV-0012[\s\S]*?</article>', html
        ).group(0)
        self.assertIn("Open Supplier Orders", with_orders)
        self.assertIn("/purchasing?invoice_id=8", with_orders)
        self.assertIn("Create Supplier Order", without_orders)
        self.assertIn("/invoices/9/documents", without_orders)
        self.assertNotIn("Create Internal Parts Order Worksheet</strong>", html)
        self.assertEqual(self._supplier_order_count(), 2)

    def test_unpaid_directory_action_is_payment_focused(self):
        with legacy_app.get_connection() as connection:
            connection.execute(
                "UPDATE invoices SET status='UNPAID',balance_due=customer_total WHERE id=9"
            )
            connection.commit()
        response = legacy_app.list_invoices(self._request("/invoices"), "active")
        self.assertEqual(response.status_code, 200)
        html = response.body.decode()
        self.assertIn("Record Payment", html)
        self.assertIn("Open &amp; Record Payment", html)
        self.assertNotIn("Create Supplier Order", html)
        self.assertEqual(self._supplier_order_count(), 2)

    def test_integrity_failure_is_fail_closed_with_operator_context(self):
        response = legacy_app.invoice_documents(
            self._request("/invoices/8/documents"), 8
        )
        self.assertEqual(response.status_code, 409)
        html = response.body.decode()
        self.assertIn("INVOICE ACCESS BLOCKED", html)
        self.assertIn("PPS-INV-0009", html)
        self.assertIn("PPS-J-0007", html)
        self.assertIn("Customer Invoice is unavailable", html)
        self.assertIn("was not regenerated or treated as valid", html)
        self.assertIn("Open Document Center", html)
        self.assertNotIn('action="/invoices/8/payments"', html)
        self.assertEqual(self._supplier_order_count(), 2)

    def test_templates_keep_jmd_secondary_and_worksheet_available(self):
        env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
        env.globals["url_for"] = lambda name, path="": path
        env.globals["manufacturer_identity"] = manufacturer_identity
        source = (ROOT / "templates" / "invoice_documents.html").read_text()
        self.assertLess(source.index('id="record-payment"'), source.index("invoice-jmd-display"))
        self.assertIn("show_jmd_total", source)
        self.assertIn("jmd_exchange_rate", source)
        self.assertIn("Additional Invoice &amp; Purchasing Documents", source)
        self.assertIn("Internal Parts Order Worksheet", source)
        env.get_template("invoice_documents.html")
        env.get_template("invoice_integrity_error.html")

    def test_jcc_uses_same_paid_no_order_task_without_get_emulation(self):
        service = (ROOT / "plg_core" / "jobs" / "service.py").read_text()
        template = (ROOT / "templates" / "job_command_center_legacy.html").read_text()
        self.assertIn('"Create Supplier Order"', service)
        self.assertIn('f"/jobs/{int(job[\'id\'])}/fulfillment/order"', service)
        self.assertIn('"POST"', service)
        self.assertIn("operational_snapshot.workflow.action_method == 'POST'", template)

    def _supplier_order_count(self):
        with legacy_app.get_connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM supplier_orders").fetchone()[0])

    @staticmethod
    def _request(path):
        return Request({
            "type": "http", "method": "GET", "path": path,
            "root_path": "", "scheme": "http", "server": ("testserver", 80),
            "app": legacy_app.app, "headers": [],
        })


if __name__ == "__main__":
    unittest.main()
