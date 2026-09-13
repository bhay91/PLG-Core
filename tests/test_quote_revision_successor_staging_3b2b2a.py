import asyncio, shutil, tempfile, unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_requested_need, create_manual_research_result, set_preferred_sourcing_option
from plg_core.revisions.service import ensure_initial_revision, commit_work_revision
from plg_core.revisions.quote_workflow import generate_quote_from_revision
from plg_core.basket.routes import center_generate_quote, center_send_quote

class Req:
 def __init__(self,v): self.values=v; self.cookies={'pps_csrf_token':'token'}
 async def form(self): return self.values

class SuccessorStagingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='pps-3b2b2a-'); self.db=Path(self.tmp.name)/'db.sqlite'
  shutil.copy2('data/plg_core.db',self.db); self.patches=[patch.object(legacy_app,'DB_PATH',self.db),patch.object(legacy_app,'DOCUMENTS_DIR',Path(self.tmp.name)/'docs')]
  for p in self.patches:p.start()
  self.addCleanup(self.cleanup); legacy_app.initialize_database(); run_migrations()
  with closing(legacy_app.get_connection()) as c:
   cid=c.execute("insert into customers(customer_number,name,active) values('3B2B2A-C','Stage Customer',1)").lastrowid
   self.job=c.execute("insert into jobs(job_number,created_date,customer_id,customer,status) values('3B2B2A-J','2026-01-01',?,'Stage Customer','REQUESTED')",(cid,)).lastrowid; c.commit()
  n=create_requested_need(self.job,job_asset_id=None,wording='Stage part'); i=create_manual_research_result(self.job,job_asset_id=None,requested_need_id=n['id'],description='Stage part',supplier_name='Stage Supplier',supplier_unit_cost=10,verification_status='VERIFIED')['items'][-1]; set_preferred_sourcing_option(self.job,n['id'],i['id'])
 def cleanup(self):
  for p in reversed(self.patches):p.stop()
  self.tmp.cleanup()
 def setup_revision(self):
  with closing(legacy_app.get_connection()) as c:r=ensure_initial_revision(c,self.job); c.commit()
  asyncio.run(center_generate_quote(Req({'csrf_token':'token','expected_revision_id':str(r['id']),'expected_version':str(r['lock_version'])}),self.job)); asyncio.run(center_send_quote(Req({'csrf_token':'token'}),self.job))
  with closing(legacy_app.get_connection()) as c:
   q=c.execute('select * from quotes where job_id=? and is_current=1',(self.job,)).fetchone(); c.execute("update quotes set status='REVISION_REQUIRED' where id=?",(q['id'],)); c.commit()
   from plg_core.revisions.quote_workflow import start_quote_revision
  rev=start_quote_revision(q['id'],'Customer requested quote revision'); return q,rev
 def test_staged_successor_activation_and_idempotency(self):
  q,rev=self.setup_revision(); out=generate_quote_from_revision(rev['id'],expected_version=rev['lock_version']); self.assertEqual(out['status'],'DRAFT')
  with closing(legacy_app.get_connection()) as c:
   original=c.execute('select status,is_current from quotes where id=?',(q['id'],)).fetchone(); succ=c.execute('select status,is_current,work_revision_id,supersedes_quote_id,quote_track_id from quotes where work_revision_id=?',(rev['id'],)).fetchone(); self.assertEqual(tuple(original),('SUPERSEDED',0)); self.assertEqual(succ['is_current'],1); self.assertEqual(succ['supersedes_quote_id'],q['id'])
  again=generate_quote_from_revision(rev['id'],expected_version=rev['lock_version']); self.assertEqual(again['id'],out['id'])
 def test_committed_revision_without_successor_retries(self):
  q,rev=self.setup_revision();
  commit_work_revision(self.job, expected_revision_id=rev['id'], expected_version=rev['lock_version'])
  out=generate_quote_from_revision(rev['id'],expected_version=rev['lock_version']); self.assertEqual(out['status'],'DRAFT')
 def test_document_failure_leaves_original_current_and_staged_successor(self):
  q,rev=self.setup_revision()
  with patch('plg_core.revisions.quote_workflow._write_documents', side_effect=RuntimeError('pdf failure')):
   with self.assertRaises(RuntimeError): generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
  with closing(legacy_app.get_connection()) as c:
   self.assertEqual(tuple(c.execute('select status,is_current from quotes where id=?',(q['id'],)).fetchone()),('REVISION_REQUIRED',1)); self.assertEqual(c.execute('select count(*) from quotes where work_revision_id=?',(rev['id'],)).fetchone()[0],1); self.assertEqual(c.execute('select is_current from quotes where work_revision_id=?',(rev['id'],)).fetchone()[0],0)
 def test_readiness_and_commit_failure_leave_no_successor(self):
     q,rev=self.setup_revision()
     with closing(legacy_app.get_connection()) as c:
         c.execute('update basket_items set supplier_unit_cost=NULL where basket_id=(select id from baskets where job_id=?)',(self.job,)); c.commit()
     with self.assertRaises(Exception): generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
     with closing(legacy_app.get_connection()) as c:
         self.assertEqual(c.execute('select state from work_revisions where id=?',(rev['id'],)).fetchone()[0],'EDITABLE'); self.assertEqual(c.execute('select count(*) from quotes where work_revision_id=?',(rev['id'],)).fetchone()[0],0)
 def test_document_health_matrix_and_repair_same_successor(self):
     q,rev=self.setup_revision(); out=generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
     from plg_core.revisions.quote_workflow import _draft_documents_healthy
     self.assertTrue(_draft_documents_healthy(out['id']))
     with closing(legacy_app.get_connection()) as c:
         row=c.execute("select id,file_path from quote_documents_manifest where quote_id=? and audience='CUSTOMER' and is_current=1",(out['id'],)).fetchone(); c.execute('update quote_documents_manifest set sha256=\'bad\' where id=?',(row['id'],)); c.commit()
     self.assertFalse(_draft_documents_healthy(out['id']))
     repaired=generate_quote_from_revision(rev['id'],expected_version=rev['lock_version']); self.assertEqual(repaired['id'],out['id']); self.assertTrue(_draft_documents_healthy(out['id']))
 def test_staged_successor_blocks_overlapping_revision_start(self):
     q,rev=self.setup_revision();
     with patch('plg_core.revisions.quote_workflow._write_documents', side_effect=RuntimeError('failure')):
         with self.assertRaises(RuntimeError): generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
     from plg_core.revisions.quote_workflow import start_quote_revision
     with self.assertRaises(Exception): start_quote_revision(q['id'],'another revision')
 def test_staged_transaction_failure_rolls_back_successor(self):
  q,rev=self.setup_revision()
  with patch('plg_core.revisions.quote_workflow._insert_quote_items', side_effect=RuntimeError('db failure')):
   with self.assertRaises(RuntimeError): generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
  with closing(legacy_app.get_connection()) as c:
   self.assertEqual(tuple(c.execute('select status,is_current from quotes where id=?',(q['id'],)).fetchone()),('REVISION_REQUIRED',1)); self.assertEqual(c.execute('select count(*) from quotes where work_revision_id=?',(rev['id'],)).fetchone()[0],0)
 def test_activation_failure_rolls_back_switch_and_retry_activates_same_successor(self):
  q,rev=self.setup_revision()
  with patch('plg_core.revisions.quote_workflow.write_audit', side_effect=RuntimeError('activation failure')):
   with self.assertRaises(RuntimeError): generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
  with closing(legacy_app.get_connection()) as c:
   self.assertEqual(tuple(c.execute('select status,is_current from quotes where id=?',(q['id'],)).fetchone()),('REVISION_REQUIRED',1)); staged=c.execute('select id,is_current from quotes where work_revision_id=?',(rev['id'],)).fetchone(); self.assertEqual(staged['is_current'],0)
  out=generate_quote_from_revision(rev['id'],expected_version=rev['lock_version']); self.assertEqual(out['id'],staged['id'])
 def test_legacy_current_successor_document_repair(self):
  q,rev=self.setup_revision(); out=generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
  with closing(legacy_app.get_connection()) as c:
   c.execute("update quote_documents_manifest set sha256='bad' where quote_id=? and audience='INTERNAL' and is_current=1",(out['id'],)); c.commit()
  repaired=generate_quote_from_revision(rev['id'],expected_version=rev['lock_version']); self.assertEqual(repaired['id'],out['id']); self.assertTrue(self._healthy(out['id']))
 def _healthy(self,qid):
  from plg_core.revisions.quote_workflow import _draft_documents_healthy
  return _draft_documents_healthy(qid)
 def test_staged_snapshot_exists_before_activation(self):
  q,rev=self.setup_revision()
  with patch('plg_core.revisions.quote_workflow._write_documents', side_effect=RuntimeError('pause')):
   with self.assertRaises(RuntimeError): generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
  with closing(legacy_app.get_connection()) as c:
   s=c.execute('select * from quotes where work_revision_id=?',(rev['id'],)).fetchone(); self.assertEqual(s['status'],'DRAFT'); self.assertEqual(s['is_current'],0); self.assertEqual(s['supersedes_quote_id'],q['id']); self.assertEqual(s['quote_track_id'],q['quote_track_id']); self.assertEqual(c.execute('select count(*) from quote_items where quote_id=?',(s['id'],)).fetchone()[0],1)
 def test_original_issued_snapshot_unchanged_after_activation(self):
  q,rev=self.setup_revision()
  with closing(legacy_app.get_connection()) as c:
   before=tuple(c.execute('select customer_total,issued_at,quote_track_id from quotes where id=?',(q['id'],)).fetchone()); items=[tuple(x) for x in c.execute('select quantity,customer_unit_price,supplier_unit_cost from quote_items where quote_id=?',(q['id'],))]; docs=[tuple(x) for x in c.execute('select id,version,sha256,file_path from quote_documents_manifest where quote_id=? and is_issued=1',(q['id'],))]
  generate_quote_from_revision(rev['id'],expected_version=rev['lock_version'])
  with closing(legacy_app.get_connection()) as c:
   after=tuple(c.execute('select customer_total,issued_at,quote_track_id from quotes where id=?',(q['id'],)).fetchone()); self.assertEqual(after,before); self.assertEqual([tuple(x) for x in c.execute('select quantity,customer_unit_price,supplier_unit_cost from quote_items where quote_id=?',(q['id'],))],items); self.assertEqual([tuple(x) for x in c.execute('select id,version,sha256,file_path from quote_documents_manifest where quote_id=? and is_issued=1',(q['id'],))],docs)

 def test_completed_revision_does_not_block_later_legitimate_revision(self):
  """A completed revision remains history while its current successor can be revised."""
  from plg_core.lifecycle.service import transition_quote
  from plg_core.revisions.quote_workflow import start_quote_revision

  source, revision_a = self.setup_revision()
  successor_a = generate_quote_from_revision(
      revision_a['id'], expected_version=revision_a['lock_version']
  )

  # Move the activated successor through the normal quote lifecycle before
  # opening the next governed revision; this is a legitimate operator path.
  transition_quote(successor_a['id'], 'SENT')
  revision_b = start_quote_revision(
      successor_a['id'], 'Customer requested another quote revision'
  )

  with closing(legacy_app.get_connection()) as connection:
   old_revision = connection.execute(
       'select state from work_revisions where id=?', (revision_a['id'],)
   ).fetchone()
   old_source = connection.execute(
       'select status,is_current from quotes where id=?', (source['id'],)
   ).fetchone()
   current_successor = connection.execute(
       'select status,is_current,supersedes_quote_id,quote_track_id from quotes where id=?',
       (successor_a['id'],),
   ).fetchone()
   new_revision = connection.execute(
       'select id,job_id,state,based_on_quote_id from work_revisions where id=?',
       (revision_b['id'],),
   ).fetchone()

  self.assertEqual(old_revision['state'], 'COMMITTED')
  self.assertEqual(tuple(old_source), ('SUPERSEDED', 0))
  self.assertEqual(tuple(current_successor[:2]), ('SENT', 1))
  self.assertEqual(current_successor['supersedes_quote_id'], source['id'])
  self.assertEqual(new_revision['state'], 'EDITABLE')
  self.assertNotEqual(revision_b['id'], revision_a['id'])
  self.assertEqual(new_revision['job_id'], self.job)
  self.assertEqual(new_revision['based_on_quote_id'], successor_a['id'])
