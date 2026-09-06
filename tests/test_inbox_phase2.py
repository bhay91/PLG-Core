from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request

import legacy_app
from plg_core.database.migrations import run_migrations
from fastapi import HTTPException
from plg_core.requests.routes import list_requests, remove_request_from_inbox, request_detail
from plg_core.requests.routes import router as requests_router
from plg_core.intake.routes import remove_proposal_from_inbox, router as intake_router


ROOT = Path(__file__).resolve().parents[1]


class InboxPhase2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-inbox-")
        self.db_path = Path(self.temp.name) / "inbox.db"
        self.patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.patch.start()
        legacy_app.initialize_database()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            customer_id = c.execute("INSERT INTO customers(customer_number,name,company,active) VALUES ('SYN-C-0001','Synthetic Inbox Customer','Synthetic Co',1)").lastrowid
            c.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,year,vin_pin_serial,active) VALUES (?,?,?,?,?,?,?,1)", (customer_id, 'SYN-M-0001', 'Synthetic Machine', 'Synthetic Make', 'SYN-420', '2026', 'SYN-PIN-0001'))
            c.commit()
        self.token = "PHASE2-INBOX-UNIQUE"
        with closing(legacy_app.get_connection()) as c:
            job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer,status) VALUES (?,?,?,'REQUESTED')",
                ("P2-JOB", "2026-08-10", "Completed Sender"),
            ).lastrowid
            self.manual_id = c.execute(
                """INSERT INTO customer_requests
                (request_number,request_text,individual_name,manufacturer,model,requested_parts,
                 status,created_at,updated_at) VALUES (?,?,?,?,?,?, 'NEW',?,?)""",
                ("P2-REQ", f"{self.token} manual fuel pump", "Manual Sender", "CAT", "420D",
                 "Fuel Pump", "2026-08-08", "2026-08-08"),
            ).lastrowid
            self.completed_id = c.execute(
                """INSERT INTO customer_requests
                (request_number,request_text,individual_name,status,job_id,created_at,updated_at)
                VALUES (?,?,?,'COMPLETED',?,?,?)""",
                ("P2-DONE", f"{self.token} confirmed", "Completed Sender", job_id,
                 "2026-08-09", "2026-08-09"),
            ).lastrowid
            self.archived_id = c.execute(
                """INSERT INTO customer_requests
                (request_number,request_text,individual_name,status,is_archived,created_at,updated_at)
                VALUES (?,?,?,'WAITING',1,?,?)""",
                ("P2-ARCH", f"{self.token} archived", "Archived Sender", "2026-08-07", "2026-08-07"),
            ).lastrowid
            self.proposal_id = c.execute(
                """INSERT INTO intake_proposals
                (raw_input,contact_name,status,review_state,created_at,updated_at)
                VALUES (?,?,'DRAFT','REVIEW',?,?)""",
                (f"{self.token} proposal starter", "Proposal Sender", "2026-08-11", "2026-08-11"),
            ).lastrowid
            c.execute(
                """INSERT INTO intake_proposals
                (raw_input,contact_name,status,review_state,created_request_id,created_job_id)
                VALUES (?,?,'CONFIRMED','CONFIDENT',?,?)""",
                (f"{self.token} confirmed duplicate", "Completed Sender", self.completed_id, job_id),
            )
            self.ready_proposal_id = c.execute(
                """INSERT INTO intake_proposals
                (raw_input,company_name,status,review_state,created_at,updated_at)
                VALUES (?,?,'DRAFT','CONFIDENT',?,?)""",
                (f"{self.token} ready radiator", "Ready Company", "2026-08-12", "2026-08-12"),
            ).lastrowid
            c.commit()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def request(self):
        scope = {"type": "http", "method": "GET", "path": "/requests", "headers": [],
                 "query_string": b"", "app": legacy_app.app, "router": legacy_app.app.router,
                 "scheme": "http", "server": ("testserver", 80), "client": ("test", 1)}
        return Request(scope)

    def render(self, **kwargs):
        return list_requests(self.request(), **kwargs)

    def test_combines_manual_and_draft_proposals_without_confirmed_duplicate(self):
        response = self.render(q=self.token, view="all")
        items = response.context["inbox_items"]
        keys = {item["key"] for item in items}
        self.assertIn(f"request:{self.manual_id}", keys)
        self.assertIn(f"proposal:{self.proposal_id}", keys)
        self.assertIn(f"proposal:{self.ready_proposal_id}", keys)
        self.assertEqual(sum(item["source"] == "PROPOSAL" for item in items), 2)
        proposal = next(item for item in items if item["key"] == f"proposal:{self.proposal_id}")
        self.assertEqual((proposal["customer"], proposal["entry_type"], proposal["review_label"]),
                         ("Proposal Sender", "Smart Intake", "Needs Review"))
        self.assertIn("proposal starter", proposal["preview"])
        self.assertEqual(proposal["url"], f"/requests/smart-intake/proposals/{self.proposal_id}")
        ready = next(item for item in items if item["key"] == f"proposal:{self.ready_proposal_id}")
        self.assertEqual((ready["review_label"], ready["next_action"]),
                         ("Ready for Review", "Review Intake"))
        self.assertEqual(items[0]["key"], f"proposal:{self.ready_proposal_id}")

    def test_manual_and_completed_routes_labels_and_filters(self):
        active = self.render(q=self.token, view="active").context["inbox_items"]
        manual = next(item for item in active if item["key"] == f"request:{self.manual_id}")
        completed = next(item for item in active if item["key"] == f"request:{self.completed_id}")
        self.assertEqual((manual["entry_type"], manual["review_label"], manual["next_action"]),
                         ("Manual Request", "New Request", "Review Request"))
        self.assertEqual(manual["url"], f"/requests/{self.manual_id}")
        self.assertEqual((completed["review_label"], completed["next_action"]),
                         ("Job Created", "Open Job"))
        self.assertTrue(completed["url"].endswith("/basket"))
        archived = self.render(q=self.token, view="archived").context["inbox_items"]
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0]["review_label"], archived[0]["next_action"]),
                         ("Archived", "View History"))
        completed_only = self.render(q=self.token, status="COMPLETED", view="all").context["inbox_items"]
        self.assertEqual([item["key"] for item in completed_only], [f"request:{self.completed_id}"])

    def test_search_empty_state_actions_and_listing_is_read_only(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        response = self.render(q="proposal starter", view="active")
        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(response.context["inbox_items"][0]["customer"], "Proposal Sender")
        empty = self.render(q="NO-SUCH-INBOX-ENTRY").body.decode()
        self.assertIn("No incoming work found", empty)
        template = (ROOT / "templates" / "requests.html").read_text()
        self.assertIn('href="/requests/smart-intake"', template)
        self.assertIn('href="/requests/new"', template)
        for path in ("request_detail.html", "request_form.html", "smart_intake.html", "smart_intake_proposal.html"):
            self.assertIn("Back to Inbox", (ROOT / "templates" / path).read_text())

    def test_inbox_preview_is_truthfully_labeled_and_original_wording_is_open(self):
        body = self.render(q=self.token, view="active").body.decode()
        self.assertIn("Request Preview", body)
        self.assertNotIn('<div class="erp-intake-need"><span>Requested Need</span>', body)
        smart_review = (ROOT / "templates" / "smart_intake_proposal.html").read_text()
        self.assertIn('<details class="panel smart-raw-request erp-original-wording" open>', smart_review)

    def test_request_summary_prefers_linked_registry_machine_and_identifier(self):
        with closing(legacy_app.get_connection()) as c:
            linked = c.execute(
                """SELECT id,customer_id,year,manufacturer,model,name,vin_pin_serial
                   FROM machines WHERE active=1 AND vin_pin_serial!='' LIMIT 1"""
            ).fetchone()
            self.assertIsNotNone(linked)
            c.execute(
                """UPDATE customer_requests
                   SET customer_id=?,machine_id=?,manufacturer='STALE MAKE',model='STALE MODEL',
                       year='1900',identifier='STALE-ID' WHERE id=?""",
                (linked["customer_id"], linked["id"], self.manual_id),
            )
            c.commit()
        response = request_detail(self.request(), self.manual_id)
        body = response.body.decode()
        summary = body[body.index('aria-label="Request operator summary"'):body.index('<div class="request-detail-grid">')]
        self.assertIn(str(linked["manufacturer"] or ""), summary)
        self.assertIn(str(linked["model"] or linked["name"] or ""), summary)
        self.assertIn(str(linked["vin_pin_serial"]), summary)
        self.assertNotIn("STALE MAKE", summary)
        self.assertNotIn("STALE-ID", summary)

    def test_draft_proposal_removal_is_posted_cancelled_and_history_preserved(self):
        before = self.render(q=self.token, view="active").context["inbox_items"]
        item = next(row for row in before if row["key"] == f"proposal:{self.proposal_id}")
        self.assertTrue(item["removable"])
        response = remove_proposal_from_inbox(self.proposal_id, item["remove_version"])
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            proposal = c.execute("SELECT * FROM intake_proposals WHERE id=?", (self.proposal_id,)).fetchone()
            self.assertEqual(proposal["status"], "CANCELLED")
            self.assertIn(self.token, proposal["raw_input"])
        keys = {row["key"] for row in self.render(q=self.token, view="active").context["inbox_items"]}
        self.assertNotIn(f"proposal:{self.proposal_id}", keys)

    def test_request_removal_archives_only_request_and_preserves_business_rows(self):
        with closing(legacy_app.get_connection()) as c:
            before = {table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("customers", "jobs", "machines", "requested_needs")}
            version = c.execute("SELECT updated_at FROM customer_requests WHERE id=?", (self.manual_id,)).fetchone()[0]
        response = remove_request_from_inbox(self.manual_id, version)
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            request = c.execute("SELECT * FROM customer_requests WHERE id=?", (self.manual_id,)).fetchone()
            self.assertEqual(request["is_archived"], 1)
            after = {table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
            self.assertEqual(after, before)

    def test_stale_and_finalized_inbox_removal_is_rejected(self):
        with closing(legacy_app.get_connection()) as c:
            stale_version = c.execute("SELECT lock_version FROM intake_proposals WHERE id=?", (self.proposal_id,)).fetchone()[0] + 1
            request_version = c.execute("SELECT updated_at FROM customer_requests WHERE id=?", (self.completed_id,)).fetchone()[0]
        with self.assertRaises(HTTPException) as stale:
            remove_proposal_from_inbox(self.proposal_id, stale_version)
        self.assertEqual(stale.exception.status_code, 409)
        with self.assertRaises(HTTPException) as finalized:
            remove_request_from_inbox(self.completed_id, request_version)
        self.assertEqual(finalized.exception.status_code, 409)
        completed = next(row for row in self.render(q=self.token, view="active").context["inbox_items"]
                         if row["key"] == f"request:{self.completed_id}")
        self.assertFalse(completed["removable"])

    def test_remove_action_is_post_form_with_history_confirmation(self):
        body = self.render(q=self.token, view="active").body.decode()
        self.assertIn('method="post"', body)
        self.assertIn("Delete this item?", body)
        self.assertIn("This will archive it and remove it from active PPS views.", body)
        self.assertIn("Historical records will be preserved.", body)
        self.assertIn("data-archive-delete", body)
        methods = {(route.path, method) for router in (requests_router, intake_router) for route in router.routes
                   for method in (getattr(route, "methods", None) or set())}
        self.assertIn(("/requests/{request_id}/remove-from-inbox", "POST"), methods)
        self.assertIn(("/requests/smart-intake/proposals/{proposal_id}/remove-from-inbox", "POST"), methods)


if __name__ == "__main__":
    unittest.main()
