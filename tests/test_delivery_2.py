from contextlib import closing
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.admin.service import accounting_snapshot
from plg_core.database.migrations import run_migrations
from plg_core.documents.integrity import verified_delivery_document
from plg_core.supply.models import (
    DeliveryCreate, DeliveryItemCreate, ReceiptCreate, ReceiptItem,
)
from plg_core.supply.service import (
    cancel_delivery, complete_delivery, create_delivery,
    create_orders_from_paid_invoice, get_delivery, get_delivery_workspace,
    get_order, place_order, record_receipt,
)


ROOT = Path(__file__).resolve().parents[1]


class Delivery2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-delivery2-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        self.document_root = root / "documents"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.env_patch = patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)})
        self.db_patch.start(); self.env_patch.start(); run_migrations(); self._fixture()

    def tearDown(self):
        self.env_patch.stop(); self.db_patch.stop(); self.temp.cleanup()

    def _fixture(self):
        with closing(legacy_app.get_connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,active) VALUES ('D2-C','Delivery Customer',1)").lastrowid
            machine1 = c.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial) VALUES (?,'D2-M1','Loader','CAT','420D','PIN-A')", (customer,)).lastrowid
            machine2 = c.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial) VALUES (?,'D2-M2','Dozer','CAT','D6','PIN-B')", (customer,)).lastrowid
            self.job_id = c.execute("INSERT INTO jobs(job_number,created_date,customer_id,machine_id,customer,manufacturer,machine,pin_serial,status) VALUES ('D2-J','2026-08-14',?,?,'Delivery Customer','CAT','420D','PIN-A','CONFIRMED')", (customer,machine1)).lastrowid
            asset1 = c.execute("INSERT INTO job_assets(job_id,machine_id,customer_id,name,manufacturer,model,vin_pin_serial,is_primary) VALUES (?,?,?,'Loader','CAT','420D','PIN-A',1)", (self.job_id,machine1,customer)).lastrowid
            asset2 = c.execute("INSERT INTO job_assets(job_id,machine_id,customer_id,name,manufacturer,model,vin_pin_serial,is_primary) VALUES (?,?,?,'Dozer','CAT','D6','PIN-B',0)", (self.job_id,machine2,customer)).lastrowid
            quote = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total) VALUES ('D2-Q',?,'2026-08-14','CONVERTED',200)",(self.job_id,)).lastrowid
            self.invoice_id = c.execute("INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) VALUES ('D2-INV',?,?,'2026-08-14','PAID',200,0)",(quote,self.job_id)).lastrowid
            for supplier,part,qty,asset in (("D2 Supplier A","A-5",5,asset1),("D2 Supplier B","B-2",2,asset2)):
                c.execute("INSERT INTO invoice_items(invoice_id,quantity,description,supplier_name,supplier_part_number,supplier_unit_cost,supplier_line_total,customer_unit_price,customer_line_total,job_asset_id) VALUES (?,?,?,?,?,10,?,20,?,?)",(self.invoice_id,qty,f"Part {part}",supplier,part,qty*10,qty*20,asset))
            c.commit()
        self.orders=create_orders_from_paid_invoice(self.invoice_id)
        for row in self.orders: place_order(row["id"])
        self.order_a=get_order(self.orders[0]["id"]); self.order_b=get_order(self.orders[1]["id"])
        record_receipt(self.order_a["id"],ReceiptCreate(items=[ReceiptItem(order_item_id=self.order_a["items"][0]["id"],quantity_received=5)],receiver="Receiver",idempotency_key="d2-receipt"))
        self.item_a=self.order_a["items"][0]["id"]; self.item_b=self.order_b["items"][0]["id"]

    def _payload(self, qty=2, key="", item_id=None):
        return DeliveryCreate(items=[DeliveryItemCreate(order_item_id=item_id or self.item_a,quantity=qty)],recipient="Delivery Customer",notes="Customer handoff",idempotency_key=key)

    def _prepare(self, qty=2, key=""):
        return create_delivery(self.job_id,self._payload(qty,key),actor="delivery.operator",request_id="delivery-request",source_path=f"/jobs/{self.job_id}/delivery")

    def _complete(self, delivery):
        return complete_delivery(delivery["id"],actor="delivery.operator",request_id="complete-request",source_path=f"/deliveries/{delivery['id']}/complete")

    @staticmethod
    def _request(path, fields, cookie="", actor="web.delivery"):
        body=urlencode(fields).encode(); headers=[(b"content-type",b"application/x-www-form-urlencoded"),(b"content-length",str(len(body)).encode())]
        if cookie: headers.append((b"cookie",f"pps_csrf_token={cookie}".encode()))
        sent=False
        async def receive():
            nonlocal sent
            if sent: return {"type":"http.request","body":b"","more_body":False}
            sent=True; return {"type":"http.request","body":body,"more_body":False}
        user=type("User",(),{"is_authenticated":True,"username":actor})()
        request=Request({"type":"http","method":"POST","scheme":"http","path":path,"raw_path":path.encode(),"query_string":b"","headers":headers,"client":("127.0.0.1",1),"server":("test",80),"user":user},receive)
        request.state.request_id="web-delivery-request"; return request

    def test_01_migration_preserves_existing_data(self):
        before=(len(get_delivery_workspace(self.job_id)["items"]),self.order_a["status"]); run_migrations(); after=(len(get_delivery_workspace(self.job_id)["items"]),self.order_a["status"]); self.assertEqual(before,after)
    def test_02_received_only_availability(self): self.assertEqual(next(x for x in get_delivery_workspace(self.job_id)["items"] if x["id"]==self.item_a)["available_to_deliver"],5)
    def test_03_ordered_unreceived_unavailable(self): self.assertEqual(next(x for x in get_delivery_workspace(self.job_id)["items"] if x["id"]==self.item_b)["available_to_deliver"],0)
    def test_04_operator_selects_partial(self): self.assertEqual(self._prepare(2)["quantity_total"],2)
    def test_05_five_deliver_two_three_remain(self): self._prepare(2); self.assertEqual(get_delivery_workspace(self.job_id)["available_total"],3)
    def test_06_multiple_partial_deliveries(self):
        first=self._prepare(2); self._complete(first); second=self._prepare(3); self.assertEqual((first["quantity_total"],second["quantity_total"]),(2,3))
    def test_07_ready_reservation_excluded(self): self._prepare(2); self.assertEqual(get_delivery_workspace(self.job_id)["items"][0]["quantity_reserved"],2)
    def test_08_delivered_excluded(self):
        delivery=self._prepare(2); self._complete(delivery); row=get_delivery_workspace(self.job_id)["items"][0]; self.assertEqual((row["quantity_delivered"],row["available_to_deliver"]),(2,3))
    def test_09_over_delivery_rejected(self):
        with self.assertRaises(HTTPException) as e: self._prepare(6)
        self.assertEqual(e.exception.status_code,409)
    def test_10_concurrent_preparation_protected(self):
        results=[]
        def run(key):
            try: results.append(("ok",self._prepare(4,key)["id"]))
            except HTTPException as exc: results.append(("error",exc.status_code))
        threads=[threading.Thread(target=run,args=(f"race-{i}",)) for i in range(2)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(sorted(x[0] for x in results),["error","ok"])
    def test_11_one_ready_per_job(self):
        self._prepare(1)
        with self.assertRaises(HTTPException): self._prepare(1,"second")
    def test_12_idempotent_replay(self):
        first=self._prepare(2,"same-key"); second=self._prepare(2,"same-key"); self.assertEqual(first["id"],second["id"]); self.assertTrue(second["replayed"])
    def test_13_missing_csrf_rejected(self):
        req=self._request(f"/jobs/{self.job_id}/delivery",{f"qty_{self.item_a}":"1"})
        with self.assertRaises(HTTPException) as e: asyncio.run(legacy_app.create_job_delivery(req,self.job_id))
        self.assertEqual(e.exception.status_code,403)
    def test_14_invalid_csrf_rejected(self):
        req=self._request(f"/jobs/{self.job_id}/delivery",{f"qty_{self.item_a}":"1","csrf_token":"bad"},cookie="good")
        with self.assertRaises(HTTPException) as e: asyncio.run(legacy_app.create_job_delivery(req,self.job_id))
        self.assertEqual(e.exception.status_code,403)
    def test_15_actor_request_source_attribution(self):
        token="valid-delivery-csrf-token-12345"; path=f"/jobs/{self.job_id}/delivery"; req=self._request(path,{f"qty_{self.item_a}":"1","csrf_token":token,"idempotency_key":"web-key","recipient":"Pat"},cookie=token)
        asyncio.run(legacy_app.create_job_delivery(req,self.job_id))
        with closing(legacy_app.get_connection()) as c:
            row=c.execute("SELECT request_id,source_path FROM deliveries WHERE idempotency_key='web-key'").fetchone(); audit=c.execute("SELECT actor,request_id FROM audit_logs WHERE action='DELIVERY_CREATED' ORDER BY id DESC").fetchone()
        self.assertEqual(tuple(row),("web-delivery-request",path)); self.assertEqual(tuple(audit),("web.delivery","web-delivery-request"))
    def test_16_ready_cancellation_releases(self):
        d=self._prepare(2); cancel_delivery(d["id"]); self.assertEqual(get_delivery_workspace(self.job_id)["available_total"],5)
    def test_17_delivered_cannot_cancel(self):
        d=self._prepare(1); self._complete(d)
        with self.assertRaises(HTTPException): cancel_delivery(d["id"])
    def test_18_completion_idempotent(self):
        d=self._prepare(1); first=self._complete(d); second=self._complete(d); self.assertEqual(first["id"],second["id"]); self.assertTrue(second["replayed"])
    def test_19_delivery_note_generated_once(self):
        d=self._prepare(1); self._complete(d); self._complete(d)
        with closing(legacy_app.get_connection()) as c: self.assertEqual(c.execute("SELECT COUNT(*) FROM delivery_documents_manifest WHERE delivery_id=?",(d["id"],)).fetchone()[0],1)
    def test_20_delivery_note_hash_valid(self):
        d=self._prepare(1); self._complete(d)
        with closing(legacy_app.get_connection()) as c: row=c.execute("SELECT * FROM delivery_documents_manifest WHERE delivery_id=?",(d["id"],)).fetchone()
        self.assertEqual(hashlib.sha256(Path(row["file_path"]).read_bytes()).hexdigest(),row["sha256"])
    def test_21_missing_note_fails_closed(self):
        d=self._prepare(1); self._complete(d)
        with closing(legacy_app.get_connection()) as c: path=verified_delivery_document(c,d["id"])
        path.unlink()
        with closing(legacy_app.get_connection()) as c,self.assertRaises(HTTPException): verified_delivery_document(c,d["id"])
    def test_22_corrupt_note_fails_closed(self):
        d=self._prepare(1); self._complete(d)
        with closing(legacy_app.get_connection()) as c: path=verified_delivery_document(c,d["id"])
        path.write_bytes(path.read_bytes()+b"corrupt")
        with closing(legacy_app.get_connection()) as c,self.assertRaises(HTTPException): verified_delivery_document(c,d["id"])
    def test_23_note_excludes_financials(self):
        d=self._prepare(1); self._complete(d)
        with closing(legacy_app.get_connection()) as c: path=verified_delivery_document(c,d["id"])
        text=subprocess.run(["pdftotext",str(path),"-"],check=True,capture_output=True,text=True).stdout.lower()
        for forbidden in ("supplier cost","customer price","markup","profit","$20.00"): self.assertNotIn(forbidden,text)
    def test_24_multi_supplier_independence(self): self.assertEqual(get_delivery_workspace(self.job_id)["available_total"],5)
    def test_25_multi_asset_context(self):
        record_receipt(self.order_b["id"],ReceiptCreate(items=[ReceiptItem(order_item_id=self.item_b,quantity_received=2)],idempotency_key="b-receipt")); d=create_delivery(self.job_id,DeliveryCreate(items=[DeliveryItemCreate(order_item_id=self.item_a,quantity=1),DeliveryItemCreate(order_item_id=self.item_b,quantity=1)])); self.assertEqual(d["machine_context"],"Multiple job assets")
    def test_26_invoice_shown(self): self.assertEqual(get_delivery_workspace(self.job_id)["job"]["invoice_number"],"D2-INV")
    def test_27_job_completion_correct(self):
        d=self._prepare(5); result=self._complete(d); self.assertFalse(result["job_complete"])
    def test_28_line_history_evidence(self):
        d=self._prepare(2); self._complete(d); history=get_delivery_workspace(self.job_id)["deliveries"][0]; self.assertEqual((history["id"],history["items"][0]["quantity_delivered"]),(d["id"],2))
    def test_29_1440_contract(self): self.assertIn("delivery-line-head",(ROOT/"templates/job_delivery.html").read_text())
    def test_30_820_contract(self): self.assertIn("@media(max-width:820px)",(ROOT/"static/app.css").read_text())
    def test_31_390_stacked_contract(self):
        css=(ROOT/"static/app.css").read_text(); self.assertIn("@media(max-width:520px)",css); self.assertIn(".delivery-line{grid-template-columns:1fr 1fr",css)
    def test_32_accounting_unchanged(self):
        before=accounting_snapshot()["financials"]; d=self._prepare(1); self._complete(d); after=accounting_snapshot()["financials"]; self.assertEqual(before,after)
    def test_33_receiving_quantity_unchanged(self):
        before=get_order(self.order_a["id"])["items"][0]["quantity_received"]; d=self._prepare(2); self._complete(d); after=get_order(self.order_a["id"])["items"][0]["quantity_received"]; self.assertEqual(before,after)
    def test_34_sqlite_integrity(self):
        with closing(legacy_app.get_connection()) as c: self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0],"ok")
    def test_35_foreign_keys(self):
        with closing(legacy_app.get_connection()) as c: self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(),[])


if __name__ == "__main__": unittest.main()
