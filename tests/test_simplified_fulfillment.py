from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from app import app
from plg_core.basket.routes import basket_page
from plg_core.database.migrations import run_migrations
from plg_core.jobs.fulfillment import (
    fulfillment_snapshot,
    mark_fulfillment_item_delivered,
    mark_fulfillment_item_received,
    mark_job_ordered,
)


ROOT = Path(__file__).resolve().parents[1]


class SimplifiedFulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-simple-fulfillment-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.env_patch = patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(root / "documents")})
        self.db_patch.start()
        self.env_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            customer = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('SF-C','Synthetic Fulfillment Customer',1)"
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('SF-J','2026-08-19',?,'Synthetic Fulfillment Customer','CONFIRMED')",
                (customer,),
            ).lastrowid
            quote_id = connection.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,supplier_total,profit_total) VALUES ('SF-Q',?,'2026-08-19','CONVERTED',300,150,150)",
                (self.job_id,),
            ).lastrowid
            self.invoice_id = connection.execute(
                "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,supplier_total,profit_total,balance_due) VALUES ('SF-I',?,?,'2026-08-19','PAID',300,150,150,0)",
                (quote_id, self.job_id),
            ).lastrowid
            for description, supplier, quantity, cost, price in (
                ("Brake Drums", "Supplier A", 2, 40, 80),
                ("Hub", "Supplier A", 1, 30, 60),
                ("Seal", "Supplier B", 1, 10, 20),
            ):
                connection.execute(
                    """INSERT INTO invoice_items(
                         invoice_id,quantity,description,supplier_name,supplier_unit_cost,
                         supplier_line_total,customer_unit_price,customer_line_total,line_profit
                       ) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (self.invoice_id, quantity, description, supplier, cost, cost * quantity,
                     price, price * quantity, (price - cost) * quantity),
                )
            connection.commit()
        self.source_before = self._source_state()

    def tearDown(self):
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def _source_state(self):
        with closing(legacy_app.get_connection()) as connection:
            invoice = tuple(connection.execute(
                "SELECT status,customer_total,supplier_total,profit_total,balance_due FROM invoices WHERE id=?",
                (self.invoice_id,),
            ).fetchone())
            items = [tuple(row) for row in connection.execute(
                "SELECT id,quantity,description,supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total,line_profit FROM invoice_items WHERE invoice_id=? ORDER BY id",
                (self.invoice_id,),
            ).fetchall()]
        return invoice, items

    def _ordered(self):
        result = mark_job_ordered(self.job_id)
        return result, {item["description"]: item for item in result["items"]}

    def _render_job(self):
        path = f"/jobs/{self.job_id}/basket"
        request = Request({
            "type": "http", "method": "GET", "scheme": "http",
            "path": path, "raw_path": path.encode(), "root_path": "",
            "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
            "server": ("test", 80), "app": app, "router": app.router,
        })
        return basket_page(request, self.job_id)

    def test_payment_makes_fulfillment_available_and_ordering_tracks_each_line(self):
        self.assertTrue(fulfillment_snapshot(self.job_id)["available"])
        result, items = self._ordered()
        self.assertEqual(result["stage"], "ORDERED")
        self.assertEqual(result["counts"], {"total": 3, "ordered": 3, "received": 0, "delivered": 0})
        self.assertEqual(list(items), ["Brake Drums", "Hub", "Seal"])

    def test_unpaid_invoice_does_not_make_fulfillment_available(self):
        with closing(legacy_app.get_connection()) as connection:
            connection.execute("UPDATE invoices SET status='UNPAID',balance_due=300 WHERE id=?", (self.invoice_id,))
            connection.commit()
        self.assertFalse(fulfillment_snapshot(self.job_id)["available"])
        with self.assertRaises(HTTPException):
            mark_job_ordered(self.job_id)

    def test_individual_receiving_and_separate_lines(self):
        _, items = self._ordered()
        result = mark_fulfillment_item_received(self.job_id, items["Hub"]["id"])
        states = {item["description"]: item["received"] for item in result["items"]}
        self.assertEqual(states, {"Brake Drums": False, "Hub": True, "Seal": False})
        self.assertEqual(result["counts"]["received"], 1)
        self.assertEqual(result["stage"], "RECEIVING")

    def test_multiple_items_can_be_received_gradually(self):
        _, items = self._ordered()
        mark_fulfillment_item_received(self.job_id, items["Hub"]["id"])
        result = mark_fulfillment_item_received(self.job_id, items["Seal"]["id"])
        self.assertEqual(result["counts"], {"total": 3, "ordered": 3, "received": 2, "delivered": 0})

    def test_received_item_can_be_delivered_while_others_are_outstanding(self):
        _, items = self._ordered()
        mark_fulfillment_item_received(self.job_id, items["Hub"]["id"])
        result = mark_fulfillment_item_delivered(self.job_id, items["Hub"]["id"])
        self.assertEqual(result["counts"], {"total": 3, "ordered": 3, "received": 1, "delivered": 1})
        self.assertEqual(result["stage"], "DELIVERED")
        with closing(legacy_app.get_connection()) as connection:
            self.assertNotEqual(connection.execute("SELECT status FROM jobs WHERE id=?", (self.job_id,)).fetchone()[0], "COMPLETED")

    def test_unreceived_item_cannot_be_delivered(self):
        _, items = self._ordered()
        with self.assertRaises(HTTPException) as raised:
            mark_fulfillment_item_delivered(self.job_id, items["Seal"]["id"])
        self.assertEqual(raised.exception.status_code, 409)

    def test_all_delivered_marks_job_completed(self):
        _, items = self._ordered()
        for item in items.values():
            mark_fulfillment_item_received(
                self.job_id, item["id"], quantity=item["quantity_ordered"]
            )
            result = mark_fulfillment_item_delivered(
                self.job_id, item["id"], quantity=item["quantity_ordered"]
            )
        self.assertEqual(result["counts"], {"total": 3, "ordered": 3, "received": 3, "delivered": 3})
        self.assertEqual(result["stage"], "COMPLETED")
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT status FROM jobs WHERE id=?", (self.job_id,)).fetchone()[0], "COMPLETED")

    def test_fulfillment_never_changes_invoice_or_source_line_accounting(self):
        _, items = self._ordered()
        mark_fulfillment_item_received(self.job_id, items["Brake Drums"]["id"])
        mark_fulfillment_item_delivered(self.job_id, items["Brake Drums"]["id"])
        self.assertEqual(self._source_state(), self.source_before)

    def test_quantity_two_item_remains_partial_until_both_units_delivered(self):
        _, items = self._ordered()
        drums = items["Brake Drums"]
        for description in ("Hub", "Seal"):
            mark_fulfillment_item_received(self.job_id, items[description]["id"], quantity=1)
            mark_fulfillment_item_delivered(self.job_id, items[description]["id"], quantity=1)

        received_one = mark_fulfillment_item_received(
            self.job_id, drums["id"], quantity=1
        )
        line = next(item for item in received_one["items"] if item["id"] == drums["id"])
        self.assertEqual(
            (line["quantity_ordered"], line["quantity_received"],
             line["quantity_delivered"], line["quantity_remaining"]),
            (2, 1, 0, 2),
        )
        self.assertFalse(line["received"])

        delivered_one = mark_fulfillment_item_delivered(
            self.job_id, drums["id"], quantity=1
        )
        line = next(item for item in delivered_one["items"] if item["id"] == drums["id"])
        self.assertEqual(
            (line["quantity_received"], line["quantity_delivered"],
             line["quantity_remaining"], line["quantity_to_receive"],
             line["quantity_available_to_deliver"]),
            (1, 1, 1, 1, 0),
        )
        self.assertFalse(line["received"])
        self.assertFalse(line["delivered"])
        with closing(legacy_app.get_connection()) as connection:
            self.assertNotEqual(
                connection.execute("SELECT status FROM jobs WHERE id=?", (self.job_id,)).fetchone()[0],
                "COMPLETED",
            )

        mark_fulfillment_item_received(self.job_id, drums["id"], quantity=1)
        completed = mark_fulfillment_item_delivered(self.job_id, drums["id"], quantity=1)
        line = next(item for item in completed["items"] if item["id"] == drums["id"])
        self.assertEqual(
            (line["quantity_received"], line["quantity_delivered"], line["quantity_remaining"]),
            (2, 2, 0),
        )
        self.assertTrue(line["received"])
        self.assertTrue(line["delivered"])
        self.assertEqual(completed["stage"], "COMPLETED")
        self.assertEqual(self._source_state(), self.source_before)

    def test_command_center_uses_simple_item_checklist(self):
        source = (ROOT / "templates" / "job_command_center.html").read_text()
        for text in ("Fulfillment", "Create Supplier Order", "Received", "Delivered", "fulfillment-progress"):
            self.assertIn(text, source)

    def test_paid_job_with_fulfillment_rows_renders(self):
        self._ordered()
        response = self._render_job()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Brake Drums", response.body)
        self.assertIn(b"Ordered 3/3", response.body)

    def test_paid_job_with_zero_supplier_orders_renders(self):
        response = self._render_job()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Create Supplier Order", response.body)
        self.assertIn(b"Ordered 0/0", response.body)

    def test_command_center_prioritizes_summary_and_collapses_secondary_sections(self):
        source = (ROOT / "templates" / "job_command_center.html").read_text()
        self.assertLess(source.index("job-primary-summary"), source.index('id="fulfillment-checklist"'))
        self.assertLess(source.index('id="fulfillment-checklist"'), source.index("Supplier Orders ("))
        for summary in (
            "Supplier Orders (", "Documents (", "Recent Activity",
            "Asset / Equipment Context", "Research", "RESEARCH CANDIDATES",
            "Items Ready for Quote", "Additional Charges", "Uncategorized / Draft Work",
            "Quote History", "Supplier Orders",
        ):
            self.assertIn(summary, source)
        for group in (
            "Sourcing &amp; Fulfillment", "Financial &amp; Customer",
            "Documents &amp; Activity", "Job Administration",
        ):
            self.assertIn(group, source)
        self.assertNotIn('<details class="cc-card job-disclosure" id="operational-orders" open', source)


if __name__ == "__main__":
    unittest.main()
