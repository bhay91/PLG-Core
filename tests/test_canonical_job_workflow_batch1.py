from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request

import legacy_app
from plg_core.basket.routes import basket_page
from plg_core.database.migrations import run_migrations


class CanonicalJobWorkflowBatch1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-canonical-job-")
        self.db = Path(self.temp.name) / "test.db"
        shutil.copy2(Path(__file__).parents[1] / "data" / "plg_core.db", self.db)
        self.patch = patch.object(legacy_app, "DB_PATH", self.db)
        self.patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers (customer_number,name,active) VALUES (?,?,1)",
                ("CANON-C", "Canonical Test Customer"),
            ).lastrowid
            self.job_id = connection.execute(
                """
                INSERT INTO jobs (job_number,created_date,customer_id,customer,company,status)
                VALUES ('PPS-J-9001','2026-01-01',?,?,?,'REQUESTED')
                """,
                (customer_id, "Canonical Test Customer", "Test Co"),
            ).lastrowid
            connection.commit()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    @staticmethod
    def request(path):
        path, _, query = path.partition("?")
        return Request({
            "type": "http", "method": "GET", "path": path,
            "query_string": query.encode(), "headers": [], "scheme": "http",
            "server": ("localhost", 80), "app": legacy_app.app,
        })

    def test_legacy_job_urls_redirect_to_advanced(self):
        detail = legacy_app.job_detail(self.request(f"/jobs/{self.job_id}"), self.job_id)
        self.assertEqual(detail.status_code, 303)
        self.assertEqual(detail.headers["location"], f"/jobs/{self.job_id}/center")

        basket = basket_page(self.request(f"/jobs/{self.job_id}/basket"), self.job_id)
        self.assertEqual(basket.status_code, 303)
        self.assertEqual(basket.headers["location"], f"/jobs/{self.job_id}/basket?view=advanced")

        contextual = basket_page(
            self.request(f"/jobs/{self.job_id}/basket?asset_id=7&need_id=9"),
            self.job_id,
            asset_id=7,
            need_id=9,
        )
        self.assertEqual(
            contextual.headers["location"],
            f"/jobs/{self.job_id}/basket?view=advanced&asset_id=7&need_id=9",
        )

    def test_explicit_views_remain_available(self):
        advanced = basket_page(
            self.request(f"/jobs/{self.job_id}/basket"), self.job_id, view="advanced"
        )
        legacy = basket_page(
            self.request(f"/jobs/{self.job_id}/basket"), self.job_id, view="legacy"
        )
        self.assertEqual(advanced.status_code, 200)
        self.assertEqual(legacy.status_code, 200)

    def test_normal_links_target_canonical_command_center(self):
        jobs = (Path(__file__).parents[1] / "templates" / "jobs.html").read_text()
        quote = (Path(__file__).parents[1] / "templates" / "quote_documents.html").read_text()
        self.assertIn('/jobs/{{ job.id }}/center', jobs)
        self.assertIn('/jobs/{{ quote.job_id }}/basket?view=advanced', quote)

    def test_representative_post_redirects_use_canonical_page(self):
        sources = [
            Path("legacy_app.py").read_text(),
            Path("plg_core/basket/routes.py").read_text(),
            Path("plg_core/sources/routes.py").read_text(),
            Path("plg_core/research/routes.py").read_text(),
        ]
        combined = "\n".join(sources)
        self.assertIn("/jobs/{job_id}/basket?view=advanced", combined)
        self.assertIn("#research-results", combined)
        self.assertIn("#parts-ready", combined)


if __name__ == "__main__":
    unittest.main()
