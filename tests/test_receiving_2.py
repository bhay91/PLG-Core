from contextlib import closing
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.documents.integrity import verified_receiving_document
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


class Receiving2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-receiving2-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        self.document_root = root / "documents"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.env_patch = patch.dict(
            os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)}
        )
        self.db_patch.start()
        self.env_patch.start()
        run_migrations()
        self._fixture()

    def tearDown(self):
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def _fixture(self):
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('R2-C','Receiving Customer',1)"
            ).lastrowid
            machine = c.execute(
                "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial) "
                "VALUES (?,'R2-M','Loader','Caterpillar','420D','PIN-R2')", (customer,)
            ).lastrowid
            self.job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,machine_id,customer,status) "
                "VALUES ('R2-J','2026-08-14',?,?,?,'CONFIRMED')",
                (customer, machine, "Receiving Customer"),
            ).lastrowid
            asset = c.execute(
                "INSERT INTO job_assets(job_id,machine_id,customer_id,name,manufacturer,model,vin_pin_serial,is_primary) "
                "VALUES (?,?,?,'Loader','Caterpillar','420D','PIN-R2',1)",
                (self.job_id, machine, customer),
            ).lastrowid
            quote = c.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total) "
                "VALUES ('R2-Q',?,'2026-08-14','CONVERTED',200)", (self.job_id,)
            ).lastrowid
            self.invoice_id = c.execute(
                "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) "
                "VALUES ('R2-INV',?,?,'2026-08-14','PAID',200,0)",
                (quote, self.job_id),
            ).lastrowid
            for supplier, part, quantity in (("R2 Supplier A", "A-5", 5), ("R2 Supplier B", "B-2", 2)):
                c.execute(
                    "INSERT INTO invoice_items(invoice_id,quantity,description,supplier_name,supplier_part_number,"
                    "supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total,job_asset_id) "
                    "VALUES (?,?,?,?,?,10,?,20,?,?)",
                    (self.invoice_id, quantity, f"Part {part}", supplier, part,
                     quantity * 10, quantity * 20, asset),
                )
            c.commit()
        self.orders = create_orders_from_paid_invoice(self.invoice_id)
        for order in self.orders:
            place_order(order["id"])
        self.order_a = get_order(self.orders[0]["id"])
        self.order_b = get_order(self.orders[1]["id"])

    def _receive(self, quantity, key, *, order=None, receiver="Casey Receiver"):
        order = order or self.order_a
        return record_receipt(
            order["id"],
            ReceiptCreate(
                items=[ReceiptItem(order_item_id=order["items"][0]["id"], quantity_received=quantity)],
                notes="Packing slip R2; no damage",
                receiver=receiver,
                idempotency_key=key,
            ),
            actor="receiving.operator",
            request_id="request-r2-001",
            source_path=f"/purchasing/orders/{order['id']}/receive",
        )

    def _render_order_detail(self):
        request = Request({
            "type": "http", "method": "GET", "path": f"/purchasing/orders/{self.order_a['id']}",
            "raw_path": b"", "query_string": b"", "headers": [], "scheme": "http",
            "server": ("testserver", 80), "client": ("testclient", 50000), "root_path": "",
            "app": legacy_app.app,
        })
        response = legacy_app.purchasing_order_detail(request, self.order_a["id"])
        self.assertEqual(response.status_code, 200)
        return response.body.decode()

    def test_order_detail_renders_without_partial_and_final_receipts(self):
        empty = self._render_order_detail()
        self.assertNotIn("receiving-history-card", empty)
        self._receive(1, "render-partial")
        partial = self._render_order_detail()
        self.assertIn("receiving-history-card", partial)
        self.assertIn("PARTIAL", partial)
        self.assertIn("This Receipt", partial)
        self._receive(4, "render-final")
        final = self._render_order_detail()
        self.assertIn("RECEIVED", final)
        self.assertEqual(final.count("receiving-history-card"), 2)
        self.assertIn("Open Receiving Summary", final)

    @staticmethod
    def _web_request(path, fields, *, cookie_token="", actor="web.receiver"):
        body = urlencode(fields).encode()
        headers = [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ]
        if cookie_token:
            headers.append((b"cookie", f"pps_csrf_token={cookie_token}".encode()))
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        user = type("User", (), {"is_authenticated": True, "username": actor})()
        request = Request({
            "type": "http", "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "headers": headers,
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000),
            "user": user,
        }, receive)
        request.state.request_id = "web-receipt-request"
        return request

    def test_partial_receipt_attribution_history_and_idempotent_replay(self):
        receipt = self._receive(2, "partial-key")
        self.assertEqual((receipt["status_after"], receipt["quantity_received"], receipt["total_remaining"]), ("PARTIAL", 2, 3))
        replay = self._receive(2, "partial-key")
        self.assertEqual(replay["id"], receipt["id"])
        self.assertTrue(replay["replayed"])
        with closing(legacy_app.get_connection()) as c:
            event = c.execute("SELECT * FROM receiving_events WHERE id=?", (receipt["id"],)).fetchone()
            audit = c.execute("SELECT * FROM audit_logs WHERE action='PARTS_RECEIVED' AND entity_id=? ORDER BY id DESC", (self.order_a["id"],)).fetchone()
            self.assertEqual((event["receiver"], event["request_id"], event["source_path"]),
                             ("Casey Receiver", "request-r2-001", f"/purchasing/orders/{self.order_a['id']}/receive"))
            self.assertEqual((audit["actor"], audit["request_id"]), ("receiving.operator", "request-r2-001"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM receiving_events WHERE idempotency_key='partial-key'").fetchone()[0], 1)

    def test_web_receiving_rejects_missing_invalid_csrf_and_accepts_valid(self):
        path = f"/purchasing/orders/{self.order_a['id']}/receive"
        base = {"qty_" + str(self.order_a["items"][0]["id"]): "1", "receiver": "Pat Receiver", "idempotency_key": "web-key"}
        for request, submitted in (
            (self._web_request(path, base), ""),
            (self._web_request(path, {**base, "csrf_token": "wrong"}, cookie_token="right"), "wrong"),
        ):
            with self.assertRaises(HTTPException) as failure:
                asyncio.run(legacy_app.receive_supplier_order_web(request, self.order_a["id"]))
            self.assertEqual(failure.exception.status_code, 403)
        token = "valid-receiving-csrf-token-123456"
        request = self._web_request(path, {**base, "csrf_token": token}, cookie_token=token)
        response = asyncio.run(legacy_app.receive_supplier_order_web(request, self.order_a["id"]))
        self.assertEqual(response.status_code, 303)
        with closing(legacy_app.get_connection()) as c:
            event = c.execute("SELECT receiver,request_id,source_path FROM receiving_events WHERE idempotency_key='web-key'").fetchone()
            audit = c.execute("SELECT actor FROM audit_logs WHERE action='PARTS_RECEIVED' AND entity_id=? ORDER BY id DESC", (self.order_a["id"],)).fetchone()
        self.assertEqual(tuple(event), ("Pat Receiver", "web-receipt-request", path))
        self.assertEqual(audit["actor"], "web.receiver")

    def test_api_receiving_uses_header_idempotency_and_request_attribution(self):
        from plg_core.supply.routes import receive
        path = f"/api/v1/supply/orders/{self.order_b['id']}/receipts"
        request = self._web_request(path, {}, actor="api.receiver")
        request.scope["headers"].append((b"idempotency-key", b"api-header-key"))
        payload = ReceiptCreate(
            items=[ReceiptItem(order_item_id=self.order_b["items"][0]["id"], quantity_received=1)],
            receiver="Warehouse Receiver",
        )
        first = receive(request, self.order_b["id"], payload)
        second = receive(request, self.order_b["id"], payload)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["replayed"])
        with closing(legacy_app.get_connection()) as c:
            event = c.execute("SELECT request_id,source_path,idempotency_key FROM receiving_events WHERE id=?", (first["id"],)).fetchone()
        self.assertEqual(tuple(event), ("web-receipt-request", path, "api-header-key"))

    def test_migration_is_repeatable_and_preserves_existing_receiving_rows(self):
        receipt = self._receive(1, "migration-preserve")
        with closing(legacy_app.get_connection()) as c:
            before = tuple(c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in ("receiving_events", "receiving_event_items", "receiving_documents_manifest"))
            values = tuple(c.execute("SELECT receiver,request_id,source_path,idempotency_key FROM receiving_events WHERE id=?", (receipt["id"],)).fetchone())
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            after = tuple(c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] for table in ("receiving_events", "receiving_event_items", "receiving_documents_manifest"))
            preserved = tuple(c.execute("SELECT receiver,request_id,source_path,idempotency_key FROM receiving_events WHERE id=?", (receipt["id"],)).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(values, preserved)

    def test_multiple_receipts_finish_five_without_duplicate_numbers(self):
        ids = [self._receive(qty, key)["id"] for qty, key in ((2, "m1"), (2, "m2"), (1, "m3"))]
        self.assertEqual(get_order(self.order_a["id"])["status"], "RECEIVED")
        with closing(legacy_app.get_connection()) as c:
            total = c.execute("SELECT SUM(quantity_received) FROM receiving_event_items WHERE order_item_id=?", (self.order_a["items"][0]["id"],)).fetchone()[0]
            self.assertEqual(total, 5)
            numbers = [r[0] for r in c.execute("SELECT receipt_number FROM receiving_events WHERE id IN (?,?,?)", ids)]
            self.assertEqual(len(numbers), len(set(numbers)))

    def test_full_receipt_and_multi_supplier_independence(self):
        before_b = get_order(self.order_b["id"])
        receipt = self._receive(5, "full")
        after_b = get_order(self.order_b["id"])
        self.assertEqual(receipt["status_after"], "RECEIVED")
        self.assertEqual((before_b["status"], before_b["items"][0]["quantity_received"]),
                         (after_b["status"], after_b["items"][0]["quantity_received"]))

    def test_over_receive_duplicate_and_cross_order_are_rejected_atomically(self):
        cases = [
            ReceiptCreate(items=[ReceiptItem(order_item_id=self.order_a["items"][0]["id"], quantity_received=6)]),
            ReceiptCreate(items=[ReceiptItem(order_item_id=self.order_a["items"][0]["id"], quantity_received=1), ReceiptItem(order_item_id=self.order_a["items"][0]["id"], quantity_received=1)]),
            ReceiptCreate(items=[ReceiptItem(order_item_id=self.order_b["items"][0]["id"], quantity_received=1)]),
        ]
        for payload in cases:
            with self.assertRaises(HTTPException):
                record_receipt(self.order_a["id"], payload)
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM receiving_events WHERE order_id=?", (self.order_a["id"],)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT quantity_received FROM supplier_order_items WHERE id=?", (self.order_a["items"][0]["id"],)).fetchone()[0], 0)

    def test_stale_second_receipt_cannot_over_receive(self):
        self._receive(4, "race-first")
        with self.assertRaises(HTTPException) as failure:
            self._receive(2, "race-stale")
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(get_order(self.order_a["id"])["items"][0]["quantity_received"], 4)

    def test_receiving_summary_is_immutable_hashed_and_supplier_safe(self):
        receipt = self._receive(2, "pdf")
        with closing(legacy_app.get_connection()) as c:
            manifest = c.execute("SELECT * FROM receiving_documents_manifest WHERE receipt_id=?", (receipt["id"],)).fetchone()
            path = verified_receiving_document(c, receipt["id"])
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest["sha256"])
        text = subprocess.run(["pdftotext", str(path), "-"], check=True, capture_output=True, text=True).stdout
        for expected in ("RECEIVING SUMMARY", receipt["receipt_number"], "Casey Receiver", "A-5", "PARTIAL", "Packing slip R2"):
            self.assertIn(expected, text)
        for forbidden in ("customer sell price", "markup", "profit", "$20.00"):
            self.assertNotIn(forbidden.lower(), text.lower())

    def test_missing_and_corrupt_receiving_summary_fail_closed(self):
        receipt = self._receive(1, "fail-closed")
        with closing(legacy_app.get_connection()) as c:
            path = verified_receiving_document(c, receipt["id"])
        original = path.read_bytes()
        path.unlink()
        with closing(legacy_app.get_connection()) as c, self.assertRaises(HTTPException):
            verified_receiving_document(c, receipt["id"])
        path.write_bytes(original + b"corrupt")
        with closing(legacy_app.get_connection()) as c, self.assertRaises(HTTPException):
            verified_receiving_document(c, receipt["id"])

    def test_delivery_availability_reservations_and_accounting_are_preserved(self):
        with closing(legacy_app.get_connection()) as c:
            placed_before = c.execute("SELECT SUM(order_total) FROM supplier_orders WHERE invoice_id=? AND status IN ('ORDERED','PARTIAL','RECEIVED')", (self.invoice_id,)).fetchone()[0]
        self._receive(2, "delivery")
        workspace = get_delivery_workspace(self.job_id)
        item = next(row for row in workspace["items"] if row["supplier_part_number"] == "A-5")
        self.assertEqual(item["available_to_deliver"], 2)
        delivery = create_delivery(self.job_id, DeliveryCreate(
            items=[DeliveryItemCreate(order_item_id=item["id"], quantity=item["available_to_deliver"]) for item in workspace["items"] if item["available_to_deliver"] > 0],
            recipient="Receiving Customer",
        ))
        self.assertFalse(get_delivery_workspace(self.job_id)["has_available"])
        with closing(legacy_app.get_connection()) as c:
            placed_after = c.execute("SELECT SUM(order_total) FROM supplier_orders WHERE invoice_id=? AND status IN ('ORDERED','PARTIAL','RECEIVED')", (self.invoice_id,)).fetchone()[0]
            reserved = c.execute("SELECT SUM(quantity_delivered) FROM delivery_items WHERE delivery_id=?", (delivery["id"],)).fetchone()[0]
        self.assertEqual((placed_before, placed_after, reserved), (placed_before, placed_before, 2))

    def test_integrity_foreign_keys_and_no_orphan_manifest(self):
        self._receive(1, "integrity")
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM receiving_documents_manifest m LEFT JOIN receiving_events r ON r.id=m.receipt_id WHERE r.id IS NULL").fetchone()[0], 0)


class Receiving2PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "supplier_order_detail.html").read_text()
        cls.css = (ROOT / "static" / "app.css").read_text()

    def test_receiving_form_has_csrf_receiver_idempotency_and_one_submit(self):
        receiving = self.template.split('id="receive-parts"', 1)[1].split('{% elif order.status', 1)[0]
        for field in ('name="csrf_token"', 'name="idempotency_key"', 'name="receiver"'):
            self.assertIn(field, receiving)
        self.assertEqual(receiving.count("Record Receipt"), 1)

    def test_history_uses_receipt_evidence_and_summary_link(self):
        for value in ('receipt["items"]', "item.quantity_received", "item.cumulative_received", "item.remaining", "Open Receiving Summary"):
            self.assertIn(value, self.template)

    def test_mobile_receiving_cards_have_no_table_overflow_contract(self):
        for value in ("receiving-line", "receiving-mobile-label", "@media (max-width: 680px)", "grid-template-columns: repeat(2, minmax(0, 1fr))"):
            self.assertIn(value, self.css)
        self.assertNotIn("<table", self.template.split('id="receive-parts"', 1)[1].split('{% elif order.status', 1)[0])


if __name__ == "__main__":
    unittest.main()
