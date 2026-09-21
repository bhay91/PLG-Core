from contextlib import closing
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import legacy_app
from starlette.requests import Request
from plg_core.application import app
from plg_core.basket.routes import (job_center_v2_page, center_add_need, center_edit_need,
                                    center_edit_job, center_edit_customer, center_add_asset, center_edit_asset)
from plg_core.web_security import CSRF_COOKIE_NAME
from plg_core.database.migrations import run_migrations
from plg_core.jobs.workspace import build_workspace
from plg_core.research.service import create_requested_need, update_requested_need
from plg_core.database import migrations as migration_module
from plg_core.revisions.service import ensure_initial_revision, touch_revision


class JobCenterV2Batch2A1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-job-center-2a1-")
        self.addCleanup(self.temp.cleanup)
        self.patches = [patch.object(legacy_app, name, Path(self.temp.name) / name.lower()) for name in ("DB_PATH", "DOCUMENTS_DIR", "UPLOADS_DIR")]
        for p in self.patches: p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        legacy_app.initialize_database(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,active) VALUES ('C-BASE','Operator Customer',1)").lastrowid
            self.job = c.execute("INSERT INTO jobs(job_number,created_date,customer,customer_id,status,notes) VALUES ('2A1-001','2026-09-12','Operator Customer',?,'REQUESTED','Initial note')", (customer,)).lastrowid
            c.commit()

    def request(self):
        return Request({'type': 'http', 'method': 'GET', 'path': f'/jobs/{self.job}/center', 'headers': [], 'query_string': b'', 'scheme': 'http', 'server': ('localhost', 80), 'app': app})

    class FormRequest:
        def __init__(self, values, valid=True):
            self.values = values
            self.cookies = {CSRF_COOKIE_NAME: "token"} if valid else {}
        async def form(self):
            return self.values

    def post_request(self, values, valid=True):
        return self.FormRequest({"csrf_token": "token", **values}, valid)

    def test_add_and_edit_need_post_persist_quantity(self):
        asyncio.run(center_add_need(self.post_request({"wording": "Valve", "quantity": "2"}), self.job))
        with closing(legacy_app.get_connection()) as c:
            need = c.execute("SELECT * FROM requested_needs WHERE job_id=? ORDER BY id DESC LIMIT 1", (self.job,)).fetchone()
        asyncio.run(center_edit_need(self.post_request({"wording": "Valve updated", "quantity": "4", "state": "OPEN"}), self.job, need["id"]))
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT wording,quantity FROM requested_needs WHERE id=?", (need["id"],)).fetchone()
        self.assertEqual((row["wording"], row["quantity"]), ("Valve updated", 4))

    def test_job_and_customer_post_persist(self):
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,company,active) VALUES ('C-2A1','Before','Old Co',1)").lastrowid
            c.execute("UPDATE jobs SET customer_id=? WHERE id=?", (customer, self.job)); c.commit()
        asyncio.run(center_edit_job(self.post_request({"customer_id": str(customer), "notes": "Updated note"}), self.job))
        asyncio.run(center_edit_customer(self.post_request({"name": "After", "company": "New Co", "phone": "555", "email": "a@b.test", "address": "1 Main"}), self.job))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT notes FROM jobs WHERE id=?", (self.job,)).fetchone()[0], "Updated note")
            self.assertEqual(tuple(c.execute("SELECT name,company,phone,email,address FROM customers WHERE id=?", (customer,)).fetchone()), ("After", "New Co", "555", "a@b.test", "1 Main"))

    def test_equipment_posts_persist(self):
        asyncio.run(center_add_asset(self.post_request({"manufacturer": "CAT", "model": "430", "vin_pin_serial": "PIN1"}), self.job))
        with closing(legacy_app.get_connection()) as c:
            asset = c.execute("SELECT * FROM job_assets WHERE job_id=? ORDER BY id DESC LIMIT 1", (self.job,)).fetchone()
        asyncio.run(center_edit_asset(self.post_request({"manufacturer": "CAT", "model": "430D", "vin_pin_serial": "PIN2"}), self.job, asset["id"]))
        with closing(legacy_app.get_connection()) as c:
            row = c.execute("SELECT manufacturer,model,vin_pin_serial FROM job_assets WHERE id=?", (asset["id"],)).fetchone()
        self.assertEqual(tuple(row), ("CAT", "430D", "PIN2"))

    def test_csrf_rejects_center_posts(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Existing", quantity=2)
        with closing(legacy_app.get_connection()) as c:
            asset = c.execute("SELECT id FROM job_assets WHERE job_id=? LIMIT 1", (self.job,)).fetchone()
        calls = [lambda: center_edit_job(self.post_request({"notes": "x"}, False), self.job),
                 lambda: center_edit_customer(self.post_request({"name": "x"}, False), self.job),
                 lambda: center_add_asset(self.post_request({"model": "x"}, False), self.job),
                 lambda: center_edit_asset(self.post_request({"model": "x"}, False), self.job, asset["id"] if asset else 999),
                 lambda: center_add_need(self.post_request({"wording": "Nope", "quantity": "1"}, False), self.job),
                 lambda: center_edit_need(self.post_request({"wording": "Nope", "quantity": "1"}, False), self.job, need["id"])]
        for call in calls:
            with self.assertRaises(Exception): asyncio.run(call())

    def test_pre_0052_upgrade_preserves_existing_row_and_is_idempotent(self):
        with closing(legacy_app.get_connection()) as c:
            c.execute("DROP TABLE requested_needs")
            c.execute("CREATE TABLE requested_needs (id INTEGER PRIMARY KEY, job_id INTEGER, job_asset_id INTEGER, customer_request_id INTEGER, wording TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'OPEN', resolution TEXT NOT NULL DEFAULT '', lock_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT)")
            c.execute("INSERT INTO requested_needs(id,job_id,wording,notes) VALUES (99,?,?,?)", (self.job, "Legacy", "old")); c.commit()
            migration_module._migration_0052_requested_need_quantity(c); c.commit()
            before = c.execute("SELECT wording,notes,state,quantity FROM requested_needs WHERE id=99").fetchone()
            migration_module._migration_0052_requested_need_quantity(c); c.commit()
            after = c.execute("SELECT wording,notes,state,quantity FROM requested_needs WHERE id=99").fetchone()
        self.assertEqual(tuple(before), ("Legacy", "old", "OPEN", None)); self.assertEqual(tuple(after), tuple(before))

    def test_stale_edit_redirects_with_safe_message_without_mutation(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Stale", quantity=2)
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job); old_id, old_version = revision["id"], revision["lock_version"]
            touch_revision(c, old_id, old_version); c.commit()
        response = asyncio.run(center_edit_need(self.post_request({"wording": "Changed", "quantity": "4", "state": "OPEN", "expected_revision_id": str(old_id), "expected_version": str(old_version)}), self.job, need["id"]))
        self.assertIn("This+job+changed+after+you+opened+it", response.headers["location"])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(tuple(c.execute("SELECT wording,quantity FROM requested_needs WHERE id=?", (need["id"],)).fetchone()), ("Stale", 2))

    def test_protected_edit_redirects_with_safe_message(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Committed", quantity=2)
        from fastapi import HTTPException
        with patch("plg_core.basket.routes.update_requested_need", side_effect=HTTPException(409, "Issued quote history protects this Customer Need wording.")):
            response = asyncio.run(center_edit_need(self.post_request({"wording": "Changed", "quantity": "3", "state": "OPEN"}), self.job, need["id"]))
        self.assertIn("This+item+is+part+of+committed+history", response.headers["location"])

    def test_center_exposes_safe_edit_controls(self):
        html = job_center_v2_page(self.request(), self.job).body.decode()
        for label in ("Edit job", "Add equipment", "+ Add item", "Description", "Quantity"):
            self.assertIn(label, html)
        self.assertIn("/center/edit", html)
        self.assertIn("/center/assets", html)
        self.assertIn("/center/needs", html)

    def test_add_item_header_control_opens_single_form(self):
        html = job_center_v2_page(self.request(), self.job).body.decode()
        self.assertEqual(html.count("+ Add item"), 1)
        self.assertEqual(html.count('action="/jobs/%s/center/needs"' % self.job), 1)
        self.assertIn('name="quantity" type="number" min="1" value="1" required', html)
        self.assertIn('type="button" onclick="this.closest(\'details\').open=false">Cancel', html)

    def test_customer_form_keeps_save_and_cancel_grouped(self):
        html = job_center_v2_page(self.request(), self.job).body.decode()
        start = html.index('class="jv2-customer-form"')
        section = html[start:html.index('</form>', start)]
        self.assertIn('Save customer', section)
        self.assertIn('type="button"', section)

    def test_requested_need_is_visible_after_authoritative_service_write(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Hydraulic pump", notes="Use existing guard", quantity=2)
        with closing(legacy_app.get_connection()) as c:
            model = build_workspace(c, self.job)
        self.assertEqual(model["parts"][0]["wording"], "Hydraulic pump")
        self.assertEqual(model["parts"][0]["quantity"], 2)
        self.assertIn("Hydraulic pump", job_center_v2_page(self.request(), self.job).body.decode())

    def test_batch2a1_does_not_add_quote_or_invoice_records(self):
        create_requested_need(self.job, job_asset_id=None, wording="Filter")
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_requested_need_quantity_validates_and_updates(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Filter", quantity=2)
        with self.assertRaises(Exception):
            update_requested_need(self.job, need["id"], wording="Filter", quantity=0)
        updated = update_requested_need(self.job, need["id"], wording="Filter", quantity=4)
        self.assertEqual(updated["quantity"], 4)

    def test_omitted_quantity_preserves_existing_value(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Seal", quantity=2)
        self.assertEqual(update_requested_need(self.job, need["id"], wording="Seal revised")["quantity"], 2)
        self.assertEqual(update_requested_need(self.job, need["id"], state="SATISFIED")["quantity"], 2)

    def test_strict_quantity_rejects_decimal_float_and_bool(self):
        need = create_requested_need(self.job, job_asset_id=None, wording="Bolt", quantity=2)
        for value in ("2.5", 2.5, True, False, 0, -1, "abc"):
            with self.assertRaises(Exception):
                update_requested_need(self.job, need["id"], quantity=value)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT quantity FROM requested_needs WHERE id=?", (need["id"],)).fetchone()[0], 2)
