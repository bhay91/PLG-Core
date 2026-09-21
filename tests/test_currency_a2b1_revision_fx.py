import shutil
import tempfile
import unittest
import json
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import legacy_app
from plg_core.commercial.service import create_selective_draft_quote
from plg_core.currency.service import update_currency_settings, is_legacy_quote_currency_snapshot
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_manual_research_result, create_requested_need, set_preferred_sourcing_option
from plg_core.revisions.quote_workflow import start_quote_revision
from plg_core.revisions.service import (
    commit_work_revision, ensure_initial_revision, reset_revision_currency_to_source,
    update_revision_currency_config,
)


class CurrencyA2B1RevisionFXTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pps-currency-a2b1-")
        root = Path(self.tmp.name)
        self.db = root / "db.sqlite"
        shutil.copy2(Path(__file__).parents[1] / "data" / "plg_core.db", self.db)
        self.patches = [patch.object(legacy_app, "DB_PATH", self.db), patch.object(legacy_app, "DOCUMENTS_DIR", root / "documents")]
        for item in self.patches: item.start()
        self.addCleanup(self.cleanup)
        legacy_app.initialize_database(); run_migrations()
        with closing(legacy_app.get_connection()) as c:
            self.job = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES ('A2B1-J','2026-01-01','A2B1 Customer','REQUESTED')").lastrowid
            c.commit()
        need = create_requested_need(self.job, job_asset_id=None, wording="A2B1 part")
        item = create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need["id"], description="A2B1 part", supplier_name="A2B1 Supplier", supplier_unit_cost=12, customer_unit_price_override=18, verification_status="VERIFIED")
        self.item_id = item["id"]
        set_preferred_sourcing_option(self.job, need["id"], self.item_id)

    def cleanup(self):
        for item in reversed(self.patches): item.stop()
        self.tmp.cleanup()

    def _initial_quote(self, *, mode="USD_JMD", rate="160"):
        with closing(legacy_app.get_connection()) as c:
            basket = c.execute("SELECT id FROM baskets WHERE job_id=?", (self.job,)).fetchone()
            c.execute("UPDATE baskets SET customer_display_currency_mode_override=?,customer_jmd_fx_rate_override=? WHERE id=?", (mode, rate if rate != "160" else None, basket["id"]))
            c.commit()
        with closing(legacy_app.get_connection()) as c:
            revision = ensure_initial_revision(c, self.job); c.commit()
        return create_selective_draft_quote(self.job, basket_item_ids=[self.item_id], expected_revision_id=revision["id"], expected_version=revision["lock_version"])

    def _revision(self, quote):
        return start_quote_revision(quote["id"], "Currency revision")

    def test_modern_source_inherits_without_lock_timestamp(self):
        quote = self._initial_quote()
        revision = self._revision(quote)
        self.assertEqual((revision["state"], revision["display_currency_mode"], revision["fx_rate"], revision["fx_rate_source"]), ("EDITABLE", "USD_JMD", "160", "BUSINESS_WORKING_RATE"))
        self.assertIsNone(revision["fx_locked_at"] if "fx_locked_at" in revision.keys() else None)
        with closing(legacy_app.get_connection()) as c:
            source = c.execute("SELECT fx_rate,fx_locked_at FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertEqual(source["fx_rate"], "160")

    def test_modern_partial_invalid_source_fails_closed(self):
        quote = self._initial_quote(); source_id = quote["id"]
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE quotes SET display_currency_mode='USD_JMD',fx_rate=NULL,fx_rate_source='BUSINESS_WORKING_RATE' WHERE id=?", (source_id,)); c.commit()
        with self.assertRaises(Exception): self._revision(quote)

    def test_strict_snapshot_classification_rejects_mixed_states(self):
        cases = [
            {"currency_code": "USD", "display_currency_mode": None, "fx_rate": None, "fx_rate_source": None, "fx_locked_at": None},
            {"currency_code": None, "display_currency_mode": "USD_JMD", "fx_rate": "160", "fx_rate_source": "BUSINESS_WORKING_RATE", "fx_locked_at": "2026-01-01"},
            {"currency_code": "USD", "display_currency_mode": "USD_JMD", "fx_rate": "160", "fx_rate_source": "BUSINESS_WORKING_RATE", "fx_locked_at": None},
            {"currency_code": None, "display_currency_mode": None, "fx_rate": None, "fx_rate_source": None, "fx_locked_at": "2026-01-01"},
            {"currency_code": "EUR", "display_currency_mode": "USD", "fx_rate": "160", "fx_rate_source": "BUSINESS_WORKING_RATE", "fx_locked_at": "2026-01-01"},
        ]
        for index, values in enumerate(cases):
            with closing(legacy_app.get_connection()) as c:
                job = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES (?, '2026-01-01','A2B1 Customer','REQUESTED')", (f'A2B1-M{index}',)).lastrowid
                c.commit()
            need = create_requested_need(job, job_asset_id=None, wording="A2B1 part")
            item = create_manual_research_result(job, job_asset_id=None, requested_need_id=need["id"], description="A2B1 part", supplier_name="A2B1 Supplier", supplier_unit_cost=12, customer_unit_price_override=18, verification_status="VERIFIED")
            set_preferred_sourcing_option(job, need["id"], item["id"])
            old_job, old_item = self.job, self.item_id
            self.job, self.item_id = job, item["id"]
            quote = self._initial_quote()
            self.job, self.item_id = old_job, old_item
            with closing(legacy_app.get_connection()) as c:
                c.execute("UPDATE quotes SET currency_code=?,display_currency_mode=?,fx_rate=?,fx_rate_source=?,fx_locked_at=? WHERE id=?", (*[values[k] for k in ("currency_code", "display_currency_mode", "fx_rate", "fx_rate_source", "fx_locked_at")], quote["id"]))
                c.commit()
                row = c.execute("SELECT * FROM quotes WHERE id=?", (quote["id"],)).fetchone()
            self.assertFalse(is_legacy_quote_currency_snapshot(row), index)
            with self.assertRaises(Exception): self._revision(quote)
            with closing(legacy_app.get_connection()) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM work_revisions WHERE based_on_quote_id=?", (quote["id"],)).fetchone()[0], 0)

    def test_public_api_rejects_rate_source_override(self):
        revision = self._revision(self._initial_quote())
        with self.assertRaises(TypeError):
            update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], jmd_rate="165", rate_source="BUSINESS_WORKING_RATE")

    def test_reset_preserves_manual_source_provenance(self):
        quote = self._initial_quote(rate="162.75")
        revision = self._revision(quote)
        changed = update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], jmd_rate="165")
        reset = reset_revision_currency_to_source(revision["id"], expected_version=changed["lock_version"])
        self.assertEqual((reset["fx_rate"], reset["fx_rate_source"]), ("162.75", "MANUAL_OVERRIDE"))

    def test_legacy_source_captures_global_once(self):
        with closing(legacy_app.get_connection()) as c:
            job = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES ('LEG-J','2026-01-01','Legacy','QUOTED')").lastrowid
            c.execute("INSERT INTO baskets(job_id,status) VALUES (?, 'COMMITTED')", (job,))
            qid = c.execute("INSERT INTO quotes(quote_number,job_id,quote_date,status,customer_total,is_current) VALUES ('LEG-Q',?,'2026-01-01','DRAFT',10,1)", (job,)).lastrowid
            c.commit()
            update_currency_settings(jmd_working_rate="165", connection=c); c.commit()
        revision = start_quote_revision(qid, "Legacy currency revision")
        with closing(legacy_app.get_connection()) as c:
            update_currency_settings(jmd_working_rate="170", connection=c); c.commit()
            row = c.execute("SELECT display_currency_mode,fx_rate,fx_rate_source FROM work_revisions WHERE id=?", (revision["id"],)).fetchone()
            source = c.execute("SELECT currency_code,fx_rate FROM quotes WHERE id=?", (qid,)).fetchone()
        self.assertEqual(tuple(row), ("USD", "165", "BUSINESS_WORKING_RATE")); self.assertEqual(tuple(source), (None, None))

    def test_display_only_edit_preserves_business_source(self):
        revision = self._revision(self._initial_quote())
        updated = update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], display_mode="JMD")
        self.assertEqual((updated["display_currency_mode"], updated["fx_rate"], updated["fx_rate_source"], updated["lock_version"]), ("JMD", "160", "BUSINESS_WORKING_RATE", revision["lock_version"] + 1))

    def test_rate_edits_are_manual_and_partial(self):
        revision = self._revision(self._initial_quote())
        updated = update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], jmd_rate="165")
        self.assertEqual((updated["fx_rate"], updated["fx_rate_source"]), ("165", "MANUAL_OVERRIDE"))
        updated = update_revision_currency_config(revision["id"], expected_version=updated["lock_version"], jmd_rate="162.75", actor="tester")
        self.assertEqual((updated["fx_rate"], updated["fx_rate_source"]), ("162.75", "MANUAL_OVERRIDE"))
        with closing(legacy_app.get_connection()) as c:
            audit = c.execute("SELECT actor,action,metadata_json FROM audit_logs WHERE entity_type='WORK_REVISION' AND entity_id=? ORDER BY id DESC LIMIT 1", (revision["id"],)).fetchone()
        self.assertEqual((audit["actor"], audit["action"]), ("tester", "WORK_REVISION_CURRENCY_UPDATED"))
        self.assertEqual(json.loads(audit["metadata_json"])["new"]["fx_rate"], "162.75")

    def test_invalid_and_stale_edits_do_not_increment(self):
        revision = self._revision(self._initial_quote())
        with self.assertRaises(Exception): update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], jmd_rate="NaN")
        current = update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], display_mode="JMD")
        with self.assertRaises(Exception): update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], display_mode="USD")
        self.assertEqual(current["lock_version"], revision["lock_version"] + 1)

    def test_reset_modern_restores_source_and_legacy_reset_is_unavailable(self):
        quote = self._initial_quote(); revision = self._revision(quote)
        changed = update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], jmd_rate="165", display_mode="USD")
        reset = reset_revision_currency_to_source(revision["id"], expected_version=changed["lock_version"])
        self.assertEqual((reset["display_currency_mode"], reset["fx_rate"], reset["fx_rate_source"]), ("USD_JMD", "160", "BUSINESS_WORKING_RATE"))

    def test_committed_revision_rejects_edit_and_commit_requires_complete_fx(self):
        revision = self._revision(self._initial_quote())
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE work_revisions SET display_currency_mode=NULL,fx_rate=NULL,fx_rate_source=NULL WHERE id=?", (revision["id"],)); c.commit()
        with self.assertRaises(Exception): commit_work_revision(self.job, expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE work_revisions SET display_currency_mode='USD_JMD',fx_rate='160',fx_rate_source='BUSINESS_WORKING_RATE' WHERE id=?", (revision["id"],)); c.commit()
        committed = commit_work_revision(self.job, expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        self.assertTrue(committed["ok"])
        with self.assertRaises(Exception): update_revision_currency_config(revision["id"], expected_version=revision["lock_version"], jmd_rate="170")
