from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import anyio
import httpx2
from fastapi import Depends, FastAPI, Request

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.requests.extension_auth import (
    FIREFOX_JOB_UPDATE_SCOPE,
    require_firefox_job_update_authorization,
)
from plg_core.requests.extension_job_models import (
    ExtensionItemAction,
    ExtensionJobAction,
    ExtensionPaymentAction,
)
from plg_core.requests.extension_routes import (
    extension_item_delivered,
    extension_item_received,
    extension_order_placed,
    extension_payment_received,
)


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "extension-job-update-token"


def extension_test_app() -> FastAPI:
    test_app = FastAPI()

    async def authorize(request: Request) -> str:
        return require_firefox_job_update_authorization(request)

    @test_app.post("/api/extension/v1/jobs/payment-received")
    async def payment_received(
        payload: ExtensionPaymentAction,
        scope: str = Depends(authorize),
    ):
        return extension_payment_received(payload, scope)

    @test_app.post("/api/extension/v1/jobs/order-placed")
    async def order_placed(
        payload: ExtensionJobAction,
        scope: str = Depends(authorize),
    ):
        return extension_order_placed(payload, scope)

    @test_app.post("/api/extension/v1/jobs/item-received")
    async def item_received(
        payload: ExtensionItemAction,
        scope: str = Depends(authorize),
    ):
        return extension_item_received(payload, scope)

    @test_app.post("/api/extension/v1/jobs/item-delivered")
    async def item_delivered(
        payload: ExtensionItemAction,
        scope: str = Depends(authorize),
    ):
        return extension_item_delivered(payload, scope)

    return test_app


class ExtensionJobUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-extension-job-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.env_patch = patch.dict(os.environ, {
            "PPS_DOCUMENT_ROOT": str(root / "documents"),
            "PPS_FIREFOX_INBOX_TOKEN": TOKEN,
            "PPS_FIREFOX_INBOX_SCOPES": FIREFOX_JOB_UPDATE_SCOPE,
        })
        self.db_patch.start()
        self.env_patch.start()
        self.app = extension_test_app()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('EXT-C','Synthetic Extension Customer',1)"
            ).lastrowid
            self.job_id = connection.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) "
                "VALUES ('EXT-J-0001','2026-08-19',?,'Synthetic Extension Customer','INVOICED')", (customer_id,)
            ).lastrowid
            quote_id = connection.execute(
                "INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,supplier_total,profit_total) "
                "VALUES ('EXT-Q',?,'2026-08-19','CONVERTED',300,120,180)", (self.job_id,)
            ).lastrowid
            self.invoice_id = connection.execute(
                "INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,supplier_total,profit_total,balance_due) "
                "VALUES ('EXT-I',?,?,'2026-08-19','UNPAID',300,120,180,300)",
                (quote_id, self.job_id),
            ).lastrowid
            for description, quantity in (("Headlight", 2), ("Seal", 1)):
                connection.execute(
                    """INSERT INTO invoice_items(
                       invoice_id,quantity,description,supplier_name,supplier_unit_cost,
                       supplier_line_total,customer_unit_price,customer_line_total,line_profit
                    ) VALUES (?,?,?,'Supplier A',40,?,?,?,?)""",
                    (self.invoice_id, quantity, description, 40 * quantity,
                     100, 100 * quantity, 60 * quantity),
                )
            connection.commit()
        self.accounting_before_fulfillment = None

    def tearDown(self):
        self.env_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    async def request(self, action, payload, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        transport = httpx2.ASGITransport(
            app=self.app, client=("127.0.0.1", 48111)
        )
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            return await client.post(
                f"/api/extension/v1/jobs/{action}", json=payload, headers=headers
            )

    def post(self, action, payload, token=TOKEN):
        return anyio.run(lambda: self.request(action, payload, token))

    def _accounting(self):
        with closing(legacy_app.get_connection()) as connection:
            invoice = tuple(connection.execute(
                "SELECT customer_total,supplier_total,profit_total FROM invoices WHERE id=?",
                (self.invoice_id,),
            ).fetchone())
            items = [tuple(row) for row in connection.execute(
                "SELECT quantity,supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total,line_profit "
                "FROM invoice_items WHERE invoice_id=? ORDER BY id", (self.invoice_id,)
            )]
        return invoice, items

    def _pay_and_order(self):
        paid = self.post("payment-received", {
            "job_number": "EXT-J-0001", "request_id": "pay-1", "amount": 300,
            "payment_method": "CARD",
        })
        self.assertEqual(paid.status_code, 200, paid.text)
        self.accounting_before_fulfillment = self._accounting()
        ordered = self.post("order-placed", {"job_number": "EXT-J-0001", "request_id": "order-1"})
        self.assertEqual(ordered.status_code, 200, ordered.text)
        return {row["description"]: row for row in ordered.json()["fulfillment"]["items"]}

    def test_payment_and_order_use_existing_workflows_and_audit(self):
        items = self._pay_and_order()
        self.assertEqual(set(items), {"Headlight", "Seal"})
        with closing(legacy_app.get_connection()) as connection:
            invoice = connection.execute("SELECT status,balance_due FROM invoices WHERE id=?", (self.invoice_id,)).fetchone()
            audit = connection.execute(
                "SELECT actor,request_id FROM audit_logs WHERE action='PAYMENT_RECEIVED' AND entity_id=? ORDER BY id DESC",
                (str(self.invoice_id),),
            ).fetchone()
        self.assertEqual(tuple(invoice), ("PAID", 0))
        self.assertEqual(tuple(audit), ("chatgpt-firefox-bridge", "pay-1"))

    def test_partial_receive_delivery_and_full_completion(self):
        items = self._pay_and_order()
        headlight = items["Headlight"]
        received = self.post("item-received", {
            "job_number": "EXT-J-0001", "request_id": "receive-1",
            "item_reference": "Headlight", "quantity": 1,
        })
        self.assertEqual(received.status_code, 200, received.text)
        delivered = self.post("item-delivered", {
            "job_number": "EXT-J-0001", "request_id": "deliver-1",
            "item_id": headlight["id"], "quantity": 1,
        })
        line = next(row for row in delivered.json()["fulfillment"]["items"] if row["id"] == headlight["id"])
        self.assertEqual((line["quantity_received"], line["quantity_delivered"], line["quantity_remaining"]), (1, 1, 1))
        self.assertNotEqual(delivered.json()["fulfillment"]["stage"], "COMPLETED")
        self.assertEqual(self._accounting(), self.accounting_before_fulfillment)

        for action, request_id, reference, quantity in (
            ("item-received", "receive-2", "Headlight", 1),
            ("item-delivered", "deliver-2", "Headlight", 1),
            ("item-received", "receive-seal", "Seal", 1),
            ("item-delivered", "deliver-seal", "Seal", 1),
        ):
            response = self.post(action, {"job_number": "EXT-J-0001", "request_id": request_id,
                                          "item_reference": reference, "quantity": quantity})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["fulfillment"]["stage"], "COMPLETED")
        self.assertEqual(self._accounting(), self.accounting_before_fulfillment)

    def test_invalid_job_quantity_and_ambiguous_item_are_rejected(self):
        missing = self.post("order-placed", {"job_number": "NO-SUCH-JOB", "request_id": "missing"})
        self.assertEqual(missing.status_code, 404)
        items = self._pay_and_order()
        invalid = self.post("item-received", {
            "job_number": "EXT-J-0001", "request_id": "too-many",
            "item_id": items["Headlight"]["id"], "quantity": 3,
        })
        self.assertEqual(invalid.status_code, 409)
        with closing(legacy_app.get_connection()) as connection:
            source = connection.execute(
                "SELECT * FROM supplier_order_items WHERE id=?", (items["Seal"]["id"],)
            ).fetchone()
            connection.execute(
                "INSERT INTO supplier_order_items(order_id,invoice_item_id,description,supplier_part_number,quantity_ordered,quantity_received,unit_cost,line_cost) "
                "VALUES (?,?,?,?,1,0,?,?)",
                (source["order_id"], source["invoice_item_id"], "Seal", "DUP-SEAL", source["unit_cost"], source["line_cost"]),
            )
            connection.commit()
        ambiguous = self.post("item-received", {
            "job_number": "EXT-J-0001", "request_id": "ambiguous",
            "item_reference": "Seal", "quantity": 1,
        })
        self.assertEqual(ambiguous.status_code, 409)

    def test_job_update_scope_and_bearer_token_are_required(self):
        payload = {"job_number": "EXT-J-0001", "request_id": "auth"}
        self.assertEqual(self.post("order-placed", payload, token=None).status_code, 401)
        self.assertEqual(self.post("order-placed", payload, token="wrong").status_code, 401)


if __name__ == "__main__":
    unittest.main()
