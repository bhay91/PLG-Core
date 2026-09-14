import shutil
import tempfile
import unittest
import asyncio
from starlette.requests import Request
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import legacy_app
from plg_core.currency.service import get_currency_settings
from plg_core.database.migrations import run_migrations


class CurrencyBAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pps-currency-b-admin-")
        root = Path(self.tmp.name)
        self.db = root / "db.sqlite"
        shutil.copy2(Path(__file__).parents[1] / "data" / "plg_core.db", self.db)
        self.patches = [patch.object(legacy_app, "DB_PATH", self.db), patch.object(legacy_app, "DOCUMENTS_DIR", root / "documents")]
        for item in self.patches:
            item.start()
        legacy_app.initialize_database()
        run_migrations()
        self.addCleanup(self.cleanup)

    def request(self, method, path, *, data=None, cookies=None):
        body = b"" if data is None else b"&".join(f"{k}={v}".encode() for k, v in data.items())
        headers = [(b"content-type", b"application/x-www-form-urlencoded")] if body else []
        if cookies:
            headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        return Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(), "scheme": "http", "headers": headers, "query_string": b"", "server": ("testserver", 80), "client": ("testclient", 1), "app": legacy_app.app}, receive)

    def cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_currency_page_and_csrf(self):
        request = self.request("GET", "/admin/currency")
        response = legacy_app.admin_currency(request)
        self.assertEqual(response.status_code, 200)
        body = response.body.decode()
        self.assertIn("USD", body)
        self.assertIn("160", body)
        self.assertIn('readonly', body)
        token = response.headers.get("set-cookie").split("pps_csrf_token=", 1)[1].split(";", 1)[0]
        rejected_request = self.request("POST", "/admin/currency", data={"jmd_working_rate": "165", "default_display_mode": "USD_JMD"})
        with self.assertRaises(Exception) as error:
            asyncio.run(legacy_app.save_admin_currency(rejected_request))
        self.assertEqual(getattr(error.exception, "status_code", None), 403)
        self.assertTrue(token)

    def test_valid_update_and_fractional_canonicalization(self):
        response = legacy_app.admin_currency(self.request("GET", "/admin/currency"))
        token = response.headers.get("set-cookie").split("pps_csrf_token=", 1)[1].split(";", 1)[0]
        response = asyncio.run(legacy_app.save_admin_currency(self.request("POST", "/admin/currency", data={"csrf_token": token, "jmd_working_rate": "162.75", "default_display_mode": "USD_JMD"}, cookies={"pps_csrf_token": token})))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            settings = get_currency_settings(connection)
        self.assertEqual((settings["jmd_working_rate"], settings["default_display_mode"]), ("162.75", "USD_JMD"))

    def test_invalid_updates_leave_settings_and_quotes_unchanged(self):
        with closing(legacy_app.get_connection()) as connection:
            job_id = connection.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES ('B-J','2026-01-01','B Customer','REQUESTED')").lastrowid
            quote_id = connection.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,is_current,currency_code,display_currency_mode,fx_rate,fx_rate_source,fx_locked_at) VALUES ('B-Q',?,'2026-01-01','DRAFT',18,1,'USD','USD_JMD','160','BUSINESS_WORKING_RATE','2026-01-01 00:00:00')", (job_id,)).lastrowid
            connection.commit()
        get_response = legacy_app.admin_currency(self.request("GET", "/admin/currency"))
        token = get_response.headers.get("set-cookie").split("pps_csrf_token=", 1)[1].split(";", 1)[0]
        with closing(legacy_app.get_connection()) as connection:
            before = dict(get_currency_settings(connection))
        for rate, mode in (("0", "USD"), ("-1", "USD"), ("NaN", "USD"), ("Infinity", "USD"), ("165", "GBP")):
            response = asyncio.run(legacy_app.save_admin_currency(self.request("POST", "/admin/currency", data={"csrf_token": token, "jmd_working_rate": rate, "default_display_mode": mode}, cookies={"pps_csrf_token": token})))
            self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as connection:
            after = dict(get_currency_settings(connection))
            quote = connection.execute("SELECT fx_rate,fx_rate_source,fx_locked_at FROM quotes WHERE id=?", (quote_id,)).fetchone()
            invoices = connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual((after["jmd_working_rate"], after["default_display_mode"]), (before["jmd_working_rate"], before["default_display_mode"]))
        self.assertEqual(tuple(quote), ("160", "BUSINESS_WORKING_RATE", "2026-01-01 00:00:00"))
        self.assertEqual(invoices, 0)


if __name__ == "__main__":
    unittest.main()
