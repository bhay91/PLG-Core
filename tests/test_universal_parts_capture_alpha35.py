from __future__ import annotations

from contextlib import closing
from pathlib import Path
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import legacy_app
from plg_core.assets.service import add_job_asset
from plg_core.basket.service import get_basket, import_cart
from plg_core.database.migrations import run_migrations
from plg_core.research.service import create_requested_need
from plg_core.verification.service import start_extension_one_time_research, start_one_time_research


ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extensions/firefox/PLG-Firefox-Extension-v0.15-Connector-SDK"
FIXTURES = ROOT / "tests/fixtures/parts_capture"


class UniversalCaptureParserAlpha35Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def capture(self, fixture: str, url: str):
        page = self.browser.new_page()
        html = (FIXTURES / fixture).read_text()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(url)
        page.add_script_tag(path=str(EXT / "sdk.js"))
        result = page.evaluate("() => window.PLGConnectorSDK.readProductPage()")
        page.close()
        return result

    def test_site_profile_matrix_extracts_identifiers_titles_and_prices(self):
        cases = (
            ("amazon.html", "https://www.amazon.com/dp/B0TEST1234", "Amazon", "B0TEST1234", 1249.95),
            ("ebay.html", "https://www.ebay.com/itm/123456789012", "eBay", "81150-TEST", 349.50),
            ("fcp-euro.html", "https://www.fcpeuro.com/products/filter", "FCP Euro", "11427566327", 28.99),
            ("miami-star.html", "https://miamistar.com/products/air-dryer", "Miami Star", "MS-AD9-TEST", 89.75),
            ("oem-parts-online.html", "https://oempartsonline.com/parts/water-pump", "OEM Parts Online", "19200-TEST", 155.0),
        )
        for fixture, url, source, identifier, price in cases:
            with self.subTest(source=source):
                capture = self.capture(fixture, url)
                item = capture["items"][0]
                self.assertEqual(capture["source_name"], source)
                self.assertTrue(item["manufacturer_part_number"] or item["supplier_part_number"])
                self.assertIn(identifier, {item["manufacturer_part_number"], item["supplier_part_number"], item["asin"], item["listing_id"]})
                self.assertAlmostEqual(item["supplier_cost"], price)
                self.assertNotEqual(item["description"], "Identified Part")

    def test_generic_jsonld_meta_and_visible_label_fallbacks(self):
        jsonld = self.capture("generic-jsonld.html", "https://supplier.test/seal")
        item = jsonld["items"][0]
        self.assertEqual(item["supplier_part_number"], "SEAL-42")
        self.assertEqual(item["currency"], "CAD")
        self.assertEqual((item["weight"], item["weight_unit"]), (3.2, "lb"))
        self.assertEqual((item["length"], item["width"], item["height"], item["dimension_unit"]), (10, 7, 2, "in"))

        meta = self.capture("generic-meta.html", "https://supplier.test/starter")
        self.assertEqual(meta["items"][0]["supplier_part_number"], "START-99")
        self.assertEqual(meta["currency"], "EUR")

        labels = self.capture("generic-labels.html", "https://supplier.test/filter")
        item = labels["items"][0]
        self.assertEqual(item["manufacturer_part_number"], "VF-100")
        self.assertEqual(item["supplier_cost"], 17.5)
        self.assertEqual((item["length"], item["width"], item["height"], item["dimension_unit"]), (300, 200, 100, "mm"))

    def test_missing_optional_fields_do_not_break_capture(self):
        no_price = self.capture("id-no-price.html", "https://supplier.test/injector")["items"][0]
        self.assertEqual(no_price["supplier_part_number"], "INJ-55")
        self.assertIsNone(no_price["supplier_cost"])
        self.assertIsNone(no_price["weight"])
        self.assertIsNone(no_price["length"])

        needs_manual_id = self.capture("price-no-id.html", "https://supplier.test/filter")["items"][0]
        self.assertFalse(needs_manual_id["manufacturer_part_number"])
        self.assertFalse(needs_manual_id["supplier_part_number"])
        self.assertEqual(needs_manual_id["supplier_cost"], 19.95)
        self.assertEqual(needs_manual_id["description"], "Unnumbered Filter")

        empty = self.capture("no-product.html", "https://supplier.test/")["items"][0]
        self.assertFalse(empty["supplier_part_number"])
        self.assertEqual(empty["description"], "Welcome to Supplier")

        popup = (EXT / "popup.js").read_text()
        self.assertIn("Part number / SKU / ASIN / listing ID", popup)
        self.assertIn("item.asin", popup)
        self.assertIn("item.listing_id", popup)
        self.assertIn("Supplier cost", popup)
        self.assertIn("Quantity", popup)

    def test_weight_units_and_package_dimensions_are_normalized(self):
        amazon = self.capture("amazon.html", "https://www.amazon.com/dp/B0TEST1234")["items"][0]
        self.assertEqual((amazon["weight"], amazon["weight_unit"]), (2.4, "lb"))
        self.assertEqual((amazon["length"], amazon["width"], amazon["height"], amazon["dimension_unit"]), (12, 8, 4, "in"))
        miami = self.capture("miami-star.html", "https://miamistar.com/products/a")["items"][0]
        self.assertEqual((miami["weight"], miami["weight_unit"]), (1.1, "kg"))

    def test_cat_sis_dedicated_adapter_fields_remain_intact(self):
        html = """<table><tr><td><a href='#/refine?' data-track-attr-context='Part Table'>1R-0750</a></td><td>Fuel Filter</td><td><input class='quantity-input' value='2'></td><td><span class='priceText'>$42.50</span><span class='findme-label'>In Stock</span></td></tr></table>"""
        page = self.browser.new_page()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto("https://sis2.cat.com/#/cart")
        page.add_script_tag(path=str(EXT / "sdk.js"))
        page.add_script_tag(path=str(EXT / "connectors.js"))
        result = page.evaluate("() => window.PLGConnectors.detectConnector().readCart()")
        page.close()
        item = result["items"][0]
        self.assertEqual(result["source_name"], "CAT SIS")
        self.assertEqual(item["manufacturer_part_number"], "1R-0750")
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(item["supplier_cost"], 42.5)


class UniversalCaptureBackendAlpha35Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-alpha35-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as connection:
            customer = connection.execute("INSERT INTO customers(customer_number,name,active) VALUES ('A35-C','Synthetic Capture Customer',1)").lastrowid
            self.job_id = int(connection.execute("INSERT INTO jobs(job_number,created_date,customer_id,customer,status) VALUES ('A35-J','2026-08-11',?,'Synthetic Capture Customer','REQUESTED')", (customer,)).lastrowid)
            connection.commit()
        self.asset = add_job_asset(self.job_id, manufacturer="CAT", model="420D", vin_pin_serial="A35-CAT", asset_type="Heavy Equipment", make_primary=True)
        revision = get_basket(self.job_id)["work_revision"]
        self.need = create_requested_need(self.job_id, job_asset_id=self.asset["id"], wording="Fuel Filter", expected_revision_id=revision["id"], expected_version=revision["lock_version"])

    def tearDown(self):
        self.patch.stop(); self.temp.cleanup()

    def test_additive_fields_map_to_research_result_and_existing_shipping_data(self):
        revision = get_basket(self.job_id)["work_revision"]
        start_one_time_research(self.job_id, self.asset["id"], "https://example.com/filter", requested_need_id=self.need["id"], expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        result = import_cart(self.job_id, {
            "source_key":"one_time_website", "source_name":"One-time Website", "source_url":"https://example.com/filter", "trust_level":"NEEDS_REVIEW",
            "capture_mode":"MERGED",
            "items":[{"description":"Fuel Filter","sku":"SKU-42","asin":"ASIN-42","supplier_name":"Example Supplier","supplier_cost":12.5,"quantity":2,"weight":2.4,"weight_unit":"lb","length":12,"width":8,"height":4,"dimension_unit":"in","product_page_url":"https://example.com/filter","cart_page_url":"https://example.com/cart","evidence":"Package dimensions on product page","evidence_fields":{"weight":"Package weight label"}}]
        })
        self.assertEqual(result["imported_count"], 1)
        with closing(legacy_app.get_connection()) as connection:
            item = connection.execute("SELECT * FROM basket_items WHERE basket_id=(SELECT id FROM baskets WHERE job_id=?)", (self.job_id,)).fetchone()
            shipping = connection.execute("SELECT * FROM part_shipping_data WHERE basket_item_id=? AND is_current=1", (item["id"],)).fetchone()
            sources = connection.execute("SELECT COUNT(*) FROM connector_profiles WHERE lower(display_name)='example supplier'").fetchone()[0]
        self.assertEqual(item["supplier_part_number"], "SKU-42")
        self.assertEqual(item["supplier_name"], "Example Supplier")
        self.assertEqual(item["research_state"], "RESEARCH_RESULT")
        self.assertEqual(item["selected"], 0)
        self.assertEqual(item["primary_requested_need_id"], self.need["id"])
        self.assertIn("Package dimensions", item["research_evidence"])
        self.assertIn("Capture mode: MERGED", item["research_evidence"])
        self.assertIn("Source: One-time Website", item["research_evidence"])
        self.assertIn("Product page: https://example.com/filter", item["research_evidence"])
        self.assertIn("Cart page: https://example.com/cart", item["research_evidence"])
        self.assertIn("SKU: SKU-42", item["research_evidence"])
        self.assertIn("ASIN: ASIN-42", item["research_evidence"])
        self.assertIn("Field evidence:", item["research_evidence"])
        self.assertEqual((shipping["unit_weight"], shipping["weight_unit"]), (2.4, "lb"))
        self.assertEqual((shipping["length"], shipping["width"], shipping["height"], shipping["dimension_unit"]), (12,8,4,"in"))
        self.assertEqual(sources, 0)


if __name__ == "__main__":
    unittest.main()
