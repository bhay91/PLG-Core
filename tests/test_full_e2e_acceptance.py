from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.admin.service import accounting_snapshot
from plg_core.basket.models import BasketItemCreate
from plg_core.basket.service import add_item
from plg_core.database.migrations import run_migrations
from plg_core.documents import invoice_pdf, quote_pdf
from plg_core.documents.integrity import verified_invoice_document
from plg_core.documents.library import query_authoritative_documents, resolve_manifest_document
from plg_core.documents.paths import resolve_manifest_path
from plg_core.intake.service import confirm_proposal, create_proposal, load_proposal
from plg_core.lifecycle import transition_quote
from plg_core.sales.service import record_invoice_payment
from plg_core.supply.models import (
    DeliveryCreate, DeliveryItemCreate, ReceiptCreate, ReceiptItem,
)
from plg_core.supply.service import (
    complete_delivery, create_delivery, create_orders_from_paid_invoice,
    get_delivery_workspace, get_order, place_order, record_actual_cost_adjustment, record_receipt,
)


ROOT = Path(__file__).resolve().parents[1]
REQUEST_SENTENCE = (
    "Need hydraulic cylinder seal kit and headlight sets for the Caterpillar 420D, "
    "and wheel seal and brake drums for the International 4700."
)


class FullEndToEndAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-full-acceptance-")
        root = Path(self.temp.name)
        self.db_path = root / "acceptance.db"
        self.document_root = root / "documents"
        self.customer_document_root = self.document_root / "Customers"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.patches = [
            patch.object(legacy_app, "DB_PATH", self.db_path),
            patch.object(legacy_app, "DOCUMENTS_DIR", self.document_root),
            patch.object(quote_pdf, "DOCUMENT_ROOT", self.customer_document_root),
            patch.object(invoice_pdf, "DOCUMENT_ROOT", self.customer_document_root),
            patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(self.document_root)}),
        ]
        for item in self.patches:
            item.start()
        run_migrations()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    @staticmethod
    def digest(path):
        return hashlib.sha256(resolve_manifest_path(path).read_bytes()).hexdigest()

    def test_complete_connected_lifecycle(self):
        report = {}
        with closing(self.connection()) as c:
            customer_id = c.execute(
                "INSERT INTO customers(customer_number,name,email,active) VALUES ('E2E-C','Synthetic E2E Customer','synthetic-e2e@example.test',1)"
            ).lastrowid
            machine_a = c.execute(
                "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'E2E-M-A','420D Backhoe Loader','Caterpillar','420D Backhoe Loader','TEST-420D-001',1)",
                (customer_id,),
            ).lastrowid
            machine_b = c.execute(
                "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'E2E-M-B','4700 Flatbed','International','4700 Flatbed','TEST-4700-001',1)",
                (customer_id,),
            ).lastrowid
            c.commit()
            raw = (
                "Synthetic E2E Customer\nsynthetic-e2e@example.test\n"
                "Caterpillar 420D Backhoe Loader\nSerial TEST-420D-001\n"
                "International 4700 Flatbed\nVIN TEST-4700-001\n\n" + REQUEST_SENTENCE
            )
            before = {
                "customers": c.execute("SELECT COUNT(*) FROM customers WHERE name='Synthetic E2E Customer'").fetchone()[0],
                "machines": c.execute("SELECT COUNT(*) FROM machines WHERE customer_id=?", (customer_id,)).fetchone()[0],
                "jobs": c.execute("SELECT COUNT(*) FROM jobs WHERE customer_id=?", (customer_id,)).fetchone()[0],
            }
            proposal_id = create_proposal(c, raw)
            proposal = load_proposal(c, proposal_id)
            self.assertEqual(proposal["raw_input"], raw)
            self.assertIn(REQUEST_SENTENCE, proposal["raw_input"])
            self.assertEqual(proposal["matched_customer_id"], customer_id)
            self.assertIn(machine_a, {a["matched_machine_id"] for a in proposal["assets"]})
            self.assertIn(machine_b, {a["matched_machine_id"] for a in proposal["assets"]})
            self.assertEqual(c.execute("SELECT COUNT(*) FROM jobs WHERE customer_id=?", (customer_id,)).fetchone()[0], before["jobs"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM customers WHERE name='Synthetic E2E Customer'").fetchone()[0], before["customers"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM machines WHERE customer_id=?", (customer_id,)).fetchone()[0], before["machines"])
            report["proposal_review_count"] = proposal["review_summary"]["review_count"]
            report["proposal_missing"] = proposal["review_summary"]["missing"]
        with closing(self.connection()) as c:
            proposal = load_proposal(c, proposal_id)
            self.assertEqual({a["matched_machine_id"] for a in proposal["assets"]}, {machine_a, machine_b})
            needs_by_make = {
                asset["manufacturer"]: {need["wording"].lower() for need in asset["needs"]}
                for asset in proposal["assets"]
            }
            self.assertEqual(needs_by_make["Caterpillar"], {"hydraulic cylinder seal kit", "headlight sets"})
            self.assertEqual(needs_by_make["International"], {"wheel seal", "brake drums"})
            job_id = confirm_proposal(c, proposal_id, proposal["lock_version"])
            self.assertEqual(confirm_proposal(c, proposal_id, proposal["lock_version"]), job_id)
            self.assertEqual(c.execute("SELECT customer_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0], customer_id)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM job_assets WHERE job_id=?", (job_id,)).fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM customers WHERE name='Synthetic E2E Customer'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM machines WHERE customer_id=?", (customer_id,)).fetchone()[0], 2)
            request_text = c.execute("SELECT request_text FROM customer_requests WHERE job_id=?", (job_id,)).fetchone()[0]
            self.assertIn(REQUEST_SENTENCE, request_text)
            assets = c.execute("SELECT id,machine_id,manufacturer,model FROM job_assets WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
            needs = c.execute("SELECT id,job_asset_id,wording FROM requested_needs WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

        asset_by_make = {row["manufacturer"]: row for row in assets}
        need_by_phrase = {row["wording"].lower(): row for row in needs}
        items = (
            ("Hydraulic cylinder seal kit", 2, "Synthetic Supplier A", 30, 50, asset_by_make["Caterpillar"]["id"]),
            ("Headlight sets", 1, "Synthetic Supplier A", 90, 150, asset_by_make["Caterpillar"]["id"]),
            ("Wheel seal", 1, "Synthetic Supplier B", 70, 120, asset_by_make["International"]["id"]),
            ("Brake drums", 1, "Synthetic Supplier B", 50, 80, asset_by_make["International"]["id"]),
        )
        for index, (description, qty, supplier, cost, sell, asset_id) in enumerate(items, 1):
            matching_need = next((n["id"] for n in needs if description.split()[0].lower() in n["wording"].lower()), None)
            add_item(job_id, BasketItemCreate(
                job_asset_id=asset_id, primary_requested_need_id=matching_need,
                requested_description=description, supplier_part_number=f"E2E-{index}",
                supplier_name=supplier, quantity=qty, supplier_unit_cost=cost,
                customer_unit_price_override=sell, verification_status="VERIFIED",
                verification_note="Acceptance fixture verified", selected=True,
            ))

        legacy_app.generate_quote(job_id)
        with closing(self.connection()) as c:
            quote = c.execute("SELECT * FROM quotes WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
            quote_id = int(quote["id"])
            self.assertEqual((quote["customer_total"], quote["supplier_total"], quote["profit_total"]), (450, 270, 180))
        legacy_app.mark_quote_sent(quote_id)
        transition_quote(quote_id, "APPROVED")
        legacy_app.convert_quote_to_invoice(quote_id)
        with closing(self.connection()) as c:
            invoice = c.execute("SELECT * FROM invoices WHERE quote_id=?", (quote_id,)).fetchone()
            invoice_id = int(invoice["id"])
            self.assertEqual((invoice["customer_total"], invoice["supplier_total"], invoice["profit_total"]), (450, 270, 180))
            customer_issued = c.execute("SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind='CUSTOMER_INVOICE'", (invoice_id,)).fetchone()
            customer_issued_hash = self.digest(customer_issued["file_path"])
            issued_poison = subprocess.run(
                ["pdftotext", str(resolve_manifest_path(customer_issued["file_path"])), "-"],
                check=True, capture_output=True, text=True,
            ).stdout.upper()
            for forbidden in ("SUPPLIER COST", "ACTUAL COST", "MARKUP", "PROFIT"):
                self.assertNotIn(forbidden, issued_poison)

        record_invoice_payment(invoice_id, 450, "BANK TRANSFER", "E2E-PAYMENT")
        with closing(self.connection()) as c:
            invoice = c.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            self.assertEqual((invoice["status"], invoice["balance_due"]), ("PAID", 0))
            customer_paid = c.execute("SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind='CUSTOMER_INVOICE_PAID'", (invoice_id,)).fetchone()
            customer_paid_hash = self.digest(customer_paid["file_path"])
            internal_before = c.execute("SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind='INTERNAL_INVOICE_PAID' AND is_current=1", (invoice_id,)).fetchone()

        orders = create_orders_from_paid_invoice(invoice_id)
        self.assertEqual({o["supplier_name"] for o in orders}, {"Synthetic Supplier A", "Synthetic Supplier B"})
        self.assertTrue(all(o["status"] == "DRAFT" for o in orders))
        draft = get_order(orders[0]["id"])
        with self.assertRaises(HTTPException):
            record_receipt(draft["id"], ReceiptCreate(items=[ReceiptItem(order_item_id=draft["items"][0]["id"], quantity_received=1)]))
        cat_row = next(o for o in orders if o["supplier_name"] == "Synthetic Supplier A")
        basket_row = next(o for o in orders if o["supplier_name"] == "Synthetic Supplier B")
        cat = place_order(cat_row["id"], actor="e2e.operator", request_id="place-cat")
        self.assertEqual(get_order(basket_row["id"])["status"], "DRAFT")
        basket = place_order(basket_row["id"], actor="e2e.operator", request_id="place-basket")
        self.assertNotEqual(cat["po_number"], basket["po_number"])
        po_rows_before = {}
        with closing(self.connection()) as c:
            for order in (cat, basket):
                manifest = c.execute("SELECT * FROM supplier_order_documents_manifest WHERE supplier_order_id=?", (order["id"],)).fetchone()
                po_rows_before[order["id"]] = (dict(manifest), self.digest(manifest["file_path"]))
        self.assertEqual(place_order(cat["id"], actor="e2e.operator", request_id="place-cat")["id"], cat["id"])

        cat = get_order(cat["id"])
        cat_a = next(i for i in cat["items"] if i["description"] == "Hydraulic cylinder seal kit")
        receipt_one = record_receipt(cat["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=cat_a["id"], quantity_received=1)],
            receiver="E2E Receiver", idempotency_key="receipt-cat-partial"),
            actor="e2e.receiver", request_id="receive-cat-1")
        self.assertEqual(receipt_one["status_after"], "PARTIAL")
        self.assertEqual(get_order(basket["id"])["status"], "ORDERED")
        self.assertTrue(record_receipt(cat["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=cat_a["id"], quantity_received=1)],
            idempotency_key="receipt-cat-partial"))["replayed"])
        cat = get_order(cat["id"])
        remaining = [ReceiptItem(order_item_id=i["id"], quantity_received=i["quantity_ordered"]-i["quantity_received"]) for i in cat["items"] if i["quantity_ordered"] > i["quantity_received"]]
        receipt_two = record_receipt(cat["id"], ReceiptCreate(items=remaining, receiver="E2E Receiver", idempotency_key="receipt-cat-final"))
        self.assertEqual(receipt_two["status_after"], "RECEIVED")

        basket = get_order(basket["id"])
        with self.assertRaises(HTTPException):
            create_delivery(job_id, DeliveryCreate(items=[DeliveryItemCreate(order_item_id=basket["items"][0]["id"], quantity=1)], recipient="Synthetic E2E Customer"))
        first_delivery = create_delivery(job_id, DeliveryCreate(
            items=[DeliveryItemCreate(order_item_id=cat_a["id"], quantity=1)],
            recipient="Synthetic E2E Customer", idempotency_key="delivery-one"),
            actor="e2e.delivery", request_id="delivery-one")
        self.assertTrue(create_delivery(job_id, DeliveryCreate(
            items=[DeliveryItemCreate(order_item_id=cat_a["id"], quantity=1)],
            recipient="Synthetic E2E Customer", idempotency_key="delivery-one"))["replayed"])
        complete_delivery(first_delivery["id"], actor="e2e.delivery", request_id="complete-one")
        with self.assertRaises(HTTPException):
            create_delivery(job_id, DeliveryCreate(items=[DeliveryItemCreate(order_item_id=cat_a["id"], quantity=99)]))
        basket_receipt = record_receipt(basket["id"], ReceiptCreate(
            items=[ReceiptItem(order_item_id=i["id"], quantity_received=i["quantity_ordered"]) for i in basket["items"]],
            receiver="E2E Receiver", idempotency_key="receipt-basket"))
        remaining_items = [
            DeliveryItemCreate(order_item_id=item["id"], quantity=item["available_to_deliver"])
            for item in get_delivery_workspace(job_id)["items"]
            if item["available_to_deliver"]
        ]
        final_delivery = create_delivery(job_id, DeliveryCreate(items=remaining_items, recipient="Synthetic E2E Customer", idempotency_key="delivery-final"))
        complete_delivery(final_delivery["id"], actor="e2e.delivery", request_id="complete-final")

        def adjust(order_id, item_id, amount, request_id):
            return record_actual_cost_adjustment(order_id, cost_kind="ITEM", supplier_order_item_id=item_id,
                new_amount=amount, reason="E2E supplier invoice", actor="e2e.accounting",
                request_id=request_id, supplier_reference="E2E-REF")
        cat = get_order(cat["id"])
        cat_amounts = {"Hydraulic cylinder seal kit": 35, "Headlight sets": 95}
        for item in cat["items"]:
            adjust(cat["id"], item["id"], cat_amounts[item["description"]], f"actual-cat-{item['id']}")
        record_actual_cost_adjustment(cat["id"], cost_kind="SHIPPING", new_amount=0, reason="No freight", actor="e2e.accounting", request_id="actual-cat-shipping")
        partial = next(r for r in accounting_snapshot()["invoice_reconciliation"] if r["invoice_id"] == invoice_id)
        self.assertEqual(partial["actual_cost_state"], "PARTIALLY_CONFIRMED")
        report["partial_confirmed_cost"] = partial["actual_supplier_cost"]
        report["partial_calculated_profit"] = partial["actual_profit"]
        basket = get_order(basket["id"])
        basket_amounts = {"Wheel seal": 65, "Brake drums": 45}
        for item in basket["items"]:
            adjust(basket["id"], item["id"], basket_amounts[item["description"]], f"actual-basket-{item['id']}")
        record_actual_cost_adjustment(basket["id"], cost_kind="SHIPPING", new_amount=0, reason="No freight", actor="e2e.accounting", request_id="actual-basket-shipping")
        before_replay_versions = None
        with closing(self.connection()) as c:
            before_replay_versions = c.execute("SELECT COUNT(*) FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind='INTERNAL_INVOICE_PAID'", (invoice_id,)).fetchone()[0]
        item = basket["items"][0]
        adjust(basket["id"], item["id"], basket_amounts[item["description"]], f"actual-basket-{item['id']}")
        final = next(r for r in accounting_snapshot()["invoice_reconciliation"] if r["invoice_id"] == invoice_id)
        self.assertEqual((final["booked_supplier_cost"], final["placed_supplier_cost"], final["actual_supplier_cost"]), (270, 270, 275))
        self.assertEqual((final["expected_profit"], final["placed_cost_profit"], final["actual_profit"]), (180, 180, 175))
        self.assertEqual((final["cost_variance"], final["profit_variance"], final["actual_cost_state"]), (5, -5, "CONFIRMED"))

        with closing(self.connection()) as c:
            internal_rows = c.execute("SELECT * FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind='INTERNAL_INVOICE_PAID' ORDER BY version", (invoice_id,)).fetchall()
            self.assertGreater(len(internal_rows), 1)
            self.assertEqual(sum(r["is_current"] for r in internal_rows), 1)
            self.assertEqual(internal_rows[-1]["is_current"], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoice_documents_manifest WHERE invoice_id=? AND document_kind='INTERNAL_INVOICE_PAID'", (invoice_id,)).fetchone()[0], before_replay_versions)
            current_internal = verified_invoice_document(c, invoice_id, "INTERNAL_INVOICE_PAID", "INTERNAL")
            internal_text = subprocess.run(["pdftotext", str(current_internal), "-"], check=True, capture_output=True, text=True).stdout
            for label in ("Estimated Cost", "Supplier Order Cost", "Final Actual Cost", "Expected Profit", "Supplier Order Profit", "Final Profit", "Profit Difference"):
                self.assertIn(label, internal_text)
            self.assertIn("$275.00", internal_text)
            self.assertIn("$175.00", internal_text)
            self.assertEqual(self.digest(customer_issued["file_path"]), customer_issued_hash)
            self.assertEqual(self.digest(customer_paid["file_path"]), customer_paid_hash)
            for order_id, (manifest, digest) in po_rows_before.items():
                current = c.execute("SELECT * FROM supplier_order_documents_manifest WHERE supplier_order_id=?", (order_id,)).fetchone()
                self.assertEqual(dict(current), manifest)
                self.assertEqual(self.digest(current["file_path"]), digest)
            docs = query_authoritative_documents(c, self.document_root, q=c.execute("SELECT job_number FROM jobs WHERE id=?", (job_id,)).fetchone()[0], version_scope="current")
            self.assertEqual({i["document_type"] for i in docs["items"]}, {"QUOTE", "CUSTOMER INVOICE", "INTERNAL INVOICE", "SUPPLIER PO", "RECEIVING SUMMARY", "DELIVERY NOTE"})
            self.assertTrue(all(i["integrity"] == "VALID" for i in docs["items"]))
            self.assertFalse(any("UNMANIFESTED" in i["file_path"] for i in docs["items"]))
            historical = query_authoritative_documents(c, self.document_root, q=c.execute("SELECT invoice_number FROM invoices WHERE id=?", (invoice_id,)).fetchone()[0], version_scope="historical")
            self.assertTrue(any(i["document_type"] == "INTERNAL INVOICE" for i in historical["items"]))
            bad_path = self.document_root / "missing-invalid.pdf"
            bad_id = c.execute("INSERT INTO delivery_documents_manifest(delivery_id,document_kind,audience,version,file_path,sha256,is_current) VALUES (?,'DELIVERY_NOTE','CUSTOMER',99,?,'bad',0)", (final_delivery["id"], str(bad_path))).lastrowid
            c.commit()
            with self.assertRaises(HTTPException):
                resolve_manifest_document(c, self.document_root, "delivery-note", bad_id)
            c.execute("DELETE FROM delivery_documents_manifest WHERE id=?", (bad_id,));c.commit()
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            report.update({
                "db_path": str(self.db_path), "document_root": str(self.document_root),
                "customer_id": customer_id, "job_id": job_id, "quote_id": quote_id,
                "invoice_id": invoice_id, "invoice_number": invoice["invoice_number"],
                "job_number": c.execute("SELECT job_number FROM jobs WHERE id=?", (job_id,)).fetchone()[0],
                "order_ids": [cat["id"], basket["id"]], "delivery_ids": [first_delivery["id"], final_delivery["id"]],
                "accounting": {k: final[k] for k in ("customer_total","booked_supplier_cost","placed_supplier_cost","actual_supplier_cost","expected_profit","placed_cost_profit","actual_profit","cost_variance","profit_variance","actual_cost_state")},
                "internal_versions": [(r["version"], r["is_current"]) for r in internal_rows],
                "documents_current": len(docs["items"]), "documents_historical": len(historical["items"]),
                "audit_actions": [r[0] for r in c.execute("SELECT DISTINCT action FROM audit_logs WHERE entity_id IN (?,?,?,?) ORDER BY action", (invoice_id, cat["id"], basket["id"], final_delivery["id"]))],
            })
        state_path = Path(self.temp.name) / "acceptance-state.json"
        state_path.write_text(json.dumps(report, indent=2))
        export_root = os.getenv("PPS_ACCEPTANCE_EXPORT", "").strip()
        if export_root:
            destination = Path(export_root)
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.db_path, destination / "acceptance.db")
            shutil.copytree(self.document_root, destination / "documents", dirs_exist_ok=True)
            (destination / "acceptance-state.json").write_text(json.dumps(report, indent=2))
        print("ACCEPTANCE_STATE=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
