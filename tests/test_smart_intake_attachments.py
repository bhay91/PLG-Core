from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.intake import attachments as attachment_service
from plg_core.intake.attachments import store_proposal_images, validate_images
from plg_core.intake.routes import preview_attachment, remove_attachment
from plg_core.intake.service import confirm_proposal, create_proposal, load_proposal


ROOT = Path(__file__).resolve().parents[1]
PNG = (b"\x89PNG\r\n\x1a\n" + b"test-image" + b"IEND" + b"\x00" * 8)


def upload(name="plate.png", content=PNG, media_type="image/png"):
    stream = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    stream.write(content)
    stream.seek(0)
    return UploadFile(stream, filename=name, headers=Headers({"content-type": media_type}))


class SmartIntakeAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-intake-images-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.proposal_root = root / "proposal-uploads"
        self.request_root = root / "request-uploads"
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db_path),
            patch.object(attachment_service, "UPLOAD_ROOT", self.proposal_root),
            patch.object(attachment_service, "REQUEST_UPLOAD_ROOT", self.request_root),
        ]
        for item in self.patches: item.start()
        run_migrations()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def create_with_images(self, count=2):
        images = asyncio.run(validate_images([upload(f"plate-{index}.png") for index in range(count)]))
        with closing(self.connection()) as connection:
            proposal_id = create_proposal(connection, "Image Person\nJohn Deere 350D\nPIN IMAGE-350D\nNeeds Filter")
            store_proposal_images(connection, proposal_id, images)
            connection.commit()
        return proposal_id

    def test_multiple_images_live_on_draft_without_business_records(self):
        with closing(self.connection()) as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("customers", "jobs", "machines", "requested_needs")}
        proposal_id = self.create_with_images()
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, proposal_id)
            self.assertEqual(len(proposal["attachments"]), 2)
            self.assertEqual(proposal["status"], "DRAFT")
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
            self.assertEqual(after, before)
            self.assertTrue(all(Path(row["stored_path"]).is_file() for row in proposal["attachments"]))

    def test_validation_rejects_type_extension_signature_size_and_count(self):
        cases = [
            [upload("plate.pdf", media_type="application/pdf")],
            [upload("plate.png", media_type="image/jpeg")],
            [upload("plate.png", content=b"not-a-png")],
            [upload(f"{index}.png") for index in range(attachment_service.MAX_FILES + 1)],
        ]
        for uploads in cases:
            with self.assertRaises(HTTPException):
                asyncio.run(validate_images(uploads))
        with patch.object(attachment_service, "MAX_FILE_BYTES", 8):
            with self.assertRaises(HTTPException):
                asyncio.run(validate_images([upload()]))

    def test_safe_name_preview_scope_and_stale_removal(self):
        images = asyncio.run(validate_images([upload("../unsafe plate.png")]))
        with closing(self.connection()) as connection:
            first = create_proposal(connection, "Image Person\nJCB 3CX\nNeeds Filter")
            store_proposal_images(connection, first, images); connection.commit()
            second = create_proposal(connection, "Other Person\nJCB 3CX\nNeeds Hose")
            row = connection.execute("SELECT * FROM intake_proposal_attachments WHERE proposal_id=?", (first,)).fetchone()
            version = connection.execute("SELECT lock_version FROM intake_proposals WHERE id=?", (first,)).fetchone()[0]
        self.assertNotIn("..", Path(row["stored_path"]).name)
        self.assertEqual(preview_attachment(first, row["id"]).media_type, "image/png")
        with self.assertRaises(HTTPException) as cross:
            preview_attachment(second, row["id"])
        self.assertEqual(cross.exception.status_code, 404)
        with self.assertRaises(HTTPException) as stale:
            remove_attachment(first, row["id"], version + 1)
        self.assertEqual(stale.exception.status_code, 409)
        self.assertTrue(Path(row["stored_path"]).exists())
        remove_attachment(first, row["id"], version)
        self.assertFalse(Path(row["stored_path"]).exists())

    def test_confirmation_copies_images_without_sharing_files(self):
        proposal_id = self.create_with_images(1)
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, proposal_id)
            proposal_path = Path(proposal["attachments"][0]["stored_path"])
            job_id = confirm_proposal(connection, proposal_id, proposal["lock_version"])
            request = connection.execute("SELECT * FROM customer_requests WHERE job_id=?", (job_id,)).fetchone()
            copied = connection.execute("SELECT * FROM customer_request_attachments WHERE request_id=?", (request["id"],)).fetchone()
            self.assertIsNotNone(copied)
            self.assertNotEqual(Path(copied["file_path"]), proposal_path)
            self.assertEqual(Path(copied["file_path"]).read_bytes(), proposal_path.read_bytes())
            connection.execute("DELETE FROM customer_request_attachments WHERE id=?", (copied["id"],)); connection.commit()
            Path(copied["file_path"]).unlink()
            self.assertTrue(proposal_path.exists())

    def test_failed_confirmation_rolls_back_attachment_relationships_and_copies(self):
        proposal_id = self.create_with_images(1)
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, proposal_id)
            proposal_path = Path(proposal["attachments"][0]["stored_path"])
            before = connection.execute("SELECT COUNT(*) FROM customer_request_attachments").fetchone()[0]
            connection.execute("CREATE TRIGGER intake_image_abort BEFORE INSERT ON job_timeline BEGIN SELECT RAISE(ABORT,'attachment rollback'); END")
            connection.commit()
            with self.assertRaises(Exception):
                confirm_proposal(connection, proposal_id, proposal["lock_version"])
            self.assertEqual(connection.execute("SELECT status FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()[0], "DRAFT")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM customer_request_attachments").fetchone()[0], before)
            self.assertTrue(proposal_path.exists())
            copied_files = list(self.request_root.rglob("*")) if self.request_root.exists() else []
            self.assertFalse([path for path in copied_files if path.is_file()])

    def test_attachment_ui_contract_has_drop_preview_and_review_thumbnails(self):
        intake = (ROOT / "templates" / "smart_intake.html").read_text()
        review = (ROOT / "templates" / "smart_intake_proposal.html").read_text()
        script = (ROOT / "static" / "smart_intake_attachments.js").read_text()
        self.assertIn('enctype="multipart/form-data"', intake)
        self.assertIn("data-smart-intake-drop", intake)
        self.assertIn("data-smart-intake-previews", intake)
        self.assertIn('customer_requests.css?v=smart-attachments-2', intake)
        self.assertIn('customer_requests.css?v=smart-attachments-2', review)
        self.assertIn("dataTransfer.files", script)
        self.assertIn("Remove", script)
        self.assertIn("proposal.attachments", review)
        self.assertIn("smart-attachment-list", review)
        self.assertIn("data-image-preview", review)
        self.assertIn("data-image-modal", intake)
        self.assertIn("data-image-modal", review)
        self.assertIn('action="/requests/smart-intake/research-import"', intake)
        self.assertIn('name="research_pdf" type="file" accept=".pdf,application/pdf" required', intake)
        self.assertIn('name="sidecar" type="file" accept=".json,application/json" required', intake)
        self.assertIn("Import Research Package", intake)
        self.assertNotIn('name="sidecar"', intake.split('action="/requests/smart-intake/analyze"', 1)[1].split('</form>', 1)[0])
        css = (ROOT / "static" / "customer_requests.css").read_text()
        self.assertIn(".smart-intake-preview img", css)
        self.assertIn("height:76px", css)
        self.assertIn(".smart-intake-review-images img", css)
        self.assertIn("max-width:85vw", css)
        self.assertIn("max-height:85vh", css)
        self.assertIn("object-fit:contain", css)
        self.assertIn("max-width:100%", css)
        self.assertIn("@media(max-width:520px)", css)
        self.assertIn("multiple", intake)
        self.assertIn("Escape", script)
        self.assertIn("event.target === modal", script)
        self.assertIn("data-image-modal-previous", review)

    def test_rendered_entry_includes_compact_stylesheet_and_every_preview_uses_row_contract(self):
        from starlette.requests import Request
        from plg_core.requests.routes import smart_intake_form
        scope = {"type": "http", "method": "GET", "path": "/requests/smart-intake",
                 "headers": [], "query_string": b"", "app": legacy_app.app,
                 "router": legacy_app.app.router, "scheme": "http",
                 "server": ("testserver", 80), "client": ("test", 1)}
        body = smart_intake_form(Request(scope)).body.decode()
        self.assertIn('/static/customer_requests.css?v=smart-attachments-2', body)
        self.assertIn('/static/smart_intake_attachments.js?v=smart-attachments-2', body)
        script = (ROOT / "static" / "smart_intake_attachments.js").read_text()
        self.assertIn('row.className = "smart-intake-preview smart-attachment-row"', script)
        self.assertIn('preview.className = "smart-attachment-preview"', script)
        self.assertIn("row.append(preview, name, remove)", script)


if __name__ == "__main__":
    unittest.main()
