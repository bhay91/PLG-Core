from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from legacy_app import app
from plg_core.dashboard.service import get_operator_dashboard_data


ROOT = Path(__file__).resolve().parents[1]


def queue_item(job_id: int, category: str, action: str) -> dict:
    return {
        "key": f"job:{job_id}:{category}",
        "category": category,
        "customer": f"Synthetic Customer {job_id}",
        "job_id": job_id,
        "job_number": f"PPS-J-9{job_id:03d}",
        "machine": f"Synthetic Machine {job_id}",
        "manufacturer": "",
        "need_action": action,
        "need_label": "Synthetic Test Part",
        "action_detail": action,
        "context_detail": "1 remaining" if category == "WAITING_FOR_PARTS" else "",
        "waiting_since": "2026-08-20",
        "age_days": 4,
        "next_action": action,
        "url": f"/jobs/{job_id}/basket",
        "is_follow_up": False,
    }


class OperatorDashboardTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE intake_proposals (
                id INTEGER PRIMARY KEY, contact_name TEXT, company_name TEXT,
                raw_input TEXT, review_state TEXT, status TEXT,
                created_at TEXT, updated_at TEXT
            );
            CREATE TABLE intake_proposal_assets (
                id INTEGER PRIMARY KEY, proposal_id INTEGER, manufacturer TEXT,
                model TEXT, included INTEGER
            );
            CREATE TABLE intake_proposal_needs (
                id INTEGER PRIMARY KEY, proposal_id INTEGER, wording TEXT,
                included INTEGER
            );
            CREATE TABLE intake_proposal_contributions (
                id INTEGER PRIMARY KEY, proposal_id INTEGER,
                contributor_type TEXT, payload_json TEXT
            );
            """
        )

    def tearDown(self):
        self.connection.close()

    def test_route_is_separate_from_existing_work_queue(self):
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        self.assertIn("/", paths)
        self.assertIn("/dashboard", paths)
        self.assertIn("/work-queue", paths)

    def test_draft_intake_age_source_and_review_link_are_read_only(self):
        self.connection.execute(
            "INSERT INTO intake_proposals VALUES (1,'Synthetic Operator','Synthetic Company',?,"
            "'REVIEW','DRAFT','2026-08-21 09:00:00','2026-08-21 09:00:00')",
            ("Find a synthetic hydraulic seal kit",),
        )
        self.connection.execute(
            "INSERT INTO intake_proposal_assets VALUES (1,1,'Synthetic','Excavator',1)"
        )
        self.connection.execute(
            "INSERT INTO intake_proposal_needs VALUES (1,1,'Hydraulic seal kit',1)"
        )
        self.connection.execute(
            "INSERT INTO intake_proposal_contributions VALUES (1,1,'AI',?)",
            (json.dumps({"origin": "CHATGPT_MOBILE"}),),
        )
        self.connection.execute(
            "INSERT INTO intake_proposals VALUES (2,'Confirmed Person','Confirmed Company','Done',"
            "'CONFIDENT','CONFIRMED','2026-08-20','2026-08-20')"
        )
        self.connection.commit()

        with patch("plg_core.dashboard.service.get_work_queue_data", return_value={"items": []}):
            result = get_operator_dashboard_data(
                self.connection, today=date(2026, 8, 24)
            )

        self.assertEqual(result["inbox_total"], 1)
        self.assertEqual(result["inbox"][0]["source"], "ChatGPT / Mobile")
        self.assertEqual(result["inbox"][0]["age_label"], "Waiting 3+ days")
        self.assertEqual(result["inbox"][0]["status"], "Needs Review")
        self.assertEqual(result["inbox"][0]["url"], "/requests/smart-intake/proposals/1")
        self.assertEqual(
            self.connection.execute("SELECT status FROM intake_proposals WHERE id=1").fetchone()[0],
            "DRAFT",
        )

    def test_existing_work_queue_states_feed_each_operational_section(self):
        items = [
            queue_item(1, "NEEDS_RESEARCH", "Research Need"),
            queue_item(2, "WAITING_FOR_PAYMENT", "Open Invoice"),
            queue_item(3, "READY_TO_ORDER", "Open Paid Invoice"),
            queue_item(4, "WAITING_FOR_PARTS", "Receive 1 remaining part"),
            queue_item(5, "READY_FOR_DELIVERY", "Prepare Delivery"),
        ]
        with patch("plg_core.dashboard.service.get_work_queue_data", return_value={"items": items}):
            result = get_operator_dashboard_data(self.connection)
        self.assertEqual(result["jobs"][0]["job_id"], 1)
        self.assertEqual(result["jobs"][0]["dashboard_url"], "/jobs/1/basket")
        self.assertEqual(result["jobs"][0]["dashboard_action"], "Open Job")
        self.assertEqual(result["payments"][0]["job_id"], 2)
        self.assertEqual(result["ordering"][0]["job_id"], 3)
        self.assertEqual(result["receiving"][0]["context_detail"], "1 remaining")
        self.assertEqual(result["delivery"][0]["job_id"], 5)

    def test_accounting_uses_only_existing_exception_states(self):
        rows = [
            {"invoice_number": "PPS-INV-9001", "job_number": "PPS-J-9001", "customer": "Synthetic A", "actual_cost_state": "NOT_CONFIRMED", "cost_variance": 0, "profit_variance": 0, "invoice_url": "/invoices/1/documents"},
            {"invoice_number": "PPS-INV-9002", "job_number": "PPS-J-9002", "customer": "Synthetic B", "actual_cost_state": "CONFIRMED", "cost_variance": 5, "profit_variance": -5, "invoice_url": "/invoices/2/documents"},
            {"invoice_number": "PPS-INV-9003", "job_number": "PPS-J-9003", "customer": "Synthetic C", "actual_cost_state": "CONFIRMED", "cost_variance": 0, "profit_variance": 0, "invoice_url": "/invoices/3/documents"},
        ]
        with patch("plg_core.dashboard.service.get_work_queue_data", return_value={"items": []}):
            result = get_operator_dashboard_data(self.connection, accounting_rows=rows)
        self.assertEqual(
            [row["state"] for row in result["accounting_exceptions"]],
            ["Awaiting Final Cost", "Variance Detected"],
        )

    def test_template_has_no_write_controls_and_navigation_is_distinct(self):
        template = (ROOT / "templates/operator_dashboard.html").read_text()
        base = (ROOT / "templates/base.html").read_text()
        self.assertNotIn("<form", template)
        self.assertNotIn('method="post"', template.lower())
        self.assertIn('href="/dashboard"', base)
        self.assertIn('href="/work-queue"', base)
        self.assertIn("active_page == 'dashboard'", base)
        Environment(loader=FileSystemLoader(ROOT / "templates")).get_template(
            "operator_dashboard.html"
        )


if __name__ == "__main__":
    unittest.main()
