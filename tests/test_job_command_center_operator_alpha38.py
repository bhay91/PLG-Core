from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "job_command_center.html"


class JobCommandCenterOperatorAlpha38Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text()

    def test_compact_job_bar_exposes_operator_context(self):
        for label in (
            "Current Stage", "Payment", "Next Action", "Items Ready for Quote",
        ):
            self.assertIn(label, self.source)
        self.assertIn('class="cc-card job-bar"', self.source)
        self.assertNotIn('class="job-glance-grid" aria-label="Job summary"', self.source)
        self.assertNotIn('class="cc-command-progress"', self.source)

    def test_machine_selector_is_compact_and_management_is_secondary(self):
        self.assertIn("Manage Assets", self.source)
        self.assertIn("Asset / Equipment Context", self.source)
        self.assertNotIn('class="asset-need-list"', self.source)

    def test_need_source_parts_workbench_keeps_context_and_routes(self):
        for label in ("machine-need-list", "RESEARCH SOURCE", "RESEARCH CANDIDATES"):
            self.assertIn(label, self.source)
        self.assertNotIn("MACHINE WORKBENCH", self.source)
        self.assertIn('id="customer-needs"', self.source)
        self.assertIn('id="research-open-form"', self.source)
        self.assertIn('data-dialog-open="quick-open-dialog"', self.source)
        self.assertIn('data-dialog-open="manual-part-dialog"', self.source)
        self.assertIn('name="requested_need_id"', self.source)
        self.assertIn('name="expected_revision_id"', self.source)
        self.assertIn('name="expected_version"', self.source)
        self.assertEqual(self.source.count(">Open Source</button>"), 1)

    def test_parts_found_prioritizes_work_and_collapses_secondary_details(self):
        self.assertIn("Supplier price", self.source)
        self.assertIn("Confirm for Quote", self.source)
        self.assertIn("Evidence &amp; More", self.source)
        self.assertIn("Shipping Details", self.source)
        self.assertIn("Remove Result", self.source)
        self.assertNotIn("<small>Weight</small><strong>{% if shipping", self.source)

    def test_parts_found_controls_and_pricing_use_readable_pps_typography(self):
        self.assertIn("min-height:44px;padding:.69rem 1.04rem", self.source)
        self.assertIn("font-size:.88rem;font-weight:700", self.source)
        self.assertIn("border:1px solid var(--plg-line-strong)", self.source)
        self.assertIn(".part-editor-grid label>span,.part-editor-metric>span{color:#53657b;font-size:12px", self.source)
        self.assertIn(".part-editor-grid input,.part-editor-grid select{min-height:44px", self.source)
        self.assertIn('class="part-editor-metric pricing-emphasis"', self.source)
        self.assertIn('class="part-editor-metric line-total-emphasis"', self.source)

    def test_pricing_editor_has_one_identifier_and_seven_visible_values(self):
        editor = self.source[
            self.source.index('<details class="quote-line-details">'):
            self.source.index('<footer class="part-editor-footer">')
        ]
        for label in (
            "Part Number", "Quantity", "Supplier Cost", "Markup %",
            "Customer Price", "Dollar Margin", "Customer Line Total",
        ):
            self.assertIn(f"<span>{label}</span>", editor)
        for removed in (
            "Job Asset", "Manufacturer / OEM #",
            "Alternate / Superseded #", "Supplier Part #",
        ):
            self.assertNotIn(f"<span>{removed}</span>", editor)
        self.assertIn("{% if item.manufacturer_part_number %}", editor)
        self.assertIn("{% elif item.supplier_part_number %}", editor)
        self.assertIn("item.internal_part_number or ''", editor)
        self.assertIn('type="hidden" name="alternate_part_number"', editor)
        self.assertIn("data-part-number-target", editor)

    def test_pricing_editor_is_compact_and_responsive(self):
        self.assertIn(
            "grid-template-columns:minmax(160px,1.7fr) minmax(62px,.55fr) "
            "minmax(100px,.85fr) minmax(82px,.65fr) minmax(105px,.9fr) "
            "minmax(105px,.8fr) minmax(120px,.95fr)",
            self.source,
        )
        self.assertIn(".part-editor-grid .part-number-field{grid-column:auto}", self.source)
        self.assertIn("@media(max-width:1100px){.part-editor-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}", self.source)
        self.assertIn("@media(max-width:620px){.part-editor-grid{grid-template-columns:1fr}}", self.source)

    def test_quote_tray_precedes_secondary_financial_detail(self):
        tray = self.source.index("QUOTE TRAY")
        charges = self.source.index("<summary>Additional Charges</summary>")
        summary = self.source.index("UNQUOTED / DRAFT WORK · {% endif %}Detailed totals &amp; profitability")
        self.assertLess(tray, charges)
        self.assertLess(charges, summary)
        for label in ("Confirmed Part", "Customer Price", "Unit Margin", "Remove from Quote", "Create Draft Quote"):
            self.assertIn(label, self.source)
        self.assertEqual(self.source.count('id="quote-builder-form"'), 1)

    def test_create_quote_is_a_compact_final_review(self):
        review = self.source[
            self.source.index('id="quote-builder-form"'):
            self.source.index('</form>', self.source.index('id="quote-builder-form"')) + 7
        ]
        for label in (
            "BILL TO", "INCLUDED PARTS", "Part Number", "Quantity",
            "Customer Price", "Line Total", "QUOTE CHARGES",
            "Sourcing Fee", "Service Charge", "Customer Total",
            "Edit Charges", "Create Draft Quote",
        ):
            self.assertIn(label, review)
        self.assertIn('name="basket_item_ids"', review)
        self.assertIn("checked data-quote-line-toggle", review)
        self.assertIn("data-quote-exclude", review)
        self.assertNotIn("Supplier Cost", review)
        self.assertNotIn("Dollar Margin", review)
        self.assertIn("data-base-total", review)
        self.assertIn("Create a draft quote for ${recipient}", self.source)
        self.assertIn("expected_revision_id", self.source)
        self.assertIn("expected_version", self.source)

    def test_secondary_information_is_collapsed_without_removing_routes(self):
        for label in (
            "View Original Customer Request", "Manage Requested Needs", "Add Source",
            "Additional Charges", "Detailed totals &amp; profitability",
        ):
            self.assertIn(label, self.source)
        self.assertNotIn("Job administration and lifecycle controls", self.source)
        self.assertNotIn('id="follow-ups"', self.source)
        self.assertNotIn("Information and next actions", self.source)
        self.assertNotIn('id="job-timeline"', self.source)
        legacy_source = (ROOT / "legacy_app.py").read_text()
        for route in ("/jobs/{job_id}/edit", "/jobs/{job_id}/archive", "/jobs/{job_id}/cancel", "/jobs/{job_id}/delete"):
            self.assertIn(route, legacy_source)
        for route in (
            "/research-results/manual", "/research/quick-open", "/research-sources",
            "/basket/items/{{ item.id }}/delete", "/research-results/{{ item.id }}/candidate",
            "/quote-builder",
        ):
            self.assertIn(route, self.source)
        followup_source = (ROOT / "plg_core" / "followups" / "routes.py").read_text()
        timeline_source = (ROOT / "plg_core" / "timeline" / "service.py").read_text()
        self.assertIn('@router.post("/jobs/{job_id}/follow-ups")', followup_source)
        self.assertRegex(legacy_source, re.compile(r'@app\.get\(\s*"/follow-up"', re.S))
        self.assertIn("INSERT INTO job_timeline", timeline_source)

    def test_empty_quote_tray_stays_compact(self):
        self.assertIn('<div class="cc-empty">No parts selected yet.</div>', self.source)
        self.assertNotIn("Start by researching parts above.", self.source)

    def test_phone_layout_prevents_machine_navigation_overflow(self):
        self.assertRegex(
            self.source,
            re.compile(r"@media\(max-width:620px\).*?\.machine-navigation,\.machine-navigation\.bottom\{grid-template-columns:1fr\}", re.S),
        )
        self.assertIn("overflow-wrap:anywhere", self.source)
        self.assertIn("min-width:0;max-width:100%", self.source)

    def test_delivered_job_has_read_only_completion_presentation(self):
        self.assertIn('{% set delivered_job = job.status == "DELIVERED" %}', self.source)
        self.assertIn('{% if delivered_job %}', self.source)
        self.assertIn('{% if not delivered_job %}', self.source)
        for text in (
            "operational_snapshot.workflow.stage", "Asset / Equipment Context",
            "Job Delivered",
            "All purchased items have been delivered to the customer.",
            "Open Invoice", "View Quote History", "View Delivery History",
            "View Original Customer Request",
        ):
            self.assertIn(text, self.source)
        self.assertIn('{% if invoice and not delivered_job %}', self.source)
        self.assertIn("Asset / Equipment Context", self.source)
        self.assertIn("Requested Needs", self.source)

    def test_late_lifecycle_stage_precedes_invoice_presentation(self):
        self.assertIn(
            "{% set operator_stage = operational_snapshot.workflow.stage %}",
            self.source,
        )
        self.assertNotIn('{% set operator_stage = "DELIVERED" %}', self.source)
        self.assertNotIn('{% set operator_stage = "RECEIVED" %}', self.source)
        self.assertNotIn('{% set operator_stage = "ORDERED" %}', self.source)
        self.assertIn("{{ operational_snapshot.workflow.next_action }}", self.source)


if __name__ == "__main__":
    unittest.main()
