from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.database.migrations import (
    _migration_0051_invoice_jmd_presentation,
    run_migrations,
)
from plg_core.documents import invoice_pdf
from plg_core.documents.integrity import issue_invoice_documents


ROOT = Path(__file__).resolve().parents[1]


class InvoiceJmdPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-jmd-")
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.env_patch = patch.dict(
            os.environ,
            {"PPS_DOCUMENT_ROOT": str(self.root / "documents")},
        )
        self.pdf_patch = patch.object(
            invoice_pdf,
            "DOCUMENT_ROOT",
            self.root / "documents" / "Customers",
        )
        self.db_patch.start()
        self.env_patch.start()
        self.pdf_patch.start()
        run_migrations()
        self._fixture()

    def tearDown(self):
        self.pdf_patch.stop()
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def _fixture(self):
        with closing(self.connection()) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "invoice_documents_manifest", "invoice_events", "invoice_items",
                "invoices", "quote_items", "quotes", "audit_logs", "jobs",
            ):
                connection.execute(f"DELETE FROM {table}")
            job_id = connection.execute(
                """INSERT INTO jobs (
                     job_number,created_date,customer,company,phone,email,address,status
                   ) VALUES ('TEST-J-JMD','2026-08-29','JMD Test Customer','',
                             '','','Kingston, Jamaica','CONFIRMED')"""
            ).lastrowid
            quote_id = connection.execute(
                """INSERT INTO quotes (
                     quote_number,job_id,quote_date,status,parts_subtotal,
                     customer_total,supplier_total,profit_total
                   ) VALUES ('TEST-Q-JMD',?,'2026-08-29','APPROVED',786,786,400,386)""",
                (job_id,),
            ).lastrowid
            self.invoice_id = connection.execute(
                """INSERT INTO invoices (
                     invoice_number,quote_id,job_id,invoice_date,status,
                     parts_subtotal,customer_total,supplier_total,profit_total,
                     balance_due,bill_to_name_snapshot,bill_to_address_snapshot
                   ) VALUES ('TEST-I-JMD',?,?,'2026-08-29','UNPAID',
                             786,786,400,386,786,'JMD Test Customer',
                             'Kingston, Jamaica')""",
                (quote_id, job_id),
            ).lastrowid
            connection.execute(
                """INSERT INTO invoice_items (
                     invoice_id,quantity,description,supplier_unit_cost,
                     customer_unit_price,supplier_line_total,
                     customer_line_total,line_profit
                   ) VALUES (?,1,'Test part',400,786,400,786,386)""",
                (self.invoice_id,),
            )
            connection.commit()

    def _invoice(self):
        with closing(self.connection()) as connection:
            return legacy_app.load_invoice(connection, self.invoice_id)

    @staticmethod
    def _text(path):
        return subprocess.run(
            ["pdftotext", str(path), "-"], check=True,
            capture_output=True, text=True,
        ).stdout

    def test_migration_defaults_preserve_existing_invoice(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE invoices (id INTEGER PRIMARY KEY, customer_total REAL NOT NULL)"
        )
        connection.execute("INSERT INTO invoices VALUES (1,786.00)")
        _migration_0051_invoice_jmd_presentation(connection)
        row = connection.execute("SELECT * FROM invoices").fetchone()
        self.assertEqual(row["show_jmd_total"], 0)
        self.assertIsNone(row["jmd_exchange_rate"])
        self.assertEqual(row["customer_total"], 786.00)
        connection.close()

    def test_validation_rejects_invalid_rates(self):
        for value in ("", "0", "-1", "NaN", "Infinity", "not-a-rate"):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                legacy_app.save_invoice_jmd_display(self.invoice_id, "1", value)
        invoice, _ = self._invoice()
        self.assertEqual(invoice["show_jmd_total"], 0)
        self.assertIsNone(invoice["jmd_exchange_rate"])

    def test_customer_jmd_snapshot_versions_without_changing_usd(self):
        invoice, items = self._invoice()
        original_total = invoice["customer_total"]
        original_balance = invoice["balance_due"]
        with closing(self.connection()) as connection:
            first = issue_invoice_documents(connection, invoice, items)
            connection.commit()
        original_hash = hashlib.sha256(Path(first["customer"]).read_bytes()).hexdigest()

        response = legacy_app.save_invoice_jmd_display(self.invoice_id, "1", "160")
        self.assertEqual(response.status_code, 303)
        with closing(self.connection()) as connection:
            invoice = connection.execute(
                "SELECT * FROM invoices WHERE id=?", (self.invoice_id,)
            ).fetchone()
            manifests = connection.execute(
                """SELECT audience,version,is_current,file_path
                   FROM invoice_documents_manifest
                   WHERE invoice_id=? ORDER BY audience,version""",
                (self.invoice_id,),
            ).fetchall()
            audit = connection.execute(
                """SELECT * FROM audit_logs
                   WHERE action='INVOICE_JMD_DISPLAY_UPDATED'
                   ORDER BY id DESC LIMIT 1"""
            ).fetchone()
        self.assertEqual(invoice["customer_total"], original_total)
        self.assertEqual(invoice["balance_due"], original_balance)
        self.assertEqual(invoice["jmd_exchange_rate"], "160")
        self.assertEqual(invoice["show_jmd_total"], 1)
        self.assertIsNotNone(audit)
        customer = [row for row in manifests if row["audience"] == "CUSTOMER"]
        internal = [row for row in manifests if row["audience"] == "INTERNAL"]
        self.assertEqual([row["version"] for row in customer], [1, 2])
        self.assertEqual([row["is_current"] for row in customer], [0, 1])
        self.assertEqual([row["version"] for row in internal], [1])
        self.assertEqual(
            hashlib.sha256(Path(first["customer"]).read_bytes()).hexdigest(),
            original_hash,
        )

        customer_text = self._text(self.root / customer[-1]["file_path"])
        internal_text = self._text(first["internal"])
        self.assertIn("Invoice Total (USD)", customer_text)
        self.assertIn("JMD Total", customer_text)
        self.assertIn("J$125,760.00", customer_text)
        self.assertIn("1 USD = 160 JMD", customer_text)
        self.assertNotIn("Sourcing Fee", customer_text)
        self.assertNotIn("Service Charge", customer_text)
        self.assertNotIn("JMD Total", internal_text)

    def test_disabled_omits_jmd_and_rate_change_creates_new_version(self):
        invoice, items = self._invoice()
        with closing(self.connection()) as connection:
            first = issue_invoice_documents(connection, invoice, items)
            connection.commit()
        self.assertNotIn("JMD Total", self._text(first["customer"]))

        legacy_app.save_invoice_jmd_display(self.invoice_id, "1", "160")
        legacy_app.save_invoice_jmd_display(self.invoice_id, "1", "160.5")
        with closing(self.connection()) as connection:
            rows = connection.execute(
                """SELECT version,is_current,file_path
                   FROM invoice_documents_manifest
                   WHERE invoice_id=? AND audience='CUSTOMER'
                   ORDER BY version""",
                (self.invoice_id,),
            ).fetchall()
        self.assertEqual([row["version"] for row in rows], [1, 2, 3])
        self.assertEqual([row["is_current"] for row in rows], [0, 0, 1])
        current_text = self._text(self.root / rows[-1]["file_path"])
        self.assertIn("J$126,153.00", current_text)
        self.assertIn("1 USD = 160.5 JMD", current_text)


if __name__ == "__main__":
    unittest.main()
