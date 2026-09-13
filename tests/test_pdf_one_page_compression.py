from pathlib import Path
import subprocess
import tempfile
import unittest
from xml.etree import ElementTree

from plg_core.documents.invoice_pdf import (
    build_invoice_pdf,
    generate_custom_invoice_pdf,
)
from plg_core.documents.pdf_fit import page_count
from plg_core.documents.quote_pdf import build_quote_pdf


def fixture_document():
    base = {
        "quote_number": "FIT-Q-0001",
        "invoice_number": "FIT-INV-0001",
        "quote_date": "2026-08-14",
        "invoice_date": "2026-08-14",
        "status": "UNPAID",
        "customer": "PDF Layout Customer",
        "company": "PDF Layout Equipment Company",
        "address": "12345 Industrial Equipment Boulevard, Pompano Beach, FL 33069",
        "phone": "555-0100",
        "email": "layout@example.test",
        "manufacturer": "Caterpillar",
        "machine": "420D Backhoe Loader",
        "year": "2004",
        "pin_serial": "TEST-PIN-PDF-001",
        "parts_subtotal": 1000,
        "shipping_total": 75,
        "sourcing_fee": 25,
        "service_charge": 50,
        "customer_total": 1150,
        "supplier_total": 700,
        "profit_total": 450,
        "balance_due": 1150,
        "credit_applied": 0,
    }
    return base


def fixture_items(count: int, *, long: bool = False):
    rows = []
    for index in range(1, count + 1):
        rows.append({
            "quantity": index % 3 + 1,
            "supplier_part_number": (
                f"LONG-SUPPLIER-PART-NUMBER-{index:02d}-ABCDEFGHIJKLMN"
                if long else f"PART-{index:04d}"
            ),
            "internal_part_number": f"PPS-{index:04d}",
            "description": (
                "Long replacement component description with application, "
                "fitment, and installation identification details. " * 2
                if long else f"Replacement equipment component {index}"
            ),
            "customer_unit_price": 100,
            "customer_line_total": 100,
            "supplier_name": "Layout Supplier",
            "supplier_line_total": 65,
            "line_profit": 35,
        })
    return rows


def pps_q_0009_style_items():
    rows = fixture_items(7)
    for index, row in enumerate(rows):
        if index < 5:
            row.update({
                "job_asset_id": 7,
                "asset_name_snapshot": "Caterpillar 420D Backhoe Loader",
                "asset_type_snapshot": "machine",
                "asset_manufacturer_snapshot": "Caterpillar",
                "asset_model_snapshot": "420D Backhoe Loader",
                "asset_serial_snapshot": "TEST-PIN-PDF-001",
            })
        else:
            row.update({
                "job_asset_id": 8,
                "asset_name_snapshot": "Flatbed",
                "asset_type_snapshot": "Truck",
                "asset_manufacturer_snapshot": "International",
                "asset_model_snapshot": "4700 4×2",
                "asset_serial_snapshot": "TEST-VIN-PDF-002",
            })
    rows[4]["description"] = "Hydraulic Cylinder Seal Kit - Standard Size"
    return rows


def text(path: Path) -> str:
    return subprocess.run(
        ["pdftotext", str(path), "-"], check=True,
        capture_output=True, text=True,
    ).stdout


def assert_boxes_inside_page(test: unittest.TestCase, path: Path):
    xml = subprocess.run(
        ["pdftotext", "-bbox", str(path), "-"], check=True,
        capture_output=True, text=True,
    ).stdout
    root = ElementTree.fromstring(xml)
    words = root.findall(".//{*}word")
    test.assertTrue(words)
    for word in words:
        test.assertGreaterEqual(float(word.attrib["xMin"]), 0)
        test.assertGreaterEqual(float(word.attrib["yMin"]), 0)
        test.assertLessEqual(float(word.attrib["xMax"]), 612.1)
        test.assertLessEqual(float(word.attrib["yMax"]), 792.1)


class PDFOnePageCompressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-pdf-fit-")
        self.root = Path(self.temp.name)
        self.document = fixture_document()

    def tearDown(self):
        self.temp.cleanup()

    def test_normal_customer_and_internal_quote_are_one_page(self):
        for internal in (False, True):
            path = self.root / f"quote-{'internal' if internal else 'customer'}.pdf"
            level, pages = build_quote_pdf(
                self.document, fixture_items(6), path, internal
            )
            self.assertEqual(pages, 1)
            self.assertLessEqual(level, 2)
            body = text(path)
            self.assertIn("FIT-Q-0001", body)
            self.assertIn("PART-0006", body)
            assert_boxes_inside_page(self, path)

    def test_normal_customer_and_internal_invoice_are_one_page(self):
        for internal in (False, True):
            path = self.root / f"invoice-{'internal' if internal else 'customer'}.pdf"
            level, pages = build_invoice_pdf(
                self.document, fixture_items(6), path, internal
            )
            self.assertEqual(pages, 1)
            self.assertLessEqual(level, 2)
            body = text(path)
            self.assertIn("FIT-INV-0001", body)
            self.assertIn("PART-0006", body)
            assert_boxes_inside_page(self, path)

    def test_pps_q_0009_style_quote_and_invoice_keep_payment_and_totals_on_page_one(self):
        document = dict(self.document)
        document.update({
            "quote_number": "PPS-Q-9009-TEST",
            "invoice_number": "PPS-INV-9009-TEST",
            "customer": "Synthetic PDF Customer",
            "address": "Test City",
            "manufacturer": "Multiple",
            "machine": "Job Assets",
            "parts_subtotal": 850,
            "shipping_total": 0,
            "service_charge": 150,
            "sourcing_fee": 0,
            "customer_total": 1000,
        })
        items = pps_q_0009_style_items()
        for name, builder, internal in (
            ("customer-quote", build_quote_pdf, False),
            ("internal-quote", build_quote_pdf, True),
            ("customer-invoice", build_invoice_pdf, False),
            ("internal-invoice", build_invoice_pdf, True),
        ):
            path = self.root / f"pps-q-0009-style-{name}.pdf"
            level, pages = builder(document, items, path, internal)
            self.assertEqual(pages, 1)
            self.assertLessEqual(level, 3)
            body = text(path)
            self.assertIn("PAYMENT INFORMATION", body)
            if internal:
                self.assertIn("Service Charge", body)
            elif name == "customer-quote":
                self.assertIn("Service charge", body)
                self.assertIn("Sourcing fee", body)
                self.assertIn("$1,000.00", body)
            if name == "internal-invoice":
                self.assertIn("Revenue", body)
                self.assertIn("Expected Profit", body)
                self.assertIn("Final Profit", body)
            elif internal:
                self.assertIn("Customer Total", body)
                self.assertIn("NET PROFIT", body)
            elif name == "customer-quote":
                self.assertIn("Parts subtotal", body)
                self.assertIn("TOTAL", body)
            else:
                self.assertIn("Subtotal", body)
            assert_boxes_inside_page(self, path)

    def test_custom_invoice_is_one_page(self):
        path = self.root / "custom.pdf"
        custom = {"custom_invoice_number": "CUSTOM-FIT-1", "custom_total": 600}
        items = []
        for item in fixture_items(6):
            item = dict(item)
            item["custom_unit_price"] = item["customer_unit_price"]
            item["custom_line_total"] = item["customer_line_total"]
            items.append(item)
        generate_custom_invoice_pdf(self.document, custom, items, path)
        self.assertEqual(page_count(path), 1)
        body = text(path)
        self.assertIn("CUSTOM-FIT-1", body)
        self.assertIn("Subtotal", body)
        self.assertIn("Invoice Total", body)
        self.assertIn("BALANCE DUE", body)
        self.assertIn("PAID IN FULL", body)
        self.assertIn("$600.00", body)
        self.assertNotIn("Service Charge", body)
        self.assertNotIn("Sourcing Fee", body)
        self.assertEqual(self.document["service_charge"], 50)
        self.assertEqual(self.document["sourcing_fee"], 25)
        assert_boxes_inside_page(self, path)

    def test_custom_invoice_one_three_six_and_ten_item_fixtures_fit(self):
        for count in (1, 3, 6, 10):
            path = self.root / f"custom-{count}.pdf"
            custom = {
                "custom_invoice_number": f"CUSTOM-FIT-{count}",
                "custom_total": count * 100,
            }
            items = []
            for item in fixture_items(count):
                item = dict(item)
                item["custom_unit_price"] = item["customer_unit_price"]
                item["custom_line_total"] = item["customer_line_total"]
                items.append(item)
            generate_custom_invoice_pdf(
                self.document, custom, items, path
            )
            self.assertEqual(page_count(path), 1)
            assert_boxes_inside_page(self, path)

    def test_one_three_six_and_ten_item_fixtures_fit(self):
        for count in (1, 3, 6, 10):
            for name, builder, internal in (
                ("customer-quote", build_quote_pdf, False),
                ("internal-quote", build_quote_pdf, True),
                ("customer-invoice", build_invoice_pdf, False),
                ("internal-invoice", build_invoice_pdf, True),
            ):
                path = self.root / f"{name}-{count}.pdf"
                _level, pages = builder(
                    self.document, fixture_items(count), path, internal
                )
                self.assertEqual(pages, 1, f"{name} with {count} items")

    def test_long_description_and_part_number_fit_without_clipping(self):
        path = self.root / "long-content.pdf"
        _level, pages = build_invoice_pdf(
            self.document, fixture_items(6, long=True), path, False
        )
        self.assertEqual(pages, 1)
        body = text(path)
        self.assertIn("LONG-SUPPLIER-PA", body)
        self.assertIn("RT-NUMBER", body)
        self.assertIn("installation identification", body)
        assert_boxes_inside_page(self, path)

    def test_genuinely_large_documents_are_allowed_two_pages(self):
        path = self.root / "large.pdf"
        level, pages = build_quote_pdf(
            self.document, fixture_items(24, long=True), path, True
        )
        self.assertEqual(level, 3)
        self.assertGreaterEqual(pages, 2)
        self.assertIn("PART", text(path))

    def test_customer_internal_separation_remains_locked(self):
        customer = self.root / "customer.pdf"
        internal = self.root / "internal.pdf"
        build_invoice_pdf(self.document, fixture_items(3), customer, False)
        build_invoice_pdf(self.document, fixture_items(3), internal, True)
        customer_text = text(customer).upper()
        internal_text = text(internal).upper()
        for forbidden in (
            "ESTIMATED COST", "SUPPLIER ORDER COST", "FINAL ACTUAL COST",
            "EXPECTED PROFIT", "FINAL PROFIT", "INTERNAL USE ONLY",
        ):
            self.assertNotIn(forbidden, customer_text)
        self.assertIn("ESTIMATED COST", internal_text)
        self.assertIn("SUPPLIER ORDER COST", internal_text)
        self.assertIn("FINAL ACTUAL COST", internal_text)
        self.assertIn("EXPECTED PROFIT", internal_text)
        self.assertIn("FINAL PROFIT", internal_text)

    def test_logo_geometry_preserves_source_aspect_ratio(self):
        from plg_core.documents.invoice_pdf import _logo, _logo_image

        source = _logo()
        self.assertIsNotNone(source)
        image = _logo_image(source)
        source_ratio = image.imageWidth / image.imageHeight
        rendered_ratio = image.drawWidth / image.drawHeight
        self.assertAlmostEqual(source_ratio, rendered_ratio, places=5)


if __name__ == "__main__":
    unittest.main()
