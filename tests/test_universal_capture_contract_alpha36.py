from __future__ import annotations

from pathlib import Path
import json
import re
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extensions/firefox/PLG-Firefox-Extension-v0.15-Connector-SDK"
FIXTURES = ROOT / "tests/fixtures/parts_capture"


class UniversalCaptureContractAlpha36Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path="/usr/bin/chromium-browser", headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.playwright.stop()

    def page_capture(self, fixture, url):
        return self.capture(fixture, url, "readProductPage")

    def cart_capture(self, fixture, url):
        return self.capture(fixture, url, "readCartPage")

    def capture(self, fixture, url, method):
        page = self.browser.new_page()
        html = (FIXTURES / fixture).read_text()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(url)
        page.add_script_tag(path=str(EXT / "sdk.js"))
        result = page.evaluate(f"() => window.PLGConnectorSDK.{method}()")
        page.close()
        return result

    def evaluate_merge(self, page_capture, cart_capture):
        page = self.browser.new_page()
        page.set_content("<title>Merge</title>")
        page.add_script_tag(path=str(EXT / "sdk.js"))
        result = page.evaluate("([a,b]) => window.PLGConnectorSDK.mergeCaptures(a,b)", [page_capture, cart_capture])
        page.close()
        return result

    def test_every_page_profile_returns_universal_contract(self):
        cases = (
            ("amazon.html", "https://www.amazon.com/dp/B0TEST1234"),
            ("ebay.html", "https://www.ebay.com/itm/123456789012"),
            ("fcp-euro.html", "https://www.fcpeuro.com/products/filter"),
            ("miami-star.html", "https://miamistar.com/products/air-dryer"),
            ("oem-parts-online.html", "https://oempartsonline.com/parts/pump"),
            ("generic-jsonld.html", "https://supplier.test/seal"),
            ("generic-meta.html", "https://supplier.test/starter"),
            ("generic-labels.html", "https://supplier.test/filter"),
        )
        capture_fields = {"source_key","source_name","source_url","source_domain","capture_mode","configured_source","items"}
        item_fields = {
            "description","brand","manufacturer_part_number","supplier_part_number","sku","asin","listing_id","item_id",
            "supplier_cost","currency","quantity","availability","weight","weight_unit","weight_type",
            "length","width","height","dimension_unit","dimension_type","shipping_cost","shipping_method","lead_time",
            "fitment","warehouse","evidence","evidence_fields","product_page_url","cart_page_url"
        }
        for fixture, url in cases:
            with self.subTest(fixture=fixture):
                capture = self.page_capture(fixture, url)
                self.assertTrue(capture_fields <= capture.keys())
                self.assertEqual(capture["capture_mode"], "PAGE")
                self.assertTrue(item_fields <= capture["items"][0].keys())

    def test_cart_profiles_return_same_contract_and_current_cart_values(self):
        cases = (
            ("amazon-cart.html", "https://www.amazon.com/gp/cart/view.html", "B0TEST1234", 3, 1199.95),
            ("ebay-cart.html", "https://www.ebay.com/cart", "123456789012", 2, 329.50),
            ("generic-cart.html", "https://supplier.test/cart", "SEAL-42", 4, 41.0),
        )
        for fixture,url,identifier,quantity,price in cases:
            with self.subTest(fixture=fixture):
                capture = self.cart_capture(fixture,url)
                item = capture["items"][0]
                self.assertEqual(capture["capture_mode"], "CART")
                self.assertIn(identifier, {item["supplier_part_number"],item["sku"],item["asin"],item["listing_id"],item["item_id"]})
                self.assertEqual(item["quantity"], quantity)
                self.assertEqual(item["supplier_cost"], price)
                self.assertEqual(item["cart_page_url"], url)

    def test_cart_scope_excludes_saved_recommended_sponsored_recent_and_related(self):
        capture = self.cart_capture(
            "universal-cart-boundaries.html",
            "https://unknown-supplier.test/cart",
        )
        self.assertEqual([item["sku"] for item in capture["items"]], ["ACTIVE-1", "ACTIVE-2"])

        amazon = self.cart_capture(
            "amazon-cart.html", "https://www.amazon.com/gp/cart/view.html"
        )
        self.assertEqual([item["asin"] for item in amazon["items"]], ["B0TEST1234"])

    def test_product_scope_ignores_related_and_sponsored_collections(self):
        capture = self.page_capture(
            "universal-product-boundaries.html",
            "https://unknown-supplier.test/product/primary",
        )
        item = capture["items"][0]
        self.assertEqual(item["description"], "Primary Fuel Filter")
        self.assertEqual(item["supplier_part_number"], "PRIMARY-1")
        self.assertNotIn("WRONG", json.dumps(item))

    def test_page_cart_merge_uses_rich_page_and_current_cart_values(self):
        for page_fixture,cart_fixture,page_url,cart_url in (
            ("amazon.html","amazon-cart.html","https://www.amazon.com/dp/B0TEST1234","https://www.amazon.com/gp/cart/view.html"),
            ("ebay.html","ebay-cart.html","https://www.ebay.com/itm/123456789012","https://www.ebay.com/cart"),
        ):
            page_capture = self.page_capture(page_fixture,page_url)
            cart_capture = self.cart_capture(cart_fixture,cart_url)
            merged = self.evaluate_merge(page_capture,cart_capture)
            self.assertEqual(len(merged["items"]),1)
            item=merged["items"][0]
            self.assertEqual(item["quantity"],cart_capture["items"][0]["quantity"])
            self.assertEqual(item["supplier_cost"],cart_capture["items"][0]["supplier_cost"])
            self.assertEqual(item["product_page_url"],page_url)
            self.assertEqual(item["cart_page_url"],cart_url)
            self.assertNotEqual(item["description"],cart_capture["items"][0]["description"])

    def test_matching_hierarchy_and_ambiguous_items(self):
        identifiers = ["manufacturer_part_number","supplier_part_number","sku","asin","listing_id","item_id"]
        for field in identifiers:
            page_item={"description":"Rich Product",field:"MATCH-1","product_page_url":"https://supplier.test/p/1"}
            cart_item={"description":"Cart Product",field:"MATCH-1","quantity":5,"supplier_cost":9.5,"cart_page_url":"https://supplier.test/cart"}
            merged=self.evaluate_merge({"items":[page_item]},{"items":[cart_item]})
            self.assertEqual(len(merged["items"]),1,field)
        by_url=self.evaluate_merge(
            {"items":[{"description":"Page","product_page_url":"https://supplier.test/p/2?utm_source=x"}]},
            {"items":[{"description":"Cart","product_page_url":"https://supplier.test/p/2","quantity":2}]},
        )
        self.assertEqual(len(by_url["items"]),1)
        ambiguous=self.evaluate_merge(
            {"items":[{"description":"Same Filter","sku":"A"}]},
            {"items":[{"description":"Same Filter","sku":"B"}]},
        )
        self.assertEqual(len(ambiguous["items"]),2)

    def test_logistics_types_and_supported_units(self):
        typed=self.page_capture("logistics-types.html","https://supplier.test/logistics")["items"][0]
        self.assertEqual((typed["weight"],typed["weight_unit"],typed["weight_type"]),(16,"oz","SHIPPING"))
        self.assertEqual((typed["length"],typed["width"],typed["height"],typed["dimension_unit"],typed["dimension_type"]),(30,20,10,"cm","PACKAGE"))
        page=self.browser.new_page(); page.set_content("<title>Units</title>"); page.add_script_tag(path=str(EXT/"sdk.js"))
        units=page.evaluate("""() => ({
          lb: PLGConnectorSDK.parseWeight('2.4 lb'), kg: PLGConnectorSDK.parseWeight('1.1 kg'),
          oz: PLGConnectorSDK.parseWeight('16 oz'), g: PLGConnectorSDK.parseWeight('500 g'),
          inch: PLGConnectorSDK.parseDimensions('12 x 8 x 4 in'),
          mm: PLGConnectorSDK.parseDimensions('300 x 200 x 100 mm'),
          cm: PLGConnectorSDK.parseDimensions('30 x 20 x 10 cm')
        })"""); page.close()
        self.assertEqual([units[key]["weight_unit"] for key in ("lb","kg","oz","g")],["lb","kg","oz","g"])
        self.assertEqual([units[key]["dimension_unit"] for key in ("inch","mm","cm")],["in","mm","cm"])

    def test_missing_optional_data_and_manual_identifier_message(self):
        self.assertTrue(self.page_capture("id-no-price.html","https://supplier.test/id")["items"][0]["supplier_part_number"])
        no_id=self.page_capture("price-no-id.html","https://supplier.test/no-id")["items"][0]
        self.assertFalse(any(no_id[key] for key in ("manufacturer_part_number","supplier_part_number","sku","asin","listing_id","item_id")))
        popup=(EXT/"popup.js").read_text()
        self.assertIn("Add an identifier before sending this part to PPS.",popup)
        self.assertIn('readCapture("PAGE")',popup)
        self.assertIn('readCapture("CART")',popup)

    def test_optional_normalizer_accepts_only_evidence_backed_fields(self):
        page=self.browser.new_page(); page.set_content("<title>AI</title>"); page.add_script_tag(path=str(EXT/"sdk.js"))
        result=page.evaluate("""async () => PLGConnectorSDK.normalizeWithProvider(
          {items:[{description:'Raw Part',sku:'RAW-1',evidence:'Visible manufacturer label: Normalized Brand'}]},
          {normalize: async () => ({items:[{values:{manufacturer_part_number:'INVENTED',brand:'Normalized Brand'},evidence_fields:{brand:'Visible manufacturer label'}}]})}
        )"""); page.close()
        self.assertEqual(result["items"][0]["brand"],"Normalized Brand")
        self.assertEqual(result["items"][0]["manufacturer_part_number"],"")

    def test_universal_popup_renders_same_fields_for_page_and_cart(self):
        html=(EXT/"popup.html").read_text()
        self.assertIn('id="readPage"',html); self.assertIn('id="readCart"',html)
        popup=(EXT/"popup.js").read_text()
        self.assertEqual(popup.count('id="'),0)
        for label in ("Supplier","Part number / SKU / ASIN / listing ID","Description","Supplier cost","Quantity","Weight","Availability"):
            self.assertIn(label,popup)

    def test_popup_page_cart_merge_edit_and_send_flow(self):
        page=self.browser.new_page(viewport={"width":440,"height":700})
        html=re.sub(r'<script[^>]*src="[^"]+"[^>]*></script>','',(EXT/"popup.html").read_text())
        page.set_content(html)
        page.add_style_tag(path=str(EXT/"style.css"))
        page.add_script_tag(path=str(EXT/"sdk.js"))
        page.evaluate("""() => {
          const saved = {};
          window.__posted = []; window.__activeCalls = 0;
          const pageCapture = PLGConnectorSDK.normalizeCapture({
            source_key:'one_time_website',source_name:'One-time Website',source_url:'https://supplier.test/p/42',capture_mode:'PAGE',
            items:[{description:'Rich Seal Kit',sku:'MERGE-42',brand:'SealCo',supplier_cost:50,weight:'2.4 lb',dimensions:'12 x 8 x 4 in',product_page_url:'https://supplier.test/p/42'}]
          });
          const cartCapture = PLGConnectorSDK.normalizeCapture({
            source_key:'one_time_website',source_name:'One-time Website',source_url:'https://supplier.test/cart',capture_mode:'CART',
            items:[{description:'Cart Seal',sku:'MERGE-42',supplier_cost:45,quantity:3,availability:'In Stock',product_page_url:'https://supplier.test/p/42',cart_page_url:'https://supplier.test/cart'}]
          });
          window.browser = {
            tabs:{query:async()=>[{id:1}],sendMessage:async(_,message)=> message.type==='PLG_DETECT_SITE'
              ? {ok:true,source_key:'one_time_website',source_name:'One-time Website',source_url:'https://supplier.test/p/42',domain:'supplier.test',configured:false}
              : {ok:true,...(message.type==='PLG_READ_CURRENT_PAGE'?pageCapture:cartCapture)},create:async()=>{}},
            scripting:{executeScript:async()=>{}},
            storage:{local:{set:async values=>Object.assign(saved,values),get:async key=>({[key]:saved[key]})}}
          };
          window.fetch = async (url,options={}) => {
            const path=new URL(url).pathname;
            if(path==='/api/active-source-import'){ window.__activeCalls += 1; return new Response(JSON.stringify({job_id:1,job_asset_id:2,requested_need_id:3,verification_session_id:8,expected_revision_id:7,expected_version:window.__activeCalls===1?4:5,job_number:'TEST',customer:'Synthetic Customer',manufacturer:'CAT',machine:'420D',pin_serial:'TEST',requested_need:'Fuel Filter',source_name:'One-time Website',source_url_snapshot:'https://supplier.test/p/42'}),{status:200,headers:{'Content-Type':'application/json'}}); }
            if(path==='/api/research/extension/one-time-context') return new Response(JSON.stringify({ok:true,verification_session_id:9,job_id:1,job_asset_id:2,requested_need_id:3}),{status:200,headers:{'Content-Type':'application/json'}});
            if(path==='/api/basket/import-source-cart'){ window.__posted.push(JSON.parse(options.body)); return new Response(JSON.stringify({ok:true,job_id:1,imported_count:1}),{status:200,headers:{'Content-Type':'application/json'}}); }
            return new Response('{}',{status:404});
          };
        }""")
        page.add_script_tag(path=str(EXT/"popup.js"))
        page.wait_for_function("document.querySelector('#jobNumber').textContent === 'TEST'")
        page.click("#readPage")
        page.wait_for_function("document.querySelector('#count').textContent.includes('PAGE')")
        self.assertEqual(page.locator('[data-field="description"]').input_value(),"Rich Seal Kit")
        page.click("#readCart")
        page.wait_for_function("document.querySelector('#count').textContent.includes('CART')")
        self.assertEqual(page.locator('[data-field="description"]').input_value(),"Rich Seal Kit")
        self.assertEqual(page.locator('[data-field="quantity"]').input_value(),"3")
        self.assertEqual(page.locator('[data-field="supplier_cost"]').input_value(),"45")
        self.assertEqual(page.locator('[data-field="weight"]').input_value(),"2.4")
        page.locator(".capture-details summary").click()
        self.assertIn("MERGED", page.locator(".capture-details").inner_text())
        self.assertIn("SKU: MERGE-42", page.locator(".capture-details").inner_text())
        self.assertIn("https://supplier.test/p/42", page.locator(".capture-details").inner_text())
        self.assertIn("https://supplier.test/cart", page.locator(".capture-details").inner_text())
        page.locator('[data-field="supplier_cost"]').fill("44.25")
        page.locator('[data-field="supplier_cost"]').press("Tab")
        page.locator(".capture-details summary").click()
        self.assertIn("Extracted → edited", page.locator(".capture-details").inner_text())
        self.assertIn("45", page.locator(".capture-details").inner_text())
        self.assertIn("44.25", page.locator(".capture-details").inner_text())
        page.click("#import")
        page.wait_for_function("window.__posted.length === 1")
        payload=page.evaluate("window.__posted[0]")
        self.assertEqual(payload["items"][0]["supplier_cost"],44.25)
        self.assertEqual(payload["items"][0]["quantity"],3)
        self.assertEqual(payload["expected_revision_id"],7)
        self.assertEqual(payload["expected_version"],5)
        self.assertEqual(payload["job_asset_id"],2)
        self.assertEqual(payload["requested_need_id"],3)
        self.assertEqual(payload["verification_session_id"],9)
        self.assertEqual(payload["items"][0]["supplier_part_number"],"MERGE-42")
        self.assertEqual(page.evaluate("window.__activeCalls"),2)
        page.close()

    def test_send_blocks_changed_context_and_surfaces_transport_errors(self):
        scenarios = (
            ("context", "active Job, machine, Need, or research session changed"),
            ("network", "network unavailable"),
            ("nonjson", "Could not refresh the current PPS work context"),
        )
        for scenario, expected in scenarios:
            with self.subTest(scenario=scenario):
                page=self.browser.new_page(viewport={"width":440,"height":700})
                html=re.sub(r'<script[^>]*src="[^"]+"[^>]*></script>','',(EXT/"popup.html").read_text())
                page.set_content(html); page.add_script_tag(path=str(EXT/"sdk.js"))
                page.evaluate("""scenario => {
                  window.__posted=[]; window.__activeCalls=0;
                  const capture=PLGConnectorSDK.normalizeCapture({source_key:'ebay',source_name:'eBay',source_url:'https://www.ebay.com/itm/42',capture_mode:'PAGE',configured_source:true,items:[{description:'Starter',listing_id:'42'}]});
                  window.browser={tabs:{query:async()=>[{id:1}],sendMessage:async(_,message)=>message.type==='PLG_DETECT_SITE'?{ok:true,configured:true,generic_capture:true,source_key:'ebay',source_name:'eBay',source_url:'https://www.ebay.com/itm/42',domain:'ebay.com'}:{ok:true,...capture},create:async()=>{}},scripting:{executeScript:async()=>{}},storage:{local:{set:async()=>{},get:async()=>({})}}};
                  window.fetch=async(url,options={})=>{
                    const path=new URL(url).pathname;
                    if(path==='/api/active-source-import'){
                      window.__activeCalls+=1;
                      if(window.__activeCalls>1&&scenario==='network') throw new Error('network unavailable');
                      if(window.__activeCalls>1&&scenario==='nonjson') return new Response('Internal Error',{status:500,headers:{'Content-Type':'text/plain'}});
                      const changed=window.__activeCalls>1&&scenario==='context';
                      return new Response(JSON.stringify({job_id:1,job_asset_id:2,requested_need_id:3,verification_session_id:changed?99:8,expected_revision_id:7,expected_version:5,job_number:'TEST',customer:'Synthetic Customer',manufacturer:'CAT',machine:'420D',requested_need:'Fuel Filter',source_name:'eBay'}),{status:200,headers:{'Content-Type':'application/json'}});
                    }
                    if(path==='/api/basket/import-source-cart'){window.__posted.push(JSON.parse(options.body));return new Response(JSON.stringify({ok:true,job_id:1,imported_count:1}),{status:200,headers:{'Content-Type':'application/json'}});}
                    return new Response('{}',{status:404,headers:{'Content-Type':'application/json'}});
                  };
                }""", scenario)
                page.add_script_tag(path=str(EXT/"popup.js"))
                page.wait_for_function("document.querySelector('#jobNumber').textContent === 'TEST'")
                page.click("#readPage"); page.wait_for_function("document.querySelector('#count').textContent.includes('PAGE')")
                page.click("#import")
                page.wait_for_function("document.querySelector('#status').className === 'error'")
                self.assertIn(expected, page.locator("#status").inner_text())
                self.assertEqual(page.evaluate("window.__posted.length"),0)
                page.close()


if __name__ == "__main__": unittest.main()
