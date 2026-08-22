import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "accounting.html"


class AccountingLanguageUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = TEMPLATE.read_text()

    def test_operator_facing_summary_labels_and_explanations(self):
        expected_copy = (
            "Supplier Order Cost",
            "Final Actual Cost",
            "Expected Profit",
            "Supplier Order Profit",
            "Final Profit",
            "Profit Difference",
            "What we expected the parts to cost when the customer was quoted/invoiced.",
            "What the supplier orders were placed for.",
            "What we ultimately confirmed the parts actually cost.",
            "Customer sale minus estimated cost.",
            "Customer sale minus supplier order cost.",
            "Customer sale minus final actual cost.",
            "Final profit compared with expected profit.",
        )
        for copy in expected_copy:
            with self.subTest(copy=copy):
                self.assertIn(copy, self.template)

    def test_reconciliation_headers_and_mobile_labels_match(self):
        labels = (
            "Invoice / Job",
            "Revenue",
            "Estimated Cost",
            "Supplier Order Cost",
            "Final Actual Cost",
            "Expected Profit",
            "Supplier Order Profit",
            "Final Profit",
            "Cost Difference",
            "Profit Difference",
            "Status / Orders",
        )
        header = self.template.split("<thead><tr>", 1)[1].split("</tr></thead>", 1)[0]
        for label in labels:
            with self.subTest(label=label):
                self.assertIn(f"<th>{label}</th>", header)
        for label in labels[1:]:
            with self.subTest(mobile_label=label):
                self.assertIn(f'data-label="{label}"', self.template)

    def test_unconfirmed_presentation_and_internal_contract_are_preserved(self):
        self.assertIn('row.actual_cost_state == "NOT_CONFIRMED" %}NOT CONFIRMED', self.template)
        self.assertIn('row.actual_cost_state == "NOT_CONFIRMED" %}—', self.template)
        for internal_name in (
            "row.booked_supplier_cost",
            "row.placed_supplier_cost",
            "row.actual_supplier_cost",
            "row.expected_profit",
            "row.placed_cost_profit",
            "row.actual_profit",
            "row.cost_variance",
            "row.profit_variance",
        ):
            with self.subTest(internal_name=internal_name):
                self.assertIn(internal_name, self.template)
        self.assertIn("/purchasing/orders/{{ order.id }}", self.template)
        self.assertIn("{{ row.invoice_url }}", self.template)
        self.assertIn("{{ row.job_url }}", self.template)

    def test_narrow_view_uses_existing_card_layout(self):
        self.assertIn("@media(max-width:1100px)", self.template)
        self.assertIn(".accounting-reconciliation-table thead{display:none}", self.template)
        self.assertIn("content:attr(data-label)", self.template)
        self.assertIn("grid-template-columns:minmax(140px,.7fr) minmax(0,1fr)", self.template)
        self.assertIn("grid-template-columns:minmax(105px,.7fr) minmax(0,1fr)", self.template)
        self.assertIn("overflow-wrap:anywhere", self.template)


if __name__ == "__main__":
    unittest.main()
