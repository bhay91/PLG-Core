import shutil
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import legacy_app
from fastapi import HTTPException
from plg_core.currency.service import build_invoice_currency_presentation, update_currency_settings
from plg_core.database.migrations import run_migrations
from plg_core.documents import invoice_pdf
from plg_core.sales.service import record_invoice_payment


class CurrencyCInvoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pps-currency-c-")
        root = Path(self.tmp.name)
        self.db = root / "db.sqlite"
        shutil.copy2(Path(__file__).parents[1] / "data" / "plg_core.db", self.db)
        self.patches = [patch.object(legacy_app, "DB_PATH", self.db), patch.object(invoice_pdf, "DOCUMENT_ROOT", root / "documents" / "Customers")]
        for item in self.patches: item.start()
        self.addCleanup(self.cleanup)
        run_migrations()

    def cleanup(self):
        for item in reversed(self.patches): item.stop()
        self.tmp.cleanup()

    def _quote(self, *, modern=True, status="APPROVED"):
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,active) VALUES (?, 'C Customer',1)", (f"C-C-{c.execute('SELECT COALESCE(MAX(id),0)+1 FROM customers').fetchone()[0]}",)).lastrowid
            job = c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES (?, '2026-01-01',?,'C Customer','REQUESTED')", (f"C-J-{c.execute('SELECT COALESCE(MAX(id),0)+1 FROM jobs').fetchone()[0]}", customer)).lastrowid
            values = ("USD", "USD_JMD", "165", "MANUAL_OVERRIDE", "2026-01-01 10:00:00") if modern else (None, None, None, None, None)
            number = f"PPS-Q-{c.execute('SELECT COALESCE(MAX(id),0)+1 FROM quotes').fetchone()[0]:04d}"
            q = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,parts_subtotal,customer_total,supplier_total,profit_total,is_current,currency_code,display_currency_mode,fx_rate,fx_rate_source,fx_locked_at) VALUES (?,?,'2026-01-01',?,1000,1000,700,300,1,?,?,?,?,?)", (number,job,status,*values)).lastrowid
            c.commit()
            return q

    def test_modern_quote_inherits_immutable_snapshot_and_payment_stays_usd(self):
        quote_id = self._quote()
        legacy_app.convert_quote_to_invoice(quote_id)
        with closing(legacy_app.get_connection()) as c:
            invoice = c.execute("SELECT * FROM invoices WHERE quote_id=?", (quote_id,)).fetchone()
            before = tuple(invoice[k] for k in ("currency_code", "display_currency_mode", "fx_rate", "fx_rate_source", "fx_locked_at"))
            update_currency_settings(jmd_working_rate="180", connection=c)
            c.commit()
        self.assertEqual(before[:4], ("USD", "USD_JMD", "165", "MANUAL_OVERRIDE"))
        self.assertEqual(build_invoice_currency_presentation(invoice)["jmd_total"], "J$165,000.00")
        record_invoice_payment(invoice["id"], 250, "CARD")
        with closing(legacy_app.get_connection()) as c:
            current = c.execute("SELECT * FROM invoices WHERE id=?", (invoice["id"],)).fetchone()
            self.assertEqual(current["balance_due"], 750)
            self.assertEqual(tuple(current[k] for k in ("currency_code", "display_currency_mode", "fx_rate", "fx_rate_source", "fx_locked_at")), before)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM customer_transactions WHERE invoice_id=? AND transaction_type='PAYMENT'", (invoice["id"],)).fetchone()[0], 1)

    def test_legacy_quote_remains_usd_without_invented_fx(self):
        quote_id = self._quote(modern=False)
        legacy_app.convert_quote_to_invoice(quote_id)
        with closing(legacy_app.get_connection()) as c:
            invoice = c.execute("SELECT currency_code,display_currency_mode,fx_rate,fx_rate_source,fx_locked_at FROM invoices WHERE quote_id=?", (quote_id,)).fetchone()
        self.assertEqual(tuple(invoice), (None, None, None, None, None))
        self.assertIsNone(build_invoice_currency_presentation(invoice)["jmd_total"])

    def test_malformed_modern_quote_fails_closed(self):
        quote_id = self._quote()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE quotes SET fx_locked_at=NULL WHERE id=?", (quote_id,))
            c.commit()
        with self.assertRaises(HTTPException):
            legacy_app.convert_quote_to_invoice(quote_id)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices WHERE quote_id=?", (quote_id,)).fetchone()[0], 0)

    def test_strict_snapshot_variants_fail_without_side_effects(self):
        variants = (
            {"display_currency_mode": None},
            {"currency_code": None},
            {"fx_locked_at": None},
            {"currency_code": "CAD"},
            {"fx_rate": "0"},
            {"fx_rate": "-1"},
            {"fx_rate": "NaN"},
            {"fx_rate_source": "UNKNOWN"},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                quote_id = self._quote()
                with closing(legacy_app.get_connection()) as c:
                    c.execute("UPDATE quotes SET " + ",".join(f"{k}=?" for k in changes) + " WHERE id=?", (*changes.values(), quote_id))
                    c.commit()
                with self.assertRaises(HTTPException):
                    legacy_app.convert_quote_to_invoice(quote_id)
                with closing(legacy_app.get_connection()) as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices WHERE quote_id=?", (quote_id,)).fetchone()[0], 0)
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM customer_transactions WHERE quote_id=?", (quote_id,)).fetchone()[0], 0)

    def test_legacy_jmd_fields_remain_compatible(self):
        invoice = {"customer_total": 1000, "show_jmd_total": 1, "jmd_exchange_rate": "160"}
        presentation = build_invoice_currency_presentation(invoice)
        self.assertEqual((presentation["jmd_total"], presentation["rate"]), ("J$160,000.00", "160"))

    def test_migration_is_idempotent_and_snapshot_timestamp_is_independent(self):
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            cols = {row[1] for row in c.execute("PRAGMA table_info(invoices)")}
            self.assertTrue({"currency_code", "display_currency_mode", "fx_rate", "fx_rate_source", "fx_locked_at"}.issubset(cols))
        quote_id = self._quote()
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT fx_locked_at FROM quotes WHERE id=?", (quote_id,)).fetchone()
        legacy_app.convert_quote_to_invoice(quote_id)
        with closing(legacy_app.get_connection()) as c:
            invoice = c.execute("SELECT fx_locked_at FROM invoices WHERE quote_id=?", (quote_id,)).fetchone()
        self.assertIsNotNone(invoice[0])
        self.assertNotEqual(invoice[0], quote[0])

    def test_reference_balance_uses_locked_rate_after_global_change(self):
        quote_id = self._quote()
        legacy_app.convert_quote_to_invoice(quote_id)
        with closing(legacy_app.get_connection()) as c:
            invoice = c.execute("SELECT * FROM invoices WHERE quote_id=?", (quote_id,)).fetchone()
            update_currency_settings(jmd_working_rate="180", connection=c); c.commit()
        self.assertEqual(build_invoice_currency_presentation(invoice, usd_amount="750")["jmd_total"], "J$123,750.00")

    def test_modern_invoice_pdf_contains_reference_without_internal_enum(self):
        quote_id = self._quote()
        legacy_app.convert_quote_to_invoice(quote_id)
        with closing(legacy_app.get_connection()) as c:
            invoice = c.execute("SELECT * FROM invoices WHERE quote_id=?", (quote_id,)).fetchone()
        path = Path(self.tmp.name) / "invoice.pdf"
        invoice_pdf.build_invoice_pdf(invoice, [], path, internal=False)
        text = subprocess.run(["pdftotext", str(path), "-"], check=True, capture_output=True, text=True).stdout
        self.assertIn("JMD Total", text)
        self.assertIn("165,000.00", text)
        self.assertNotIn("MANUAL_OVERRIDE", text)


if __name__ == "__main__":
    unittest.main()
