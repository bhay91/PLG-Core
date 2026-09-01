from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import anyio
import httpx2

import legacy_app
from plg_core.application import app
from plg_core.database.migrations import run_migrations
from plg_core.intake.service import load_proposal
from plg_core.requests.extension_auth import FIREFOX_RESEARCH_IMPORT_CREATE_SCOPE


TOKEN = "research-import-connector-test-token"
PDF_SOURCE = next((Path(__file__).resolve().parents[1] / "documents").rglob("*.pdf"))
PDF_BYTES = PDF_SOURCE.read_bytes()


class ResearchImportConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-research-connector-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        self.upload_root = root / "uploads"
        self.document_root = root / "documents"
        self.upload_root.mkdir()
        self.document_root.mkdir()
        # Disposable copy only; never open the repository DB for writes.
        shutil.copy2(Path(__file__).resolve().parents[1] / "data" / "plg_core.db", self.db_path)
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db_path),
            patch.object(legacy_app, "UPLOADS_DIR", self.upload_root),
            patch.object(legacy_app, "DOCUMENTS_DIR", self.document_root),
            patch.dict(os.environ, {
                "PPS_FIREFOX_INBOX_TOKEN": TOKEN,
                "PPS_FIREFOX_INBOX_SCOPES": FIREFOX_RESEARCH_IMPORT_CREATE_SCOPE,
            }, clear=False),
        ]
        for item in self.patches:
            item.start()
        import plg_core.intake.attachments as attachments
        self.attachment_patch = patch.object(attachments, "UPLOAD_ROOT", self.upload_root / "intake-proposals")
        self.attachment_patch.start()
        run_migrations()

    def tearDown(self):
        self.attachment_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def package(self, *, package_id="CONNECTOR-001", target=None):
        return {
            "package_id": package_id,
            "source_pdf": {"filename": "research.pdf", "sha256": hashlib.sha256(PDF_BYTES).hexdigest()},
            "target": target or {"mode": "NEW_JOB"},
            "customer": {"name": "Synthetic Customer", "company": "Synthetic Co"},
            "machine": {"reference": "truck", "manufacturer": "International", "model": "5600i", "asset_type": "vehicle", "identifiers": []},
            "requested_needs": [{"reference": "need-1", "original_wording": "Hydraulic pump", "quantity": 2, "machine_reference": "truck"}],
            "research_options": [],
        }

    async def request(self, package=None, *, token=TOKEN, scopes=FIREFOX_RESEARCH_IMPORT_CREATE_SCOPE, files_override=None):
        package = package or self.package()
        files = files_override or {
            "research_pdf": ("research.pdf", PDF_BYTES, "application/pdf"),
            "sidecar": ("package.json", json.dumps(package).encode(), "application/json"),
        }
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        transport = httpx2.ASGITransport(app=app, client=("127.0.0.1", 45121))
        with patch.dict(os.environ, {
            "PPS_FIREFOX_INBOX_TOKEN": TOKEN,
            "PPS_FIREFOX_INBOX_SCOPES": scopes,
        }, clear=False):
            async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.post("/api/extension/v1/research-import/packages", headers=headers, files=files)

    def call(self, *args, **kwargs):
        return anyio.run(lambda: self.request(*args, **kwargs))

    def test_valid_package_stages_one_draft_and_preserves_provenance(self):
        with closing(legacy_app.get_connection()) as connection:
            proposals_before = connection.execute("SELECT COUNT(*) FROM intake_proposals").fetchone()[0]
            candidates = ["customers", "machines", "jobs", "quotes", "invoices", "customer_transactions", "supplier_orders", "receiving_events", "deliveries"]
            existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            immutable_tables = [table for table in candidates if table in existing]
            immutable_before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in immutable_tables}
        response = self.call()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "DRAFT")
        self.assertEqual(result["target_mode"], "NEW_JOB")
        self.assertEqual(result["package_id"], "CONNECTOR-001")
        self.assertEqual(result["pdf_filename"], "research.pdf")
        self.assertEqual(result["pdf_sha256"], hashlib.sha256(PDF_BYTES).hexdigest())
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM intake_proposals").fetchone()[0], proposals_before + 1)
            proposal = load_proposal(connection, result["proposal_id"])
            self.assertEqual(proposal["status"], "DRAFT")
            self.assertEqual({table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in immutable_tables}, immutable_before)
            attachment = connection.execute("SELECT original_filename,media_type FROM intake_proposal_attachments WHERE proposal_id=?", (result["proposal_id"],)).fetchone()
            self.assertEqual((attachment["original_filename"], attachment["media_type"]), ("research.pdf", "application/pdf"))
            contribution = connection.execute("SELECT payload_json FROM intake_proposal_contributions WHERE proposal_id=? AND contributor_type='AI'", (result["proposal_id"],)).fetchone()
            self.assertIn("CONNECTOR-001", contribution["payload_json"])

    def test_authentication_and_dedicated_scope_are_required(self):
        self.assertEqual(self.call(token=None).status_code, 401)
        self.assertEqual(self.call(scopes="pps:firefox:inbox:create").status_code, 403)

    def test_missing_invalid_and_mismatched_uploads_are_rejected(self):
        self.assertEqual(self.call(files_override={"research_pdf": ("research.pdf", PDF_BYTES, "application/pdf")}).status_code, 400)
        self.assertEqual(self.call(files_override={"sidecar": ("package.json", b"{}", "application/json")}).status_code, 400)
        self.assertEqual(self.call(files_override={
            "research_pdf": ("research.pdf", PDF_BYTES, "application/pdf"),
            "sidecar": ("package.json", b"{}", "application/json"),
            "extra": ("extra.txt", b"x", "text/plain"),
        }).status_code, 400)
        self.assertEqual(self.call(files_override={
            "research_pdf": ("research.pdf", PDF_BYTES, "application/pdf"),
            "sidecar": ("package.json", b"not json", "application/json"),
        }).status_code, 400)
        bad_name = self.package(); bad_name["source_pdf"]["filename"] = "other.pdf"
        self.assertEqual(self.call(bad_name).status_code, 400)
        bad_hash = self.package(); bad_hash["source_pdf"]["sha256"] = "0" * 64
        self.assertEqual(self.call(bad_hash).status_code, 400)

    def test_replay_is_idempotent_and_conflict_is_rejected(self):
        with closing(legacy_app.get_connection()) as connection:
            proposals_before = connection.execute("SELECT COUNT(*) FROM intake_proposals").fetchone()[0]
        first = self.call()
        second = self.call()
        self.assertEqual(first.json()["proposal_id"], second.json()["proposal_id"])
        self.assertTrue(second.json()["duplicate"])
        changed = self.package(); changed["customer"]["name"] = "Different"
        self.assertEqual(self.call(changed).status_code, 409)
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM intake_proposals").fetchone()[0], proposals_before + 1)

    def test_existing_job_mode_remains_draft_and_unconfirmed(self):
        with closing(legacy_app.get_connection()) as connection:
            jobs_before = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        response = self.call(self.package(package_id="CONNECTOR-EXISTING", target={"mode": "EXISTING_JOB", "job_number": "PPS-J-0001"}))
        self.assertEqual(response.status_code, 200)
        with closing(legacy_app.get_connection()) as connection:
            result = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            self.assertEqual(result, jobs_before)
            self.assertEqual(connection.execute("SELECT status FROM intake_proposals ORDER BY id DESC").fetchone()[0], "DRAFT")
