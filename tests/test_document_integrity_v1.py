from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from plg_core.database.migrations import _migration_0044_document_integrity
from plg_core.documents import invoice_pdf, quote_pdf
from plg_core.documents.integrity import (
    issue_invoice_documents,
    issue_quote_documents,
    verified_invoice_document,
)


class DocumentIntegrityV1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._legacy_schema()
        _migration_0044_document_integrity(self.connection)
        self.quote_patch = patch.object(
            quote_pdf, "DOCUMENT_ROOT", self.root / "documents" / "Customers"
        )
        self.invoice_patch = patch.object(
            invoice_pdf, "DOCUMENT_ROOT", self.root / "documents" / "Customers"
        )
        self.quote_patch.start()
        self.invoice_patch.start()

    def tearDown(self):
        self.invoice_patch.stop()
        self.quote_patch.stop()
        self.connection.close()
        self.temp.cleanup()

    def _legacy_schema(self):
        self.connection.executescript(
            """
            CREATE TABLE quotes (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE invoices (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE quote_documents_manifest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quote_id INTEGER NOT NULL,
                audience TEXT NOT NULL CHECK (audience IN ('CUSTOMER','INTERNAL')),
                document_kind TEXT NOT NULL DEFAULT 'QUOTE',
                file_path TEXT NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_issued INTEGER NOT NULL DEFAULT 0 CHECK (is_issued IN (0,1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(quote_id,audience,document_kind,is_issued),
                FOREIGN KEY(quote_id) REFERENCES quotes(id)
            );
            """
        )

    @staticmethod
    def _quote(status="SENT"):
        return {
            "id": 1, "quote_number": "TEST-Q-0001", "quote_date": "2026-08-14",
            "status": status, "customer": "Document Test", "company": "",
            "address": "Test Address", "phone": "", "email": "",
            "manufacturer": "CAT", "machine": "420D", "pin_serial": "TESTPIN",
            "parts_subtotal": 150.0, "shipping_total": 0.0,
            "service_charge": 0.0, "sourcing_fee": 0.0,
            "customer_total": 150.0, "supplier_total": 73.21,
            "profit_total": 76.79,
        }

    @staticmethod
    def _invoice(status="UNPAID"):
        return {
            "id": 1, "invoice_number": "TEST-I-0001", "invoice_date": "2026-08-14",
            "status": status, "customer": "Document Test", "company": "",
            "address": "Test Address", "phone": "", "email": "",
            "manufacturer": "CAT", "machine": "420D", "pin_serial": "TESTPIN",
            "parts_subtotal": 150.0, "shipping_total": 0.0,
            "service_charge": 0.0, "sourcing_fee": 0.0,
            "customer_total": 150.0, "supplier_total": 73.21,
            "profit_total": 76.79, "balance_due": 150.0,
            "credit_applied": 0.0,
        }

    @staticmethod
    def _items():
        return [{
            "description": "Filter", "quantity": 1,
            "supplier_name": "CONFIDENTIAL SUPPLIER",
            "supplier_part_number": "PART-1", "internal_part_number": "INT-1",
            "supplier_unit_cost": 73.21, "supplier_line_total": 73.21,
            "customer_unit_price": 150.0, "customer_line_total": 150.0,
            "line_profit": 76.79,
        }]

    def test_migration_preserves_legacy_rows_and_hashes(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.executescript(
            """
            CREATE TABLE quotes (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE invoices (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE quote_documents_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT, quote_id INTEGER NOT NULL,
            audience TEXT NOT NULL, document_kind TEXT NOT NULL DEFAULT 'QUOTE',
            file_path TEXT NOT NULL, sha256 TEXT NOT NULL DEFAULT '',
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_issued INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(quote_id,audience,document_kind,is_issued),
            FOREIGN KEY(quote_id) REFERENCES quotes(id));
            INSERT INTO quotes(id,status) VALUES (9,'SENT');
            INSERT INTO quote_documents_manifest(
                quote_id,audience,file_path,sha256,is_issued
            ) VALUES (9,'CUSTOMER','old.pdf','abc123',1);
            """
        )
        before = dict(c.execute("SELECT * FROM quote_documents_manifest").fetchone())
        _migration_0044_document_integrity(c)
        after = c.execute("SELECT * FROM quote_documents_manifest").fetchone()
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["sha256"], "abc123")
        self.assertEqual(after["version"], 1)
        self.assertEqual(after["quote_status"], "")
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
        c.close()

    def test_sent_quote_creates_version_two_and_preserves_version_one(self):
        self.connection.execute("INSERT INTO quotes(id,status) VALUES (1,'SENT')")
        old = self.root / "old.pdf"
        old.write_bytes(b"historical")
        old_hash = hashlib.sha256(old.read_bytes()).hexdigest()
        for audience in ("CUSTOMER", "INTERNAL"):
            self.connection.execute(
                """INSERT INTO quote_documents_manifest(
                quote_id,audience,document_kind,file_path,sha256,is_issued,
                version,quote_status,is_current) VALUES (1,?,'QUOTE',?,?,1,1,'DRAFT',1)""",
                (audience, str(old), old_hash),
            )
        paths = issue_quote_documents(
            self.connection, self._quote(), self._items(), force_new=True
        )
        rows = self.connection.execute(
            "SELECT version,quote_status,is_current,sha256,file_path FROM quote_documents_manifest ORDER BY version,audience"
        ).fetchall()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["is_current"] == 0 for row in rows[:2]))
        self.assertTrue(all(row["version"] == 2 and row["is_current"] == 1 for row in rows[2:]))
        self.assertTrue(all(row["quote_status"] == "SENT" for row in rows[2:]))
        for path in paths.values():
            text = subprocess.run(
                ["pdftotext", path, "-"], check=True, capture_output=True, text=True
            ).stdout.upper()
            self.assertIn("SENT", text)

    def test_quote_issue_requires_persisted_sent_status(self):
        self.connection.execute("INSERT INTO quotes(id,status) VALUES (1,'DRAFT')")
        with self.assertRaises(HTTPException) as blocked:
            issue_quote_documents(
                self.connection, self._quote("DRAFT"), self._items()
            )
        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM quote_documents_manifest"
            ).fetchone()[0],
            0,
        )

    def test_invoice_issue_hashes_and_customer_internal_separation(self):
        self.connection.execute("INSERT INTO invoices(id,status) VALUES (1,'UNPAID')")
        paths = issue_invoice_documents(
            self.connection, self._invoice(), self._items(), variant="ISSUED"
        )
        rows = self.connection.execute(
            "SELECT * FROM invoice_documents_manifest ORDER BY audience"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            path = Path(row["file_path"])
            self.assertEqual(row["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        customer = subprocess.run(
            ["pdftotext", paths["customer"], "-"], check=True,
            capture_output=True, text=True,
        ).stdout.upper()
        internal = subprocess.run(
            ["pdftotext", paths["internal"], "-"], check=True,
            capture_output=True, text=True,
        ).stdout.upper()
        self.assertNotIn("CONFIDENTIAL SUPPLIER", customer)
        self.assertNotIn("$73.21", customer)
        self.assertNotIn("PROFIT", customer)
        self.assertIn("CONFIDENTIAL", internal)
        self.assertIn("SUPPLIER", internal)
        self.assertIn("$73.21", internal)
        self.assertIn("PROFIT", internal)

    def test_missing_and_corrupted_invoice_documents_fail_closed(self):
        self.connection.execute("INSERT INTO invoices(id,status) VALUES (1,'UNPAID')")
        issue_invoice_documents(
            self.connection, self._invoice(), self._items(), variant="ISSUED"
        )
        customer = self.connection.execute(
            "SELECT file_path FROM invoice_documents_manifest WHERE audience='CUSTOMER'"
        ).fetchone()[0]
        Path(customer).unlink()
        with self.assertRaises(HTTPException) as missing:
            verified_invoice_document(
                self.connection, 1, "CUSTOMER_INVOICE", "CUSTOMER"
            )
        self.assertEqual(missing.exception.status_code, 409)

        internal = self.connection.execute(
            "SELECT file_path FROM invoice_documents_manifest WHERE audience='INTERNAL'"
        ).fetchone()[0]
        Path(internal).write_bytes(b"corrupt")
        with self.assertRaises(HTTPException) as corrupt:
            verified_invoice_document(
                self.connection, 1, "INTERNAL_INVOICE", "INTERNAL"
            )
        self.assertEqual(corrupt.exception.status_code, 409)

    def test_paid_variant_does_not_overwrite_original(self):
        self.connection.execute("INSERT INTO invoices(id,status) VALUES (1,'UNPAID')")
        issued = issue_invoice_documents(
            self.connection, self._invoice(), self._items(), variant="ISSUED"
        )
        original_hashes = {
            key: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for key, path in issued.items()
        }
        paid_invoice = self._invoice("PAID")
        paid_invoice["balance_due"] = 0.0
        paid = issue_invoice_documents(
            self.connection, paid_invoice, self._items(), variant="PAID"
        )
        customer_text = subprocess.run(
            ["pdftotext", paid["customer"], "-"], check=True,
            capture_output=True, text=True,
        ).stdout.upper()
        self.assertIn("PAID IN FULL", customer_text)
        self.assertIn("BALANCE DUE", customer_text)
        self.assertIn("$0.00", customer_text)
        self.assertNotEqual(issued, paid)
        for key, path in issued.items():
            self.assertEqual(
                hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                original_hashes[key],
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM invoice_documents_manifest"
            ).fetchone()[0],
            4,
        )

    def test_void_variant_does_not_overwrite_original(self):
        self.connection.execute("INSERT INTO invoices(id,status) VALUES (1,'UNPAID')")
        issued = issue_invoice_documents(
            self.connection, self._invoice(), self._items(), variant="ISSUED"
        )
        original_hashes = {
            key: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for key, path in issued.items()
        }
        void_invoice = self._invoice("VOID")
        void_paths = issue_invoice_documents(
            self.connection, void_invoice, self._items(), variant="VOID"
        )
        self.assertTrue(all("VOID" in Path(path).stem for path in void_paths.values()))
        for key, path in issued.items():
            self.assertEqual(
                hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                original_hashes[key],
            )


if __name__ == "__main__":
    unittest.main()
