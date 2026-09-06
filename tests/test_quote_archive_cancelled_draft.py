import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import legacy_app
from plg_core.dashboard.service import get_work_queue_data
from plg_core.database.migrations import run_migrations


class CancelledDraftQuoteArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-quote-archive-")
        self.db = Path(self.temp.name) / "test.db"
        self.patch = patch.object(legacy_app, "DB_PATH", self.db)
        self.patch.start()
        legacy_app.initialize_database()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            customer_id = c.execute("INSERT INTO customers(customer_number,name,company,active) VALUES ('SYN-Q-C','Synthetic Quote Customer','Synthetic Quote Co',1)").lastrowid
            machine_id = c.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,?,?,?,?,?,1)", (customer_id, 'SYN-Q-M', 'Synthetic Quote Machine', 'Synthetic Make', 'SYN-1', 'SYN-SERIAL-1')).lastrowid
            c.execute("INSERT INTO jobs(id,job_number,created_date,customer,company,customer_id,machine_id,status,is_archived,cancelled_at) VALUES (10,'SYN-Q-J','2026-01-01','Synthetic Quote Customer','Synthetic Quote Co',?,?, 'REQUESTED',1,DATE('now'))", (customer_id, machine_id))
            for revision_id in (12, 13):
                c.execute("INSERT INTO work_revisions(id,job_id,revision_number,state,reason) VALUES (?,?,?,?,?)", (revision_id, 10, revision_id - 11, 'COMMITTED', 'Synthetic lineage'))
            c.execute("INSERT INTO quotes(id,quote_number,job_id,quote_date,status,work_revision_id,is_current) VALUES (10,'PPS-Q-0010',10,DATE('now'),'DRAFT',12,1)")
            c.execute("INSERT INTO quotes(id,quote_number,job_id,quote_date,status,is_current) VALUES (11,'SYN-Q-DRAFT',10,DATE('now'),'DRAFT',1)")
            c.execute("INSERT INTO jobs(id,job_number,created_date,customer,company,customer_id,machine_id,status) VALUES (11,'SYN-Q-J2','2026-01-01','Synthetic Quote Customer','Synthetic Quote Co',?,?, 'REQUESTED')", (customer_id, machine_id))
            c.execute("UPDATE quotes SET job_id=11 WHERE id=11")
            c.execute("INSERT INTO invoices(id,invoice_number,quote_id,job_id,invoice_date,status,customer_total,balance_due) VALUES (10,'SYN-INV-0010',11,10,DATE('now'),'UNPAID',100,100)")
            c.commit()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_pps_q_0010_archives_without_mutating_lineage_or_children(self):
        with closing(legacy_app.get_connection()) as c:
            quote = c.execute("SELECT * FROM quotes WHERE quote_number='PPS-Q-0010'").fetchone()
            self.assertIsNotNone(quote)
            qid, jid = quote["id"], quote["job_id"]
            before = {
                "quote_items": c.execute("SELECT COUNT(*) FROM quote_items WHERE quote_id=?", (qid,)).fetchone()[0],
                "documents": c.execute("SELECT COUNT(*) FROM quote_documents_manifest WHERE quote_id=?", (qid,)).fetchone()[0],
                "invoices": c.execute("SELECT COUNT(*) FROM invoices WHERE quote_id=?", (qid,)).fetchone()[0],
                "revisions": c.execute("SELECT COUNT(*) FROM work_revisions WHERE id IN (12,13)").fetchone()[0],
                "customer": c.execute("SELECT customer_id FROM jobs WHERE id=?", (jid,)).fetchone()[0],
                "machine": c.execute("SELECT machine_id FROM jobs WHERE id=?", (jid,)).fetchone()[0],
                "supersedes": quote["supersedes_quote_id"], "work_revision": quote["work_revision_id"],
            }
        legacy_app.archive_quote(qid)
        with closing(legacy_app.get_connection()) as c:
            after = c.execute("SELECT * FROM quotes WHERE id=?", (qid,)).fetchone()
            self.assertEqual(after["is_archived"], 1)
            self.assertEqual(after["supersedes_quote_id"], before["supersedes"])
            self.assertEqual(after["work_revision_id"], before["work_revision"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_items WHERE quote_id=?", (qid,)).fetchone()[0], before["quote_items"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM quote_documents_manifest WHERE quote_id=?", (qid,)).fetchone()[0], before["documents"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM invoices WHERE quote_id=?", (qid,)).fetchone()[0], before["invoices"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM work_revisions WHERE id IN (12,13)").fetchone()[0], before["revisions"])
            job = c.execute("SELECT customer_id,machine_id FROM jobs WHERE id=?", (jid,)).fetchone()
            self.assertEqual((job["customer_id"], job["machine_id"]), (before["customer"], before["machine"]))
            active = c.execute("SELECT COUNT(*) FROM quotes WHERE quote_number='PPS-Q-0010' AND is_archived=0 AND is_current=1").fetchone()[0]
            archived = c.execute("SELECT COUNT(*) FROM quotes WHERE quote_number='PPS-Q-0010' AND is_archived=1").fetchone()[0]
            all_rows = c.execute("SELECT COUNT(*) FROM quotes WHERE quote_number='PPS-Q-0010'").fetchone()[0]
            self.assertEqual((active, archived, all_rows), (0, 1, 1))
            queue = get_work_queue_data(c)
            self.assertFalse(any(row.get("quote_number") == "PPS-Q-0010" for row in queue["items"]))

    def test_operational_or_issued_draft_remains_protected(self):
        with closing(legacy_app.get_connection()) as c:
            row = c.execute(
                """SELECT q.id FROM quotes q JOIN jobs j ON j.id=q.job_id
                   WHERE q.status='DRAFT' AND COALESCE(j.is_archived,0)=0 LIMIT 1"""
            ).fetchone()
        self.assertIsNotNone(row)
        with self.assertRaises(HTTPException) as caught:
            legacy_app.archive_quote(row["id"])
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
