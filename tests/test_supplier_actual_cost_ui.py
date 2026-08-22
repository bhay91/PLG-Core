from pathlib import Path
import re
import unittest

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]


class SupplierActualCostPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "supplier_order_detail.html").read_text()
        cls.css = (ROOT / "static" / "app.css").read_text()
        cls.invoice_template = (ROOT / "templates" / "invoice_documents.html").read_text()

    def test_actual_cost_uses_cards_not_a_wide_table(self):
        section = self.template.split('id="actual-cost"', 1)[1].split(
            '<section class="panel supplier-order-receiving"', 1
        )[0]
        self.assertIn('class="actual-cost-list"', section)
        self.assertIn('class="actual-cost-card"', section)
        self.assertNotIn("<table", section)

    def test_item_and_shipping_forms_keep_required_fields(self):
        self.assertGreaterEqual(self.template.count('class="actual-cost-form"'), 2)
        for field in ('name="new_amount"', 'name="reason"', 'name="supplier_reference"', 'name="request_id_value"', 'name="csrf_token"'):
            self.assertGreaterEqual(self.template.count(field), 2)
        self.assertGreaterEqual(self.template.count('name="reason" maxlength="1000"'), 2)

    def test_actual_cost_labels_and_full_width_form_regions_exist(self):
        for label in ("Placed Unit Cost", "Actual Unit Cost", "Variance", "Status", "Reason", "Supplier Reference", "Record Actual Cost"):
            self.assertIn(label, self.template)
        for css_class in ("actual-cost-input", "actual-cost-reason", "actual-cost-reference", "actual-cost-submit"):
            self.assertRegex(self.template, rf'class="[^"]*\b{css_class}\b')

    def test_responsive_cards_stack_without_horizontal_table_scroll(self):
        self.assertRegex(self.css, r"@media \(max-width: 680px\)[\s\S]*?\.actual-cost-card,[\s\S]*?grid-template-columns: minmax\(0, 1fr\)")
        self.assertIn(".actual-cost-form input:not([type=\"hidden\"])", self.css)
        self.assertIn("min-width: 0;", self.css)
        self.assertNotIn("actual-cost-list { overflow-x: auto", self.css)

    def test_global_topbar_participates_in_layout_and_page_has_clearance(self):
        final_topbar = list(re.finditer(r"\.topbar\s*\{([^}]*)\}", self.css))[-1].group(1)
        self.assertIn("position: static", final_topbar)
        self.assertRegex(self.css, r"\.page\s*\{[^}]*padding-bottom: max\(24px, env\(safe-area-inset-bottom\)\)")

    def test_supplier_tables_are_contained_inside_their_panels(self):
        self.assertIn(".supplier-order-desk > .panel", self.css)
        self.assertIn(".supplier-order-desk .invoice-center-table-wrap", self.css)
        self.assertIn("overscroll-behavior-inline: contain", self.css)

    def test_invoice_action_button_grid_is_preserved(self):
        self.assertIn('class="invoice-document-buttons"', self.invoice_template)
        self.assertIn(".invoice-document-buttons { display: grid; grid-template-columns: 1fr 1fr; width: 100%; }", self.css)
        self.assertIn(".invoice-desk-header-actions", self.css)

    def test_templates_parse(self):
        Environment().parse(self.template)
        Environment().parse(self.invoice_template)


if __name__ == "__main__":
    unittest.main()
