from __future__ import annotations

import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.lifecycle import delete_job_safely, get_job_delete_eligibility
from plg_core.requests.service import (
    delete_request_safely,
    get_request_delete_eligibility,
)


ROOT = Path(__file__).resolve().parents[1]


class SafeDeleteControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-safe-delete-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "delivery_items", "deliveries", "receiving_event_items", "receiving_events",
                "supplier_order_items", "supplier_orders", "invoice_documents_manifest",
                "invoice_events", "invoice_items", "invoices", "quote_documents_manifest",
                "quote_events", "quote_items", "quotes", "customer_transactions",
                "verification_sessions", "requested_needs", "job_assets", "job_parts",
                "basket_attachments", "basket_sources", "basket_items", "baskets",
                "customer_request_attachments", "customer_requests", "job_timeline",
                "audit_logs", "deletion_tombstones", "jobs",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.commit()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def _job(self, number: str) -> int:
        with closing(legacy_app.get_connection()) as connection:
            cursor = connection.execute(
                "INSERT INTO jobs (job_number,created_date,customer,status) VALUES (?,'2026-08-29','TEST ONLY','REQUESTED')",
                (number,),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def _request(self, number: str, **values) -> int:
        columns = ["request_number", "status", *values]
        params = [number, "NEW", *values.values()]
        with closing(legacy_app.get_connection()) as connection:
            cursor = connection.execute(
                f"INSERT INTO customer_requests ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                params,
            )
            connection.commit()
            return int(cursor.lastrowid)

    @staticmethod
    def _web_request(path: str, token: str = "") -> Request:
        headers = [(b"cookie", f"pps_csrf_token={token}".encode())] if token else []
        return Request({"type": "http", "method": "POST", "path": path, "headers": headers})

    def test_empty_job_requires_confirmations_and_writes_tombstone_and_audit(self):
        job_id = self._job("TEST-J-DELETE")
        with closing(legacy_app.get_connection()) as connection:
            revision_id = connection.execute(
                "INSERT INTO work_revisions (job_id,revision_number,state) VALUES (?,1,'EDITABLE')", (job_id,)
            ).lastrowid
            connection.execute(
                "UPDATE jobs SET active_work_revision_id=? WHERE id=?", (revision_id, job_id)
            )
            connection.commit()
        self.assertTrue(get_job_delete_eligibility(job_id)["can_permanently_delete"])
        with self.assertRaises(HTTPException):
            delete_job_safely(job_id, "", "TEST-J-DELETE")
        with self.assertRaises(HTTPException):
            delete_job_safely(job_id, "Accidental shell", "WRONG")
        delete_job_safely(job_id, "Accidental shell", "TEST-J-DELETE", actor="operator")
        with closing(legacy_app.get_connection()) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM work_revisions WHERE id=?", (revision_id,)
            ).fetchone())
            self.assertIsNotNone(connection.execute("SELECT 1 FROM deletion_tombstones WHERE entity_number='TEST-J-DELETE'").fetchone())
            audit = connection.execute("SELECT actor,action FROM audit_logs WHERE entity_id='TEST-J-DELETE'").fetchone()
            self.assertEqual(tuple(audit), ("operator", "JOB_DELETED"))

    def test_meaningful_active_work_revision_remains_protected_and_referenced(self):
        job_id = self._job("TEST-J-REVISION-PROTECTED")
        with closing(legacy_app.get_connection()) as connection:
            revision_id = connection.execute(
                "INSERT INTO work_revisions (job_id,revision_number,state) VALUES (?,1,'COMMITTED')",
                (job_id,),
            ).lastrowid
            connection.execute(
                "UPDATE jobs SET active_work_revision_id=? WHERE id=?", (revision_id, job_id)
            )
            connection.commit()

        eligibility = get_job_delete_eligibility(job_id)
        self.assertFalse(eligibility["can_permanently_delete"])
        self.assertIn("Work revision evidence (1)", eligibility["blockers"])
        with self.assertRaises(HTTPException) as error:
            delete_job_safely(job_id, "cleanup", "TEST-J-REVISION-PROTECTED")
        self.assertEqual(error.exception.status_code, 409)

        with closing(legacy_app.get_connection()) as connection:
            job = connection.execute(
                "SELECT active_work_revision_id FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            revision = connection.execute(
                "SELECT state FROM work_revisions WHERE id=?", (revision_id,)
            ).fetchone()
            self.assertEqual(job["active_work_revision_id"], revision_id)
            self.assertEqual(revision["state"], "COMMITTED")

    def test_job_with_operational_or_commercial_evidence_is_protected(self):
        job_id = self._job("TEST-J-PROTECTED")
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers (customer_number,name,active) VALUES ('TEST-C-DELETE','Test Customer',1)"
            ).lastrowid
            request_id = connection.execute(
                "INSERT INTO customer_requests (request_number,status,job_id) VALUES ('TEST-R-LINK','COMPLETED',?)", (job_id,)
            ).lastrowid
            need_id = connection.execute(
                "INSERT INTO requested_needs (job_id,customer_request_id,wording) VALUES (?,?, 'Test need')", (job_id, request_id)
            ).lastrowid
            basket_id = connection.execute("INSERT INTO baskets (job_id,status) VALUES (?,'RESEARCH')", (job_id,)).lastrowid
            connection.execute("INSERT INTO basket_items (basket_id,requested_description) VALUES (?,'Test evidence')", (basket_id,))
            quote_id = connection.execute(
                "INSERT INTO quotes (quote_number,job_id,quote_date,status) VALUES ('TEST-Q-PROTECTED',?,'2026-08-29','DRAFT')", (job_id,)
            ).lastrowid
            invoice_id = connection.execute(
                "INSERT INTO invoices (invoice_number,quote_id,job_id,invoice_date,status) VALUES ('TEST-I-PROTECTED',?,?,'2026-08-29','UNPAID')", (quote_id, job_id)
            ).lastrowid
            connection.execute(
                "INSERT INTO customer_transactions (customer_id,transaction_date,transaction_type,amount,job_id,invoice_id) VALUES (?,'2026-08-29','PAYMENT',1,?,?)", (customer_id, job_id, invoice_id)
            )
            connection.execute("INSERT INTO supplier_orders (po_number,job_id,status) VALUES ('TEST-PO',?,'DRAFT')", (job_id,))
            connection.execute("INSERT INTO deliveries (job_id,invoice_id,status) VALUES (?,?,'READY')", (job_id, invoice_id))
            connection.commit()
        eligibility = get_job_delete_eligibility(job_id)
        joined = " | ".join(eligibility["blockers"])
        for expected in ("Linked customer request", "Requested Need", "Basket / research evidence", "Quote", "Invoice", "Payment", "Supplier Order", "Delivery"):
            with self.subTest(expected=expected):
                self.assertIn(expected, joined)
        with self.assertRaises(HTTPException):
            delete_job_safely(job_id, "cleanup", "TEST-J-PROTECTED")
        with closing(legacy_app.get_connection()) as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone())

    def test_empty_request_delete_and_protected_request_preflight(self):
        empty_id = self._request("TEST-R-EMPTY")
        self.assertTrue(get_request_delete_eligibility(empty_id)["can_permanently_delete"])
        with self.assertRaises(HTTPException):
            delete_request_safely(empty_id, "", "TEST-R-EMPTY")
        with self.assertRaises(HTTPException):
            delete_request_safely(empty_id, "duplicate", "WRONG")
        delete_request_safely(empty_id, "duplicate", "TEST-R-EMPTY", actor="operator")

        protected_id = self._request("TEST-R-PROTECTED", request_text="Original customer wording")
        with closing(legacy_app.get_connection()) as connection:
            connection.execute(
                "INSERT INTO customer_request_attachments (request_id,original_filename,stored_filename,file_path) VALUES (?,?,?,?)",
                (protected_id, "test.txt", "test.txt", "/tmp/test.txt"),
            )
            job_id = connection.execute(
                "INSERT INTO jobs (job_number,created_date,customer,status) VALUES ('TEST-J-LINK','2026-08-29','Test','REQUESTED')"
            ).lastrowid
            connection.execute("UPDATE customer_requests SET job_id=? WHERE id=?", (job_id, protected_id))
            connection.execute(
                "INSERT INTO requested_needs (job_id,customer_request_id,wording) VALUES (?,?,'Need')", (job_id, protected_id)
            )
            connection.execute(
                "INSERT INTO verification_sessions (job_id,connector_profile_id,customer_request_id) VALUES (?,1,?)", (job_id, protected_id)
            )
            connection.commit()
        blockers = " | ".join(get_request_delete_eligibility(protected_id)["blockers"])
        for expected in ("Original customer wording", "Attachment", "Linked Job", "Requested Need", "Verification"):
            with self.subTest(expected=expected):
                self.assertIn(expected, blockers)
        with self.assertRaises(HTTPException):
            delete_request_safely(protected_id, "cleanup", "TEST-R-PROTECTED")

    def test_permanent_delete_web_routes_require_matching_csrf(self):
        job_id = self._job("TEST-J-CSRF")
        request_id = self._request("TEST-R-CSRF")
        with self.assertRaises(HTTPException) as job_error:
            legacy_app.delete_job_web(
                self._web_request(f"/jobs/{job_id}/delete", "cookie-token"), job_id,
                "duplicate", "TEST-J-CSRF", "wrong-token",
            )
        self.assertEqual(job_error.exception.status_code, 403)
        from plg_core.requests.routes import delete_request_web
        with self.assertRaises(HTTPException) as request_error:
            delete_request_web(
                self._web_request(f"/requests/{request_id}/delete", "cookie-token"), request_id,
                "duplicate", "TEST-R-CSRF", "wrong-token",
            )
        self.assertEqual(request_error.exception.status_code, 403)
        with closing(legacy_app.get_connection()) as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT 1 FROM customer_requests WHERE id=?", (request_id,)).fetchone())

    def test_confirmation_templates_never_expose_delete_as_get(self):
        request_template = (ROOT / "templates" / "request_delete_review.html").read_text()
        job_template = (ROOT / "templates" / "job_delete_review.html").read_text()
        for source, endpoint in (
            (request_template, '/requests/{{ record.id }}/delete'),
            (job_template, '/jobs/{{ job.id }}/delete'),
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIn(f'method="post" action="{endpoint}"', source)
                self.assertIn('name="csrf_token"', source)
                self.assertNotIn(f'href="{endpoint}"', source)


if __name__ == "__main__":
    unittest.main()
