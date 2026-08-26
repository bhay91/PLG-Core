from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "quote_documents.html"
CSS = ROOT / "static" / "app.css"


class QuoteReviewPhase33Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE.read_text(encoding="utf-8")
        cls.css = CSS.read_text(encoding="utf-8")

    def test_operator_hierarchy_and_compact_context(self):
        positions = [
            self.template.index('class="quote-review-heading"'),
            self.template.index('class="panel quote-review-command"'),
            self.template.index('class="panel quote-review-items"'),
            self.template.index('class="panel quote-review-total-card"'),
            self.template.index('class="panel quote-review-documents"'),
            self.template.index('class="panel quote-review-history"'),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Job {{ quote.job_number }}", self.template)
        self.assertNotIn("quote-review-identity-grid", self.template)
        self.assertNotIn("COMMERCIAL IDENTITY", self.template)

    def test_bill_to_summary_and_draft_editor_contract(self):
        self.assertIn("BILL TO", self.template)
        self.assertIn("Change Bill To", self.template)
        self.assertIn('action="/quotes/{{ quote.id }}/bill-to"', self.template)
        for field in ("bill_to_kind", "bill_to_name", "bill_to_company", "bill_to_address"):
            self.assertIn(f'name="{field}"', self.template)

    def test_lifecycle_actions_keep_existing_post_routes(self):
        expected = {
            "/mark-sent": "Mark Quote as Sent",
            "/decision": "Record Customer Approval",
            "/revise": "Revise Quote",
            "/convert-to-invoice": "Create Invoice",
        }
        for suffix, label in expected.items():
            self.assertIn(f'action="/quotes/{{{{ quote.id }}}}{suffix}"', self.template)
            self.assertIn(label, self.template)
        self.assertIn('value="REVISION_REQUIRED"', self.template)
        self.assertIn("Customer Requested Changes", self.template)
        self.assertIn('value="REJECTED"', self.template)
        self.assertIn("Reject Quote", self.template)

    def test_items_total_and_document_contract(self):
        for label in ("Qty", "Item / Reference", "Description", "Customer Price", "Line Total"):
            self.assertIn(f"<th>{label}</th>", self.template)
        self.assertNotIn("<th>Type</th>", self.template)
        for label in ("Parts", "Shipping", "Sourcing Fee", "Service Charge", "Customer Total"):
            self.assertIn(label, self.template)
        for path in ("customer/pdf", "customer/pdf?download=1", "internal/pdf", "internal/pdf?download=1"):
            self.assertIn(path, self.template)

    def test_history_and_exception_controls_are_secondary(self):
        self.assertIn('<details class="panel quote-review-history">', self.template)
        self.assertIn("No quote history yet.", self.template)
        self.assertIn('<details class="panel quote-review-advanced">', self.template)
        self.assertIn('action="/quotes/{{ quote.id }}/split"', self.template)

    def test_responsive_no_overflow_contract(self):
        self.assertIn("@media (max-width: 1000px)", self.css)
        self.assertIn("@media (max-width: 680px)", self.css)
        self.assertIn(".quote-review-table { table-layout: fixed; overflow-wrap: anywhere; }", self.css)
        self.assertIn(".quote-primary-actions { width: 100%; flex-direction: column; }", self.css)


if __name__ == "__main__":
    unittest.main()
