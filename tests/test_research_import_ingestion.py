from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.datastructures import UploadFile

from reportlab.pdfgen import canvas

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.intake.attachments import UPLOAD_ROOT
from plg_core.intake import attachments
from plg_core.intake.service import confirm_proposal, load_proposal
from plg_core.requests.routes import ingest_research_import
from plg_core.research.branding import manufacturer_identity
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


def pdf_bytes() -> bytes:
    stream = BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(36, 760, "Synthetic research import fixture")
    document.save()
    return stream.getvalue()



class ResearchImportIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-research-import-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        self.upload_root = root / "uploads"
        self.upload_root.mkdir()
        self.pdf_bytes = pdf_bytes()
        self.pdf_name = "research-package.pdf"
        # The test PDF is copied to the expected name so filename matching is exercised.
        self.pdf_source_named = root / self.pdf_name
        self.pdf_source_named.write_bytes(self.pdf_bytes)
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db_path),
            patch.object(legacy_app, "DOCUMENTS_DIR", root / "documents"),
            patch.object(legacy_app, "UPLOADS_DIR", self.upload_root),
            patch.object(attachments, "UPLOAD_ROOT", self.upload_root / "intake-proposals"),
            patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(root / "documents")}),
        ]
        for item in self.patches:
            item.start()
        legacy_app.initialize_database()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            connection.execute(
                """INSERT INTO jobs (
                       job_number, created_date, customer, company, machine, pin_serial
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                ("PPS-J-0001", "2026-01-01", "Synthetic Customer",
                 "Synthetic Co", "Synthetic Machine", "TEST-PIN-001"),
            )
            connection.commit()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _package(self, **changes):
        value = {
            "package_id": "RESEARCH-IMPORT-001",
            "source_pdf": {"filename": self.pdf_name, "sha256": hashlib.sha256(self.pdf_bytes).hexdigest()},
            "target": {"mode": "EXISTING_JOB", "job_number": "PPS-J-0001"},
            "customer": {"name": "Synthetic Customer", "company": "Synthetic Co"},
            "machine": {"reference": "truck", "manufacturer": "International", "model": "5600i", "asset_type": "vehicle", "identifiers": []},
            "requested_needs": [{"reference": "need-1", "original_wording": "Brake part", "quantity": 2, "machine_reference": "truck"}],
            "research_options": [],
        }
        value.update(changes)
        return value

    def _upload(self, filename, content, content_type):
        file = __import__("tempfile").SpooledTemporaryFile(max_size=16 * 1024 * 1024)
        file.write(content)
        file.seek(0)
        return UploadFile(filename=filename, file=file, headers={"content-type": content_type})

    def _call(self, package=None, pdf_bytes=None, pdf_name=None, sidecar=True, pdf=True):
        package = package or self._package()
        pdf_upload = self._upload(pdf_name or self.pdf_name, pdf_bytes if pdf_bytes is not None else self.pdf_bytes, "application/pdf") if pdf else None
        sidecar_upload = self._upload("package.json", json.dumps(package).encode(), "application/json") if sidecar else None
        return asyncio.run(ingest_research_import(None, pdf_upload, sidecar_upload))

    def test_valid_package_stages_draft_and_preserves_candidate_contribution(self):
        with legacy_app.get_connection() as connection:
            jobs_before = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            invoices_before = connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        response = self._call()
        self.assertEqual(response.status_code, 303)
        proposal_id = int(re.search(r"/(\d+)$", response.headers["location"]).group(1))
        with legacy_app.get_connection() as connection:
            proposal = connection.execute("SELECT status FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()
            contribution = connection.execute("SELECT payload_json FROM intake_proposal_contributions WHERE proposal_id=? AND contributor_type='AI' AND payload_json LIKE '%RESEARCH_IMPORT_PACKAGE%'", (proposal_id,)).fetchone()
            attachment = connection.execute("SELECT original_filename FROM intake_proposal_attachments WHERE proposal_id=?", (proposal_id,)).fetchone()
            self.assertEqual(proposal["status"], "DRAFT")
            self.assertIn("RESEARCH-IMPORT-001", contribution["payload_json"])
            self.assertEqual(attachment["original_filename"], self.pdf_name)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], jobs_before)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], invoices_before)

    def test_invalid_schema_hash_and_missing_parts_are_rejected(self):
        with self.assertRaises(HTTPException):
            self._call(package={"package_id": "bad"})
        bad = self._package(source_pdf={"filename": self.pdf_name, "sha256": "b" * 64})
        with self.assertRaises(HTTPException):
            self._call(package=bad)
        with self.assertRaises(HTTPException):
            self._call(pdf=False)
        with self.assertRaises(HTTPException):
            self._call(sidecar=False)
        with self.assertRaises(HTTPException):
            self._call(pdf_name="other.pdf")

    def test_duplicate_package_is_idempotent_and_conflicting_replay_rejected(self):
        with legacy_app.get_connection() as connection:
            proposal_count = connection.execute("SELECT COUNT(*) FROM intake_proposals").fetchone()[0]
            attachment_count = connection.execute("SELECT COUNT(*) FROM intake_proposal_attachments").fetchone()[0]
        first = self._call()
        second = self._call()
        self.assertEqual(second.headers["x-pps-research-import-duplicate"], "1")
        with legacy_app.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM intake_proposals").fetchone()[0], proposal_count + 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM intake_proposal_attachments").fetchone()[0], attachment_count + 1)
        changed = self._package(customer={"name": "Different", "company": "Other"})
        with self.assertRaises(HTTPException) as error:
            self._call(package=changed)
        self.assertEqual(error.exception.status_code, 409)

    def test_review_context_resolves_target_and_preserves_imported_fields(self):
        with legacy_app.get_connection() as connection:
            job = connection.execute("SELECT job_number,customer,company,machine,pin_serial FROM jobs ORDER BY id LIMIT 1").fetchone()
        option = {
            "reference": "option-1", "requested_need_reference": "need-1", "description": "Brake rotor",
            "manufacturer": "Bendix", "oem_part_number": "OEM-1", "cross_reference_part_numbers": ["ALT-1"],
            "supplier": "Supplier", "supplier_url": "https://supplier.example/item", "quantity": 2,
            "supplier_base_unit_price": "100.00", "supplier_base_extended_price": "200.00",
            "tax_rate_percent": "18.00", "tax_amount": "36.00", "tax_inclusive_unit_cost": "118.00",
            "tax_inclusive_extended_cost": "236.00", "fitment_evidence": "Fits 5600i",
            "part_number_evidence": "Catalog evidence", "notes": "Candidate only", "verification_status": "UNVERIFIED",
            "confidence": "MEDIUM", "source_evidence": {"pdf_reference": self.pdf_name, "page": 1, "source_urls": ["https://supplier.example/item"]},
        }
        value = self._package(
            target={"mode": "EXISTING_JOB", "job_number": job["job_number"]},
            estimated_inbound_freight={"amount": "225.00", "currency": "USD", "status": "ESTIMATED", "evidence": "Shipping estimate"},
            research_options=[option],
        )
        response = self._call(package=value)
        proposal_id = int(re.search(r"/(\d+)$", response.headers["location"]).group(1))
        with legacy_app.get_connection() as connection:
            proposal = load_proposal(connection, proposal_id)
        env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
        env.globals["manufacturer_logo"] = lambda *args: ""
        env.globals["url_for"] = lambda *args, **kwargs: ""
        env.globals["manufacturer_identity"] = manufacturer_identity
        rendered = env.get_template("smart_intake_proposal.html").render(
            proposal=proposal, identifier_types=[], markets=[], customers=[],
        )
        self.assertTrue(proposal["research_import"]["target_found"])
        for text in (job["job_number"], "TARGET PPS JOB", "IMPORTED IDENTITY", "Brake rotor", "Qty 2", "OEM-1", "ALT-1", "Supplier", "118.00", "Fits 5600i", "225.00", self.pdf_name, "RESEARCH-IMPORT-001"):
            self.assertIn(text, rendered)
        self.assertIn("No update action is available yet", rendered)

    def test_review_marks_missing_target_and_blocks_update(self):
        response = self._call(package=self._package(target={"mode": "EXISTING_JOB", "job_number": "PPS-J-9999"}))
        proposal_id = int(re.search(r"/(\d+)$", response.headers["location"]).group(1))
        with legacy_app.get_connection() as connection:
            proposal = load_proposal(connection, proposal_id)
        self.assertFalse(proposal["research_import"]["target_found"])
        env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
        env.globals["manufacturer_logo"] = lambda *args: ""
        env.globals["url_for"] = lambda *args, **kwargs: ""
        env.globals["manufacturer_identity"] = manufacturer_identity
        rendered = env.get_template("smart_intake_proposal.html").render(
            proposal=proposal, identifier_types=[], markets=[], customers=[],
        )
        self.assertIn("Target Job not found", rendered)
        self.assertIn("No update action is available yet", rendered)

    def test_new_job_mode_is_proposed_without_target_resolution(self):
        response = self._call(package=self._package(target={"mode": "NEW_JOB"}))
        proposal_id = int(re.search(r"/(\d+)$", response.headers["location"]).group(1))
        with legacy_app.get_connection() as connection:
            proposal = load_proposal(connection, proposal_id)
            jobs_before = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(proposal["research_import"]["target_mode"], "NEW_JOB")
        self.assertFalse(proposal["research_import"]["target_found"])
        env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
        env.globals.update({"manufacturer_logo": lambda *args: "", "url_for": lambda *args, **kwargs: "", "manufacturer_identity": manufacturer_identity})
        rendered = env.get_template("smart_intake_proposal.html").render(proposal=proposal, identifier_types=[], markets=[], customers=[])
        self.assertIn("NEW JOB PROPOSED", rendered)
        self.assertIn("Ready to create the Job?", rendered)
        self.assertIn("Confirm &amp; Create is unavailable", rendered)
        with legacy_app.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], jobs_before)

    def test_new_job_confirmation_carries_research_without_creating_commercial_records(self):
        option = {
            "reference": "option-1", "requested_need_reference": "need-1", "description": "Brake rotor",
            "manufacturer": "Bendix", "oem_part_number": "OEM-1", "cross_reference_part_numbers": ["ALT-1"],
            "supplier": "Supplier", "supplier_url": "https://supplier.example/item", "quantity": 2,
            "supplier_base_unit_price": "100.00", "supplier_base_extended_price": "200.00",
            "tax_rate_percent": "18.00", "tax_amount": "36.00", "tax_inclusive_unit_cost": "118.00",
            "tax_inclusive_extended_cost": "236.00", "fitment_evidence": "Fits 5600i",
            "part_number_evidence": "Catalog evidence", "notes": "Candidate only", "verification_status": "VERIFIED",
            "confidence": "HIGH", "source_evidence": {"pdf_reference": self.pdf_name, "page": 1, "source_urls": ["https://supplier.example/item"]},
        }
        response = self._call(package=self._package(target={"mode": "NEW_JOB"}, estimated_inbound_freight={"amount": "225.00", "currency": "USD", "status": "ESTIMATED"}, research_options=[option]))
        proposal_id = int(re.search(r"/(\d+)$", response.headers["location"]).group(1))
        with legacy_app.get_connection() as connection:
            connection.execute("UPDATE intake_proposals SET review_state='CONFIDENT' WHERE id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_assets SET review_state='CONFIDENT' WHERE proposal_id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_identifiers SET review_state='CONFIDENT' WHERE proposal_id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_needs SET review_state='CONFIDENT' WHERE proposal_id=?", (proposal_id,))
            connection.commit()
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("invoices", "supplier_orders", "receiving_events", "deliveries")}
            job_id = confirm_proposal(connection, proposal_id, 1)
            job = connection.execute("SELECT job_number FROM jobs WHERE id=?", (job_id,)).fetchone()
            need = connection.execute("SELECT wording FROM requested_needs WHERE job_id=?", (job_id,)).fetchone()
            item = connection.execute("SELECT quantity,supplier_name,supplier_unit_cost,research_evidence FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)", (job_id,)).fetchone()
            freight = connection.execute("SELECT details FROM basket_activity WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?) AND activity_type='RESEARCH_IMPORT_ESTIMATED_FREIGHT'", (job_id,)).fetchone()
            self.assertTrue(job["job_number"].startswith("PPS-J-"))
            self.assertEqual(need["wording"], "Brake part")
            self.assertEqual(item["quantity"], 2)
            self.assertEqual(item["supplier_name"], "Supplier")
            self.assertEqual(item["supplier_unit_cost"], 118.0)
            self.assertIn("tax_inclusive_extended_cost", item["research_evidence"])
            self.assertIn('"status": "ESTIMATED"', freight["details"])
            self.assertEqual({table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}, before)
            self.assertEqual(connection.execute("SELECT status FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()[0], "CONFIRMED")

    def test_new_job_multi_need_options_keep_explicit_reference_mapping(self):
        needs = [
            {"reference": "need-a", "original_wording": "Front brake rotor", "quantity": 2, "machine_reference": "truck"},
            {"reference": "need-b", "original_wording": "Rear seal kit", "quantity": 5, "machine_reference": "truck"},
            {"reference": "need-c", "original_wording": "Cab air filter", "quantity": 3, "machine_reference": "truck"},
        ]
        options = []
        for reference, description, quantity, supplier, pn in (
            ("need-c", "Cab filter", 3, "Supplier C", "PN-C"),
            ("need-a", "Front rotor", 2, "Supplier A", "PN-A"),
            ("need-b", "Rear seal", 5, "Supplier B", "PN-B"),
        ):
            options.append({
                "reference": f"option-{reference}", "requested_need_reference": reference, "description": description,
                "manufacturer": "Maker", "oem_part_number": pn, "cross_reference_part_numbers": [f"X-{pn}"],
                "supplier": supplier, "supplier_url": f"https://{supplier.lower().replace(' ', '')}.example/item",
                "quantity": quantity, "supplier_base_unit_price": "10.00", "supplier_base_extended_price": str(quantity * 10),
                "tax_rate_percent": "18.00", "tax_amount": "1.80", "tax_inclusive_unit_cost": "11.80",
                "tax_inclusive_extended_cost": str(quantity * 11.8), "fitment_evidence": f"Fitment {pn}",
                "part_number_evidence": f"Evidence {pn}", "notes": f"Notes {pn}",
                "verification_status": "VERIFIED", "confidence": "HIGH",
                "source_evidence": {"pdf_reference": self.pdf_name, "page": quantity, "source_urls": [f"https://{supplier.lower().replace(' ', '')}.example/item"]},
            })
        value = self._package(target={"mode": "NEW_JOB"}, requested_needs=needs, research_options=options,
                              estimated_inbound_freight={"amount": "225.00", "currency": "USD", "status": "ESTIMATED"})
        response = self._call(package=value)
        proposal_id = int(re.search(r"/(\d+)$", response.headers["location"]).group(1))
        with legacy_app.get_connection() as connection:
            connection.execute("UPDATE intake_proposals SET review_state='CONFIDENT' WHERE id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_assets SET review_state='CONFIDENT' WHERE proposal_id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_needs SET review_state='CONFIDENT' WHERE proposal_id=?", (proposal_id,))
            connection.commit()
            jobs_before = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            job_id = confirm_proposal(connection, proposal_id, 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], jobs_before + 1)
            rows = connection.execute(
                """SELECT rn.wording,bi.quantity,bi.supplier_name,bi.manufacturer_part_number,
                          bi.supplier_part_number,bi.research_evidence
                   FROM basket_items bi
                   JOIN basket_item_need_links link ON link.basket_item_id=bi.id
                   JOIN requested_needs rn ON rn.id=link.requested_need_id
                  WHERE bi.basket_id=(SELECT id FROM baskets WHERE job_id=?)""", (job_id,)
            ).fetchall()
            by_wording = {row["wording"]: row for row in rows}
            self.assertEqual(set(by_wording), {item["original_wording"] for item in needs})
            expected = {"Front brake rotor": (2, "Supplier A", "PN-A"), "Rear seal kit": (5, "Supplier B", "PN-B"), "Cab air filter": (3, "Supplier C", "PN-C")}
            for wording, (quantity, supplier, pn) in expected.items():
                row = by_wording[wording]
                self.assertEqual((row["quantity"], row["supplier_name"], row["manufacturer_part_number"]), (quantity, supplier, pn))
                self.assertIn(f'"reference": "option-need-{"a" if wording.startswith("Front") else "b" if wording.startswith("Rear") else "c"}"', row["research_evidence"])
            activities = connection.execute(
                "SELECT activity_type,details FROM basket_activity WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)", (job_id,)
            ).fetchall()
            freight = [row for row in activities if row["activity_type"] == "RESEARCH_IMPORT_ESTIMATED_FREIGHT"]
            self.assertEqual(len(freight), 1)
            self.assertIn('"status": "ESTIMATED"', freight[0]["details"])


if __name__ == "__main__":
    unittest.main()
