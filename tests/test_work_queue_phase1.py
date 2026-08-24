from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

from starlette.requests import Request

import legacy_app
from plg_core.dashboard.service import (
    WORK_QUEUE_CATEGORIES,
    get_recent_jobs,
    get_work_queue_data,
)
from plg_core.jobs.engine import JobEngine


ROOT = Path(__file__).resolve().parents[1]


def job_row(number, *, selected=1, quote_status=None, invoice_status=None, status="VERIFIED"):
    job_id = int(number[1:])
    return {
        "id": job_id, "job_number": number, "created_date": "2026-08-01",
        "customer": f"Customer {number}", "company": "", "customer_id": job_id,
        "manufacturer": "CAT", "machine": "420D", "machine_id": job_id,
        "pin_serial": f"PIN-{number}", "status": status, "is_archived": 0,
        "selected_items": selected, "quote_id": job_id if quote_status else None,
        "quote_status": quote_status, "invoice_id": job_id if invoice_status else None,
        "invoice_status": invoice_status, "receiving_order_id": job_id,
        "draft_order_id": None, "quote_waiting_since": "2026-08-02",
        "invoice_waiting_since": "2026-08-03", "order_waiting_since": "2026-08-04",
        "outstanding_order_quantity": 1,
        "requested_need_wording": None,
        "quote_number": f"PPS-Q-{job_id:04d}" if quote_status else None,
        "quote_total": 1000 if quote_status else None,
        "invoice_number": f"PPS-INV-{job_id:04d}" if invoice_status else None,
        "balance_due": 3175.57 if invoice_status else None,
        "open_supplier_count": 1, "open_supplier_name": "Synthetic Supplier",
        "open_order_number": f"PPS-PO-{job_id:04d}",
        "open_order_expected_at": "2026-08-20",
    }


class WorkQueuePhase1Tests(unittest.TestCase):
    def connection(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript("""
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,job_number TEXT,customer TEXT,company TEXT,
                manufacturer TEXT,machine TEXT,status TEXT,is_archived INTEGER DEFAULT 0);
            CREATE TABLE job_assets(id INTEGER PRIMARY KEY,job_id INTEGER,name TEXT,
                manufacturer TEXT,model TEXT);
            CREATE TABLE requested_needs(id INTEGER PRIMARY KEY,job_id INTEGER,job_asset_id INTEGER,
                wording TEXT,state TEXT,created_at TEXT);
            CREATE TABLE baskets(id INTEGER PRIMARY KEY,job_id INTEGER);
            CREATE TABLE basket_items(id INTEGER PRIMARY KEY,basket_id INTEGER,primary_requested_need_id INTEGER,
                selected INTEGER,supplier_unit_cost REAL,pricing_mode TEXT,supplier_name TEXT);
            CREATE TABLE basket_item_need_links(basket_item_id INTEGER,requested_need_id INTEGER);
            CREATE TABLE job_follow_ups(id INTEGER PRIMARY KEY,job_id INTEGER,job_asset_id INTEGER,
                requested_need_id INTEGER,category TEXT,summary TEXT,reason TEXT,resolution TEXT,
                status TEXT,requested_at TEXT,received_at TEXT);
        """)
        for job_id in (1, 2, 3):
            c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,0)",
                      (job_id, f"J{job_id}", f"Customer J{job_id}", "", "CAT", "420D", "REQUESTED"))
            c.execute("INSERT INTO job_assets VALUES (?,?,?,?,?)", (job_id, job_id, "CAT 420D", "CAT", "420D"))
            c.execute("INSERT INTO requested_needs VALUES (?,?,?,?,?,?)",
                      (job_id, job_id, job_id, ("Starter", "Turbo", "Radiator")[job_id-1], "OPEN", "2026-08-01"))
            c.execute("INSERT INTO baskets VALUES (?,?)", (job_id, job_id))
        c.execute("INSERT INTO basket_items VALUES (1,2,2,0,NULL,'AUTO','Synthetic Supplier')")
        c.execute("INSERT INTO basket_items VALUES (2,3,3,1,25,'AUTO','CAT')")
        c.commit()
        return c

    def jobs(self):
        return [
            job_row("J1", selected=0, status="REQUESTED"),
            job_row("J2", selected=0, status="REQUESTED"),
            job_row("J3", selected=1),
            job_row("J4", quote_status="SENT"),
            job_row("J5", quote_status="APPROVED"),
            job_row("J6", quote_status="APPROVED", invoice_status="UNPAID"),
            job_row("J7", quote_status="APPROVED", invoice_status="PAID"),
            job_row("J8", quote_status="APPROVED", invoice_status="PAID", status="ORDERED"),
            job_row("J9", quote_status="APPROVED", invoice_status="PAID", status="RECEIVED"),
            job_row("J10", quote_status="APPROVED", invoice_status="PAID", status="DELIVERED"),
            {**job_row("J11", selected=0, status="CANCELLED"), "is_archived": 1},
        ]

    def test_all_ten_categories_routes_age_and_read_only_projection(self):
        c = self.connection()
        with patch("plg_core.dashboard.service.get_recent_jobs", return_value=self.jobs()):
            before = c.total_changes
            result = get_work_queue_data(c, today=date(2026, 8, 12))
            self.assertEqual(c.total_changes, before)
        expected = {key for key, _label in WORK_QUEUE_CATEGORIES}
        self.assertEqual({row["category"] for row in result["items"]}, expected)
        need = next(row for row in result["items"] if row["category"] == "NEEDS_RESEARCH")
        self.assertEqual((need["customer"], need["machine"], need["need_action"]),
                         ("Customer J1", "CAT 420D", "Starter"))
        self.assertEqual(need["age_days"], 11)
        self.assertIn("asset_id=1&need_id=1", need["url"])
        waiting_price = next(
            row for row in result["items"]
            if row["category"] == "WAITING_SUPPLIER_PRICING"
        )
        self.assertEqual(
            (waiting_price["need_action"], waiting_price["next_action"], waiting_price["url"]),
            ("Turbo", "Add Supplier Price", "/jobs/2/basket?asset_id=2&need_id=2#research-results"),
        )
        ready = next(
            row for row in result["items"]
            if row["category"] == "READY_TO_QUOTE" and row["job_number"] == "J3"
        )
        self.assertEqual(
            (ready["need_action"], ready["next_action"], ready["url"]),
            ("Radiator", "Confirm for Quote", "/jobs/3/basket?asset_id=3&need_id=3#parts-ready"),
        )
        self.assertEqual(result["counts"]["READY_TO_ORDER"], 1)
        self.assertNotIn("J11", {row["job_number"] for row in result["items"]})
        c.close()

    def test_category_empty_and_follow_up_filters(self):
        c = self.connection()
        c.execute("INSERT INTO job_follow_ups VALUES (1,1,1,1,'CUSTOMER_INFORMATION',"
                  "'Need photo','Confirm connector','','OPEN','2026-08-10',NULL)")
        c.commit()
        with patch("plg_core.dashboard.service.get_recent_jobs", return_value=self.jobs()):
            followups = get_work_queue_data(c, category="FOLLOW_UP")
            empty = get_work_queue_data(c, category="READY_TO_ORDER", limit=1)
        self.assertTrue(followups["items"])
        self.assertTrue(all(row["is_follow_up"] for row in followups["items"]))
        manual = next(row for row in followups["items"] if row["key"] == "follow-up:1")
        self.assertEqual(manual["url"], "/follow-up")
        self.assertEqual(
            len([row for row in followups["items"] if row["key"] == "follow-up:1"]),
            1,
        )
        service_source = (ROOT / "plg_core" / "dashboard" / "service.py").read_text()
        self.assertNotIn('basket#follow-ups', service_source)
        self.assertEqual(service_source.count('"url": "/follow-up"'), 1)
        # Filtering is safe even where the bounded visible result is empty.
        self.assertEqual(empty["selected_category"], "READY_TO_ORDER")
        c.close()

    def test_delivered_empty_current_basket_is_terminal_not_research(self):
        delivered = job_row(
            "J10", selected=0, quote_status="APPROVED",
            invoice_status="PAID", status="DELIVERED",
        )
        intelligence = JobEngine.evaluate(
            delivered,
            selected_items=0,
            research_items=0,
            quote={"id": 10, "status": "APPROVED"},
            invoice={"id": 10, "status": "PAID"},
        )
        self.assertEqual(intelligence.workflow_stage, "COMPLETE")

        c = self.connection()
        with patch(
            "plg_core.dashboard.service.get_recent_jobs",
            return_value=[delivered],
        ):
            result = get_work_queue_data(c, today=date(2026, 8, 12))
        rows = [row for row in result["items"] if row["job_number"] == "J10"]
        self.assertEqual([row["category"] for row in rows], ["COMPLETE"])
        self.assertEqual(result["counts"]["NEEDS_RESEARCH"], 0)
        c.close()

    def test_durable_ordered_and_received_states_outrank_empty_basket(self):
        for status, expected in (
            ("ORDERED", "WAITING_PARTS"),
            ("RECEIVED", "READY_TO_COMPLETE"),
        ):
            job = job_row(
                "J8", selected=0, quote_status="APPROVED",
                invoice_status="PAID", status=status,
            )
            intelligence = JobEngine.evaluate(
                job,
                selected_items=0,
                quote={"id": 8, "status": "APPROVED"},
                invoice={"id": 8, "status": "PAID"},
            )
            self.assertEqual(intelligence.workflow_stage, expected)

    def test_quote_and_commercial_next_action_matrix(self):
        jobs = [
            job_row("J4", selected=0, quote_status="DRAFT")
            | {"requested_need_wording": "STARTER"},
            job_row("J5", quote_status="SENT"),
            job_row("J6", quote_status="APPROVED"),
            job_row("J7", quote_status="APPROVED", invoice_status="PARTIAL"),
            job_row("J8", quote_status="APPROVED", invoice_status="PAID"),
            job_row(
                "J9", selected=0, quote_status="APPROVED",
                invoice_status="PAID", status="ORDERED",
            ) | {"outstanding_order_quantity": 3},
            job_row(
                "J10", selected=0, quote_status="APPROVED",
                invoice_status="PAID", status="RECEIVED",
            ) | {"outstanding_order_quantity": 0},
            job_row(
                "J11", selected=0, quote_status="APPROVED",
                invoice_status="PAID", status="DELIVERED",
            ) | {"outstanding_order_quantity": 0},
        ]
        c = self.connection()
        with patch(
            "plg_core.dashboard.service.get_recent_jobs",
            return_value=jobs,
        ):
            result = get_work_queue_data(c, today=date(2026, 8, 12))
        rows = {row["job_number"]: row for row in result["items"]}
        expected = {
            "J4": ("READY_TO_QUOTE", "Review Draft Quote", "Open Quote", "/quotes/4/documents"),
            "J5": ("CUSTOMER_DECISION_FOLLOW_UP", "Waiting for Customer", "Open Quote", "/quotes/5/documents"),
            "J6": ("READY_TO_INVOICE", "Create Invoice for Payment", "Open Quote", "/quotes/6/documents"),
            "J7": ("WAITING_FOR_PAYMENT", "Waiting for Payment", "Open Invoice", "/invoices/7/documents"),
            "J8": ("READY_TO_ORDER", "Order 1 part", "Open Paid Invoice", "/invoices/8/documents"),
            "J9": ("WAITING_FOR_PARTS", "Receive 3 remaining parts", "Open Supplier Order", "/purchasing/orders/9"),
            "J10": ("READY_FOR_DELIVERY", "Prepare Delivery", "Prepare Delivery", "/jobs/10/delivery"),
            "J11": ("COMPLETE", "Completed", "Open Job", "/jobs/11/basket"),
        }
        for number, values in expected.items():
            row = rows[number]
            self.assertEqual(
                (row["category"], row["need_action"], row["next_action"], row["url"]),
                values,
            )
        self.assertEqual((rows["J4"]["need_label"], rows["J4"]["action_detail"]),
                         ("DRAFT QUOTE", "Review Draft Quote"))
        self.assertEqual(rows["J4"]["context_detail"], "PPS-Q-0004 · $1,000.00")
        self.assertEqual((rows["J5"]["need_label"], rows["J5"]["action_detail"]),
                         ("CUSTOMER DECISION", "Waiting for Customer"))
        self.assertEqual((rows["J7"]["need_label"], rows["J7"]["action_detail"]),
                         ("INVOICE", "Waiting for Payment"))
        self.assertEqual((rows["J9"]["need_label"], rows["J9"]["action_detail"]),
                         ("SUPPLIER PARTS", "Receive 3 remaining parts"))
        self.assertEqual(
            rows["J9"]["context_detail"],
            "Synthetic Supplier · PPS-PO-0009 · 3 remaining · Expected Aug 20",
        )
        self.assertEqual((rows["J10"]["need_label"], rows["J10"]["action_detail"]),
                         ("DELIVERY", "Prepare Delivery"))
        self.assertEqual((rows["J11"]["need_label"], rows["J11"]["action_detail"]),
                         ("COMPLETED", "Completed"))
        c.close()

    def test_sent_quote_suppresses_open_need_and_is_not_manual_follow_up(self):
        c = self.connection()
        sent = job_row("J1", selected=0, quote_status="SENT")
        with patch("plg_core.dashboard.service.get_recent_jobs", return_value=[sent]):
            result = get_work_queue_data(c, today=date(2026, 8, 12))
            followups = get_work_queue_data(c, category="FOLLOW_UP", today=date(2026, 8, 12))
        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertEqual(row["category"], "CUSTOMER_DECISION_FOLLOW_UP")
        self.assertEqual(row["next_action"], "Open Quote")
        self.assertEqual(row["url"], "/quotes/1/documents")
        self.assertEqual(
            row["context_detail"],
            "PPS-Q-0001 · $1,000.00 · Sent 10 days ago",
        )
        self.assertFalse(row["is_follow_up"])
        self.assertEqual(followups["items"], [])
        c.close()

    def test_automatic_payment_and_parts_waiting_are_not_manual_followups(self):
        jobs = [
            job_row("J6", quote_status="APPROVED", invoice_status="PARTIAL"),
            job_row("J8", selected=0, quote_status="APPROVED", invoice_status="PAID", status="ORDERED"),
        ]
        c = self.connection()
        with patch("plg_core.dashboard.service.get_recent_jobs", return_value=jobs):
            all_rows = get_work_queue_data(c, today=date(2026, 8, 12))["items"]
            followups = get_work_queue_data(c, category="FOLLOW_UP")["items"]
        self.assertEqual(followups, [])
        self.assertTrue(all(not row["is_follow_up"] for row in all_rows))
        c.close()

    def test_multiple_supplier_orders_use_honest_compact_summary(self):
        job = job_row(
            "J8", selected=0, quote_status="APPROVED",
            invoice_status="PAID", status="ORDERED",
        ) | {"outstanding_order_quantity": 3, "open_supplier_count": 2}
        c = self.connection()
        with patch("plg_core.dashboard.service.get_recent_jobs", return_value=[job]):
            row = get_work_queue_data(c)["items"][0]
        self.assertEqual(
            row["context_detail"],
            "Synthetic Supplier + 1 other · PPS-PO-0008 · 3 remaining · Expected Aug 20",
        )
        c.close()

    def test_need_wording_is_primary_and_action_is_secondary(self):
        c = self.connection()
        with patch("plg_core.dashboard.service.get_recent_jobs", return_value=self.jobs()):
            result = get_work_queue_data(c, today=date(2026, 8, 12))
        by_need = {row["need_label"]: row for row in result["items"]
                   if row["key"].startswith("need:")}
        self.assertEqual(by_need["Starter"]["action_detail"], "Research Parts")
        self.assertEqual(by_need["Turbo"]["action_detail"], "Add Supplier Price")
        self.assertEqual(by_need["Radiator"]["action_detail"], "Confirm for Quote")
        self.assertEqual(by_need["Turbo"]["need_action"], "Turbo")
        template = (ROOT / "templates" / "dashboard.html").read_text()
        css = (ROOT / "static" / "app.css").read_text()
        self.assertLess(template.index("item.need_label"), template.index("item.action_detail"))
        self.assertIn('class="work-queue-need-label"', template)
        self.assertIn('class="work-queue-action-detail"', template)
        self.assertIn(".work-queue-need-label{", css)
        self.assertIn(".work-queue-context-detail{", css)
        self.assertIn("item.context_detail", template)
        self.assertIn("overflow-wrap:anywhere", css)
        self.assertIn("@media(max-width:760px)", css)
        c.close()

    def test_multiple_open_needs_on_one_job_remain_separate_rows(self):
        c = self.connection()
        c.execute(
            "INSERT INTO requested_needs VALUES (?,?,?,?,?,?)",
            (12, 1, 1, "Oil Filter", "OPEN", "2026-08-02"),
        )
        c.commit()
        with patch(
            "plg_core.dashboard.service.get_recent_jobs",
            return_value=[job_row("J1", selected=0, status="REQUESTED")],
        ):
            rows = get_work_queue_data(c)["items"]
        sourcing = [row for row in rows if row["key"].startswith("need:")]
        self.assertEqual(
            {row["need_label"] for row in sourcing},
            {"Starter", "Oil Filter"},
        )
        self.assertEqual(len(sourcing), 2)
        self.assertNotEqual(sourcing[0]["url"], sourcing[1]["url"])
        c.close()

    def test_zero_outstanding_order_quantity_is_not_waiting_for_parts(self):
        job = job_row(
            "J8", selected=0, quote_status="APPROVED",
            invoice_status="PAID", status="ORDERED",
        ) | {"outstanding_order_quantity": 0}
        c = self.connection()
        with patch(
            "plg_core.dashboard.service.get_recent_jobs", return_value=[job]
        ):
            result = get_work_queue_data(c)
        self.assertEqual(result["items"][0]["category"], "READY_FOR_DELIVERY")
        self.assertEqual(result["counts"]["WAITING_FOR_PARTS"], 0)
        c.close()

    def test_work_queue_route_renders_queue_and_navigation_contract(self):
        empty = {
            "items": [], "counts": {key: 0 for key, _ in WORK_QUEUE_CATEGORIES},
            "total": 0, "selected_category": "ALL",
            "categories": WORK_QUEUE_CATEGORIES, "report_date": "2026-08-12",
        }
        scope = {"type": "http", "method": "GET", "path": "/work-queue", "headers": [],
                 "query_string": b"", "app": legacy_app.app,
                 "router": legacy_app.app.router, "scheme": "http",
                 "server": ("testserver", 80), "client": ("testclient", 50000)}
        with patch.object(legacy_app, "get_connection", return_value=sqlite3.connect(":memory:")), \
             patch.object(legacy_app, "get_work_queue_data", return_value=empty):
            body = legacy_app.work_queue(Request(scope)).body.decode()
        self.assertIn("<h1>Work Queue</h1>", body)
        self.assertIn("/requests/smart-intake", body)
        self.assertIn("No work in this queue", body)
        base = (ROOT / "templates" / "base.html").read_text()
        for label in ("Inbox", "Jobs", "Quotes", "Orders", "Invoices",
                      "Customers", "Machines", "Sources", "Suppliers", "Administration"):
            self.assertIn(f"<span>{label}</span>", base)
        self.assertNotIn("<span>Work Queue</span>", base)
        self.assertEqual(base.count('href="/requests"'), 1)
        self.assertNotIn("Open Inbox", base)
        self.assertIn("active_page == 'requests'", base)
        self.assertNotIn("<span>Follow-Up Center</span>", base)

    def test_category_navigation_wraps_without_horizontal_scrolling(self):
        css = (ROOT / "static" / "app.css").read_text()
        template = (ROOT / "templates" / "dashboard.html").read_text()
        self.assertIn(".work-queue-tabs{display:flex;flex-wrap:wrap", css)
        self.assertIn(".work-queue-tabs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))", css)
        self.assertIn("@media(max-width:1100px){.work-queue-row{grid-template-columns:repeat(2,minmax(0,1fr))}}", css)
        self.assertNotIn(".work-queue-tabs{display:flex;gap:8px;overflow-x:auto", css)
        self.assertIn('href="/work-queue?queue={{ key }}"', template)
        self.assertIn("{{ counts[key] }}", template)
        self.assertIn("selected_category == key", template)

    def test_recent_job_projection_keeps_500_job_bound(self):
        connection = Mock()
        connection.execute.return_value.fetchall.return_value = []
        self.assertEqual(get_recent_jobs(connection, limit=900), [])
        self.assertEqual(connection.execute.call_args.args[1], (500,))


if __name__ == "__main__":
    unittest.main()
