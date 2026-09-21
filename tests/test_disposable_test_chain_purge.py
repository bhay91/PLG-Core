from contextlib import closing
import unittest

from fastapi import HTTPException

from plg_core.disposable import service
from plg_core.disposable import test_chain
from tests.test_disposable_record_deletion import DisposableRecordDeletionTests


class DisposableTestChainPurgeTests(DisposableRecordDeletionTests):
    def seed_completed_test_chain(self):
        chain = self.seed_chain(shared=True)
        with closing(self.connection()) as c:
            c.execute("UPDATE jobs SET status='DELIVERED',customer='TEST CUSTOMER',company='TEST COMPANY' WHERE id=?", (chain["job"],))
            quote = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,parts_subtotal,shipping_total,customer_total,supplier_total,profit_total) VALUES (?, ?, DATE('now'),'CONVERTED',100,0,130,100,30)", (f"Q-{chain['job_number']}", chain["job"])).lastrowid
            quote_item = c.execute("INSERT INTO quote_items(quote_id,part_id,source_id,description,supplier_name,source_type,supplier_unit_cost,customer_unit_price,supplier_line_total,customer_line_total,line_profit,job_asset_id,primary_requested_need_id) VALUES (?,?,?,'TEST STARTER','TEST SUPPLIER','SUPPLIER',100,130,100,130,30,?,?)", (quote, 1, 1, chain["asset"], chain["need"])).lastrowid
            invoice = c.execute("INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,parts_subtotal,shipping_total,customer_total,supplier_total,profit_total,balance_due) VALUES (?,?,?,DATE('now'),'PAID',100,0,130,100,30,0)", (f"I-{chain['job_number']}", quote, chain["job"])).lastrowid
            invoice_item = c.execute("INSERT INTO invoice_items(invoice_id,quote_item_id,quantity,description,supplier_unit_cost,customer_unit_price,supplier_line_total,customer_line_total,line_profit,job_asset_id,primary_requested_need_id) VALUES (?,?,1,'TEST STARTER',100,130,100,130,30,?,?)", (invoice, quote_item, chain["asset"], chain["need"])).lastrowid
            supplier = c.execute("INSERT INTO suppliers(name) VALUES (?)", (f"TEST SUPPLIER {chain['job']}",)).lastrowid
            order = c.execute("INSERT INTO supplier_orders(po_number,job_id,invoice_id,supplier_id,supplier_name,status,parts_total,order_total) VALUES (?,?,?,?,?,'RECEIVED',100,100)", (f"PO-{chain['job_number']}", chain["job"], invoice, supplier, "TEST SUPPLIER")).lastrowid
            order_item = c.execute("INSERT INTO supplier_order_items(order_id,invoice_item_id,description,quantity_ordered,quantity_received,unit_cost,line_cost,job_asset_id) VALUES (?,?,'TEST STARTER',1,1,100,100,?)", (order, invoice_item, chain["asset"])).lastrowid
            receipt = c.execute("INSERT INTO receiving_events(receipt_number,order_id) VALUES (?,?)", (f"REC-{chain['job_number']}", order)).lastrowid
            c.execute("INSERT INTO receiving_event_items(receipt_id,order_item_id,quantity_received) VALUES (?,?,1)", (receipt, order_item))
            delivery = c.execute("INSERT INTO deliveries(job_id,invoice_id,status,recipient) VALUES (?,?,'DELIVERED','TEST CUSTOMER')", (chain["job"], invoice)).lastrowid
            c.execute("INSERT INTO delivery_items(delivery_id,order_item_id,quantity_delivered) VALUES (?,?,1)", (delivery, order_item))
            c.execute("INSERT INTO customer_transactions(customer_id,transaction_date,transaction_type,amount,job_id,quote_id,invoice_id) VALUES (?,DATE('now'),'PAYMENT',130,?,?,?)", (chain["customer"], chain["job"], quote, invoice))
            c.commit()
        chain.update(quote=quote, invoice=invoice, supplier=supplier, order=order, delivery=delivery,
                     invoice_number=f"I-{chain['job_number']}")
        return chain

    def purge(self, chain, **overrides):
        plan = test_chain.build_test_chain_purge_plan(chain["job_number"], chain["invoice_number"])
        values = dict(job_number=chain["job_number"], invoice_number=chain["invoice_number"],
                      reason="synthetic completed workflow", confirmation_job=chain["job_number"],
                      confirmation_invoice=chain["invoice_number"],
                      confirmation_phrase=test_chain.PURGE_PHRASE, expected_token=plan["token"])
        values.update(overrides)
        return test_chain.purge_test_chain(**values)

    def test_completed_normal_job_remains_blocked_but_explicit_test_chain_purges_atomically(self):
        chain = self.seed_completed_test_chain()
        self.assertTrue(service.build_request_deletion_plan(chain["request"])["blockers"])
        self.purge(chain)
        with closing(self.connection()) as c:
            for table, key, value in (("jobs", "id", chain["job"]), ("customer_requests", "id", chain["request"]),
                                      ("quotes", "id", chain["quote"]), ("invoices", "id", chain["invoice"]),
                                      ("supplier_orders", "id", chain["order"]), ("deliveries", "id", chain["delivery"])):
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table} WHERE {key}=?", (value,)).fetchone()[0], 0)
            self.assertIsNotNone(c.execute("SELECT 1 FROM customers WHERE id=?", (chain["customer"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM machines WHERE id=?", (chain["machine"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM suppliers WHERE id=?", (chain["supplier"],)).fetchone())
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIsNotNone(c.execute("SELECT 1 FROM deletion_tombstones WHERE entity_type='DISPOSABLE_TEST_CHAIN' AND entity_number=?", (chain["job_number"],)).fetchone())

    def test_exact_confirmations_and_pairing_are_required(self):
        chain = self.seed_completed_test_chain()
        with self.assertRaises(HTTPException):
            self.purge(chain, confirmation_invoice="WRONG")
        with self.assertRaises(HTTPException):
            test_chain.build_test_chain_purge_plan(chain["job_number"], "WRONG-INVOICE")
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id=?", (chain["job"],)).fetchone())

    def test_failure_rolls_back_the_entire_chain(self):
        chain = self.seed_completed_test_chain()
        with closing(self.connection()) as c:
            c.execute("CREATE TRIGGER fail_test_purge BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT,'forced'); END")
            c.commit()
        with self.assertRaises(Exception):
            self.purge(chain)
        with closing(self.connection()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM invoices WHERE id=?", (chain["invoice"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM supplier_orders WHERE id=?", (chain["order"],)).fetchone())
            self.assertEqual(c.execute("SELECT COUNT(*) FROM deletion_tombstones WHERE entity_type='DISPOSABLE_TEST_CHAIN' AND entity_number=?", (chain["job_number"],)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
