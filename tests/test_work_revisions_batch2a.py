from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

import legacy_app
from plg_core.basket.models import BasketItemCreate, BasketItemUpdate
from plg_core.basket.service import add_item, commit_basket, get_basket, update_item
from plg_core.basket.service import get_or_create_basket
from plg_core.database.migrations import (
    _migration_0029_work_quote_revision_foundation,
    run_migrations,
)
from plg_core.pricing import pricing_assessment
from plg_core.revisions import (
    cancel_work_revision,
    commit_work_revision,
    get_revision_context,
    revision_diff,
    start_work_revision,
)


ROOT = Path(__file__).resolve().parents[1]
PRE_BATCH2A = Path("/tmp/pps-batch2a-safety-dHEIJ0/plg_core.pre-batch2a.db")


class WorkRevisionBatch2ATests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-batch2a-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()
        self._clear()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def _clear(self):
        with closing(self.connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "quote_documents_manifest", "work_revision_attachments",
                "work_revision_items", "work_revision_sources", "work_revisions",
                "delivery_items", "deliveries", "receiving_event_items",
                "receiving_events", "supplier_order_items", "supplier_orders",
                "invoice_events", "invoice_items", "invoices", "quote_events",
                "quote_items", "quotes", "customer_transactions", "part_sources",
                "job_parts", "basket_activity", "basket_attachments", "basket_items",
                "basket_sources", "baskets", "customer_request_attachments",
                "customer_requests", "job_timeline", "audit_logs", "machines",
                "customers", "jobs",
            ):
                c.execute(f'DELETE FROM "{table}"')
            c.commit()

    def job(self) -> int:
        with closing(self.connection()) as c:
            customer_id = c.execute(
                "INSERT INTO customers (customer_number,name,active) VALUES ('T-C','Test',1)"
            ).lastrowid
            job_id = c.execute(
                """
                INSERT INTO jobs (
                    job_number, created_date, customer_id, customer, status
                ) VALUES ('T-J','2026-08-11',?,'Test','REQUESTED')
                """,
                (customer_id,),
            ).lastrowid
            c.commit()
            return int(job_id)

    def add_verified(
        self,
        job_id: int,
        *,
        description: str = "Filter",
        cost: float = 100,
        markup: float = 30,
        override: float | None = None,
        selected: bool = True,
        revision: dict | None = None,
    ):
        return add_item(
            job_id,
            BasketItemCreate(
                requested_description=description,
                supplier_name="Supplier A",
                supplier_part_number="SUP-1",
                quantity=1,
                supplier_unit_cost=cost,
                markup_percent=markup,
                customer_unit_price_override=override,
                verification_status="VERIFIED",
                selected=selected,
            ),
            expected_revision_id=revision["id"] if revision else None,
            expected_version=revision["lock_version"] if revision else None,
        )

    def committed_job(self, *, override: float | None = None) -> tuple[int, int]:
        job_id = self.job()
        self.add_verified(job_id, override=override)
        result = commit_basket(job_id)
        return job_id, int(result["revision_id"])

    def quote(self, job_id: int, revision_id: int, *, status: str = "SENT") -> int:
        with closing(self.connection()) as c:
            part = c.execute(
                "SELECT * FROM job_parts WHERE job_id=? AND work_revision_id=?",
                (job_id, revision_id),
            ).fetchone()
            source = c.execute(
                "SELECT * FROM part_sources WHERE part_id=? AND selected_for_quote=1",
                (part["id"],),
            ).fetchone()
            customer_price = float(part["customer_unit_price"] or 0)
            supplier_cost = float(source["supplier_cost"] or 0)
            quote_id = c.execute(
                """
                INSERT INTO quotes (
                    quote_number, job_id, quote_date, status, parts_subtotal,
                    customer_total, supplier_total, profit_total, work_revision_id
                ) VALUES (?,?, '2026-08-11',?,?,?,?,?,?)
                """,
                (
                    f"T-Q-{job_id}", job_id, status, customer_price,
                    customer_price, supplier_cost,
                    customer_price - supplier_cost, revision_id,
                ),
            ).lastrowid
            c.execute(
                """
                INSERT INTO quote_items (
                    quote_id, part_id, source_id, quantity, description,
                    supplier_name, source_type, supplier_unit_cost,
                    customer_unit_price, supplier_line_total,
                    customer_line_total, line_profit, pricing_mode
                ) VALUES (?,?,?,1,'Filter','Supplier A','AFTERMARKET',
                          ?,?,?,?,?,'LEGACY_FIXED')
                """,
                (
                    quote_id, part["id"], source["id"], supplier_cost,
                    customer_price, supplier_cost, customer_price,
                    customer_price - supplier_cost,
                ),
            )
            c.commit()
            return int(quote_id)

    def test_initial_work_manual_add_and_revision_linked_commit(self):
        job_id = self.job()
        basket = self.add_verified(job_id)
        self.assertEqual(basket["totals"]["customer_parts_total"], 130)
        result = commit_basket(job_id)
        with closing(self.connection()) as c:
            revision = c.execute(
                "SELECT * FROM work_revisions WHERE id=?", (result["revision_id"],)
            ).fetchone()
            part = c.execute("SELECT * FROM job_parts WHERE job_id=?", (job_id,)).fetchone()
            source = c.execute("SELECT * FROM part_sources WHERE part_id=?", (part["id"],)).fetchone()
            self.assertEqual(revision["state"], "COMMITTED")
            self.assertEqual(part["work_revision_id"], revision["id"])
            self.assertIsNotNone(part["work_revision_item_id"])
            self.assertIsNotNone(source["work_revision_source_id"])

    def test_existing_basket_read_does_not_consume_autoincrement_id(self):
        job_id = self.job()
        get_basket(job_id)
        with closing(self.connection()) as c:
            before = c.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='baskets'"
            ).fetchone()[0]
            basket_id = c.execute(
                "SELECT id FROM baskets WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            for _ in range(5):
                basket = get_or_create_basket(c, job_id)
                self.assertEqual(basket["id"], basket_id)
            c.commit()
            after = c.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='baskets'"
            ).fetchone()[0]
        self.assertEqual(after, before)

    def test_concurrent_first_basket_creation_creates_one_row(self):
        job_id = self.job()
        barrier = Barrier(2)

        def create():
            with closing(self.connection()) as c:
                barrier.wait()
                basket = get_or_create_basket(c, job_id)
                c.commit()
                return basket["id"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(ids[0], ids[1])
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM baskets WHERE job_id=?", (job_id,)
            ).fetchone()[0], 1)

    def test_browser_audit_uses_disposable_database(self):
        job_id = self.job()
        get_basket(job_id)
        with closing(self.connection()) as c:
            c.execute(
                "UPDATE customers SET last_viewed_at='2026-08-11 00:00:00'"
            )
            c.commit()
        before_hash = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        with closing(self.connection()) as c:
            before_counts = {
                table: c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ("customers", "jobs", "baskets")
            }
            before_pps = c.execute(
                "SELECT * FROM pps_number_sequences ORDER BY entity_type"
            ).fetchall()
            before_sqlite = c.execute(
                "SELECT * FROM sqlite_sequence ORDER BY name"
            ).fetchall()
            before_customer = c.execute(
                "SELECT id,last_viewed_at FROM customers ORDER BY id"
            ).fetchall()
            before_baskets = c.execute(
                "SELECT id,job_id FROM baskets ORDER BY id"
            ).fetchall()

        from scripts import pps_browser_audit

        def exercise_disposable(_base_url, disposable_db):
            connection = sqlite3.connect(disposable_db)
            try:
                connection.execute(
                    "UPDATE customers SET last_viewed_at=CURRENT_TIMESTAMP"
                )
                connection.execute(
                    "INSERT INTO baskets (job_id) VALUES (?) "
                    "ON CONFLICT(job_id) DO NOTHING",
                    (job_id,),
                )
                connection.commit()
            finally:
                connection.close()
            return {
                "tested_pages": ["/customers/1", f"/jobs/{job_id}/basket"],
                "tested_viewports": 6,
                "warnings": [],
                "failures": [],
            }

        fake_process = Mock()
        fake_process.wait.return_value = 0
        with (
            patch.object(
                pps_browser_audit, "_audit_server", side_effect=exercise_disposable
            ),
            patch.object(pps_browser_audit, "_available_port", return_value=8765),
            patch.object(pps_browser_audit, "_wait_for_server"),
            patch.object(
                pps_browser_audit.subprocess,
                "Popen",
                return_value=fake_process,
            ),
        ):
            result = pps_browser_audit.run_browser_audit(
                source_db_path=self.db_path
            )
        self.assertEqual(result["failures"], [])
        self.assertEqual(
            hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before_hash
        )
        with closing(self.connection()) as c:
            self.assertEqual({
                table: c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in ("customers", "jobs", "baskets")
            }, before_counts)
            self.assertEqual(c.execute(
                "SELECT * FROM pps_number_sequences ORDER BY entity_type"
            ).fetchall(), before_pps)
            self.assertEqual(c.execute(
                "SELECT * FROM sqlite_sequence ORDER BY name"
            ).fetchall(), before_sqlite)
            self.assertEqual(c.execute(
                "SELECT id,last_viewed_at FROM customers ORDER BY id"
            ).fetchall(), before_customer)
            self.assertEqual(c.execute(
                "SELECT id,job_id FROM baskets ORDER BY id"
            ).fetchall(), before_baskets)

    def test_auto_price_recalculates_for_cost_and_quantity_changes(self):
        job_id = self.job()
        basket = self.add_verified(job_id, cost=100, markup=30)
        item_id = basket["items"][0]["id"]
        update_item(item_id, BasketItemUpdate(supplier_unit_cost=120, quantity=2))
        basket = get_basket(job_id)
        self.assertEqual(basket["totals"]["customer_parts_total"], 312)
        self.assertEqual(basket["items"][0]["pricing_mode"], "AUTO")

    def test_promised_and_zero_prices_survive_cost_changes(self):
        for promised, expected_markup in ((175.0, 45.83), (0.0, -100.0)):
            with self.subTest(promised=promised):
                self._clear()
                job_id = self.job()
                basket = self.add_verified(job_id, cost=100, override=promised)
                item_id = basket["items"][0]["id"]
                update_item(item_id, BasketItemUpdate(supplier_unit_cost=120))
                basket = get_basket(job_id)
                item = basket["items"][0]
                self.assertEqual(item["customer_unit_price_override"], promised)
                self.assertEqual(item["pricing_mode"], "OVERRIDE")
                self.assertEqual(basket["totals"]["customer_parts_total"], promised)
                assessment = pricing_assessment(120, 30, promised)
                self.assertEqual(assessment["actual_markup_percent"], expected_markup)

    def test_override_can_be_deliberately_restored_to_auto(self):
        job_id = self.job()
        basket = self.add_verified(job_id, override=175)
        update_item(
            basket["items"][0]["id"],
            BasketItemUpdate(customer_unit_price_override=None),
        )
        item = get_basket(job_id)["items"][0]
        self.assertIsNone(item["customer_unit_price_override"])
        self.assertEqual(item["pricing_mode"], "AUTO")

    def test_start_revision_twice_is_idempotent_and_clones_legacy_fixed_price(self):
        job_id, revision_id = self.committed_job()
        quote_id = self.quote(job_id, revision_id)
        first = start_work_revision(job_id, reason="Customer change", based_on_quote_id=quote_id)
        second = start_work_revision(job_id, reason="Repeated click", based_on_quote_id=quote_id)
        self.assertEqual(first["id"], second["id"])
        basket = get_basket(job_id)
        self.assertEqual(basket["items"][0]["customer_unit_price_override"], 130)
        self.assertEqual(basket["items"][0]["pricing_mode"], "LEGACY_FIXED")
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM work_revisions WHERE job_id=? AND state='EDITABLE'",
                (job_id,),
            ).fetchone()[0], 1)

    def test_promised_and_explicit_zero_prices_survive_revision_clone(self):
        for promised in (175.0, 0.0):
            with self.subTest(promised=promised):
                self._clear()
                job_id, revision_id = self.committed_job(override=promised)
                quote_id = self.quote(job_id, revision_id)
                revision = start_work_revision(
                    job_id, reason="Supplier correction", based_on_quote_id=quote_id
                )
                item = get_basket(job_id)["items"][0]
                self.assertEqual(item["customer_unit_price_override"], promised)
                self.assertEqual(item["pricing_mode"], "LEGACY_FIXED")
                update_item(
                    item["id"], BasketItemUpdate(supplier_unit_cost=150),
                    expected_job_id=job_id, expected_revision_id=revision["id"],
                    expected_version=revision["lock_version"],
                )
                self.assertEqual(
                    get_basket(job_id)["items"][0]["customer_unit_price_override"],
                    promised,
                )

    def test_concurrent_revision_start_has_one_winner(self):
        job_id, revision_id = self.committed_job()
        quote_id = self.quote(job_id, revision_id)
        barrier = Barrier(2)
        def start():
            barrier.wait()
            return start_work_revision(
                job_id, reason="Concurrent request", based_on_quote_id=quote_id
            )["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(ids[0], ids[1])
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM work_revisions WHERE job_id=? AND state='EDITABLE'",
                (job_id,),
            ).fetchone()[0], 1)

    def test_stale_form_and_two_tab_conflict_preserve_first_change(self):
        job_id, revision_id = self.committed_job()
        quote_id = self.quote(job_id, revision_id)
        revision = start_work_revision(job_id, reason="Correction", based_on_quote_id=quote_id)
        item_id = get_basket(job_id)["items"][0]["id"]
        update_item(
            item_id, BasketItemUpdate(quantity=2),
            expected_job_id=job_id, expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        with self.assertRaises(HTTPException) as caught:
            update_item(
                item_id, BasketItemUpdate(quantity=9),
                expected_job_id=job_id, expected_revision_id=revision["id"],
                expected_version=revision["lock_version"],
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(get_basket(job_id)["items"][0]["quantity"], 2)

    def test_revision_supplier_quantity_cost_and_price_intent(self):
        job_id, revision_id = self.committed_job(override=175)
        quote_id = self.quote(job_id, revision_id)
        revision = start_work_revision(job_id, reason="Better source", based_on_quote_id=quote_id)
        item = get_basket(job_id)["items"][0]
        update_item(
            item["id"],
            BasketItemUpdate(
                supplier_name="Supplier B", supplier_unit_cost=80, quantity=3
            ),
            expected_job_id=job_id, expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        changed = get_basket(job_id)["items"][0]
        self.assertEqual(changed["supplier_name"], "Supplier B")
        self.assertEqual(changed["supplier_unit_cost"], 80)
        self.assertEqual(changed["quantity"], 3)
        # Quote clone conservatively retains the exact customer price.
        self.assertEqual(changed["customer_unit_price_override"], 175)

    def test_revision_commit_twice_does_not_duplicate_job_parts(self):
        job_id, revision_id = self.committed_job()
        quote_id = self.quote(job_id, revision_id)
        revision = start_work_revision(job_id, reason="Add part", based_on_quote_id=quote_id)
        self.add_verified(job_id, description="Hose", revision=revision)
        current = get_revision_context(job_id)
        first = commit_work_revision(
            job_id, expected_revision_id=current["id"],
            expected_version=current["lock_version"],
        )
        second = commit_work_revision(
            job_id, expected_revision_id=current["id"],
            expected_version=current["lock_version"],
        )
        self.assertFalse(first["already_committed"])
        self.assertTrue(second["already_committed"])
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM job_parts WHERE work_revision_id=?",
                (revision["id"],),
            ).fetchone()[0], 2)

    def test_concurrent_revision_commit_is_idempotent(self):
        job_id, revision_id = self.committed_job()
        quote_id = self.quote(job_id, revision_id)
        revision = start_work_revision(
            job_id, reason="Concurrent commit", based_on_quote_id=quote_id
        )
        barrier = Barrier(2)

        def commit():
            barrier.wait()
            return commit_work_revision(
                job_id,
                expected_revision_id=revision["id"],
                expected_version=revision["lock_version"],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: commit(), range(2)))
        self.assertEqual(
            sorted(result["already_committed"] for result in results),
            [False, True],
        )
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM job_parts WHERE work_revision_id=?",
                (revision["id"],),
            ).fetchone()[0], 1)

    def test_cancel_twice_is_idempotent_and_retains_snapshot(self):
        job_id, revision_id = self.committed_job()
        quote_id = self.quote(job_id, revision_id)
        revision = start_work_revision(job_id, reason="Try change", based_on_quote_id=quote_id)
        first = cancel_work_revision(
            revision["id"], reason="Customer withdrew", expected_version=revision["lock_version"]
        )
        second = cancel_work_revision(
            revision["id"], reason="Repeated click", expected_version=revision["lock_version"]
        )
        self.assertEqual(first["state"], "CANCELLED")
        self.assertEqual(second["state"], "CANCELLED")
        with closing(self.connection()) as c:
            self.assertGreater(c.execute(
                "SELECT COUNT(*) FROM work_revision_items WHERE work_revision_id=?",
                (revision["id"],),
            ).fetchone()[0], 0)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='WORK_REVISION_CANCELLED'"
            ).fetchone()[0], 1)

    def test_revision_after_invoice_boundary_is_blocked(self):
        job_id, revision_id = self.committed_job()
        quote_id = self.quote(job_id, revision_id, status="APPROVED")
        with closing(self.connection()) as c:
            c.execute(
                """
                INSERT INTO invoices (
                    invoice_number, quote_id, job_id, invoice_date,
                    customer_total, balance_due
                ) VALUES ('T-I',?,?,'2026-08-11',130,130)
                """,
                (quote_id, job_id),
            )
            c.commit()
        with self.assertRaises(HTTPException) as caught:
            start_work_revision(job_id, reason="Too late", based_on_quote_id=quote_id)
        self.assertEqual(caught.exception.status_code, 409)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM work_revisions WHERE job_id=?", (job_id,)
            ).fetchone()[0], 1)

    def test_commit_creates_only_new_revision_parts_and_preserves_history(self):
        job_id, old_revision_id = self.committed_job()
        quote_id = self.quote(job_id, old_revision_id)
        with closing(self.connection()) as c:
            old_part = dict(c.execute(
                "SELECT * FROM job_parts WHERE work_revision_id=?", (old_revision_id,)
            ).fetchone())
            old_quote = dict(c.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone())
            old_item = dict(c.execute(
                "SELECT * FROM quote_items WHERE quote_id=?", (quote_id,)
            ).fetchone())
        revision = start_work_revision(job_id, reason="Cost correction", based_on_quote_id=quote_id)
        item = get_basket(job_id)["items"][0]
        update_item(
            item["id"], BasketItemUpdate(supplier_unit_cost=75),
            expected_job_id=job_id, expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        current = get_revision_context(job_id)
        commit_work_revision(
            job_id, expected_revision_id=current["id"],
            expected_version=current["lock_version"],
        )
        with closing(self.connection()) as c:
            self.assertEqual(dict(c.execute(
                "SELECT * FROM job_parts WHERE id=?", (old_part["id"],)
            ).fetchone()), old_part)
            self.assertEqual(dict(c.execute(
                "SELECT * FROM quotes WHERE id=?", (quote_id,)
            ).fetchone()), old_quote)
            self.assertEqual(dict(c.execute(
                "SELECT * FROM quote_items WHERE id=?", (old_item["id"],)
            ).fetchone()), old_item)
            revision_ids = {row[0] for row in c.execute(
                "SELECT DISTINCT work_revision_id FROM job_parts WHERE job_id=?", (job_id,)
            )}
            self.assertEqual(revision_ids, {old_revision_id, revision["id"]})

    def test_revision_diff_reports_changed_work(self):
        job_id, old_revision_id = self.committed_job()
        revision = start_work_revision(job_id, reason="New requirement")
        item = get_basket(job_id)["items"][0]
        update_item(
            item["id"], BasketItemUpdate(quantity=4),
            expected_job_id=job_id, expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        current = get_revision_context(job_id)
        commit_work_revision(
            job_id, expected_revision_id=current["id"],
            expected_version=current["lock_version"],
        )
        diff = revision_diff(revision["id"])
        self.assertEqual(len(diff["added"]), 1)
        self.assertEqual(len(diff["removed"]), 1)

    def test_migration_preflight_refuses_ambiguous_quotes_without_changes(self):
        path = Path(self.temp.name) / "ambiguous.db"
        shutil.copy2(PRE_BATCH2A, path)
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        c.execute("DROP INDEX IF EXISTS uq_quotes_one_active_per_job")
        quote = c.execute("SELECT * FROM quotes WHERE is_archived=0 LIMIT 1").fetchone()
        c.execute(
            """
            INSERT INTO quotes (
                quote_number, job_id, quote_date, status, is_archived
            ) VALUES ('AMBIGUOUS',?,?, 'DRAFT',0)
            """,
            (quote["job_id"], "2026-08-11"),
        )
        c.commit()
        with self.assertRaises(RuntimeError):
            _migration_0029_work_quote_revision_foundation(c)
        self.assertIsNone(c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_revisions'"
        ).fetchone())
        c.close()

    def test_migration_fresh_copy_rerun_and_interrupted_retry(self):
        for name in ("fresh", "retry"):
            with self.subTest(name=name):
                path = Path(self.temp.name) / f"{name}.db"
                legacy_app.DB_PATH = path
                if name == "fresh":
                    legacy_app.initialize_database()
                else:
                    shutil.copy2(PRE_BATCH2A, path)
                    with closing(self.connection()) as c:
                        _migration_0029_work_quote_revision_foundation(c)
                        c.commit()  # schema applied, marker intentionally absent
                run_migrations()
                run_migrations()
                with closing(self.connection()) as c:
                    self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(c.execute(
                        "SELECT COUNT(*) FROM schema_migrations "
                        "WHERE migration_id='0029_work_quote_revision_foundation'"
                    ).fetchone()[0], 1)
        legacy_app.DB_PATH = self.db_path

    def test_migration_preserves_business_sequences_and_historical_values(self):
        path = Path(self.temp.name) / "production-copy.db"
        shutil.copy2(PRE_BATCH2A, path)
        before = sqlite3.connect(path)
        tables = ("jobs", "job_parts", "part_sources", "quotes", "quote_items", "invoices")
        values = {table: before.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
                  for table in tables}
        sequences = before.execute(
            "SELECT * FROM pps_number_sequences ORDER BY entity_type"
        ).fetchall()
        before.close()
        legacy_app.DB_PATH = path
        run_migrations()
        after = sqlite3.connect(path)
        for table in tables:
            old_column_count = len(values[table][0]) if values[table] else len(
                sqlite3.connect(PRE_BATCH2A).execute(f"PRAGMA table_info({table})").fetchall()
            )
            rows = after.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
            self.assertEqual([row[:old_column_count] for row in rows], values[table])
        self.assertEqual(
            after.execute("SELECT * FROM pps_number_sequences ORDER BY entity_type").fetchall(),
            sequences,
        )
        self.assertEqual(after.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(after.execute("PRAGMA foreign_key_check").fetchall(), [])
        after.close()
        legacy_app.DB_PATH = self.db_path


if __name__ == "__main__":
    unittest.main()
