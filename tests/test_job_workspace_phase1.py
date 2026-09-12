from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request
import legacy_app
from plg_core.application import app
from plg_core.basket.routes import basket_page
from plg_core.database.migrations import run_migrations
from plg_core.jobs.workspace import build_workspace
from plg_core.research.service import create_requested_need, create_manual_research_result, set_quote_candidate

ROOT = Path(__file__).resolve().parents[1]


class JobWorkspacePhase1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-workspace-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('DB_PATH', self.root / 'test.db'), ('DOCUMENTS_DIR', self.root / 'documents'), ('UPLOADS_DIR', self.root / 'uploads')]:
            patcher = patch.object(legacy_app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        legacy_app.initialize_database()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            self.job = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES ('MOBILE-PHASE1','2026-09-06','Synthetic Customer','REQUESTED')").lastrowid
            c.commit()

    def need(self, wording='Hydraulic Pump'):
        return create_requested_need(self.job, job_asset_id=None, wording=wording)

    def option(self, need=None, supplier='Supplier A', cost=420):
        basket = create_manual_research_result(self.job, job_asset_id=None, requested_need_id=need['id'] if need else None,
            description='Hydraulic Pump Offer', supplier_name=supplier, supplier_unit_cost=cost,
            availability='In stock', lead_time='2 days', verification_status='VERIFIED',
            source_url='https://example.test/' + 'long-reference-' * 20,
            research_evidence='Original catalog evidence')
        return basket['items'][-1]

    def model(self):
        with closing(legacy_app.get_connection()) as c:
            return build_workspace(c, self.job)

    def html(self, **kwargs):
        kwargs.setdefault('view', 'advanced')
        request = Request({'type':'http','method':'GET','path':f'/jobs/{self.job}/basket',
            'headers':[], 'query_string':b'', 'scheme':'http','server':('localhost',80),'app':app})
        return basket_page(request, self.job, **kwargs).body.decode()

    def test_request_remains_container_after_selection_and_options_remain_distinct(self):
        need = self.need()
        offers = [self.option(need, f'Supplier {i}') for i in range(3)]
        before = self.model()['parts'][0]
        self.assertEqual(len(before['options']), 3)
        self.assertEqual(before['selected_options'], [])
        set_quote_candidate(self.job, offers[1]['id'], candidate=True)
        after = self.model()['parts'][0]
        self.assertEqual(after['id'], need['id'])
        self.assertEqual([o['id'] for o in after['options']], [o['id'] for o in offers])
        self.assertEqual(after['selected_options'][0]['id'], offers[1]['id'])
        html = self.html()
        self.assertIn(f'data-requested-part="{need["id"]}"', html)
        self.assertIn('Selected options for Hydraulic Pump', html)
        self.assertIn('Supplier 1', html)
        self.assertNotIn('Use This Option', html)

    def test_each_requested_need_is_a_self_contained_workspace(self):
        first = self.need('Hydraulic Pump')
        second = self.need('Seal Kit')
        first_offer = self.option(first, 'Pump Supplier')
        second_offer = self.option(second, 'Seal Supplier')
        set_quote_candidate(self.job, first_offer['id'], candidate=True)
        html = self.html()
        self.assertEqual(html.count('data-need-workspace'), 2)
        self.assertEqual(html.count('class="jcc-needs-list"'), 1)
        first_group = html[html.index(f'data-requested-need-id="{first["id"]}"'):html.index(f'data-requested-need-id="{second["id"]}"')]
        second_group = html[html.index(f'data-requested-need-id="{second["id"]}"'):html.index('id="add-part"')]
        self.assertIn('Hydraulic Pump', first_group)
        self.assertIn('Pump Supplier', first_group)
        self.assertNotIn('Seal Supplier', first_group)
        self.assertIn('Seal Kit', second_group)
        self.assertIn('Seal Supplier', second_group)
        self.assertNotIn('Pump Supplier', second_group)

    def test_selected_option_stays_inside_its_need_and_unassigned_stays_outside(self):
        need = self.need('Hydraulic Pump')
        selected = self.option(need, 'Selected Supplier')
        unassigned = self.option(None, 'Unassigned Supplier')
        set_quote_candidate(self.job, selected['id'], candidate=True)
        html = self.html()
        group_start = html.index(f'data-requested-need-id="{need["id"]}"')
        group_end = html.index('id="other-items"', group_start)
        group = html[group_start:group_end]
        self.assertIn('Selected Supplier', group)
        self.assertIn('Selected options for Hydraulic Pump', group)
        other_start = html.index('id="other-items"')
        self.assertGreaterEqual(other_start, group_end)
        self.assertIn('Unassigned Supplier', html[other_start:])
        self.assertNotIn('Unassigned Supplier', group)

    def test_multiple_and_shared_selections_are_not_replaced(self):
        need, second = self.need(), self.need('Seal Kit')
        first, other = self.option(need), self.option(need, 'Supplier B')
        set_quote_candidate(self.job, first['id'], candidate=True, requested_need_ids=[need['id'],second['id']])
        set_quote_candidate(self.job, other['id'], candidate=True)
        model = self.model()
        self.assertTrue(model['parts'][0]['ambiguous'])
        self.assertEqual(len(model['parts'][0]['selected_options']), 2)
        self.assertEqual(len(model['selected_options']), 2)
        self.assertIn('Multiple selections', self.html())
        self.assertNotIn('Use This Option', self.html())

    def test_unassigned_and_legacy_records_remain_reachable(self):
        option = self.option()
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE basket_items SET research_state='LEGACY_CANDIDATE',selected=1 WHERE id=?", (option['id'],))
            c.execute("INSERT INTO job_parts(job_id,requested_description,quantity) VALUES (?,'Historical Pump',2)", (self.job,))
            c.commit()
        self.assertEqual(self.model()['other_options'][0]['id'], option['id'])
        html = self.html()
        for text in ['Other job items', 'Historical Pump', 'Earlier saved items', 'view=advanced']:
            self.assertIn(text, html)

    def test_read_is_sqlite_read_only_even_without_basket_or_revision(self):
        denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE}
        with closing(legacy_app.get_connection()) as c:
            c.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
            model = build_workspace(c, self.job)
            self.assertIsNone(model['basket']['id'])
            self.assertIsNone(model['revision'])
        self.html()
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM baskets WHERE job_id=?', (self.job,)).fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM work_revisions WHERE job_id=?', (self.job,)).fetchone()[0], 0)

    def test_loading_does_not_mutate_associations_or_business_records(self):
        need = self.need()
        self.option(need)
        def dump():
            with closing(legacy_app.get_connection()) as c:
                return '\n'.join(c.iterdump())
        before = dump()
        self.html()
        self.html(need_id=need['id'])
        self.assertEqual(before, dump())

    def test_no_requested_quantity_is_invented(self):
        need = self.need('Pump qty unknown')
        self.option(need)
        model = self.model()['parts'][0]
        self.assertNotIn('quantity', model)
        self.assertIn('Quantity not recorded', self.html())
        self.assertIn('Option quantity', self.html())

    def test_suggestion_does_not_rank_unknown_or_unverified_offers(self):
        need = self.need()
        self.option(need, cost=None)
        self.assertIsNone(self.model()['parts'][0]['suggested'])
        self.option(need, 'Verified Supplier', 420)
        self.assertEqual(self.model()['parts'][0]['suggested']['supplier_name'], 'Verified Supplier')
        self.assertIn('shipping excluded', self.html())

    def test_details_are_closed_and_five_sections_have_no_tables(self):
        self.option(self.need())
        html = self.html()
        for name in ['overview', 'parts', 'quote', 'orders', 'activity']:
            self.assertIn(f'id="{name}"', html)
        self.assertIn('data-advanced><summary>Option Details / Research Details', html)
        self.assertNotIn('data-advanced open', html)
        self.assertNotIn('<table', html)
        for phrase in ['Source Directory', 'connector-profile', 'verification-session', 'RESEARCH CANDIDATES', 'Open Source', 'Confirm for Quote']:
            self.assertNotIn(phrase, html)

    def test_cross_job_need_cannot_be_focused(self):
        with self.assertRaises(HTTPException) as raised:
            self.html(need_id=999999)
        self.assertEqual(raised.exception.status_code, 404)

    def test_manual_offer_records_existing_stock_fields_without_selecting(self):
        option = self.option(self.need())
        self.assertEqual((option['availability'], option['lead_time'], option['selected']), ('In stock', '2 days', 0))

    def test_protected_work_shows_options_but_no_mutation_forms(self):
        self.option(self.need())
        with closing(legacy_app.get_connection()) as c:
            c.execute("UPDATE work_revisions SET state='COMMITTED' WHERE job_id=?", (self.job,))
            c.commit()
        html = self.html()
        self.assertIn('Hydraulic Pump Offer', html)
        self.assertNotIn('>Use This Option</button>', html)
        self.assertNotIn('>Save Option</button>', html)

    def test_advanced_workspace_remains_available(self):
        self.need()
        html = self.html(view='advanced')
        self.assertIn('Technical', html)
        self.assertIn('Commercial history', html)
        self.assertIn('← Back to Job', html)
        legacy = self.html(view='legacy')
        self.assertIn('machine-workspace', legacy)

    def test_operational_sections_use_plain_language(self):
        html = self.html()
        for label in ('Overview', 'Parts', 'Quote', 'Orders', 'Activity', 'Job progress', 'Customer quote', 'Payment'):
            self.assertIn(label, html)
        self.assertIn('Advanced history', html)
        self.assertNotIn('quote candidate', html.lower())
        self.assertNotIn('verification session', html.lower())

    def test_loading_shell_does_not_change_business_records(self):
        need = self.need()
        self.option(need)
        with closing(legacy_app.get_connection()) as c:
            before = '\n'.join(c.iterdump())
        self.html()
        with closing(legacy_app.get_connection()) as c:
            self.assertEqual(before, '\n'.join(c.iterdump()))

    def test_speed_path_exposes_inline_pricing_and_quick_add_preview(self):
        html = self.html()
        self.assertIn('quick-add-input', html)
        self.assertIn('quick-add-preview-button', html)
        option = self.option(self.need())
        html = self.html()
        for field in ('supplier_name', 'supplier_part_number', 'supplier_unit_cost', 'customer_unit_price_override', 'quantity', 'availability'):
            self.assertIn(f'name="{field}"', html)

    def test_job_theme_uses_existing_pps_theme_tokens(self):
        css = (ROOT / 'static' / 'job_workspace.css').read_text()
        for token in ('--jcc-surface', '--jcc-text', '--jcc-input-bg', '--jcc-warning-bg'):
            self.assertIn(token, css)
        self.assertIn('var(--erp-panel)', css)
        self.assertIn('var(--erp-text)', css)
        self.assertIn('var(--erp-blue)', css)
        self.assertIn('--jcc-text: #f5f8fc', css)
        self.assertIn('--jcc-muted: #c3cfdd', css)
        self.assertIn('--jcc-input-bg: #1a2d47', css)
        self.assertIn('--jcc-border: #3a5472', css)
        self.assertIn('.erp-app .jcc .jcc-need-workspace > #find-part', css)
        self.assertIn('#find-part input', css)
        self.assertNotIn('localStorage', css)
