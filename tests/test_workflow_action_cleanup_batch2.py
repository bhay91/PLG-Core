from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request

import legacy_app
from plg_core.application import app
from plg_core.basket.models import BasketItemCreate
from plg_core.basket.routes import basket_page
from plg_core.basket.service import add_item, commit_basket
from plg_core.database.migrations import run_migrations
from plg_core.documents import quote_pdf
from plg_core.revisions import (
    cancel_quote_revision,
    commit_work_revision,
    generate_quote_from_revision,
    start_quote_revision,
)


ROOT = Path(__file__).resolve().parents[1]


class WorkflowActionCleanupBatch2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-action-cleanup-")
        self.root = Path(self.temp.name)
        self.db = self.root / "test.db"
        self.documents = self.root / "documents"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db)
        self.pdf_patch = patch.object(quote_pdf, "DOCUMENT_ROOT", self.documents)
        self.db_patch.start()
        self.pdf_patch.start()
        run_migrations()

    def tearDown(self):
        self.pdf_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    @staticmethod
    def request(path):
        return Request({
            "type": "http", "method": "GET", "path": path,
            "query_string": path.partition("?")[2].encode(), "headers": [],
            "scheme": "http", "server": ("localhost", 80), "app": app,
        })

    def job(self, *, asset=False):
        with closing(self.connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers (customer_number,name,active) VALUES (?,?,1)",
                (f"ACTION-C-{id(self)}-{int(asset)}", "Action Cleanup Customer"),
            ).lastrowid
            job_id = connection.execute(
                """INSERT INTO jobs
                   (job_number,created_date,customer_id,customer,company,status)
                   VALUES (?,?,?,?,'Action Cleanup Co','REQUESTED')""",
                (f"ACTION-J-{id(self)}-{int(asset)}", "2026-09-11", customer_id,
                 "Action Cleanup Customer"),
            ).lastrowid
            asset_id = None
            if asset:
                asset_id = connection.execute(
                    """INSERT INTO job_assets
                       (job_id,customer_id,name,manufacturer,model,vin_pin_serial,is_primary)
                       VALUES (?,?, 'Test Asset','TestCo','Model R','ACTION-ASSET',1)""",
                    (job_id, customer_id),
                ).lastrowid
            connection.commit()
            return int(job_id), int(asset_id) if asset_id else None

    def part(self, job_id, asset_id=None):
        return add_item(
            job_id,
            BasketItemCreate(
                requested_description="Synthetic filter",
                supplier_name="Synthetic Supplier",
                supplier_part_number="ACTION-FILTER",
                quantity=1,
                supplier_unit_cost=100,
                markup_percent=30,
                verification_status="VERIFIED",
                selected=True,
                job_asset_id=asset_id,
            ),
        )

    def quote_with_pending_revision(self):
        job_id, _ = self.job()
        self.part(job_id)
        committed = commit_basket(job_id)
        with closing(self.connection()) as connection:
            quote_id = connection.execute(
                """INSERT INTO quotes
                   (quote_number,job_id,quote_date,status,customer_total,
                    supplier_total,profit_total,work_revision_id,is_current)
                   VALUES ('ACTION-Q',?,'2026-09-11','DRAFT',130,100,30,?,1)""",
                (job_id, committed["revision_id"]),
            ).lastrowid
            part = connection.execute(
                "SELECT id FROM job_parts WHERE work_revision_id=?",
                (committed["revision_id"],),
            ).fetchone()
            source_id = connection.execute(
                "SELECT id FROM part_sources WHERE part_id=? ORDER BY id LIMIT 1",
                (part["id"],),
            ).fetchone()["id"]
            connection.execute(
                """INSERT INTO quote_items
                   (quote_id,part_id,source_id,quantity,description,supplier_name,source_type,
                    supplier_unit_cost,customer_unit_price,supplier_line_total,
                    customer_line_total,line_profit)
                   VALUES (?,?,?,1,'Synthetic filter','Synthetic Supplier','AFTERMARKET',100,130,100,130,30)""",
                (quote_id, part["id"], source_id),
            )
            connection.commit()
        revision = start_quote_revision(int(quote_id), "Synthetic correction")
        committed_revision = commit_work_revision(
            job_id,
            expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        return job_id, int(quote_id), revision, committed_revision

    def render(self, job_id, *, view="advanced"):
        return basket_page(
            self.request(f"/jobs/{job_id}/basket?view={view}"),
            job_id,
            view=view,
        ).body.decode()

    def test_committed_pending_hides_edit_and_keeps_one_primary(self):
        job_id, quote_id, revision, committed = self.quote_with_pending_revision()
        html = self.render(job_id)
        command_start = html.index('<div class="job-command-next">')
        command_end = html.index('</div>', command_start) + 6
        command = html[command_start:command_end]
        self.assertEqual(html.count('class="job-command-next"'), 1)
        self.assertIn("Generate Revised Quote", command)
        self.assertIn(f"/work-revisions/{committed['revision_id']}/generate-quote", command)
        self.assertEqual(command.count('name="expected_version"'), 1)
        self.assertNotIn("Edit Draft", html)
        self.assertNotIn("Cancel Changes", html)
        legacy = self.render(job_id, view="legacy")
        self.assertNotIn('class="job-command-next"', legacy)
        self.assertNotIn("Edit Draft", legacy)

        quote_html = legacy_app.quote_documents(
            self.request(f"/quotes/{quote_id}/documents"), quote_id
        ).body.decode()
        self.assertIn("Changes are committed and ready for a revised quote", quote_html)
        self.assertEqual(quote_html.count("Generate Revised Quote"), 1)
        self.assertEqual(quote_html.count('name="expected_version"'), 1)
        self.assertNotIn("Correct / Edit Draft", quote_html)

        successor = generate_quote_from_revision(
            revision["id"], expected_version=revision["lock_version"]
        )
        after = self.render(job_id)
        self.assertNotIn("Generate Revised Quote", after)
        self.assertIn("Edit Draft", after)
        after_quote = legacy_app.quote_documents(
            self.request(f"/quotes/{successor['id']}/documents"), successor["id"]
        ).body.decode()
        self.assertNotIn("Changes are committed and ready for a revised quote", after_quote)
        self.assertNotIn("Generate Revised Quote", after_quote)

    def test_editable_revision_keeps_cancel_without_premature_generation(self):
        job_id, quote_id, revision, _ = self.quote_with_pending_revision()
        # Project the first correction, then open a new editable correction.
        successor = generate_quote_from_revision(
            revision["id"], expected_version=revision["lock_version"]
        )
        editable = start_quote_revision(successor["id"], "Another correction")
        html = self.render(job_id)
        self.assertIn("Cancel Changes", html)
        self.assertIn("Finish Changes", html)
        self.assertEqual(html.count('action="/jobs/%s/basket/commit"' % job_id), 1)
        commit_form = html.split('action="/jobs/%s/basket/commit"' % job_id, 1)[1].split('</form>', 1)[0]
        self.assertEqual(commit_form.count('name="expected_revision_id"'), 1)
        self.assertEqual(commit_form.count('name="expected_version"'), 1)
        self.assertNotIn("Generate Revised Quote", html)
        self.assertIn("Editing changes for", html)
        cancel_quote_revision(
            editable["id"], expected_version=editable["lock_version"], reason="Synthetic cancel"
        )

    def test_editable_revision_commit_contract_and_stale_version(self):
        job_id, quote_id, revision, _ = self.quote_with_pending_revision()
        successor = generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        editable = start_quote_revision(successor["id"], "Mobile commit")
        html = self.render(job_id)
        self.assertEqual(html.count('action="/jobs/%s/basket/commit"' % job_id), 1)
        self.assertIn(f'name="expected_revision_id" value="{editable["id"]}"', html)
        self.assertIn(f'name="expected_version" value="{editable["lock_version"]}"', html)
        with self.assertRaises(Exception):
            commit_basket(job_id, expected_revision_id=editable["id"], expected_version=editable["lock_version"] - 1)
        result = commit_basket(job_id, expected_revision_id=editable["id"], expected_version=editable["lock_version"])
        self.assertEqual(result["revision_id"], editable["id"])
        with closing(self.connection()) as connection:
            state = connection.execute("SELECT state FROM work_revisions WHERE id=?", (editable["id"],)).fetchone()["state"]
        self.assertEqual(state, "COMMITTED")

    def test_quick_open_requires_real_asset(self):
        no_asset_job, _ = self.job()
        self.part(no_asset_job)
        no_asset_html = self.render(no_asset_job)
        self.assertNotIn('<dialog class="cc-dialog" id="quick-open-dialog">', no_asset_html)
        self.assertNotIn("/assets/0/", no_asset_html)

        asset_job, asset_id = self.job(asset=True)
        self.part(asset_job, asset_id)
        asset_html = self.render(asset_job)
        self.assertIn("quick-open-dialog", asset_html)
        self.assertIn(
            f"/jobs/{asset_job}/assets/{asset_id}/research/quick-open",
            asset_html,
        )
        self.assertNotIn("/assets/0/", asset_html)


if __name__ == "__main__":
    unittest.main()
