import shutil
import tempfile
import unittest
import asyncio
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import legacy_app
from plg_core.commercial.service import create_selective_draft_quote
from plg_core.currency.service import update_currency_settings
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_manual_research_result, create_requested_need, set_preferred_sourcing_option
from plg_core.revisions.service import ensure_initial_revision, ensure_initial_revision_currency_snapshot
from plg_core.basket.routes import center_generate_quote


class _Request:
    def __init__(self, values):
        self.values = values
        self.cookies = {"pps_csrf_token": "token"}

    async def form(self):
        return self.values


class CurrencyA2AInitialQuoteSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pps-currency-a2a-")
        root = Path(self.tmp.name)
        self.db = root / "db.sqlite"
        shutil.copy2(Path(__file__).parents[1] / "data" / "plg_core.db", self.db)
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db),
            patch.object(legacy_app, "DOCUMENTS_DIR", root / "documents"),
            patch.object(legacy_app, "UPLOADS_DIR", root / "uploads"),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)
        legacy_app.initialize_database()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            self.job = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer,status) "
                "VALUES ('A2A-J','2026-01-01','A2A Customer','REQUESTED')"
            ).lastrowid
            c.commit()
        self.need = create_requested_need(self.job, job_asset_id=None, wording="A2A part")
        result = create_manual_research_result(
            self.job, job_asset_id=None, requested_need_id=self.need["id"],
            description="A2A part", supplier_name="A2A Supplier",
            supplier_unit_cost=12, customer_unit_price_override=18,
            verification_status="VERIFIED",
        )
        self.item_id = result["id"]
        set_preferred_sourcing_option(self.job, self.need["id"], self.item_id)

    def cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _revision_tokens(self):
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job)
            c.commit()
            return int(revision["id"]), int(revision["lock_version"])

    def _generate(self):
        revision_id, version = self._revision_tokens()
        return create_selective_draft_quote(
            self.job, basket_item_ids=[self.item_id],
            expected_revision_id=revision_id, expected_version=version,
        )

    def test_modern_default_snapshot_and_timestamp(self):
        quote = self._generate()
        self.assertEqual((quote["currency_code"], quote["display_currency_mode"], quote["fx_rate"], quote["fx_rate_source"]), ("USD", "USD", "160", "BUSINESS_WORKING_RATE"))
        self.assertIsNotNone(quote["fx_locked_at"])
        self.assertEqual((quote["customer_total"], quote["supplier_total"], quote["profit_total"]), (18.0, 12.0, 6.0))

    def test_basket_override_and_source_currency_are_separate(self):
        with closing(legacy_app.get_connection()) as c:
            basket = c.execute("SELECT id FROM baskets WHERE job_id=?", (self.job,)).fetchone()
            c.execute("UPDATE baskets SET currency='CAD',customer_display_currency_mode_override='USD_JMD',customer_jmd_fx_rate_override='162.75' WHERE id=?", (basket["id"],))
            c.commit()
        quote = self._generate()
        self.assertEqual((quote["currency_code"], quote["display_currency_mode"], quote["fx_rate"], quote["fx_rate_source"]), ("USD", "USD_JMD", "162.75", "MANUAL_OVERRIDE"))
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT currency FROM baskets WHERE job_id=?", (self.job,)).fetchone()[0], "CAD")

    def test_display_only_override_keeps_business_rate_source(self):
        with closing(legacy_app.get_connection()) as c:
            basket = c.execute("SELECT id FROM baskets WHERE job_id=?", (self.job,)).fetchone()
            c.execute("UPDATE baskets SET customer_display_currency_mode_override='USD_JMD' WHERE id=?", (basket["id"],))
            c.commit()
        quote = self._generate()
        self.assertEqual((quote["display_currency_mode"], quote["fx_rate"], quote["fx_rate_source"]), ("USD_JMD", "160", "BUSINESS_WORKING_RATE"))

    def test_rate_only_override_uses_manual_source(self):
        with closing(legacy_app.get_connection()) as c:
            basket = c.execute("SELECT id FROM baskets WHERE job_id=?", (self.job,)).fetchone()
            c.execute("UPDATE baskets SET customer_jmd_fx_rate_override='165' WHERE id=?", (basket["id"],))
            c.commit()
        quote = self._generate()
        self.assertEqual((quote["display_currency_mode"], quote["fx_rate"], quote["fx_rate_source"]), ("USD", "165", "MANUAL_OVERRIDE"))

    def test_frozen_revision_wins_after_global_change(self):
        revision_id, version = self._revision_tokens()
        with closing(legacy_app.get_connection()) as c:
            basket = c.execute("SELECT id FROM baskets WHERE job_id=?", (self.job,)).fetchone()
            ensure_initial_revision_currency_snapshot(c, revision_id, basket_id=basket["id"])
            update_currency_settings(jmd_working_rate="165", connection=c)
            c.commit()
        quote = create_selective_draft_quote(self.job, basket_item_ids=[self.item_id], expected_revision_id=revision_id, expected_version=version)
        self.assertEqual(quote["fx_rate"], "160")

    def test_quote_snapshot_survives_basket_override_change(self):
        with closing(legacy_app.get_connection()) as c:
            basket = c.execute("SELECT id FROM baskets WHERE job_id=?", (self.job,)).fetchone()
            c.execute("UPDATE baskets SET customer_jmd_fx_rate_override='162.75' WHERE id=?", (basket["id"],))
            c.commit()
        quote = self._generate()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE baskets SET customer_jmd_fx_rate_override='170' WHERE id=(SELECT id FROM baskets WHERE job_id=?)", (self.job,))
            c.commit()
        with closing(legacy_app.get_connection()) as c:
            current = c.execute("SELECT fx_rate,fx_locked_at FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertEqual(current["fx_rate"], "162.75")
            self.assertEqual(current["fx_locked_at"], quote["fx_locked_at"])

    def test_quote_items_and_totals_are_unchanged_by_snapshot(self):
        quote = self._generate()
        with closing(legacy_app.get_connection()) as c:
            item = c.execute("SELECT supplier_unit_cost,customer_unit_price,supplier_line_total,customer_line_total,line_profit FROM quote_items WHERE quote_id=?", (quote["id"],)).fetchone()
            self.assertEqual(tuple(item), (12.0, 18.0, 12.0, 18.0, 6.0))
            self.assertEqual(tuple(c.execute("SELECT parts_subtotal,shipping_total,sourcing_fee,service_charge,customer_total,supplier_total,profit_total FROM quotes WHERE id=?", (quote["id"],)).fetchone()), (18.0, 0.0, 0.0, 0.0, 18.0, 12.0, 6.0))
            for table in ("invoices", "invoice_items", "customer_transactions", "supplier_orders", "supplier_order_items", "payments", "receiving_events", "deliveries"):
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table} WHERE job_id=?", (self.job,)).fetchone()[0] if "job_id" in {r[1] for r in c.execute(f"PRAGMA table_info({table})")} else 0, 0)

    def test_quote_insert_failure_rolls_back_before_fx_completion(self):
        from plg_core.commercial import service as commercial_service
        original = commercial_service._insert_quote
        def insert_then_fail(*args, **kwargs):
            original_id = original(*args, **kwargs)
            raise RuntimeError("injected after quote insert")
        with patch("plg_core.commercial.service._insert_quote", side_effect=insert_then_fail), self.assertRaises(RuntimeError):
            self._generate()
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_items WHERE quote_id IN (SELECT id FROM quotes WHERE job_id=?)", (self.job,)).fetchone()[0], 0)

    def test_invalid_source_fails_through_generation(self):
        revision_id, _ = self._revision_tokens()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE work_revisions SET display_currency_mode='USD',fx_rate='160',fx_rate_source='UNKNOWN_SOURCE' WHERE id=?", (revision_id,))
            c.commit()
        with self.assertRaises(Exception):
            self._generate()
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_partial_snapshot_fails_through_generation(self):
        revision_id, _ = self._revision_tokens()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE work_revisions SET display_currency_mode='USD_JMD',fx_rate=NULL,fx_rate_source='BUSINESS_WORKING_RATE' WHERE id=?", (revision_id,))
            c.commit()
        with self.assertRaises(Exception):
            self._generate()
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_completed_quote_retry_does_not_rewrite_snapshot(self):
        quote = self._generate()
        before = (quote["id"], quote["fx_rate"], quote["fx_rate_source"], quote["fx_locked_at"])
        with closing(legacy_app.get_connection()) as c:
            basket = c.execute("SELECT id FROM baskets WHERE job_id=?", (self.job,)).fetchone()
            c.execute("UPDATE baskets SET customer_jmd_fx_rate_override='175' WHERE id=?", (basket["id"],))
            update_currency_settings(jmd_working_rate="170", connection=c)
            c.commit()
        revision_id, version = self._revision_tokens()
        response = asyncio.run(center_generate_quote(_Request({"csrf_token":"token", "expected_revision_id":str(revision_id), "expected_version":str(version)}), self.job))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            after = c.execute("SELECT id,fx_rate,fx_rate_source,fx_locked_at FROM quotes WHERE job_id=?", (self.job,)).fetchone()
            self.assertEqual(tuple(after), before)

    def test_legacy_invalid_fx_rolls_back_quote(self):
        revision_id, _ = self._revision_tokens()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE work_revisions SET display_currency_mode='USD',fx_rate='bad',fx_rate_source='BUSINESS_WORKING_RATE' WHERE id=?", (revision_id,))
            c.commit()
        with patch.object(legacy_app, "generate_quote_pdfs", lambda *_: None), self.assertRaises(Exception):
            legacy_app.generate_quote(self.job)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_all_null_initial_revision_freezes_once_and_partial_fails_closed(self):
        revision_id, _ = self._revision_tokens()
        with closing(legacy_app.get_connection()) as c:
            basket = c.execute("SELECT id FROM baskets WHERE job_id=?", (self.job,)).fetchone()
            first = ensure_initial_revision_currency_snapshot(c, revision_id, basket_id=basket["id"])
            update_currency_settings(jmd_working_rate="175", connection=c)
            second = ensure_initial_revision_currency_snapshot(c, revision_id, basket_id=basket["id"])
            self.assertEqual(first["fx_rate"], second["fx_rate"])
            c.execute("UPDATE work_revisions SET display_currency_mode='USD_JMD',fx_rate=NULL,fx_rate_source='BUSINESS_WORKING_RATE' WHERE id=?", (revision_id,))
            with self.assertRaises(Exception):
                ensure_initial_revision_currency_snapshot(c, revision_id, basket_id=basket["id"])

    def test_invalid_revision_snapshot_rejects_quote_creation(self):
        revision_id, version = self._revision_tokens()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE work_revisions SET display_currency_mode='USD',fx_rate='garbage',fx_rate_source='BUSINESS_WORKING_RATE' WHERE id=?", (revision_id,))
            c.commit()
        with self.assertRaises(Exception):
            create_selective_draft_quote(self.job, basket_item_ids=[self.item_id], expected_revision_id=revision_id, expected_version=version)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_legacy_generation_receives_complete_snapshot(self):
        with patch.object(legacy_app, "generate_quote_pdfs", lambda *_: None):
            response = legacy_app.generate_quote(self.job)
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT currency_code,display_currency_mode,fx_rate,fx_rate_source,fx_locked_at FROM quotes WHERE job_id=?", (self.job,)).fetchone()
            self.assertEqual(tuple(quote[:4]), ("USD", "USD", "160", "BUSINESS_WORKING_RATE"))
            self.assertIsNotNone(quote["fx_locked_at"])
