from contextlib import closing
from pathlib import Path
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.supply.models import DeliveryCreate, DeliveryItemCreate, ReceiptCreate, ReceiptItem
from plg_core.supply.service import (
    complete_delivery, create_delivery, create_orders_from_paid_invoice,
    get_delivery_workspace, get_order, place_order, record_receipt,
    update_order, update_order_item_cost,
)


ROOT = Path(__file__).resolve().parents[1]


class SupplierPurchasingPhase35Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-phase35-")
        self.db_path = Path(self.temp.name) / "test.db"
        self.document_root = Path(self.temp.name) / "documents"
        self.document_patch = patch.dict(
            os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)}
        )
        self.document_patch.start()
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in ("delivery_items", "deliveries", "receiving_event_items", "receiving_events",
                          "supplier_order_items", "supplier_orders", "invoice_events", "invoice_items",
                          "invoices", "quote_items", "quotes", "customer_transactions", "job_timeline",
                          "audit_logs", "suppliers", "job_assets", "requested_needs", "machines",
                          "jobs", "customers"):
                c.execute(f'DELETE FROM "{table}"')
            c.commit()
        self.job_id, self.invoice_id = self._invoice("PAID", 0)

    def tearDown(self):
        self.db_patch.stop()
        self.document_patch.stop()
        self.temp.cleanup()

    def _invoice(self, status, balance):
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,active) VALUES ('P35-C','Customer P35',1)").lastrowid
            job = c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('P35-J','2026-08-13',?,'Customer P35','CONFIRMED')", (customer,)).lastrowid
            quote = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total) VALUES ('PPS-Q-9035',?,'2026-08-13','CONVERTED',100)", (job,)).lastrowid
            invoice = c.execute("INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) VALUES ('PPS-INV-9035',?,?,'2026-08-13',?,100,?)", (quote, job, status, balance)).lastrowid
            for supplier, part, quantity, cost in (("Supplier A", "A-1", 3, 10), ("Supplier A", "A-2", 1, 20), ("Supplier B", "B-1", 2, 15)):
                c.execute("INSERT INTO invoice_items(invoice_id,quantity,description,supplier_name,supplier_part_number,supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total) VALUES (?,?,?,?,?,?,?,?,?)", (invoice, quantity, f"Part {part}", supplier, part, cost, quantity*cost, cost+5, quantity*(cost+5)))
            c.commit()
        return int(job), int(invoice)

    def test_paid_required_supplier_grouping_lineage_and_idempotency(self):
        orders = create_orders_from_paid_invoice(self.invoice_id)
        self.assertEqual([row["supplier_name"] for row in orders], ["Supplier A", "Supplier B"])
        self.assertEqual(len(create_orders_from_paid_invoice(self.invoice_id)), 2)
        first = get_order(orders[0]["id"])
        self.assertEqual(first["invoice_number"], "PPS-INV-9035")
        self.assertEqual(first["job_number"], "P35-J")
        self.assertEqual(first["customer"], "Customer P35")
        self.assertEqual([(i["supplier_part_number"], i["quantity_ordered"], i["unit_cost"]) for i in first["items"]], [("A-1", 3, 10), ("A-2", 1, 20)])
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE invoices SET status='PARTIAL',balance_due=1 WHERE id=?", (self.invoice_id,)); c.execute("DELETE FROM supplier_order_items"); c.execute("DELETE FROM supplier_orders"); c.commit()
        with self.assertRaises(HTTPException):
            create_orders_from_paid_invoice(self.invoice_id)

    def test_draft_cost_details_and_ordered_locks(self):
        order_id = create_orders_from_paid_invoice(self.invoice_id)[0]["id"]
        item_id = get_order(order_id)["items"][0]["id"]
        updated = update_order_item_cost(order_id, item_id, 12.5)
        self.assertEqual(updated["items"][0]["unit_cost"], 12.5)
        updated = update_order(order_id, shipping_total=8, expected_at="2026-08-20", notes="CONF-1")
        self.assertEqual((updated["shipping_total"], updated["expected_at"], updated["notes"]), (8, "2026-08-20", "CONF-1"))
        self.assertEqual(place_order(order_id)["status"], "ORDERED")
        with self.assertRaises(HTTPException): update_order_item_cost(order_id, item_id, 13)
        with self.assertRaises(HTTPException): update_order(order_id, shipping_total=9)

    def test_receiving_guards_partial_remaining_and_final(self):
        order_id = create_orders_from_paid_invoice(self.invoice_id)[0]["id"]
        items = get_order(order_id)["items"]
        with self.assertRaises(HTTPException): record_receipt(order_id, ReceiptCreate(items=[ReceiptItem(order_item_id=items[0]["id"], quantity_received=1)]))
        place_order(order_id)
        partial = record_receipt(order_id, ReceiptCreate(items=[ReceiptItem(order_item_id=items[0]["id"], quantity_received=1)], notes="partial"))
        self.assertEqual(partial["status"], "PARTIAL")
        self.assertEqual(partial["items"][0]["quantity_ordered"] - partial["items"][0]["quantity_received"], 2)
        with self.assertRaises(HTTPException): record_receipt(order_id, ReceiptCreate(items=[ReceiptItem(order_item_id=items[0]["id"], quantity_received=3)]))
        final = record_receipt(order_id, ReceiptCreate(items=[ReceiptItem(order_item_id=items[0]["id"], quantity_received=2), ReceiptItem(order_item_id=items[1]["id"], quantity_received=1)]))
        self.assertEqual(final["status"], "RECEIVED")

    def test_received_inventory_uses_existing_delivery_authority(self):
        orders = create_orders_from_paid_invoice(self.invoice_id)
        for row in orders:
            order = get_order(row["id"]); place_order(row["id"])
            record_receipt(row["id"], ReceiptCreate(items=[ReceiptItem(order_item_id=i["id"], quantity_received=i["quantity_ordered"]) for i in order["items"]]))
        self.assertTrue(get_delivery_workspace(self.job_id)["has_available"])
        available = get_delivery_workspace(self.job_id)["items"]
        delivery = create_delivery(self.job_id, DeliveryCreate(
            items=[DeliveryItemCreate(order_item_id=item["id"], quantity=item["available_to_deliver"]) for item in available if item["available_to_deliver"] > 0],
            recipient="Customer P35",
        ))
        self.assertEqual(delivery["status"], "READY")
        self.assertEqual(complete_delivery(delivery["id"])["job_complete"], True)


class SupplierPurchasingPhase35PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.list_template = (ROOT / "templates" / "supplier_orders.html").read_text()
        cls.detail_template = (ROOT / "templates" / "supplier_order_detail.html").read_text()
        cls.css = (ROOT / "static" / "app.css").read_text()

    def test_filter_uses_business_number_without_changing_query_contract(self):
        self.assertIn("Filtered to invoice {{ filtered_invoice_number", self.list_template)
        self.assertNotIn("Filtered to invoice #{{ invoice_id }}", self.list_template)
        source = (ROOT / "legacy_app.py").read_text()
        self.assertIn('invoice_id=invoice_id, limit=500', source)
        self.assertIn('"filtered_invoice_number": filtered_invoice_number', source)

    def test_filter_projection_displays_joined_number_and_filters_by_id(self):
        rows = [{"id": 1, "identity": {"invoice_id": 31, "invoice_number": "PPS-INV-0011"}}]
        result = {"orders": rows, "options": {"suppliers": [], "statuses": []}}
        with patch("plg_core.supply.service.list_purchasing_operational_snapshots", return_value=result), patch.object(
            legacy_app.templates, "TemplateResponse", side_effect=lambda **kwargs: kwargs["context"]
        ):
            context = legacy_app.purchasing_center(None, invoice_id=31)
        self.assertEqual([row["id"] for row in context["orders"]], [1])
        self.assertEqual(context["invoice_id"], 31)
        self.assertEqual(context["filtered_invoice_number"], "PPS-INV-0011")

    def test_state_actions_and_routes_are_preserved(self):
        for label in ("Update Cost", "Save Purchase Details", "Place Purchase Order", "Receive Parts", "Record Receipt", "Open Delivery"):
            self.assertIn(label, self.detail_template)
        for path in ("/items/{{ item.id }}/cost", "/update", "/place", "/receive", "/jobs/{{ order.job_id }}/delivery"):
            self.assertIn(path, self.detail_template)

    def test_placement_has_one_csrf_protected_strong_confirmation(self):
        action = 'action="/purchasing/orders/{{ order.id }}/place"'
        self.assertEqual(self.detail_template.count(action), 1)
        self.assertIn('name="csrf_token"', self.detail_template)
        for text in (
            "PO: ", "Supplier: ", "Order Total: ",
            "lock the order and create an issued Supplier PO",
        ):
            self.assertIn(text, self.detail_template)

    def test_operator_hierarchy_and_responsive_contract(self):
        content = self.detail_template.index('<div class="supplier-order-desk">')
        positions = [self.detail_template.index(value, content) for value in ("supplier-order-heading", "purchase-ops\"", "supplier-order-context", "supplier-order-items", "supplier-order-total", "supplier-order-details")]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("invoice-center-identity-grid", self.detail_template)
        self.assertIn("@media (max-width: 1000px)", self.css)
        self.assertIn("@media (max-width: 680px)", self.css)
        self.assertIn(".supplier-order-table { table-layout: fixed; overflow-wrap: anywhere; }", self.css)


if __name__ == "__main__": unittest.main()
