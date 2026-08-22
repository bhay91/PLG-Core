from __future__ import annotations

import asyncio
from contextlib import closing
from io import BytesIO
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from reportlab.pdfgen import canvas
from starlette.datastructures import Headers

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.intake import attachments as attachment_service
from plg_core.intake.attachments import store_proposal_images, validate_attachments
from plg_core.intake.documents import MAX_EXTRACTED_CHARACTERS, MAX_PDF_PAGES, classify_document
from plg_core.intake.service import confirm_proposal, create_proposal, load_proposal


ROOT = Path(__file__).resolve().parents[1]


def pdf_bytes(lines: list[str], *, pages: int = 1, password: str = "") -> bytes:
    stream = BytesIO()
    document = canvas.Canvas(stream)
    if password:
        document.setEncrypt(password)
    for page in range(pages):
        y = 760
        for line in lines:
            document.drawString(36, y, line)
            y -= 14
        if page < pages - 1:
            document.showPage()
    document.save()
    return stream.getvalue()


def upload_pdf(data: bytes, name: str = "request.pdf") -> UploadFile:
    stream = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    stream.write(data)
    stream.seek(0)
    return UploadFile(stream, filename=name, headers=Headers({"content-type": "application/pdf"}))


class SmartIntake2PDFMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-intake-2-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.proposal_root = root / "proposal"
        self.request_root = root / "request"
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db_path),
            patch.object(attachment_service, "UPLOAD_ROOT", self.proposal_root),
            patch.object(attachment_service, "REQUEST_UPLOAD_ROOT", self.request_root),
        ]
        for item in self.patches:
            item.start()
        run_migrations()
        with closing(self.connection()) as connection:
            connection.execute(
                "INSERT INTO customers(customer_number,name,company,active) "
                "VALUES ('PDF-MATCH-C','Jordan Example','Example Equipment Development',1)"
            )
            connection.commit()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def proposal_from_pdf(self, lines, name="request.pdf"):
        files = asyncio.run(validate_attachments([upload_pdf(pdf_bytes(lines), name)]))
        extracted = [{
            "filename": files[0].original_filename,
            "media_type": files[0].media_type,
            "text": files[0].extracted_text,
            "extraction_status": files[0].extraction_status,
            "extraction_evidence": files[0].extraction_evidence,
            "page_count": files[0].page_count,
        }]
        with closing(self.connection()) as connection:
            proposal_id = create_proposal(connection, "", extracted_documents=extracted)
            store_proposal_images(connection, proposal_id, files)
            connection.commit()
        return proposal_id

    def test_valid_pdf_is_extracted_retained_and_customer_request_classified(self):
        proposal_id = self.proposal_from_pdf([
            "Customer: PDF Person", "John Deere 350D", "PIN PDF-350D-001",
            "Requested Parts:", "Fuel filter kit",
        ])
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, proposal_id)
            attachment = proposal["attachments"][0]
            self.assertEqual(attachment["media_type"], "application/pdf")
            self.assertEqual(attachment["extraction_status"], "PROPOSED")
            self.assertTrue(Path(attachment["stored_path"]).is_file())
            self.assertEqual(proposal["document_analysis"]["classification"]["document_type"], "CUSTOMER_REQUEST")
            self.assertIn("Fuel filter kit", proposal["source_documents"][0]["text"])

    def test_corrupt_encrypted_and_excessive_page_pdfs_are_rejected(self):
        cases = [
            upload_pdf(b"%PDF-not-a-pdf", "corrupt.pdf"),
            upload_pdf(pdf_bytes(["secret"], password="password"), "encrypted.pdf"),
            upload_pdf(pdf_bytes(["page"], pages=MAX_PDF_PAGES + 1), "too-many.pdf"),
        ]
        for item in cases:
            with self.assertRaises(HTTPException):
                asyncio.run(validate_attachments([item]))

    def test_extracted_text_is_bounded(self):
        long_line = "A" * 5000
        files = asyncio.run(validate_attachments([upload_pdf(pdf_bytes([long_line] * 30))]))
        self.assertLessEqual(len(files[0].extracted_text), MAX_EXTRACTED_CHARACTERS)

    def test_document_classification_matrix(self):
        cases = {
            "CUSTOMER_REQUEST": "Customer Request\nNeed fuel filter kit",
            "CUSTOMER_QUOTE": "QUOTE\nBill To: Customer\nQuoted Items",
            "SUPPLIER_QUOTE": "Supplier: FleetPride\nSupplier Quote\nQuote No: FP-22",
            "SUPPLIER_INVOICE": "Supplier: FleetPride\nSUPPLIER INVOICE\nInvoice No: FP-I-1",
            "PPS_QUOTE": "PPS-Q-0009",
            "PPS_INVOICE": "PPS-INV-0001",
            "OTHER": "Unclassified document content",
        }
        for expected, body in cases.items():
            self.assertEqual(classify_document(body)["document_type"], expected)

    def test_pps_documents_link_existing_records_and_unknown_number_conflicts(self):
        with closing(self.connection()) as connection:
            quote = connection.execute("SELECT quote_number FROM quotes ORDER BY id LIMIT 1").fetchone()
            invoice = connection.execute("SELECT invoice_number FROM invoices ORDER BY id LIMIT 1").fetchone()
            self.assertIsNotNone(quote)
            if invoice is None:
                source = connection.execute("SELECT id,job_id FROM quotes ORDER BY id LIMIT 1").fetchone()
                connection.execute(
                    "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status) VALUES ('PPS-INV-9000',?,?,DATE('now'),'UNPAID')",
                    (source["id"], source["job_id"]),
                )
                connection.commit()
                invoice = connection.execute("SELECT invoice_number FROM invoices WHERE invoice_number='PPS-INV-9000'").fetchone()
            for number, kind in ((quote[0], "PPS_QUOTE"), (invoice[0], "PPS_INVOICE")):
                proposal_id = create_proposal(connection, number)
                proposal = load_proposal(connection, proposal_id)
                self.assertEqual(proposal["document_analysis"]["classification"]["document_type"], kind)
                self.assertEqual(proposal["document_analysis"]["existing_document"]["number"], number)
                self.assertFalse(proposal["document_analysis"]["confirm_allowed"])
            missing = create_proposal(connection, "PPS-Q-999999")
            analysis = load_proposal(connection, missing)["document_analysis"]
            self.assertIsNone(analysis["existing_document"])
            self.assertIn("not found", " ".join(analysis["blockers"]).lower())

    def test_customer_matching_priority_and_ambiguity(self):
        with closing(self.connection()) as connection:
            first = connection.execute(
                "INSERT INTO customers(customer_number,name,company,email,phone,active) VALUES ('PPS-C-9001','Match Person','Match Co','match@example.test','(555) 900-1000',1)"
            ).lastrowid
            connection.execute("INSERT INTO customers(customer_number,name,company,active) VALUES ('PPS-C-9002','Duplicate Name','One',1)")
            connection.execute("INSERT INTO customers(customer_number,name,company,active) VALUES ('PPS-C-9003','Duplicate Name','Two',1)")
            connection.commit()
            samples = (
                ("PPS-C-9001\nMatch Person\nNeed filter", "MATCHED", first),
                ("Match Person\nmatch@example.test\nNeed filter", "MATCHED", first),
                ("Match Person\n555-900-1000\nNeed filter", "MATCHED", first),
                ("Match Person\nMatch Co\nNeed filter", "MATCHED", first),
                ("Duplicate Name\nNeed filter", "AMBIGUOUS", None),
                ("Entirely New Person\nNeed filter", "NEW", None),
            )
            for text, state, matched_id in samples:
                proposal = load_proposal(connection, create_proposal(connection, text))
                match = proposal["document_analysis"]["customer_match"]
                self.assertEqual(match["state"], state)
                self.assertEqual(match["matched_id"], matched_id)

    def test_machine_exact_conflict_cross_customer_and_multiple_candidates(self):
        with closing(self.connection()) as connection:
            customer = connection.execute("INSERT INTO customers(customer_number,name,email,active) VALUES ('PPS-C-9100','Machine Owner','owner@example.test',1)").lastrowid
            other = connection.execute("INSERT INTO customers(customer_number,name,email,active) VALUES ('PPS-C-9101','Other Owner','other@example.test',1)").lastrowid
            exact = connection.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'M-9100','350D','John Deere','350D','PIN-EXACT-9100',1)", (customer,)).lastrowid
            connection.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'M-9101','350D','John Deere','350D','PIN-OTHER-9101',1)", (other,))
            connection.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'M-9102','JCB','JCB','3CX','PIN-DUPLICATE',1)", (customer,))
            connection.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'M-9103','JCB','JCB','3CX','PIN-DUPLICATE',1)", (customer,))
            connection.commit()
            exact_proposal = load_proposal(connection, create_proposal(connection, "Machine Owner\nowner@example.test\nJohn Deere 350D\nPIN PIN-EXACT-9100\nNeeds filter"))
            self.assertEqual(exact_proposal["document_analysis"]["machine_matches"][0]["state"], "MATCHED")
            self.assertEqual(exact_proposal["assets"][0]["matched_machine_id"], exact)
            conflict = load_proposal(connection, create_proposal(connection, "Machine Owner\nowner@example.test\nJohn Deere 350D\nPIN DIFFERENT-9100\nNeeds filter"))
            self.assertEqual(conflict["document_analysis"]["machine_matches"][0]["state"], "CONFLICT")
            cross = load_proposal(connection, create_proposal(connection, "Machine Owner\nowner@example.test\nJohn Deere 350D\nPIN PIN-OTHER-9101\nNeeds filter"))
            self.assertEqual(cross["document_analysis"]["machine_matches"][0]["state"], "CONFLICT")
            duplicate = load_proposal(connection, create_proposal(connection, "Machine Owner\nowner@example.test\nJCB 3CX\nPIN PIN-DUPLICATE\nNeeds filter"))
            self.assertEqual(duplicate["document_analysis"]["machine_matches"][0]["state"], "AMBIGUOUS")

    def test_supplier_quote_is_review_only_and_extracts_evidence_without_business_writes(self):
        text = "Supplier: FleetPride\nSupplier Quote\nQuote No: FP-44\nDate: 2026-08-14\nUSD\nDER-93592 Fuel Filter Kit 2 10.00 20.00"
        with closing(self.connection()) as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("customers", "customer_requests", "jobs", "quotes", "invoices")}
            proposal_id = create_proposal(connection, text)
            proposal = load_proposal(connection, proposal_id)
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
            self.assertEqual(before, after)
            analysis = proposal["document_analysis"]
            self.assertEqual(analysis["classification"]["document_type"], "SUPPLIER_QUOTE")
            self.assertFalse(analysis["confirm_allowed"])
            self.assertEqual(analysis["supplier_document"]["supplier"], "FleetPride")
            self.assertEqual(analysis["supplier_document"]["lines"][0]["supplier_part_number"], "DER-93592")
            with self.assertRaises(HTTPException):
                confirm_proposal(connection, proposal_id, proposal["lock_version"])
            self.assertEqual(before, {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before})

    def test_supplier_invoice_81547_role_extraction_matches_bill_to_customer(self):
        proposal_id = self.proposal_from_pdf([
            "Ontrack Machinery & Parts Inc.",
            "Invoice",
            "8311 NW 64 ST #8",
            "MIAMI FL 33166",
            "TEL #3054206096",
            "FAX #3055923828",
            "Bill To",
            "Date",
            "Invoice #",
            "3/12/2026",
            "81547",
            "Ship To",
            "Example Equipment Development",
            "Jordan Example",
            "312 Fenimore Ave",
            "Uniondale, FL 11553",
            "Example Equipment Development",
            "Jordan Example",
            "312 Fenimore Ave",
            "Uniondale, FL 11553",
            "Ship Via",
            "Terms",
            "P.O. No.",
            "S.O. No.",
            "FOB",
            "61090",
            "Quantity",
            "Item",
            "1.00 332/K6848",
            "Ordered Prev. Inv.",
            "1.00",
            "0.00",
            "B/O",
            "Weight (lbs)",
            "0.00 9.26",
            "Description",
            "Rate",
            "Amount",
            "2,273.48",
            "2,273.48",
            "1461847",
            "ECU UNIT - GENUINE",
            "Transaction complete $2,432.62",
            "Invoice Number: 61090",
            "Sales Tax (0.07)",
            "$159.14",
            "Total",
            "$2,432.62",
            "Balance Due",
            "$2,432.62",
        ], "ontrack-invoice-81547.pdf")
        with closing(self.connection()) as connection:
            expected = connection.execute(
                "SELECT id,name,company FROM customers WHERE name='Jordan Example' ORDER BY id LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(expected)
            proposal = load_proposal(connection, proposal_id)
            analysis = proposal["document_analysis"]
            supplier = analysis["supplier_document"]
            customer = analysis["customer_match"]
            self.assertEqual(analysis["classification"]["document_type"], "SUPPLIER_INVOICE")
            self.assertEqual(analysis["classification"]["document_number"], "81547")
            self.assertEqual(supplier["supplier"], "Ontrack Machinery & Parts Inc.")
            self.assertEqual(supplier["supplier_invoice_number"], "81547")
            self.assertEqual(supplier["customer_person"], "Jordan Example")
            self.assertEqual(supplier["customer_company"], "Example Equipment Development")
            self.assertEqual(supplier["customer_address"], "312 Fenimore Ave\nUniondale, FL 11553")
            self.assertEqual(supplier["supplier_phone"], "3054206096")
            self.assertEqual(customer["state"], "MATCHED")
            self.assertEqual(customer["matched_id"], expected["id"])
            self.assertEqual(proposal["matched_customer_id"], expected["id"])
            self.assertEqual(proposal["contact_name"], "Jordan Example")
            self.assertEqual(proposal["company_name"], "Example Equipment Development")
            self.assertNotEqual(proposal["contact_name"], "Ontrack Machinery & Parts Inc.")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM customers WHERE name='Ontrack Machinery & Parts Inc.' OR company='Ontrack Machinery & Parts Inc.'").fetchone()[0], 0)
            self.assertIsNone(proposal["created_job_id"])
            self.assertEqual(analysis["machine_match_state"], "NOT_IDENTIFIED")
            self.assertEqual(proposal["assets"], [])
            self.assertEqual(len(supplier["lines"]), 1)
            line = supplier["lines"][0]
            self.assertEqual(line["supplier_part_number"], "332/K6848")
            self.assertIn("ECU UNIT - GENUINE", line["description"])
            self.assertEqual(line["unit_cost"], 2273.48)
            self.assertEqual(supplier["total"], 2432.62)
            self.assertFalse(analysis["confirm_allowed"])
            with self.assertRaises(HTTPException):
                confirm_proposal(connection, proposal_id, proposal["lock_version"])
        from starlette.requests import Request
        from plg_core.intake.routes import review
        scope = {"type": "http", "method": "GET", "path": f"/requests/smart-intake/proposals/{proposal_id}", "headers": [], "query_string": b"", "app": legacy_app.app, "router": legacy_app.app.router, "scheme": "http", "server": ("test", 80), "client": ("test", 1)}
        body = review(Request(scope), proposal_id).body.decode()
        for expected_text in (
            "Supplier Invoice", "Invoice #:</strong> 81547", "Ontrack Machinery &amp; Parts Inc.",
            "Jordan Example", "Example Equipment Development", "CUSTOMER MATCH · MATCHED",
            "Machine:</strong> Not identified", "332/K6848", "ECU UNIT - GENUINE",
            "$2,273.48", "$2,432.62", "Confirm &amp; Create is unavailable",
        ):
            self.assertIn(expected_text, body)

    def test_original_need_wording_and_explicit_confirmation_boundary_remain(self):
        raw = "Unique PDF Boundary Person\nJohn Deere 350D\nPIN NEW-PDF-BOUNDARY\nNeeds fuel filter kit"
        with closing(self.connection()) as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("customers", "customer_requests", "jobs", "machines", "requested_needs")}
            proposal_id = create_proposal(connection, raw)
            proposal = load_proposal(connection, proposal_id)
            self.assertEqual(proposal["assets"][0]["needs"][0]["original_wording"], "fuel filter kit")
            self.assertEqual(before, {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before})
            job_id = confirm_proposal(connection, proposal_id, proposal["lock_version"])
            self.assertIsInstance(job_id, int)

    def test_review_template_compiles_with_document_analysis(self):
        from starlette.requests import Request
        from plg_core.intake.routes import review
        with closing(self.connection()) as connection:
            proposal_id = create_proposal(connection, "PPS-Q-999999")
        scope = {"type": "http", "method": "GET", "path": f"/requests/smart-intake/proposals/{proposal_id}", "headers": [], "query_string": b"", "app": legacy_app.app, "router": legacy_app.app.router, "scheme": "http", "server": ("test", 80), "client": ("test", 1)}
        body = review(Request(scope), proposal_id).body.decode()
        self.assertIn("DOCUMENT CLASSIFICATION", body)
        self.assertIn("Confirm &amp; Create is unavailable", body)


if __name__ == "__main__":
    unittest.main()
