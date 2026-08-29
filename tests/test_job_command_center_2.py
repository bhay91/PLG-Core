from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import legacy_app
from plg_core.jobs.service import get_job_operational_snapshot
from plg_core.database.migrations import run_migrations


ROOT = Path(__file__).resolve().parents[1]


class JobCommandCenter2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-job-command-2-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        self.document_root = root / "documents"
        self.document_root.mkdir()
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.docs_patch = patch.object(legacy_app, "DOCUMENTS_DIR", self.document_root)
        self.db_patch.start(); self.docs_patch.start()
        run_migrations()
        self._fixture()

    def tearDown(self):
        self.docs_patch.stop(); self.db_patch.stop(); self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def _pdf(self, name):
        path = self.document_root / name
        data = b"%PDF-1.4\nJob Command Center fixture\n%%EOF"
        path.write_bytes(data)
        return str(path), hashlib.sha256(data).hexdigest()

    def _fixture(self):
        with closing(self.connection()) as c:
            customer = c.execute("INSERT INTO customers(customer_number,name,active) VALUES ('JCC2-C','Synthetic Command Customer',1)").lastrowid
            self.job_id = c.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('JCC2-J','2026-08-16',?,'Synthetic Command Customer','ORDERED')", (customer,)).lastrowid
            asset_a = c.execute("INSERT INTO job_assets(job_id,customer_id,name,manufacturer,model,vin_pin_serial,is_primary) VALUES (?,?,'Backhoe','Caterpillar','420D','FIX-CAT',1)", (self.job_id,customer)).lastrowid
            asset_b = c.execute("INSERT INTO job_assets(job_id,customer_id,name,manufacturer,model,vin_pin_serial,is_primary) VALUES (?,?,'Flatbed','International','4700','FIX-INT',0)", (self.job_id,customer)).lastrowid
            c.execute("INSERT INTO requested_needs(job_id,job_asset_id,wording,state) VALUES (?,?,'Seal kit','OPEN')", (self.job_id,asset_a))
            c.execute("INSERT INTO requested_needs(job_id,job_asset_id,wording,state) VALUES (?,?,'Brake drums','SATISFIED')", (self.job_id,asset_b))
            c.execute("INSERT INTO baskets(job_id,status) VALUES (?,'COMMITTED')", (self.job_id,))
            quote = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,supplier_total,profit_total,is_current) VALUES ('JCC2-Q',?,'2026-08-16','CONVERTED',1000,600,400,1)", (self.job_id,)).lastrowid
            self.invoice_id = c.execute("INSERT INTO invoices(invoice_number,quote_id,job_id,invoice_date,status,customer_total,supplier_total,profit_total,balance_due) VALUES ('JCC2-INV',?,?,'2026-08-16','PAID',1000,600,400,0)", (quote,self.job_id)).lastrowid
            order_specs = (("JCC2-PO-A","Synthetic Supplier A",250),("JCC2-PO-B","Synthetic Supplier B",250))
            self.order_ids=[]
            for po,supplier,total in order_specs:
                self.order_ids.append(c.execute("INSERT INTO supplier_orders(po_number,job_id,invoice_id,supplier_name,status,parts_total,shipping_total,order_total,actual_shipping_total) VALUES (?,?,?,?,'ORDERED',?,0,?,0)", (po,self.job_id,self.invoice_id,supplier,total,total)).lastrowid)
            lines = (
                (self.order_ids[0],"Synthetic Part A",1,150,125,asset_b),
                (self.order_ids[0],"Synthetic Part B",1,150,125,asset_b),
                (self.order_ids[1],"Synthetic Part C",1,60,50,asset_a),
                (self.order_ids[1],"Synthetic Part D",1,60,50,asset_a),
                (self.order_ids[1],"Synthetic Part E",1,60,50,asset_a),
                (self.order_ids[1],"Synthetic Part F",1,60,50,asset_a),
                (self.order_ids[1],"Synthetic Part G",1,60,50,asset_a),
            )
            for index,(order,desc,qty,booked,actual,asset) in enumerate(lines,1):
                invoice_item=c.execute("INSERT INTO invoice_items(invoice_id,quantity,description,supplier_name,supplier_line_total,customer_line_total,job_asset_id) VALUES (?,?,?,?,?,0,?)", (self.invoice_id,qty,desc,'Synthetic Supplier A' if order==self.order_ids[0] else 'Synthetic Supplier B',booked,asset)).lastrowid
                c.execute("INSERT INTO supplier_order_items(order_id,invoice_item_id,description,quantity_ordered,quantity_received,unit_cost,line_cost,actual_unit_cost,job_asset_id) VALUES (?,?,?, ?,0,?,?,?,?)", (order,invoice_item,desc,qty,actual,actual,actual,asset))
            c.execute("INSERT INTO customer_transactions(customer_id,invoice_id,transaction_type,amount,transaction_date) VALUES (?,?,'PAYMENT',1000,'2026-08-16')", (customer,self.invoice_id))
            c.execute("INSERT INTO job_timeline(job_id,event_type,icon,message) VALUES (?,'PAYMENT_RECEIVED','💳','Fixture invoice paid')", (self.job_id,))
            c.execute("INSERT INTO job_timeline(job_id,event_type,icon,message) VALUES (?,'SUPPLIER_ORDER_PLACED','🛒','Two fixture orders placed')", (self.job_id,))
            quote_path,quote_hash=self._pdf('quote.pdf'); invoice_path,invoice_hash=self._pdf('invoice.pdf'); internal_path,internal_hash=self._pdf('internal.pdf')
            c.execute("INSERT INTO quote_documents_manifest(quote_id,audience,document_kind,file_path,sha256,is_issued,version,quote_status,is_current) VALUES (?,'CUSTOMER','CUSTOMER_QUOTE',?,?,1,1,'CONVERTED',1)", (quote,quote_path,quote_hash))
            c.execute("INSERT INTO invoice_documents_manifest(invoice_id,document_kind,audience,version,invoice_status,file_path,sha256,is_current) VALUES (?,'CUSTOMER_INVOICE_PAID','CUSTOMER',1,'PAID',?,?,1)", (self.invoice_id,invoice_path,invoice_hash))
            c.execute("INSERT INTO invoice_documents_manifest(invoice_id,document_kind,audience,version,invoice_status,file_path,sha256,is_current) VALUES (?,'INTERNAL_INVOICE_PAID','INTERNAL',1,'PAID',?,?,1)", (self.invoice_id,internal_path,internal_hash))
            c.commit()

    def snapshot(self):
        return get_job_operational_snapshot(self.job_id, document_root=self.document_root)

    def test_snapshot_aggregates_identity_needs_payment_and_authoritative_financials(self):
        result=self.snapshot()
        self.assertEqual(result['customer']['name'],'Synthetic Command Customer')
        self.assertEqual({a['vin_pin_serial'] for a in result['assets']},{'FIX-CAT','FIX-INT'})
        self.assertEqual(result['needs_summary'],{'total':2,'open':1,'covered':1})
        self.assertEqual(result['invoice']['payment_state'],'PAID')
        self.assertEqual(result['invoice']['balance_due'],0)
        f=result['financial']
        self.assertEqual((f['customer_total'],f['booked_supplier_cost'],f['placed_supplier_cost'],f['actual_supplier_cost']),(1000,600,500,500))
        self.assertEqual((f['expected_profit'],f['placed_cost_profit'],f['actual_profit'],f['cost_variance'],f['profit_variance']),(400,500,500,-100,100))

    def test_order_and_movement_aggregation_uses_supplier_items(self):
        result=self.snapshot()
        self.assertEqual([o['status'] for o in result['supplier_orders']],['ORDERED','ORDERED'])
        self.assertEqual(result['movement'],{'ordered_units':7,'received_units':0,'delivered_units':0,'remaining_units':7,'available_to_deliver_units':0})
        self.assertEqual(result['workflow']['stage'],'Waiting for Supplier')
        self.assertEqual(result['workflow']['next_action'],'Receive incoming items')

    def test_mixed_supplier_and_partial_receiving_stages(self):
        with closing(self.connection()) as c:
            c.execute("UPDATE supplier_orders SET status='DRAFT' WHERE id=?",(self.order_ids[0],));c.commit()
        result=self.snapshot(); self.assertEqual(result['workflow']['stage'],'Ordering');self.assertIn('1 remaining',result['workflow']['next_action'])
        with closing(self.connection()) as c:
            c.execute("UPDATE supplier_orders SET status='ORDERED' WHERE id=?",(self.order_ids[0],))
            c.execute("UPDATE supplier_orders SET status='PARTIAL' WHERE id=?",(self.order_ids[1],))
            item=c.execute("SELECT id FROM supplier_order_items WHERE order_id=? ORDER BY id LIMIT 1",(self.order_ids[1],)).fetchone()[0]
            c.execute("UPDATE supplier_order_items SET quantity_received=1 WHERE id=?",(item,));c.commit()
        result=self.snapshot();self.assertEqual(result['workflow']['stage'],'Partial Receiving');self.assertEqual(result['movement']['received_units'],1)

    def test_delivered_and_available_quantities_are_transaction_based(self):
        with closing(self.connection()) as c:
            item=c.execute("SELECT id FROM supplier_order_items WHERE order_id=? LIMIT 1",(self.order_ids[0],)).fetchone()[0]
            c.execute("UPDATE supplier_order_items SET quantity_received=1 WHERE id=?",(item,))
            delivery=c.execute("INSERT INTO deliveries(job_id,invoice_id,status,recipient) VALUES (?,?,'DELIVERED','Fixture')",(self.job_id,self.invoice_id)).lastrowid
            c.execute("INSERT INTO delivery_items(delivery_id,order_item_id,quantity_delivered) VALUES (?,?,1)",(delivery,item));c.commit()
        result=self.snapshot();self.assertEqual(result['movement']['delivered_units'],1);self.assertEqual(result['movement']['available_to_deliver_units'],0)

    def test_current_documents_recent_timeline_and_no_duplicate_state(self):
        tables=('jobs','invoices','supplier_orders','supplier_order_items','job_timeline','invoice_documents_manifest')
        with closing(self.connection()) as c: before={t:c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}
        first=self.snapshot();second=self.snapshot()
        with closing(self.connection()) as c: after={t:c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in tables}
        self.assertEqual(before,after)
        self.assertEqual({d['document_type'] for d in first['documents']},{'QUOTE','CUSTOMER INVOICE','INTERNAL INVOICE'})
        self.assertTrue(all(d['integrity']=='VALID' for d in first['documents']))
        self.assertEqual(first['activity'],second['activity']);self.assertEqual(first['activity'][0]['event_type'],'SUPPLIER_ORDER_PLACED')

    def test_unresolved_exception_prioritizes_review_without_hiding_deliverable_quantity(self):
        with closing(self.connection()) as c:
            item = c.execute("SELECT id,order_id FROM supplier_order_items WHERE order_id=? ORDER BY id LIMIT 1", (self.order_ids[0],)).fetchone()
            receipt = c.execute("INSERT INTO receiving_events(order_id,receipt_number) VALUES (?,'JCC2-R')", (item['order_id'],)).lastrowid
            c.execute("INSERT INTO receiving_event_items(receipt_id,order_item_id,quantity_received) VALUES (?,?,1)", (receipt,item['id']))
            c.execute("UPDATE supplier_order_items SET quantity_received=1 WHERE id=?", (item['id'],))
            c.execute("INSERT INTO receiving_exception_items(receipt_id,order_item_id,disposition,quantity,reason) VALUES (?,?,'QUARANTINED',1,'Inspection pending')", (receipt,item['id']))
            c.commit()
        result = self.snapshot()
        self.assertEqual(result['workflow']['stage'], 'Receiving Exception')
        self.assertEqual(result['workflow']['next_action'], 'Review quarantined units')
        self.assertEqual(result['movement']['available_to_deliver_units'], 1)
        self.assertEqual(result['receiving_exceptions'][0]['unresolved_quantity'], 1)

    def _add_receiving_overlay(self, disposition="QUARANTINED"):
        with closing(self.connection()) as c:
            item = c.execute("SELECT id,order_id FROM supplier_order_items WHERE order_id=? ORDER BY id LIMIT 1", (self.order_ids[0],)).fetchone()
            receipt = c.execute("INSERT INTO receiving_events(order_id,receipt_number) VALUES (?,?)", (item['order_id'], f"JCC2-{disposition}")).lastrowid
            return_item = int(item['id'])
            if disposition:
                c.execute("INSERT INTO receiving_exception_items(receipt_id,order_item_id,disposition,quantity,reason) VALUES (?,?,?,?,?)", (receipt,item['id'],disposition,1,"Operator follow-up"))
            c.commit()
        return return_item

    def test_exception_overlay_does_not_outrank_unplaced_supplier_order(self):
        self._add_receiving_overlay("QUARANTINED")
        with closing(self.connection()) as c:
            c.execute("UPDATE supplier_orders SET status='DRAFT' WHERE id=?", (self.order_ids[1],)); c.commit()
        result = self.snapshot()
        self.assertEqual(result['workflow']['stage'], 'Ordering')
        self.assertIn('Place 1 remaining supplier order', result['workflow']['next_action'])
        self.assertEqual(len(result['receiving_exceptions']), 1)

    def test_backorder_overlay_does_not_outrank_unplaced_supplier_order(self):
        item_id = self._add_receiving_overlay("")
        with closing(self.connection()) as c:
            c.execute("INSERT INTO supplier_order_item_backorder_events(order_item_id,event_kind,backordered_quantity,reason,idempotency_key) VALUES (?,'DECLARED',1,'Supplier delay','jcc-priority-backorder')", (item_id,))
            c.execute("UPDATE supplier_orders SET status='DRAFT' WHERE id=?", (self.order_ids[1],)); c.commit()
        result = self.snapshot()
        self.assertEqual(result['workflow']['stage'], 'Ordering')
        self.assertEqual(len(result['active_backorders']), 1)

    def test_exception_overlay_does_not_outrank_payment(self):
        self._add_receiving_overlay("DAMAGED")
        with closing(self.connection()) as c:
            c.execute("UPDATE invoices SET status='UNPAID',balance_due=100 WHERE id=?", (self.invoice_id,)); c.commit()
        result = self.snapshot()
        self.assertEqual(result['workflow']['stage'], 'Waiting for Payment')
        self.assertEqual(result['workflow']['next_action'], 'Collect payment')
        self.assertEqual(len(result['receiving_exceptions']), 1)

    def test_template_has_operational_panels_draft_warning_and_responsive_contract(self):
        source=(ROOT/'templates/job_command_center.html').read_text()
        for value in ('Job Operational Summary','NEXT ACTION','Financial State','Supplier Orders','Supplier-order movement totals','Documents','Recent Activity','UNQUOTED / DRAFT WORK','These are current editable basket values and are not the authoritative issued-invoice/accounting totals.'):
            self.assertIn(value,source)
        self.assertIn('{% set operator_stage = operational_snapshot.workflow.stage %}',source)
        self.assertNotIn('operator_stage = "Waiting for Parts"',source)
        self.assertIn('@media(max-width:620px)',source)
        self.assertIn('.job-ops-grid{grid-template-columns:1fr}',source)

    def test_top_summary_uses_only_authoritative_operational_workflow(self):
        source = (ROOT / 'templates' / 'job_command_center.html').read_text()
        start = source.index('<section class="cc-card job-bar job-command-header"')
        job_bar = source[start:source.index('</section>', start)]
        self.assertIn('{{ operator_stage }}', job_bar)
        self.assertIn('{{ operational_snapshot.workflow.next_action }}', job_bar)
        self.assertIn('href="{{ operational_snapshot.workflow.next_url }}"', job_bar)
        self.assertNotIn('{{ intelligence.next_action }}', job_bar)
        self.assertNotIn('Open Invoice', job_bar)

        self._add_receiving_overlay('QUARANTINED')
        receiving = self.snapshot()['workflow']
        self.assertEqual(receiving, {
            'stage': 'Receiving Exception',
            'next_action': 'Review quarantined units',
            'next_url': f"/purchasing/orders/{self.order_ids[0]}#receiving-exceptions",
            'action_method': 'GET',
        })
        with closing(self.connection()) as c:
            c.execute("UPDATE supplier_orders SET status='DRAFT' WHERE id=?", (self.order_ids[1],))
            c.commit()
        ordering = self.snapshot()['workflow']
        self.assertEqual(ordering['stage'], 'Ordering')
        self.assertIn('Place 1 remaining supplier order', ordering['next_action'])
        self.assertEqual(ordering['next_url'], '/purchasing')

    def test_ready_to_order_exposes_existing_post_transition(self):
        with closing(self.connection()) as c:
            c.execute("DELETE FROM supplier_order_items WHERE order_id IN (?,?)", tuple(self.order_ids))
            c.execute("DELETE FROM supplier_orders WHERE id IN (?,?)", tuple(self.order_ids))
            c.commit()
        workflow = self.snapshot()['workflow']
        self.assertEqual(workflow, {
            'stage': 'Ready to Order',
            'next_action': 'Mark Order Placed',
            'next_url': f'/jobs/{self.job_id}/fulfillment/order',
            'action_method': 'POST',
        })

    def test_quote_and_invoice_creation_preserve_post_method(self):
        with closing(self.connection()) as c:
            c.execute("DELETE FROM supplier_order_items WHERE order_id IN (?,?)", tuple(self.order_ids))
            c.execute("DELETE FROM supplier_orders WHERE id IN (?,?)", tuple(self.order_ids))
            basket_id = c.execute(
                "SELECT id FROM baskets WHERE job_id=? ORDER BY id DESC LIMIT 1",
                (self.job_id,),
            ).fetchone()[0]
            c.execute(
                "INSERT INTO basket_items"
                "(basket_id,requested_description,selected,part_status) "
                "VALUES (?,'Synthetic quoted part',1,'QUOTED')",
                (basket_id,),
            )
            holding_job = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer,status) "
                "VALUES ('JCC2-HOLD','2026-08-16','Synthetic Holding','VERIFIED')"
            ).lastrowid
            c.execute("UPDATE invoices SET job_id=? WHERE id=?", (holding_job, self.invoice_id))
            quote_id = c.execute("SELECT id FROM quotes WHERE job_id=?", (self.job_id,)).fetchone()[0]
            c.execute("UPDATE quotes SET status='APPROVED' WHERE id=?", (quote_id,))
            c.commit()
        invoice_action = self.snapshot()['workflow']
        self.assertEqual(invoice_action['stage'], 'Ready to Invoice')
        self.assertEqual(invoice_action['next_action'], 'Create Invoice for Payment')
        self.assertEqual(invoice_action['next_url'], f'/quotes/{quote_id}/convert-to-invoice')
        self.assertEqual(invoice_action['action_method'], 'POST')

        with closing(self.connection()) as c:
            c.execute("UPDATE quotes SET job_id=? WHERE id=?", (holding_job, quote_id))
            c.execute("UPDATE jobs SET status='VERIFIED' WHERE id=?", (self.job_id,))
            c.commit()
        quote_action = self.snapshot()['workflow']
        self.assertEqual(quote_action['next_url'], f'/jobs/{self.job_id}/generate-quote')
        self.assertEqual(quote_action['action_method'], 'POST')

    def test_jcc_renders_get_links_and_post_forms_without_getting_post_routes(self):
        source = (ROOT / 'templates' / 'job_command_center.html').read_text()
        self.assertIn("operational_snapshot.workflow.action_method == 'POST'", source)
        self.assertIn('method="post" action="{{ operational_snapshot.workflow.next_url }}"', source)
        self.assertIn('name="csrf_token" value="{{ csrf_token }}"', source)
        self.assertIn('href="{{ operational_snapshot.workflow.next_url }}"', source)
        self.assertNotIn('href="/jobs/{{ job.id }}/generate-quote"', source)
        self.assertNotIn('href="/quotes/{{ quote.id }}/convert-to-invoice"', source)


if __name__ == '__main__':
    unittest.main()
