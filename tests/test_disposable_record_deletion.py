from __future__ import annotations

import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.disposable import service
from plg_core.disposable.routes import review_request_delete, router


ROOT = Path(__file__).resolve().parents[1]


class DisposableRecordDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-disposable-")
        self.root = Path(self.temp.name)
        self.db = self.root / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db)
        self.upload_patch = patch.object(service, "UPLOAD_ROOT", (self.root / "uploads").resolve())
        self.db_patch.start(); self.upload_patch.start()
        self.sequence = 0

    def tearDown(self):
        self.upload_patch.stop(); self.db_patch.stop(); self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def seed_chain(self, *, shared=False, attachment=False):
        self.sequence += 1
        tag = f"DISP-{self.sequence}"
        with closing(self.connection()) as c:
            customer_id = c.execute("INSERT INTO customers(customer_number,name,active) VALUES (?,?,1)", (f"C-{tag}", tag)).lastrowid
            machine_id = c.execute(
                "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial) VALUES (?,?,?,?,?,?)",
                (customer_id, f"M-{tag}", "Test machine", "CAT", "420D", tag),
            ).lastrowid
            job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,machine_id,customer,status) VALUES (?,DATE('now'),?,?,?,'REQUESTED')",
                (f"J-{tag}", customer_id, machine_id, tag),
            ).lastrowid
            request_id = c.execute(
                "INSERT INTO customer_requests(request_number,request_text,individual_name,status,customer_id,machine_id,job_id) VALUES (?,?,?,'COMPLETED',?,?,?)",
                (f"R-{tag}", "Disposable request", tag, customer_id, machine_id, job_id),
            ).lastrowid
            asset_id = c.execute(
                "INSERT INTO job_assets(job_id,machine_id,customer_id,asset_type,name,manufacturer,model,is_primary) VALUES (?,?,?,'machine','CAT 420D','CAT','420D',1)",
                (job_id, machine_id, customer_id),
            ).lastrowid
            need_id = c.execute(
                "INSERT INTO requested_needs(job_id,job_asset_id,customer_request_id,wording) VALUES (?,?,?,'Starter')",
                (job_id, asset_id, request_id),
            ).lastrowid
            basket_id = c.execute("INSERT INTO baskets(job_id,status) VALUES (?,'DRAFT')", (job_id,)).lastrowid
            item_id = c.execute(
                "INSERT INTO basket_items(basket_id,requested_description,supplier_part_number,job_asset_id,primary_requested_need_id,research_state) VALUES (?,'Starter','SKU-1',?,?,'RESEARCH_RESULT')",
                (basket_id, asset_id, need_id),
            ).lastrowid
            c.execute("INSERT INTO basket_item_need_links(basket_item_id,requested_need_id) VALUES (?,?)", (item_id, need_id))
            c.execute("INSERT INTO part_shipping_data(basket_item_id,unit_weight,weight_unit) VALUES (?,2,'lb')", (item_id,))
            revision_id = c.execute("INSERT INTO work_revisions(job_id,revision_number,state,reason) VALUES (?,1,'EDITABLE','test')", (job_id,)).lastrowid
            c.execute("UPDATE jobs SET active_work_revision_id=? WHERE id=?", (revision_id, job_id))
            c.execute("INSERT INTO job_timeline(job_id,event_type,icon,message) VALUES (?,'TEST','','test')", (job_id,))
            if attachment:
                path = service.UPLOAD_ROOT / str(request_id) / "owned.png"
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"owned")
                c.execute("INSERT INTO customer_request_attachments(request_id,original_filename,stored_filename,file_path,media_type) VALUES (?,?,?,?,?)",
                          (request_id, "owned.png", "owned.png", str(path), "image/png"))
            else:
                path = None
            sibling = None
            if shared:
                sibling = c.execute(
                    "INSERT INTO jobs(job_number,created_date,customer_id,machine_id,customer,status) VALUES (?,DATE('now'),?,?,?,'REQUESTED')",
                    (f"J-{tag}-S", customer_id, machine_id, tag),
                ).lastrowid
            c.commit()
        return {"customer": customer_id, "machine": machine_id, "job": job_id, "request": request_id,
                "asset": asset_id, "need": need_id, "basket": basket_id, "item": item_id,
                "revision": revision_id, "sibling": sibling, "path": path, "job_number": f"J-{tag}"}

    def delete(self, chain, **overrides):
        plan = service.build_request_deletion_plan(chain["request"])
        args = dict(reason="confirmed disposable fixture", confirmation_number=plan["job_number"],
                    confirmation_phrase=service.DELETE_PHRASE, expected_token=plan["token"])
        args.update(overrides)
        return service.delete_disposable_request(chain["request"], **args)

    def assert_blocked(self, chain):
        plan = service.build_request_deletion_plan(chain["request"])
        self.assertTrue(plan["blockers"])
        with self.assertRaises(HTTPException) as caught:
            service.delete_disposable_request(
                chain["request"], reason="test", confirmation_number=plan["job_number"],
                confirmation_phrase=service.DELETE_PHRASE, expected_token=plan["token"])
        self.assertEqual(caught.exception.status_code, 409)
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id=?", (chain["job"],)).fetchone())

    def test_eligible_research_chain_deletes_dependents_and_keeps_tombstone(self):
        chain = self.seed_chain(attachment=True)
        self.delete(chain)
        with closing(self.connection()) as c:
            for table, column, value in [
                ("customer_requests", "id", chain["request"]), ("jobs", "id", chain["job"]),
                ("job_assets", "id", chain["asset"]), ("requested_needs", "id", chain["need"]),
                ("basket_items", "id", chain["item"]), ("baskets", "id", chain["basket"]),
                ("work_revisions", "id", chain["revision"]),
            ]:
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (value,)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM deletion_tombstones WHERE entity_number=?", (chain["job_number"],)).fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_logs WHERE action='DISPOSABLE_RECORD_DELETED' AND entity_id=?", (chain["job_number"],)).fetchone()[0], 1)
            self.assertEqual(len(c.execute("PRAGMA foreign_key_check").fetchall()), 0)
        self.assertFalse(chain["path"].exists())

    def test_shared_customer_machine_and_sibling_are_preserved(self):
        chain = self.seed_chain(shared=True)
        self.delete(chain)
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id=?", (chain["sibling"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM customers WHERE id=?", (chain["customer"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM machines WHERE id=?", (chain["machine"],)).fetchone())

    def test_exclusively_owned_customer_and_machine_are_removed(self):
        chain = self.seed_chain()
        self.delete(chain)
        with closing(self.connection()) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM machines WHERE id=?", (chain["machine"],)).fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM customers WHERE id=?", (chain["customer"],)).fetchone())

    def test_standalone_draft_or_cancelled_proposal_deletes_but_confirmed_blocks(self):
        with closing(self.connection()) as c:
            draft = c.execute("INSERT INTO intake_proposals(raw_input,status,contact_name) VALUES ('draft','DRAFT','Test')").lastrowid
            confirmed = c.execute("INSERT INTO intake_proposals(raw_input,status,contact_name) VALUES ('done','CONFIRMED','Test')").lastrowid
            c.commit()
        plan = service.build_proposal_deletion_plan(draft)
        service.delete_disposable_proposal(draft, reason="fixture", confirmation_number=plan["request_number"],
                                           confirmation_phrase=service.DELETE_PHRASE, expected_token=plan["token"])
        with closing(self.connection()) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM intake_proposals WHERE id=?", (draft,)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM deletion_tombstones WHERE former_entity_id=?", (draft,)).fetchone())
        self.assertTrue(service.build_proposal_deletion_plan(confirmed)["blockers"])

    def test_quote_and_issued_document_block(self):
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            quote = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,parts_subtotal,shipping_total,customer_total,supplier_total,profit_total) VALUES (?,?,DATE('now'),'DRAFT',0,0,0,0,0)",
                              ("Q-BLOCK", chain["job"])).lastrowid
            c.execute("INSERT INTO quote_documents_manifest(quote_id,audience,document_kind,file_path,sha256,is_issued) VALUES (?,'CUSTOMER','QUOTE','documents/test.pdf','x',1)", (quote,))
            c.commit()
        self.assert_blocked(chain)

    def test_invoice_including_voided_blocks(self):
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            q = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,parts_subtotal,shipping_total,customer_total,supplier_total,profit_total) VALUES (?,?,DATE('now'),'CONVERTED',0,0,0,0,0)", ("Q-INV", chain["job"])).lastrowid
            c.execute("INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,parts_subtotal,shipping_total,customer_total,supplier_total,profit_total) VALUES (?,?,?,DATE('now'),'VOID',0,0,0,0,0)", ("I-BLOCK", q, chain["job"]))
            c.commit()
        self.assert_blocked(chain)

    def test_payment_refund_or_adjustment_blocks(self):
        for kind in ("PAYMENT", "REFUND", "ADJUSTMENT"):
            chain = self.seed_chain()
            with closing(self.connection()) as c:
                c.execute("INSERT INTO customer_transactions(customer_id,transaction_date,transaction_type,amount,job_id) VALUES (?,DATE('now'),?,1,?)", (chain["customer"], kind, chain["job"]))
                c.commit()
            self.assert_blocked(chain)

    def test_supplier_order_receiving_and_delivery_each_block(self):
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            c.execute("INSERT INTO supplier_orders(po_number,job_id,supplier_name,status) VALUES ('PO-BLOCK',?,'Supplier','DRAFT')", (chain["job"],))
            c.commit()
        self.assert_blocked(chain)
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            c.execute("INSERT INTO deliveries(job_id,status) VALUES (?,'READY')", (chain["job"],)); c.commit()
        self.assert_blocked(chain)

    def test_committed_revision_and_machine_history_block(self):
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            c.execute("UPDATE work_revisions SET state='COMMITTED' WHERE id=?", (chain["revision"],)); c.commit()
        self.assert_blocked(chain)
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            c.execute("INSERT INTO machine_parts_history(machine_id,part_description,original_job_number) VALUES (?,'Starter',?)", (chain["machine"], chain["job_number"])); c.commit()
        self.assert_blocked(chain)

    def test_external_or_unknown_dependency_blocks_and_rolls_back(self):
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            c.execute("INSERT INTO opportunities(opportunity_number,customer_id,title,converted_job_id) VALUES ('O-BLOCK',?,'External',?)", (chain["customer"], chain["job"])); c.commit()
        self.assert_blocked(chain)
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            c.execute("CREATE TABLE future_dependency(id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id))")
            c.execute("INSERT INTO future_dependency(job_id) VALUES (?)", (chain["job"],)); c.commit()
        self.assert_blocked(chain)

    def test_confirmation_fields_are_required_and_exact(self):
        chain = self.seed_chain(); plan = service.build_request_deletion_plan(chain["request"])
        cases = [
            dict(reason="", confirmation_number=plan["job_number"], confirmation_phrase=service.DELETE_PHRASE),
            dict(reason="x", confirmation_number="WRONG", confirmation_phrase=service.DELETE_PHRASE),
            dict(reason="x", confirmation_number=plan["job_number"], confirmation_phrase="delete"),
        ]
        for values in cases:
            with self.assertRaises(HTTPException) as caught:
                service.delete_disposable_request(chain["request"], expected_token=plan["token"], **values)
            self.assertEqual(caught.exception.status_code, 400)

    def test_stale_request_proposal_and_revision_versions_are_rejected(self):
        chain = self.seed_chain(); plan = service.build_request_deletion_plan(chain["request"])
        with closing(self.connection()) as c:
            c.execute("UPDATE customer_requests SET updated_at='2099-01-01' WHERE id=?", (chain["request"],)); c.commit()
        with self.assertRaises(HTTPException) as caught:
            self.delete(chain, expected_token=plan["token"])
        self.assertEqual(caught.exception.status_code, 409)
        chain = self.seed_chain(); plan = service.build_request_deletion_plan(chain["request"])
        with closing(self.connection()) as c:
            c.execute("UPDATE work_revisions SET lock_version=lock_version+1 WHERE id=?", (chain["revision"],)); c.commit()
        with self.assertRaises(HTTPException): self.delete(chain, expected_token=plan["token"])
        chain = self.seed_chain()
        with closing(self.connection()) as c:
            proposal = c.execute(
                "INSERT INTO intake_proposals(raw_input,status,contact_name,created_request_id,created_job_id) VALUES ('confirmed','CONFIRMED','Test',?,?)",
                (chain["request"], chain["job"]),
            ).lastrowid
            c.commit()
        plan = service.build_request_deletion_plan(chain["request"])
        with closing(self.connection()) as c:
            c.execute("UPDATE intake_proposals SET lock_version=lock_version+1 WHERE id=?", (proposal,)); c.commit()
        with self.assertRaises(HTTPException): self.delete(chain, expected_token=plan["token"])

    def test_blocker_created_between_preview_and_post_is_detected(self):
        chain = self.seed_chain(); plan = service.build_request_deletion_plan(chain["request"])
        with closing(self.connection()) as c:
            c.execute("INSERT INTO supplier_orders(po_number,job_id,supplier_name) VALUES ('PO-LATE',?,'Late')", (chain["job"],)); c.commit()
        with self.assertRaises(HTTPException) as caught:
            self.delete(chain, expected_token=plan["token"])
        self.assertEqual(caught.exception.status_code, 409)

    def test_get_is_read_only_and_mutation_routes_are_post_only(self):
        chain = self.seed_chain()
        scope = {"type":"http","method":"GET","path":"/","headers":[],"query_string":b"","app":legacy_app.app,
                 "router":legacy_app.app.router,"scheme":"http","server":("test",80),"client":("test",1)}
        with closing(self.connection()) as c:
            before = c.total_changes
        response = review_request_delete(Request(scope), chain["request"])
        self.assertEqual(response.status_code, 200)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM deletion_tombstones WHERE former_entity_id=?", (chain["job"],)).fetchone()[0], 0)
        methods = {(route.path, method) for route in router.routes for method in route.methods}
        self.assertIn(("/requests/{request_id}/delete-disposable", "GET"), methods)
        self.assertIn(("/requests/{request_id}/delete-disposable", "POST"), methods)
        self.assertNotIn(("/requests/{request_id}/delete-disposable", "DELETE"), methods)

    def test_sql_failure_rolls_back_tombstone_and_operational_deletion(self):
        chain = self.seed_chain(); plan = service.build_request_deletion_plan(chain["request"])
        with closing(self.connection()) as c:
            c.execute("CREATE TRIGGER fail_disposable BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT,'forced failure'); END"); c.commit()
        with self.assertRaises(Exception):
            self.delete(chain, expected_token=plan["token"])
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id=?", (chain["job"],)).fetchone())
            self.assertEqual(c.execute("SELECT COUNT(*) FROM deletion_tombstones WHERE entity_number=?", (chain["job_number"],)).fetchone()[0], 0)

    def test_attachment_outside_approved_root_blocks_without_file_or_db_change(self):
        chain = self.seed_chain()
        outside = self.root / "outside.png"; outside.write_bytes(b"keep")
        with closing(self.connection()) as c:
            c.execute("INSERT INTO customer_request_attachments(request_id,original_filename,stored_filename,file_path) VALUES (?,'outside.png','outside.png',?)", (chain["request"], str(outside))); c.commit()
        with self.assertRaises(HTTPException) as caught: self.delete(chain)
        self.assertEqual(caught.exception.status_code, 409); self.assertTrue(outside.exists())
        with closing(self.connection()) as c: self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id=?", (chain["job"],)).fetchone())


if __name__ == "__main__":
    unittest.main()
