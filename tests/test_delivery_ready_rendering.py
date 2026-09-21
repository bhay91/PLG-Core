from contextlib import closing
from pathlib import Path
import os
import asyncio
import shutil
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from starlette.requests import Request

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.supply.models import DeliveryCreate, DeliveryItemCreate, ReceiptCreate, ReceiptItem
from plg_core.supply.service import (
    create_delivery,
    create_orders_from_paid_invoice,
    get_delivery_workspace,
    get_order,
    place_order,
    record_receipt,
)


ROOT = Path(__file__).resolve().parents[1]


class DeliveryReadyRenderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-delivery-render-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.document_patch = patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(Path(self.temp.name) / "documents")})
        self.db_patch.start()
        self.document_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "delivery_items", "deliveries", "receiving_event_items",
                "receiving_events", "supplier_order_items", "supplier_orders",
                "invoice_items", "invoices", "quote_items", "quotes",
                "customer_transactions", "job_timeline", "audit_logs",
                "jobs", "customers",
            ):
                c.execute(f'DELETE FROM "{table}"')
            customer_id = c.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('DR-C','Delivery Customer',1)"
            ).lastrowid
            self.job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,manufacturer,machine,pin_serial,status) VALUES ('DR-J','2026-08-13',?,'Delivery Customer','CAT','420D','TEST-PIN-DELIVERY-001','CONFIRMED')",
                (customer_id,),
            ).lastrowid
            quote_id = c.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total) VALUES ('PPS-Q-9931',?,'2026-08-13','CONVERTED',100)",
                (self.job_id,),
            ).lastrowid
            invoice_id = c.execute(
                "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) VALUES ('PPS-INV-9931',?,?,'2026-08-13','PAID',100,0)",
                (quote_id, self.job_id),
            ).lastrowid
            for part, description in (("READY-1", "Ready item one"), ("READY-2", "Ready item two")):
                c.execute(
                    "INSERT INTO invoice_items(invoice_id,quantity,description,supplier_name,supplier_part_number,supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total) VALUES (?,1,?,'Delivery Supplier',?,10,10,15,15)",
                    (invoice_id, description, part),
                )
            c.commit()
        order_id = create_orders_from_paid_invoice(invoice_id)[0]["id"]
        order = get_order(order_id)
        place_order(order_id)
        record_receipt(
            order_id,
            ReceiptCreate(items=[
                ReceiptItem(order_item_id=item["id"], quantity_received=1)
                for item in order["items"]
            ]),
        )

    def tearDown(self):
        self.document_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def request(path):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        return Request({
            "type": "http", "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "root_path": "", "query_string": b"", "headers": [],
            "client": ("127.0.0.1", 1), "server": ("test", 80),
            "app": legacy_app.app, "router": legacy_app.app.router,
        }, receive)

    @staticmethod
    def post_request(path, fields, token):
        body = urlencode(fields).encode()
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return Request({
            "type": "http", "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cookie", f"pps_csrf_token={token}".encode())],
            "client": ("127.0.0.1", 1), "server": ("test", 80),
        }, receive)

    def test_ready_delivery_page_renders_multiple_items_and_route_contract(self):
        items = get_delivery_workspace(self.job_id)["items"]
        delivery = create_delivery(self.job_id, DeliveryCreate(items=[DeliveryItemCreate(order_item_id=item["id"], quantity=item["available_to_deliver"]) for item in items], recipient="Delivery Customer"))
        response = legacy_app.job_delivery_workspace(
            self.request(f"/jobs/{self.job_id}/delivery"), self.job_id
        )
        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("READY-1", body)
        self.assertIn("READY-2", body)
        self.assertIn(f'action="/deliveries/{delivery["id"]}/complete"', body)

    def test_existing_ready_delivery_is_reused(self):
        items = get_delivery_workspace(self.job_id)["items"]
        payload = DeliveryCreate(items=[DeliveryItemCreate(order_item_id=item["id"], quantity=item["available_to_deliver"]) for item in items], recipient="Delivery Customer", idempotency_key="render-replay")
        first = create_delivery(self.job_id, payload)
        second = create_delivery(self.job_id, payload)
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["replayed"])
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM deliveries WHERE job_id=? AND status='READY'",
                (self.job_id,),
            ).fetchone()[0], 1)

    def test_prepare_route_redirects_to_renderable_ready_page(self):
        item = get_delivery_workspace(self.job_id)["items"][0]
        token = "delivery-render-csrf-token-123456"
        request = self.post_request(
            f"/jobs/{self.job_id}/delivery",
            {"csrf_token": token, "idempotency_key": "route-render",
             "recipient": "Delivery Customer", f"qty_{item['id']}": "1"}, token,
        )
        response = asyncio.run(legacy_app.create_job_delivery(request, self.job_id))
        self.assertEqual(response.status_code, 303)
        self.assertIn(f"/jobs/{self.job_id}/delivery?delivery_id=", response.headers["location"])
        rendered = legacy_app.job_delivery_workspace(
            self.request(response.headers["location"]), self.job_id
        )
        self.assertEqual(rendered.status_code, 200)
        self.assertIn("Mark Delivered", rendered.body.decode())

    def test_template_uses_dict_key_and_mark_delivered_route_is_unchanged(self):
        template = (ROOT / "templates" / "job_delivery.html").read_text()
        self.assertIn('ready_delivery["items"]', template)
        self.assertNotIn("ready_delivery.items", template)
        self.assertIn('action="/deliveries/{{ ready_delivery.id }}/complete"', template)


if __name__ == "__main__":
    unittest.main()
