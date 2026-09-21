from pathlib import Path
import unittest

from jinja2 import Environment, FileSystemLoader
from plg_core.research.branding import manufacturer_identity


ROOT = Path(__file__).resolve().parents[1]


class InvoiceReviewPhase34Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "invoice_documents.html").read_text(encoding="utf-8")
        cls.invoices_template = (ROOT / "templates" / "invoices.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        cls.env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
        cls.env.globals["url_for"] = lambda name, path="": path
        cls.env.globals["manufacturer_identity"] = manufacturer_identity

    def render(self, status="UNPAID", *, balance=125.0, orders=()):
        invoice = {
            "id": 31, "invoice_number": "PPS-INV-0031", "status": status,
            "job_id": 17, "job_number": "PPS-J-9017", "customer": "Synthetic Invoice Customer",
            "company": "Synthetic Equipment Company", "address": "Test City",
            "manufacturer": "CAT", "machine": "420D", "pin_serial": "TEST-PIN-INVOICE-001",
            "parts_subtotal": 100.0, "shipping_total": 10.0,
            "sourcing_fee": 5.0, "service_charge": 10.0,
            "customer_total": 125.0, "balance_due": balance,
            "credit_applied": 0, "invoice_date": "2026-08-13", "paid_date": None,
        }
        item = {
            "quantity": 2, "supplier_part_number": "DER-93592",
            "internal_part_number": "PPS-MAN-1", "description": "Starter",
            "customer_unit_price": 50.0, "customer_line_total": 100.0,
        }
        return self.env.get_template("invoice_documents.html").render(
            invoice=invoice, items=[item], payments=[], invoice_events=[],
            payment_total=0, today="2026-08-13", supplier_orders=list(orders),
            custom_invoice_exists=False, parts_order_sheet_exists=False,
            can_void_invoice=status not in {"VOID"}, void_block_reason="",
            active_page="invoices", customer_document_version=1,
            internal_document_version=2,
        )

    def test_operator_hierarchy_and_compact_identity(self):
        html = self.render()
        positions = [html.index("invoice-desk-header"), html.index("invoice-desk-bill-to"),
                     html.index("invoice-center-items"), html.index("invoice-desk-totals"),
                     html.index("invoice-desk-payment"), html.index("invoice-desk-documents")]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("invoice-center-identity-grid", self.template)
        self.assertIn("Job {{ invoice.job_number }}", self.template)

    def test_number_status_bill_to_items_and_persisted_totals_render(self):
        html = self.render()
        for value in ("PPS-INV-0031", "UNPAID", "Synthetic Invoice Customer", "Synthetic Equipment Company",
                      "Test City", "DER-93592", "Starter", "$50.00", "$100.00",
                      "Parts", "Shipping", "Sourcing Fee", "Service Charge", "$125.00"):
            self.assertIn(value, html)
        for label in ("Qty", "Item / Reference", "Description", "Customer Price", "Line Total"):
            self.assertIn(f"<th>{label}</th>", html)

    def test_unpaid_and_partial_offer_record_payment(self):
        for status, balance in (("UNPAID", 125), ("PARTIAL", 40)):
            html = self.render(status, balance=balance)
            self.assertIn("Record Payment", html)
            self.assertIn('action="/invoices/31/payments"', html)
            self.assertIn(f'value="{balance:.2f}"', html)
            self.assertNotIn("Create Supplier Order", html)

    def test_paid_invoice_offers_supplier_orders_and_existing_orders_open(self):
        fresh = self.render("PAID", balance=0)
        self.assertIn("Create Supplier Order", fresh)
        self.assertIn('action="/invoices/31/supplier-orders"', fresh)
        existing = self.render("PAID", balance=0, orders=({"id": 9},))
        self.assertIn("Open Supplier Order", existing)
        self.assertIn('href="/purchasing?invoice_id=31"', existing)

    def test_payment_precedes_optional_jmd_controls(self):
        html = self.render("UNPAID", balance=125)
        self.assertLess(html.index('id="record-payment"'), html.index("invoice-jmd-display"))
        self.assertIn("show_jmd_total", html)
        self.assertIn("jmd_exchange_rate", html)

    def test_void_has_no_payment_or_purchasing_action(self):
        html = self.render("VOID", balance=125)
        self.assertIn("Historical · No commercial action", html)
        self.assertNotIn('action="/invoices/31/payments"', html)
        self.assertNotIn('action="/invoices/31/supplier-orders"', html)

    def test_customer_and_internal_document_routes_are_preserved(self):
        html = self.render()
        self.assertIn("Customer Invoice", html)
        self.assertIn("Internal Invoice", html)
        for path in ("/invoices/31/customer/pdf", "/invoices/31/customer/pdf?download=1",
                     "/invoices/31/internal/pdf", "/invoices/31/internal/pdf?download=1"):
            self.assertIn(path, html)
        self.assertIn("customer/pdf?v=1", html)
        self.assertIn("internal/pdf?v=2", html)

    def test_paid_invoice_uses_paid_manifest_document_routes(self):
        html = self.render("PAID", balance=0)
        self.assertIn("Customer Invoice — Paid", html)
        self.assertIn("Internal Invoice — Current", html)
        for path in (
            "/invoices/31/customer/paid-pdf",
            "/invoices/31/customer/paid-pdf?download=1&amp;v=1",
            "/invoices/31/internal/paid-pdf",
            "/invoices/31/internal/paid-pdf?download=1&amp;v=2",
        ):
            self.assertIn(path, html)
        self.assertIn("Customer PDF — Paid", self.invoices_template)
        self.assertIn(
            "/customer/paid-pdf?v={{ invoice.customer_document_version }}",
            self.invoices_template,
        )
        self.assertIn(
            "/internal/paid-pdf?v={{ invoice.internal_document_version }}",
            self.invoices_template,
        )

    def test_history_refund_void_and_extras_remain_secondary(self):
        for label in ("Payment History", "Invoice History", "Reverse / Refund Payment",
                      "Invoice Administration", "Additional Invoice &amp; Purchasing Documents",
                      "Internal Parts Order Worksheet", "Custom Paid Invoice"):
            self.assertIn(label, self.template)
        self.assertIn('action="/invoices/{{ invoice.id }}/void"', self.template)
        self.assertIn('action="/invoices/{{ invoice.id }}/payments/{{ payment.id }}/reverse"', self.template)

    def test_responsive_contract_has_no_forced_horizontal_scroller(self):
        self.assertIn("@media (max-width: 1100px)", self.css)
        self.assertIn("@media (max-width: 680px)", self.css)
        self.assertIn(".invoice-document-row > div:first-child { min-width: 0; }", self.css)
        self.assertIn(
            ".invoice-desk .invoice-document-buttons { flex: 0 1 auto; "
            "grid-template-columns: repeat(2, minmax(0, 1fr)); min-width: 0; }",
            self.css,
        )
        self.assertIn(
            ".invoice-payment-record { align-items: stretch; flex-direction: column; min-width: 0; }",
            self.css,
        )
        self.assertIn("word-break: break-word", self.css)
        self.assertIn("table-layout: fixed", self.css)
        self.assertIn("overflow-wrap: anywhere", self.css)
        self.assertIn(".invoice-payment-inline { grid-template-columns: 1fr; }", self.css)


if __name__ == "__main__":
    unittest.main()
