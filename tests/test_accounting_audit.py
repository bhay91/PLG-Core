from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import legacy_app
from plg_core.admin import service as accounting_service
from plg_core.database.migrations import run_migrations


ROOT = Path(__file__).resolve().parents[1]


class AccountingAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-accounting-")
        self.db_path = Path(self.temp.name) / "accounting.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        with patch.object(legacy_app, "DB_PATH", self.db_path):
            run_migrations()
        with closing(self.connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            tables = [
                row[0] for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'"
                )
            ]
            for table in tables:
                c.execute(f'DELETE FROM "{table}"')
            c.commit()
        self._seed()
        self.connection_patch = patch.object(
            accounting_service, "get_connection", side_effect=self.connection
        )
        self.connection_patch.start()

    def tearDown(self):
        self.connection_patch.stop()
        self.temp.cleanup()

    def connection(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _seed(self):
        with closing(self.connection()) as c:
            customer = c.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('PPS-C-ACCT','Accounting Test',1)"
            ).lastrowid
            self.invoice_ids = {}
            for index, spec in enumerate((
                ("0001", "PARTIAL", 130, 100, 30, 80, 0),
                ("0002", "UNPAID", 200, 120, 80, 200, 0),
                ("0003", "PAID", 300, 180, 120, 0, 0),
                ("0004", "PAID", 150, 90, 60, 0, 0),
                ("0005", "UNPAID", 100, 60, 40, 100, 1),
                ("VOID", "VOID", 999, 500, 499, 0, 0),
            ), start=1):
                suffix, status, total, supplier, profit, balance, archived = spec
                job = c.execute(
                    "INSERT INTO jobs(job_number,created_date,customer_id,customer,status,is_archived) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"PPS-J-A{suffix}", "2026-08-01", customer, "Accounting Test", "CONFIRMED", archived),
                ).lastrowid
                quote = c.execute(
                    "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,supplier_total,profit_total,is_archived) "
                    "VALUES (?,?,?,'CONVERTED',?,?,?,1)",
                    (f"PPS-Q-A{suffix}", job, "2026-08-01", total, supplier, profit),
                ).lastrowid
                invoice = c.execute(
                    "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,supplier_total,profit_total,balance_due) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"PPS-INV-A{suffix}", quote, job, "2026-08-02", status, total, supplier, profit, balance),
                ).lastrowid
                self.invoice_ids[suffix] = invoice

            suppliers = {
                "0001": (("Supplier A", 100),),
                "0002": (("Supplier A", 60), ("Supplier B", 60)),
                "0003": (("Supplier A", 100), ("Supplier B", 80)),
                "0004": (("Supplier A", 90),),
                "0005": (("Supplier A", 60),),
            }
            for suffix, lines in suppliers.items():
                for supplier_name, cost in lines:
                    c.execute(
                        "INSERT INTO invoice_items(invoice_id,description,supplier_name,supplier_line_total,customer_line_total,line_profit) "
                        "VALUES (?,'Part',?,?,?,?)",
                        (self.invoice_ids[suffix], supplier_name, cost, cost + 20, 20),
                    )

            order_specs = (
                ("0002", "Supplier A", "ORDERED", 60),
                ("0002", "Supplier B", "DRAFT", 60),
                ("0003", "Supplier A", "ORDERED", 100),
                ("0003", "Supplier B", "PARTIAL", 80),
                ("0004", "Supplier A", "RECEIVED", 90),
                ("VOID", "Supplier A", "RECEIVED", 777),
            )
            for number, (suffix, supplier_name, status, cost) in enumerate(order_specs, start=1):
                job_id = c.execute("SELECT job_id FROM invoices WHERE id=?", (self.invoice_ids[suffix],)).fetchone()[0]
                order = c.execute(
                    "INSERT INTO supplier_orders(po_number,job_id,invoice_id,supplier_name,status,parts_total,order_total) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"PPS-PO-A{number}", job_id, self.invoice_ids[suffix], supplier_name, status, cost, cost),
                ).lastrowid
                c.execute(
                    "INSERT INTO supplier_order_items(order_id,description,quantity_ordered,quantity_received,unit_cost,line_cost) "
                    "VALUES (?,'Part',1,?,?,?)",
                    (order, 1 if status == "RECEIVED" else 0, cost, cost),
                )

            transaction_specs = (
                (self.invoice_ids["0001"], "PAYMENT", 50),
                (self.invoice_ids["0003"], "PAYMENT", 300),
                (self.invoice_ids["0004"], "PAYMENT", 150),
                (None, "PAYMENT", 25),
                (None, "REFUND", -10),
                (None, "ADJUSTMENT", -5),
            )
            for invoice_id, kind, amount in transaction_specs:
                c.execute(
                    "INSERT INTO customer_transactions(customer_id,transaction_date,transaction_type,amount,invoice_id) "
                    "VALUES (?,'2026-08-10',?,?,?)",
                    (customer, kind, amount, invoice_id),
                )

            c.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,is_archived) "
                "VALUES ('PPS-Q-0010',(SELECT id FROM jobs LIMIT 1),'2026-08-11','DRAFT',500,1)"
            )
            c.commit()

    def snapshot(self):
        return accounting_service.accounting_snapshot()

    def test_clean_collection_and_headline_metrics(self):
        data = self.snapshot()
        money = data["financials"]
        self.assertEqual(money["invoiced_revenue"], 880)
        self.assertEqual(money["invoice_collections"], 500)
        self.assertEqual(money["unallocated_customer_money"], 25)
        self.assertEqual(money["refunds"], 10)
        self.assertEqual(money["adjustments"], -5)
        self.assertEqual(money["receivables"], 380)
        self.assertEqual(money["booked_profit"], 330)
        self.assertEqual(money["placed_supplier_cost"], 330)
        self.assertEqual(money["placed_cost_profit"], 550)
        self.assertEqual(sum(row["customer_total"] for row in data["invoice_reconciliation"]), money["invoiced_revenue"])
        self.assertEqual(sum(row["placed_supplier_cost"] for row in data["invoice_reconciliation"]), money["placed_supplier_cost"])

    def test_invoice_reconciliation_states_costs_and_links(self):
        rows = {row["invoice_number"]: row for row in self.snapshot()["invoice_reconciliation"]}
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows["PPS-INV-A0001"]["reconciliation_state"], "UNRECONCILED")
        self.assertEqual(rows["PPS-INV-A0002"]["reconciliation_state"], "PARTIALLY_COSTED")
        self.assertEqual(rows["PPS-INV-A0003"]["reconciliation_state"], "ORDER_COSTED")
        self.assertEqual(rows["PPS-INV-A0004"]["reconciliation_state"], "RECEIVED")
        self.assertEqual(rows["PPS-INV-A0001"]["paid"], 50)
        self.assertEqual(rows["PPS-INV-A0001"]["balance_due"], 80)
        self.assertEqual(rows["PPS-INV-A0002"]["booked_supplier_cost"], 120)
        self.assertEqual(rows["PPS-INV-A0002"]["placed_supplier_cost"], 60)
        self.assertEqual(rows["PPS-INV-A0002"]["placed_cost_profit"], 140)
        self.assertTrue(rows["PPS-INV-A0002"]["invoice_url"].endswith("/documents"))
        self.assertTrue(rows["PPS-INV-A0002"]["job_url"].endswith("/basket"))
        self.assertEqual(len(rows["PPS-INV-A0003"]["orders"]), 2)
        self.assertIn("PPS-INV-A0005", rows)  # archived Job financial history remains
        self.assertNotIn("PPS-INV-AVOID", rows)

    def test_archived_and_purged_records_do_not_pollute_reporting(self):
        data = self.snapshot()
        numbers = {row["invoice_number"] for row in data["invoice_reconciliation"]}
        self.assertNotIn("PPS-INV-0011", numbers)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quotes WHERE quote_number='PPS-Q-0010' AND is_archived=1").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices i JOIN quotes q ON q.id=i.quote_id WHERE q.quote_number='PPS-Q-0010'").fetchone()[0], 0)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_projection_is_read_only_and_template_is_responsive(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.snapshot()
        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        template = (ROOT / "templates" / "accounting.html").read_text()
        self.assertIn('id="invoice-reconciliation"', template)
        self.assertIn("accounting-reconciliation-table td::before", template)
        self.assertIn("@media(max-width:620px)", template)
        self.assertIn("/purchasing/orders/{{ order.id }}", template)


if __name__ == "__main__":
    unittest.main()
