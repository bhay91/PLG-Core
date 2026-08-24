from pathlib import Path
import re
import unittest

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]


class InvoiceCardActionPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "invoices.html").read_text()
        cls.css = (ROOT / "static" / "app.css").read_text()

    def test_paid_actions_keep_labels_and_routes(self):
        for label in (
            "Open Invoice", "Internal Parts Order Worksheet",
            "Customer PDF — Paid", "Edit Custom Invoice",
            "Internal PDF — Current", "Custom Invoice",
        ):
            self.assertIn(label, self.template)
        for route in (
            "/documents", "/parts-order-sheet/pdf", "/customer/paid-pdf",
            "/custom", "/internal/paid-pdf", "/custom/pdf",
        ):
            self.assertIn(route, self.template)

        document_workspace = (ROOT / "templates" / "invoice_documents.html").read_text()
        self.assertIn("Internal Invoice — Current", document_workspace)
        self.assertIn("Previous versions remain historical.", document_workspace)
        self.assertIn("?v={{ invoice.internal_document_version }}", self.template)
        self.assertIn("?v={{ internal_document_version }}", document_workspace)

    def test_next_action_has_dedicated_scoped_panel(self):
        self.assertIn("invoice-action-panel", self.template)
        self.assertIn("invoice-secondary-actions", self.template)
        self.assertIn("<summary>More Actions</summary>", self.template)
        self.assertRegex(
            self.css,
            r"\.invoice-row\s*\{[\s\S]*?minmax\(260px,\.85fr\)",
        )
        self.assertRegex(
            self.css,
            r"@media \(max-width: 1200px\)[\s\S]*?\.invoice-action-panel\s*\{[\s\S]*?grid-column: 2 / -1",
        )

    def test_open_invoice_is_primary_and_secondary_controls_are_disclosed(self):
        paid = self.template.index('{% if invoice.status == "PAID" %}')
        primary = self.template.index("workspace-btn workspace-btn-primary", paid)
        disclosure = self.template.index("invoice-secondary-actions", paid)
        worksheet = self.template.index("Internal Parts Order Worksheet", disclosure)
        self.assertLess(primary, disclosure)
        self.assertGreater(worksheet, disclosure)

    def test_two_columns_are_equal_and_shrink_safely(self):
        self.assertIn("container-type: inline-size", self.css)
        self.assertRegex(
            self.css,
            r"@container invoice-actions \(min-width: 420px\)[\s\S]*?grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)",
        )

    def test_main_stylesheet_is_cache_versioned(self):
        base = (ROOT / "templates" / "base.html").read_text()
        self.assertIn("app.css') }}?v=invoice-action-grid-v2", base)

    def test_button_text_wraps_without_clipping_or_ellipsis(self):
        rule = re.search(
            r"\.invoice-paid-grid \.workspace-btn\s*\{([^}]*)\}", self.css
        ).group(1)
        for declaration in (
            "min-width: 0", "min-height: 48px", "white-space: normal",
            "text-overflow: clip", "overflow-wrap: anywhere",
        ):
            self.assertIn(declaration, rule)
        self.assertNotIn("overflow: hidden", rule)

    def test_phone_layout_is_one_column(self):
        self.assertRegex(
            self.css,
            r"@media \(max-width: 720px\)[\s\S]*?\.invoice-paid-grid\s*\{\s*grid-template-columns: minmax\(0, 1fr\)",
        )

    def test_template_parses(self):
        Environment().parse(self.template)


if __name__ == "__main__":
    unittest.main()
