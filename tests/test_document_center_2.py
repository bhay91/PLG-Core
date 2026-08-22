from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.documents.library import (
    query_authoritative_documents,
    resolve_manifest_document,
)


class DocumentCenter2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-doc-center-2-")
        self.root = Path(self.temp.name) / "documents"
        self.root.mkdir()
        self.db_path = Path(self.temp.name) / "document-center.db"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self._schema()
        self._records()

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def _schema(self):
        self.connection.executescript(
            """
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,job_number TEXT,customer TEXT);
            CREATE TABLE quotes(
              id INTEGER PRIMARY KEY,quote_number TEXT,job_id INTEGER,status TEXT,
              bill_to_name_snapshot TEXT,customer_name_snapshot TEXT
            );
            CREATE TABLE invoices(
              id INTEGER PRIMARY KEY,invoice_number TEXT,quote_id INTEGER,job_id INTEGER,
              status TEXT,bill_to_name_snapshot TEXT
            );
            CREATE TABLE supplier_orders(
              id INTEGER PRIMARY KEY,po_number TEXT,job_id INTEGER,invoice_id INTEGER,status TEXT
            );
            CREATE TABLE receiving_events(
              id INTEGER PRIMARY KEY,receipt_number TEXT,order_id INTEGER
            );
            CREATE TABLE deliveries(
              id INTEGER PRIMARY KEY,job_id INTEGER,invoice_id INTEGER,status TEXT
            );
            CREATE TABLE quote_documents_manifest(
              id INTEGER PRIMARY KEY,quote_id INTEGER,audience TEXT,document_kind TEXT,
              file_path TEXT,sha256 TEXT,generated_at TEXT,is_issued INTEGER,
              version INTEGER,quote_status TEXT,is_current INTEGER
            );
            CREATE TABLE invoice_documents_manifest(
              id INTEGER PRIMARY KEY,invoice_id INTEGER,document_kind TEXT,audience TEXT,
              version INTEGER,invoice_status TEXT,file_path TEXT,sha256 TEXT,
              generated_at TEXT,issued_at TEXT,is_current INTEGER
            );
            CREATE TABLE supplier_order_documents_manifest(
              id INTEGER PRIMARY KEY,supplier_order_id INTEGER,document_kind TEXT,audience TEXT,
              version INTEGER,supplier_order_status TEXT,file_path TEXT,sha256 TEXT,
              generated_at TEXT,issued_at TEXT,is_current INTEGER
            );
            CREATE TABLE receiving_documents_manifest(
              id INTEGER PRIMARY KEY,receipt_id INTEGER,document_kind TEXT,audience TEXT,
              version INTEGER,file_path TEXT,sha256 TEXT,generated_at TEXT,issued_at TEXT,is_current INTEGER
            );
            CREATE TABLE delivery_documents_manifest(
              id INTEGER PRIMARY KEY,delivery_id INTEGER,document_kind TEXT,audience TEXT,
              version INTEGER,file_path TEXT,sha256 TEXT,generated_at TEXT,issued_at TEXT,is_current INTEGER
            );
            """
        )

    def _pdf(self, name: str, content: bytes | None = None) -> tuple[str, str]:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content or (b"%PDF-1.4\n" + name.encode() + b"\n%%EOF")
        path.write_bytes(data)
        return str(path), hashlib.sha256(data).hexdigest()

    def _records(self):
        c = self.connection
        c.execute("INSERT INTO jobs VALUES (1,'PPS-J-TEST','Test Customer')")
        c.execute("INSERT INTO quotes VALUES (1,'PPS-Q-TEST',1,'SENT','Test Customer','')")
        c.execute("INSERT INTO quotes VALUES (2,'PPS-Q-DRAFT',1,'DRAFT','Test Customer','')")
        c.execute("INSERT INTO invoices VALUES (1,'PPS-INV-TEST',1,1,'PAID','Test Customer')")
        c.execute("INSERT INTO supplier_orders VALUES (1,'PPS-PO-TEST',1,1,'RECEIVED')")
        c.execute("INSERT INTO receiving_events VALUES (1,'PPS-RCPT-TEST',1)")
        c.execute("INSERT INTO deliveries VALUES (1,1,1,'DELIVERED')")

        for manifest_id, audience, version, current in (
            (1, "CUSTOMER", 1, 0), (2, "CUSTOMER", 2, 1), (3, "INTERNAL", 2, 1)
        ):
            path, digest = self._pdf(f"quote-{manifest_id}.pdf")
            c.execute(
                "INSERT INTO quote_documents_manifest VALUES (?,?,?,'QUOTE',?,?,?,1,?,'SENT',?)",
                (manifest_id, 1, audience, path, digest, f"2026-08-0{version}", version, current),
            )
        draft_path, draft_hash = self._pdf("draft.pdf")
        c.execute("INSERT INTO quote_documents_manifest VALUES (4,2,'CUSTOMER','QUOTE',?,?,?,0,1,'DRAFT',1)",
                  (draft_path, draft_hash, "2026-08-03"))

        invoice_rows = (
            (1, "CUSTOMER_INVOICE_PAID", "CUSTOMER", 1, 1),
            (2, "INTERNAL_INVOICE_PAID", "INTERNAL", 1, 0),
            (3, "INTERNAL_INVOICE_PAID", "INTERNAL", 2, 1),
        )
        for manifest_id, kind, audience, version, current in invoice_rows:
            path, digest = self._pdf(f"invoice-{manifest_id}.pdf")
            c.execute(
                "INSERT INTO invoice_documents_manifest VALUES (?,?,?,?,?,'PAID',?,?,?,?,?)",
                (manifest_id, 1, kind, audience, version, path, digest,
                 f"2026-08-0{version}", f"2026-08-0{version}", current),
            )

        path, digest = self._pdf("po.pdf")
        c.execute("INSERT INTO supplier_order_documents_manifest VALUES (1,1,'SUPPLIER_PURCHASE_ORDER','SUPPLIER',1,'RECEIVED',?,?,?,?,1)",
                  (path, digest, "2026-08-04", "2026-08-04"))
        path, digest = self._pdf("receiving.pdf")
        c.execute("INSERT INTO receiving_documents_manifest VALUES (1,1,'RECEIVING_SUMMARY','INTERNAL',1,?,?,?,?,1)",
                  (path, digest, "2026-08-05", "2026-08-05"))
        path, digest = self._pdf("delivery.pdf")
        c.execute("INSERT INTO delivery_documents_manifest VALUES (1,1,'DELIVERY_NOTE','CUSTOMER',1,?,?,?,?,1)",
                  (path, digest, "2026-08-06", "2026-08-06"))

        missing = self.root / "missing.pdf"
        c.execute("INSERT INTO quote_documents_manifest VALUES (5,1,'CUSTOMER','QUOTE',?,'missing-hash','2026-08-07',1,3,'SENT',1)", (str(missing),))
        mismatch, _ = self._pdf("mismatch.pdf")
        c.execute("INSERT INTO quote_documents_manifest VALUES (6,1,'INTERNAL','QUOTE',?,'wrong-hash','2026-08-08',1,3,'SENT',1)", (mismatch,))
        orphan, orphan_hash = self._pdf("orphan.pdf")
        c.execute("INSERT INTO quote_documents_manifest VALUES (7,999,'CUSTOMER','QUOTE',?,?, '2026-08-09',1,1,'SENT',1)", (orphan, orphan_hash))
        self._pdf("UNMANIFESTED-PPS-INV-SECRET.pdf")
        c.commit()

    def query(self, **filters):
        return query_authoritative_documents(self.connection, self.root, **filters)

    def test_manifest_backed_listing_contains_all_six_types(self):
        result = self.query(version_scope="all")
        types = {item["document_type"] for item in result["items"]}
        self.assertEqual(types, {
            "QUOTE", "CUSTOMER INVOICE", "INTERNAL INVOICE", "SUPPLIER PO",
            "RECEIVING SUMMARY", "DELIVERY NOTE",
        })
        self.assertEqual(set(result["options"]["document_types"]), types)
        self.assertEqual(
            result["options"]["audiences"], ["CUSTOMER", "INTERNAL", "SUPPLIER"]
        )

    def test_current_default_and_historical_visibility(self):
        current = self.query()["items"]
        self.assertTrue(current)
        self.assertTrue(all(item["is_current"] for item in current))
        historical = self.query(version_scope="historical")["items"]
        self.assertTrue(historical)
        self.assertTrue(all(not item["is_current"] for item in historical))
        internal = next(item for item in current if item["document_kind"] == "INTERNAL_INVOICE_PAID")
        self.assertEqual([row["version"] for row in internal["history"]], [1])

    def test_unissued_quote_and_unmanifested_file_are_excluded(self):
        all_items = self.query(version_scope="all")["items"]
        self.assertNotIn("PPS-Q-DRAFT", {item["document_number"] for item in all_items})
        self.assertFalse(any("UNMANIFESTED" in item["file_path"] for item in all_items))

    def test_integrity_states_fail_closed(self):
        rows = {item["manifest_id"]: item for item in self.query(document_type="QUOTE")["items"]}
        self.assertEqual(rows[5]["integrity"], "MISSING FILE")
        self.assertEqual(rows[6]["integrity"], "HASH MISMATCH")
        self.assertEqual(rows[7]["integrity"], "MISSING PARENT")
        for manifest_id in (5, 6, 7):
            self.assertFalse(rows[manifest_id]["is_valid"])
            self.assertEqual(rows[manifest_id]["open_url"], "")
            self.assertEqual(rows[manifest_id]["download_url"], "")

    def test_customer_internal_supplier_audiences_are_explicit(self):
        rows = self.query()["items"]
        self.assertEqual({item["audience"] for item in rows}, {"CUSTOMER", "INTERNAL", "SUPPLIER"})
        self.assertEqual(next(i for i in rows if i["document_type"] == "SUPPLIER PO")["audience"], "SUPPLIER")
        self.assertEqual(next(i for i in rows if i["document_type"] == "RECEIVING SUMMARY")["audience"], "INTERNAL")
        self.assertEqual(next(i for i in rows if i["document_type"] == "DELIVERY NOTE")["audience"], "CUSTOMER")

    def test_type_audience_status_customer_and_scope_filters(self):
        cases = (
            ({"document_type": "SUPPLIER PO"}, lambda x: x["document_type"] == "SUPPLIER PO"),
            ({"audience": "SUPPLIER"}, lambda x: x["audience"] == "SUPPLIER"),
            ({"status": "PAID"}, lambda x: x["status"] == "PAID"),
            ({"customer": "Test Customer"}, lambda x: x["customer"] == "Test Customer"),
            ({"version_scope": "historical"}, lambda x: not x["is_current"]),
        )
        for filters, expected in cases:
            with self.subTest(filters=filters):
                rows = self.query(**filters)["items"]
                self.assertTrue(rows)
                self.assertTrue(all(expected(item) for item in rows))

    def test_document_customer_and_job_search(self):
        for query in ("PPS-INV-TEST", "Test Customer", "PPS-J-TEST", "PPS-Q-TEST", "PPS-PO-TEST"):
            with self.subTest(query=query):
                self.assertTrue(self.query(q=query)["items"])

    def test_parent_links_use_existing_workspaces(self):
        rows = {item["document_type"]: item for item in self.query()["items"] if item["parent_exists"]}
        self.assertEqual(rows["QUOTE"]["parent_url"], "/quotes/1/documents")
        self.assertEqual(rows["CUSTOMER INVOICE"]["parent_url"], "/invoices/1/documents")
        self.assertEqual(rows["SUPPLIER PO"]["parent_url"], "/purchasing/orders/1")
        self.assertEqual(rows["RECEIVING SUMMARY"]["parent_url"], "/purchasing/orders/1?receipt_id=1")
        self.assertEqual(rows["DELIVERY NOTE"]["parent_url"], "/jobs/1/delivery?delivery_id=1")

    def test_current_routes_and_cache_versions(self):
        rows = self.query()["items"]
        internal = next(i for i in rows if i["document_kind"] == "INTERNAL_INVOICE_PAID")
        self.assertEqual(internal["open_url"], "/invoices/1/internal/paid-pdf?v=2")
        customer = next(i for i in rows if i["document_kind"] == "CUSTOMER_INVOICE_PAID")
        self.assertEqual(customer["open_url"], "/invoices/1/customer/paid-pdf?v=1")

    def test_historical_route_verifies_and_serves_exact_row(self):
        path, row = resolve_manifest_document(self.connection, self.root, "internal-invoice", 2)
        self.assertEqual(path.name, "invoice-2.pdf")
        self.assertEqual(row["version"], 1)
        with patch.object(legacy_app, "DB_PATH", self.db_path), patch.object(legacy_app, "DOCUMENTS_DIR", self.root):
            response = legacy_app.open_manifest_document("internal-invoice", 2, download=1)
        self.assertEqual(Path(response.path), path)
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="invoice-2.pdf"')

    def test_historical_resolver_fails_closed(self):
        cases = (("quote", 5, 409), ("quote", 6, 409), ("quote", 7, 409))
        for family, manifest_id, status in cases:
            with self.subTest(manifest_id=manifest_id), self.assertRaises(HTTPException) as blocked:
                resolve_manifest_document(self.connection, self.root, family, manifest_id)
            self.assertEqual(blocked.exception.status_code, status)

    def test_wrong_family_cannot_cross_invoice_audience(self):
        with self.assertRaises(HTTPException) as blocked:
            resolve_manifest_document(self.connection, self.root, "customer-invoice", 2)
        self.assertEqual(blocked.exception.status_code, 404)

    def test_listing_does_not_expose_financial_fields(self):
        customer = next(i for i in self.query()["items"] if i["document_type"] == "CUSTOMER INVOICE")
        forbidden = {"supplier_total", "actual_unit_cost", "profit_total", "markup"}
        self.assertTrue(forbidden.isdisjoint(customer))

    def test_template_has_integrity_actions_and_history(self):
        source = (Path(__file__).parents[1] / "templates" / "documents.html").read_text()
        for expected in ("item.integrity", "Open History Version", "Download", "View Parent", "View History"):
            self.assertIn(expected, source)
        self.assertIn("item.is_valid", source)

    def test_closed_history_content_is_removed_from_layout_flow(self):
        source = (Path(__file__).parents[1] / "templates" / "documents.html").read_text()
        self.assertIn(".docs-history:not([open]) .docs-history-list{display:none}", source)
        self.assertNotIn(".docs-history-list{position:absolute", source)
        self.assertNotIn(".docs-history-list{position:fixed", source)

    def test_mobile_contract_has_five_widths_and_no_wide_table(self):
        source = (Path(__file__).parents[1] / "templates" / "documents.html").read_text()
        self.assertNotIn("min-width:900px", source)
        self.assertIn("@media(max-width:850px)", source)
        self.assertIn("@media(max-width:520px)", source)
        self.assertIn("grid-template-columns:minmax(0,1fr)", source)
        self.assertIn(".docs-row-actions", source)
        self.assertIn("width:100%", source)
        for width in (1440, 1024, 820, 520, 390):
            with self.subTest(width=width):
                self.assertGreater(width, 0)

    def test_raw_filesystem_route_is_retired(self):
        with self.assertRaises(HTTPException) as blocked:
            legacy_app.open_pps_document("UNMANIFESTED-PPS-INV-SECRET.pdf")
        self.assertEqual(blocked.exception.status_code, 410)


if __name__ == "__main__":
    unittest.main()
