import sqlite3
import unittest

from jinja2 import Template

from plg_core.documents.integrity import current_invoice_document_versions


class InvoiceDocumentCacheVersionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE invoice_documents_manifest (
                id INTEGER PRIMARY KEY, invoice_id INTEGER, document_kind TEXT,
                audience TEXT, version INTEGER, is_current INTEGER
            )"""
        )

    def tearDown(self):
        self.connection.close()

    def _insert(self, kind, audience, version, current=1):
        self.connection.execute(
            "INSERT INTO invoice_documents_manifest(invoice_id,document_kind,audience,version,is_current) VALUES (8,?,?,?,?)",
            (kind, audience, version, current),
        )

    def test_paid_internal_cache_key_tracks_current_v2_then_v3(self):
        self._insert("CUSTOMER_INVOICE_PAID", "CUSTOMER", 1)
        self._insert("INTERNAL_INVOICE_PAID", "INTERNAL", 1, 0)
        self._insert("INTERNAL_INVOICE_PAID", "INTERNAL", 2)
        versions = current_invoice_document_versions(self.connection, 8, "PAID")
        self.assertEqual(versions, {
            "customer_document_version": 1,
            "internal_document_version": 2,
        })
        link = Template(
            "/invoices/{{ invoice.id }}/internal/paid-pdf"
            "?v={{ invoice.internal_document_version }}"
        )
        self.assertEqual(
            link.render(invoice={"id": 8, **versions}),
            "/invoices/8/internal/paid-pdf?v=2",
        )

        self.connection.execute(
            "UPDATE invoice_documents_manifest SET is_current=0 WHERE invoice_id=8 AND document_kind='INTERNAL_INVOICE_PAID' AND audience='INTERNAL'"
        )
        self._insert("INTERNAL_INVOICE_PAID", "INTERNAL", 3)
        versions = current_invoice_document_versions(self.connection, 8, "PAID")
        self.assertEqual(versions["internal_document_version"], 3)
        self.assertEqual(
            link.render(invoice={"id": 8, **versions}),
            "/invoices/8/internal/paid-pdf?v=3",
        )

    def test_unpaid_uses_issued_document_family(self):
        self._insert("CUSTOMER_INVOICE", "CUSTOMER", 4)
        self._insert("INTERNAL_INVOICE", "INTERNAL", 5)
        self._insert("CUSTOMER_INVOICE_PAID", "CUSTOMER", 9)
        self._insert("INTERNAL_INVOICE_PAID", "INTERNAL", 9)
        self.assertEqual(
            current_invoice_document_versions(self.connection, 8, "UNPAID"),
            {"customer_document_version": 4, "internal_document_version": 5},
        )


if __name__ == "__main__":
    unittest.main()
