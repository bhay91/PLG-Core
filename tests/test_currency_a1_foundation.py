import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import legacy_app
from plg_core.currency.service import (
    canonical_rate,
    convert_usd_to_jmd,
    get_currency_settings,
    resolve_basket_currency_config,
    update_currency_settings,
)
from plg_core.database.migrations import run_migrations


class CurrencyA1FoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pps-currency-a1-")
        self.db = Path(self.tmp.name) / "db.sqlite"
        shutil.copy2("data/plg_core.db", self.db)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db)
        self.db_patch.start()
        self.addCleanup(self.cleanup)
        run_migrations()

    def cleanup(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_schema_and_singleton_defaults(self):
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM currency_settings").fetchone()[0], 1)
            self.assertEqual(get_currency_settings(c)["base_currency"], "USD")
            self.assertEqual(get_currency_settings(c)["jmd_working_rate"], "160")
            self.assertEqual(get_currency_settings(c)["default_display_mode"], "USD")
            self.assertEqual(c.execute("SELECT typeof(jmd_working_rate) FROM currency_settings").fetchone()[0], "text")
            self.assertTrue({r[1] for r in c.execute("PRAGMA table_info(quotes)")} >= {"currency_code", "display_currency_mode", "fx_rate", "fx_rate_source", "fx_locked_at"})
            self.assertTrue({r[1] for r in c.execute("PRAGMA table_info(work_revisions)")} >= {"display_currency_mode", "fx_rate", "fx_rate_source"})

    def test_migration_is_idempotent_and_legacy_invoice_fields_survive(self):
        with closing(legacy_app.get_connection()) as c:
            c.execute("CREATE TABLE IF NOT EXISTS invoices (id INTEGER PRIMARY KEY, show_jmd_total INTEGER, jmd_exchange_rate TEXT)")
            c.commit()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM currency_settings").fetchone()[0], 1)
            columns = {r[1] for r in c.execute("PRAGMA table_info(invoices)")}
            self.assertIn("show_jmd_total", columns)
            self.assertIn("jmd_exchange_rate", columns)

    def test_settings_updates_and_audit(self):
        with closing(legacy_app.get_connection()) as c:
            updated = update_currency_settings(jmd_working_rate="165.00", default_display_mode="USD_JMD", actor="tester", connection=c)
            c.commit()
            self.assertEqual(updated["jmd_working_rate"], "165")
            self.assertEqual(updated["default_display_mode"], "USD_JMD")
            audit = c.execute("SELECT action,metadata_json FROM audit_logs WHERE action='CURRENCY_SETTINGS_UPDATED'").fetchone()
            self.assertIsNotNone(audit)
            self.assertIn("165", audit["metadata_json"])

    def test_invalid_rates_and_modes_rejected(self):
        for value in (0, -1, "", "NaN", "Infinity", "not-a-rate", True):
            with self.subTest(value=value), self.assertRaises(ValueError): canonical_rate(value)
        with closing(legacy_app.get_connection()) as c:
            with self.assertRaises(ValueError): update_currency_settings(default_display_mode="GBP", connection=c)

    def test_decimal_conversion(self):
        self.assertEqual(convert_usd_to_jmd(Decimal("18"), "160"), Decimal("2880.00"))
        self.assertEqual(convert_usd_to_jmd("10.01", "160.5"), Decimal("1606.61"))
        self.assertEqual(convert_usd_to_jmd("0", "160"), Decimal("0.00"))
        self.assertEqual(convert_usd_to_jmd("0.005", "160"), Decimal("0.80"))
        for amount in (None, True, "bad", "NaN", "Infinity"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                convert_usd_to_jmd(amount, "160")

    def test_null_basket_follows_later_global_defaults(self):
        with closing(legacy_app.get_connection()) as c:
            job_id = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES('CUR-J2','2026-01-01','Customer','REQUESTED')").lastrowid
            basket_id = c.execute("INSERT INTO baskets(job_id) VALUES(?)", (job_id,)).lastrowid
            c.commit()
            self.assertEqual(resolve_basket_currency_config(c, basket_id)["jmd_working_rate"], "160")
            update_currency_settings(jmd_working_rate="165", default_display_mode="USD_JMD", connection=c)
            c.commit()
            resolved = resolve_basket_currency_config(c, basket_id)
            self.assertEqual(resolved["jmd_working_rate"], "165")
            self.assertEqual(resolved["display_currency_mode"], "USD_JMD")
            self.assertEqual(resolved["fx_rate_source"], "BUSINESS_WORKING_RATE")
            row = c.execute("SELECT customer_display_currency_mode_override,customer_jmd_fx_rate_override FROM baskets WHERE id=?", (basket_id,)).fetchone()
            self.assertEqual(tuple(row), (None, None))

    def test_basket_overrides_resolve_without_changing_source_currency(self):
        with closing(legacy_app.get_connection()) as c:
            job_id = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES('CUR-J','2026-01-01','Customer','REQUESTED')").lastrowid
            basket_id = c.execute("INSERT INTO baskets(job_id,currency) VALUES(?, 'CAD')", (job_id,)).lastrowid
            c.commit()
            effective = resolve_basket_currency_config(c, basket_id)
            self.assertEqual(effective["display_currency_mode"], "USD")
            self.assertEqual(effective["jmd_working_rate"], "160")
            self.assertEqual(effective["fx_rate_source"], "BUSINESS_WORKING_RATE")
            from plg_core.basket.service import set_customer_display_fx, clear_customer_display_fx
            set_customer_display_fx(c, basket_id, display_mode="JMD", jmd_rate="162.75")
            c.commit()
            effective = resolve_basket_currency_config(c, basket_id)
            self.assertEqual(effective["display_currency_mode"], "JMD")
            self.assertEqual(effective["jmd_working_rate"], "162.75")
            self.assertEqual(effective["fx_rate_source"], "MANUAL_OVERRIDE")
            self.assertEqual(c.execute("SELECT currency FROM baskets WHERE id=?", (basket_id,)).fetchone()[0], "CAD")
            set_customer_display_fx(c, basket_id, jmd_rate="165")
            self.assertEqual(resolve_basket_currency_config(c, basket_id)["display_currency_mode"], "JMD")
            set_customer_display_fx(c, basket_id, display_mode="USD_JMD")
            self.assertEqual(resolve_basket_currency_config(c, basket_id)["jmd_working_rate"], "165")
            update_currency_settings(jmd_working_rate="170", default_display_mode="USD", connection=c)
            self.assertEqual(resolve_basket_currency_config(c, basket_id)["jmd_working_rate"], "165")
            clear_customer_display_fx(c, basket_id, jmd_rate=True)
            self.assertEqual(resolve_basket_currency_config(c, basket_id)["display_currency_mode"], "USD_JMD")
            self.assertEqual(resolve_basket_currency_config(c, basket_id)["jmd_working_rate"], "170")
            clear_customer_display_fx(c, basket_id, display_mode=True)
            self.assertEqual(resolve_basket_currency_config(c, basket_id)["display_currency_mode"], "USD")
            clear_customer_display_fx(c, basket_id, display_mode=True, jmd_rate=True)
            c.commit()
            effective = resolve_basket_currency_config(c, basket_id)
            self.assertEqual(effective["display_currency_mode"], "USD")
            self.assertEqual(effective["jmd_working_rate"], "170")

    def test_quotes_and_revisions_fx_columns_are_nullable_for_legacy_rows(self):
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT currency_code,display_currency_mode,fx_rate,fx_rate_source,fx_locked_at FROM quotes LIMIT 1").fetchone()
            if quote:
                self.assertTrue(all(value is None for value in quote))
            revision = c.execute("SELECT display_currency_mode,fx_rate,fx_rate_source FROM work_revisions LIMIT 1").fetchone()
            if revision:
                self.assertTrue(all(value is None for value in revision))

    def test_currency_operations_do_not_touch_accounting_tables(self):
        with closing(legacy_app.get_connection()) as c:
            before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("invoices", "invoice_items", "customer_transactions")}
            update_currency_settings(jmd_working_rate="161", connection=c)
            after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
