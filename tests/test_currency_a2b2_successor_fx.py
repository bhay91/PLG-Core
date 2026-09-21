import unittest
from contextlib import closing
from unittest.mock import patch

import legacy_app
from plg_core.currency.service import update_currency_settings
from plg_core.revisions.quote_workflow import generate_quote_from_revision
from plg_core.revisions.service import update_revision_currency_config

from test_quote_revision_successor_staging_3b2b2a import SuccessorStagingTests


class CurrencyA2B2SuccessorFXTests(SuccessorStagingTests):
    def test_unchanged_revision_gets_new_complete_snapshot(self):
        source, revision = self.setup_revision()
        successor = generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            source_row = connection.execute("SELECT * FROM quotes WHERE id=?", (source["id"],)).fetchone()
            successor_row = connection.execute("SELECT * FROM quotes WHERE id=?", (successor["id"],)).fetchone()
        self.assertEqual((successor_row["currency_code"], successor_row["display_currency_mode"], successor_row["fx_rate"], successor_row["fx_rate_source"]), ("USD", "USD", "160", "BUSINESS_WORKING_RATE"))
        self.assertIsNotNone(successor_row["fx_locked_at"])
        self.assertNotEqual(source_row["fx_locked_at"], successor_row["fx_locked_at"])

    def test_revision_override_wins_over_global_and_preserves_totals(self):
        source, revision = self.setup_revision()
        changed = update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], jmd_rate="165")
        with closing(legacy_app.get_connection()) as connection:
            update_currency_settings(jmd_working_rate="170", connection=connection)
            connection.commit()
        successor = generate_quote_from_revision(revision["id"], expected_version=changed["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            row = connection.execute("SELECT * FROM quotes WHERE id=?", (successor["id"],)).fetchone()
            self.assertEqual((row["fx_rate"], row["fx_rate_source"], row["customer_total"], row["supplier_total"], row["profit_total"]), ("165", "MANUAL_OVERRIDE", 14.0, 10.0, 4.0))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoices WHERE job_id=?", (self.job,)).fetchone()[0], 0)

    def test_staged_recovery_reuses_snapshot_and_timestamp(self):
        source, revision = self.setup_revision()
        with patch("plg_core.revisions.quote_workflow._write_documents", side_effect=RuntimeError("pause")):
            with self.assertRaises(RuntimeError):
                generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            staged = connection.execute("SELECT id,fx_rate,fx_rate_source,fx_locked_at FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()
            update_currency_settings(jmd_working_rate="180", connection=connection)
            connection.commit()
        recovered = generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        self.assertEqual(recovered["id"], staged["id"])
        with closing(legacy_app.get_connection()) as connection:
            row = connection.execute("SELECT fx_rate,fx_rate_source,fx_locked_at FROM quotes WHERE id=?", (staged["id"],)).fetchone()
        self.assertEqual(tuple(row), (staged["fx_rate"], staged["fx_rate_source"], staged["fx_locked_at"]))

    def test_staged_snapshot_mismatch_fails_closed(self):
        source, revision = self.setup_revision()
        with patch("plg_core.revisions.quote_workflow._write_documents", side_effect=RuntimeError("pause")):
            with self.assertRaises(RuntimeError):
                generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            staged = connection.execute("SELECT id FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()
            connection.execute("UPDATE quotes SET fx_rate='170' WHERE id=?", (staged["id"],))
            connection.commit()
        with self.assertRaises(Exception):
            generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT is_current FROM quotes WHERE id=?", (source["id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0], 1)

    def test_document_repair_does_not_rewrite_fx(self):
        source, revision = self.setup_revision()
        successor = generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            before = tuple(connection.execute("SELECT fx_rate,fx_rate_source,fx_locked_at FROM quotes WHERE id=?", (successor["id"],)).fetchone())
            connection.execute("UPDATE quote_documents_manifest SET sha256='bad' WHERE quote_id=? AND audience='CUSTOMER' AND is_current=1", (successor["id"],))
            update_currency_settings(jmd_working_rate="190", connection=connection)
            connection.commit()
        repaired = generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        self.assertEqual(repaired["id"], successor["id"])
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(tuple(connection.execute("SELECT fx_rate,fx_rate_source,fx_locked_at FROM quotes WHERE id=?", (successor["id"],)).fetchone()), before)

    def test_invalid_committed_revision_fails_without_successor(self):
        source, revision = self.setup_revision()
        from plg_core.revisions.service import commit_work_revision
        commit_work_revision(self.job, expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            connection.execute("UPDATE work_revisions SET fx_rate=NULL WHERE id=?", (revision["id"],))
            connection.commit()
        with self.assertRaises(Exception):
            generate_quote_from_revision(revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quotes WHERE work_revision_id=?", (revision["id"],)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT is_current FROM quotes WHERE id=?", (source["id"],)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
