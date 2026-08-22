from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.application import app
from plg_core.assets.service import add_job_asset
from plg_core.basket.routes import basket_page, update_basket_item_form
from plg_core.basket.service import delete_item, get_basket, import_cart
from plg_core.database.migrations import run_migrations
from plg_core.commercial.service import create_selective_draft_quote
from plg_core.research.service import (
    add_supplier_quote_to_result,
    create_manual_research_result,
    create_requested_need,
    save_shipping_data,
    set_quote_candidate,
)
from plg_core.sources.service import (
    build_launch_url,
    canonical_asset_category,
    create_capture_proposal,
    create_source,
    list_sources_for_context,
    validate_source_url,
)
from plg_core.sources.routes import add_job_source
from plg_core.verification.service import start_asset_research
from plg_core.verification.routes import start_asset_research_route
from plg_core.sources.routes import open_research_source


ROOT = Path(__file__).resolve().parents[1]


class PartsResearchBatch3ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-batch3e-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()
        with closing(legacy_app.get_connection()) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            for table in (
                "research_capture_proposals", "part_shipping_data", "basket_item_need_links",
                "work_revision_item_need_links", "requested_needs", "verification_sessions",
                "active_source_import", "work_revision_items", "work_revision_sources",
                "work_revisions", "quote_items", "quotes", "part_sources", "job_parts",
                "basket_activity", "basket_items", "basket_sources", "baskets",
                "customer_requests", "job_timeline", "audit_logs", "job_assets", "machines",
                "customers", "jobs",
            ):
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(f'DELETE FROM "{table}"')
            c.execute(
                "DELETE FROM connector_profiles WHERE provenance!='PPS_DEFAULT' "
                "AND lower(display_name) NOT IN ('cat sis','caterpillar sis','worldpac')"
            )
            c.execute("PRAGMA foreign_keys=ON")
            c.commit()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def make_job(self):
        with closing(self.connection()) as c:
            customer_id = c.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES ('3E-C','Research Test',1)"
            ).lastrowid
            request_id = c.execute(
                "INSERT INTO customer_requests(request_number,individual_name,status,job_id) "
                "VALUES ('PPS-R-3E','Research Test','COMPLETED',NULL)"
            ).lastrowid
            job_id = c.execute(
                "INSERT INTO jobs(job_number,created_date,customer_id,customer,status) "
                "VALUES ('PPS-J-3E','2026-08-11',?,'Research Test','REQUESTED')",
                (customer_id,),
            ).lastrowid
            c.execute("UPDATE customer_requests SET job_id=? WHERE id=?", (job_id, request_id))
            c.commit()
        deere = add_job_asset(
            int(job_id), manufacturer="John Deere", model="350D",
            vin_pin_serial="TEST-3E-JD-350D", asset_type="machine", make_primary=True,
        )
        jcb = add_job_asset(
            int(job_id), manufacturer="JCB", model="3CX",
            vin_pin_serial="TEST-3E-JCB-3CX", asset_type="machine",
        )
        return int(job_id), deere, jcb

    def revision(self, job_id):
        return get_basket(job_id)["work_revision"]

    def source(self, **values):
        with closing(self.connection()) as c:
            source_id = create_source(c, **values)
            c.commit()
            return source_id

    def need(self, job_id, asset_id, wording):
        revision = self.revision(job_id)
        return create_requested_need(
            job_id, job_asset_id=asset_id, wording=wording,
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )

    def test_source_registry_routes_deterministically_and_never_fabricates_url(self):
        deere_source = self.source(
            display_name="Deere Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere/{identifier}?need={need}",
            manufacturer_applicability="John Deere", asset_category_applicability="machine",
            is_default=True,
        )
        self.source(
            display_name="JCB Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/jcb", manufacturer_applicability="JCB",
            asset_category_applicability="machine",
        )
        with closing(self.connection()) as c:
            disabled_id = create_source(
                c, display_name="Disabled Deere", source_type="OEM_CATALOG",
                launch_url="https://example.test/disabled", manufacturer_applicability="John Deere",
                asset_category_applicability="machine",
            )
            c.execute("UPDATE connector_profiles SET is_enabled=0 WHERE id=?", (disabled_id,))
            c.commit()
            routed = list_sources_for_context(c, manufacturer="John Deere", asset_category="machine")
            unknown = list_sources_for_context(c, manufacturer="Unconfigured Make", asset_category="machine")
        self.assertEqual(routed[0]["id"], deere_source)
        self.assertNotIn("JCB Catalog", [row["display_name"] for row in routed])
        self.assertNotIn("Disabled Deere", [row["display_name"] for row in routed])
        self.assertEqual([row["source_type"] for row in unknown], ["GENERAL_RESEARCH"])
        self.assertEqual(unknown[0]["safe_launch_url"], "")

    def test_parts_found_identifier_prefers_mpn_then_labeled_supplier_part(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Starter")
        revision = self.revision(job_id)
        create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Supplier-only starter", supplier_part_number="DER-93592",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        revision = self.revision(job_id)
        create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="OEM starter", manufacturer_part_number="MPN-PRIMARY",
            supplier_part_number="SUP-SECONDARY", expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        request = Request({"type": "http", "method": "GET", "path": f"/jobs/{job_id}/basket", "query_string": b"", "headers": [], "app": app, "router": app.router})
        html = basket_page(request, job_id, asset_id=deere["id"]).body.decode()
        self.assertIn("<h3><small>Supplier Part</small> DER-93592 · Supplier-only starter</h3>", html)
        self.assertIn("<h3>MPN-PRIMARY · OEM starter</h3>", html)
        self.assertNotIn("<small>Supplier Part</small> SUP-SECONDARY · OEM starter", html)

    def test_universal_quote_candidate_identifier_contract(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Starter")

        def result(description, mpn="", supplier=""):
            revision = self.revision(job_id)
            return create_manual_research_result(
                job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
                description=description, manufacturer_part_number=mpn, supplier_part_number=supplier,
                expected_revision_id=revision["id"], expected_version=revision["lock_version"],
            )["items"][-1]

        supplier = result("FleetPride starter", supplier="DER-93592")
        mpn = result("OEM starter", mpn="MPN-1", supplier="SUP-1")
        internal = result("Internally identified starter")
        missing_id = result("Missing identifier")
        missing_description = result("Temporary description", supplier="SUP-NO-DESC")
        with closing(self.connection()) as c:
            c.execute("UPDATE basket_items SET internal_part_number='' WHERE id IN (?,?)", (supplier["id"], missing_description["id"]))
            c.execute("UPDATE basket_items SET internal_part_number='',manufacturer_part_number='',supplier_part_number='' WHERE id=?", (missing_id["id"],))
            c.execute("UPDATE basket_items SET requested_description='' WHERE id=?", (missing_description["id"],))
            c.commit()

        for item_id in (supplier["id"], mpn["id"], internal["id"]):
            revision = self.revision(job_id)
            promoted = set_quote_candidate(job_id, item_id, candidate=True, requested_need_ids=[need["id"]],
                                           expected_revision_id=revision["id"], expected_version=revision["lock_version"])
            self.assertEqual((promoted["research_state"], promoted["selected"]), ("QUOTE_CANDIDATE", 1))
        for item_id in (missing_id["id"], missing_description["id"]):
            revision = self.revision(job_id)
            with self.assertRaises(HTTPException) as rejected:
                set_quote_candidate(job_id, item_id, candidate=True, requested_need_ids=[need["id"]],
                                    expected_revision_id=revision["id"], expected_version=revision["lock_version"])
            self.assertEqual(rejected.exception.status_code, 409)

        request = Request({"type":"http", "method":"GET", "path":f"/jobs/{job_id}/basket", "query_string":b"", "headers":[], "app":app, "router":app.router})
        html = basket_page(request, job_id, asset_id=deere["id"]).body.decode()
        self.assertIn("DER-93592", html)
        self.assertIn("FleetPride starter", html)
        self.assertIn("MPN-1", html)
        self.assertIn("OEM starter", html)
        self.assertIn("Parts Ready for Quote", html)
        self.assertNotIn("Missing identifier —", html)

    def test_imported_supplier_identifier_mappings_all_promote(self):
        job_id, _, _ = self.make_job()
        revision = self.revision(job_id)
        result = import_cart(job_id, {
            "source_key":"one_time_website", "source_name":"One-time Website",
            "source_url":"https://example.test/catalog", "items":[
                {"description":"SKU item", "sku":"SKU-ONLY"},
                {"description":"ASIN item", "asin":"B0ASINONLY"},
                {"description":"Listing item", "listing_id":"LIST-ONLY"},
                {"description":"Item ID item", "item_id":"ITEM-ONLY"},
            ],
        }, expected_revision_id=revision["id"], expected_version=revision["lock_version"])
        self.assertEqual(result["imported_count"], 4)
        with closing(self.connection()) as c:
            rows = c.execute("SELECT * FROM basket_items WHERE requested_description IN ('SKU item','ASIN item','Listing item','Item ID item') ORDER BY id").fetchall()
        self.assertEqual([row["supplier_part_number"] for row in rows], ["SKU-ONLY","B0ASINONLY","LIST-ONLY","ITEM-ONLY"])
        for row in rows:
            revision = self.revision(job_id)
            promoted = set_quote_candidate(job_id, row["id"], candidate=True,
                                           expected_revision_id=revision["id"], expected_version=revision["lock_version"])
            self.assertEqual(promoted["research_state"], "QUOTE_CANDIDATE")

    def test_remove_result_is_scoped_cascading_audited_and_candidate_safe(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Fuel Filter")
        revision = self.revision(job_id)
        target = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Wrong duplicate", manufacturer_part_number="WRONG-1",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )["items"][-1]
        save_shipping_data(job_id, target["id"], quality="MANUAL", unit_weight=2, provenance="Test")
        revision = self.revision(job_id)
        sibling = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Keep result", manufacturer_part_number="KEEP-1",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )["items"][-1]
        revision = self.revision(job_id)
        candidate = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Candidate", manufacturer_part_number="CAND-1",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )["items"][-1]
        set_quote_candidate(job_id, candidate["id"], candidate=True, requested_need_ids=[need["id"]])

        revision = self.revision(job_id)
        with self.assertRaises(HTTPException) as stale:
            delete_item(target["id"], expected_job_id=job_id, expected_revision_id=revision["id"],
                        expected_version=revision["lock_version"] - 1, require_unpromoted_research_result=True)
        self.assertEqual(stale.exception.status_code, 409)
        with self.assertRaises(HTTPException) as protected:
            delete_item(candidate["id"], expected_job_id=job_id, expected_revision_id=revision["id"],
                        expected_version=revision["lock_version"], require_unpromoted_research_result=True)
        self.assertEqual(protected.exception.status_code, 409)

        delete_item(target["id"], expected_job_id=job_id, expected_revision_id=revision["id"],
                    expected_version=revision["lock_version"], require_unpromoted_research_result=True)
        with closing(self.connection()) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM basket_items WHERE id=?", (target["id"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM basket_items WHERE id=?", (sibling["id"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM basket_items WHERE id=?", (candidate["id"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM requested_needs WHERE id=?", (need["id"],)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone())
            self.assertIsNotNone(c.execute("SELECT 1 FROM job_assets WHERE id=?", (deere["id"],)).fetchone())
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_item_need_links WHERE basket_item_id=?", (target["id"],)).fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM part_shipping_data WHERE basket_item_id=?", (target["id"],)).fetchone()[0], 0)
            event = c.execute("SELECT * FROM job_timeline WHERE job_id=? AND event_type='PART_REMOVED' ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        self.assertIsNotNone(event)
        self.assertIn("Wrong duplicate", event["message"])

        request = Request({"type": "http", "method": "GET", "path": f"/jobs/{job_id}/basket", "query_string": b"", "headers": [], "app": app, "router": app.router})
        html = basket_page(request, job_id, asset_id=deere["id"]).body.decode()
        self.assertEqual(html.count("Remove Result"), 1)
        self.assertIn("Remove this identified result?", html)
        self.assertIn("＋ Add Supplier Quote", html)
        self.assertIn("Shipping Details", html)
        self.assertIn("Confirm for Quote", html)
        self.assertLess(html.index("＋ Add Supplier Quote"), html.index("Shipping Details"))
        self.assertLess(html.index("Shipping Details"), html.index("Remove Result"))
        self.assertLess(html.index("Remove Result"), html.index("Confirm for Quote"))

    def test_global_source_matches_unknown_and_regional_markets(self):
        source_id = self.source(
            display_name="Global Deere Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere", manufacturer_applicability="John Deere",
            asset_category_applicability="machine", market_applicability="GLOBAL",
        )
        with closing(self.connection()) as c:
            for market in ("UNKNOWN", "GLOBAL", "JDM", "USDM", "UK"):
                routed = list_sources_for_context(
                    c, manufacturer="John Deere", asset_category="machine", market=market,
                )
                self.assertIn(source_id, [row["id"] for row in routed], market)
            unrelated = list_sources_for_context(
                c, manufacturer="JCB", asset_category="machine", market="UNKNOWN",
            )
        self.assertNotIn(source_id, [row["id"] for row in unrelated])

    def test_equipment_category_family_preserves_manufacturer_and_vehicle_isolation(self):
        deere_source = self.source(
            display_name="Deere Equipment Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere", manufacturer_applicability="John Deere",
            asset_category_applicability="machine", market_applicability="GLOBAL",
        )
        jcb_source = self.source(
            display_name="JCB Equipment Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/jcb", manufacturer_applicability="JCB",
            asset_category_applicability="equipment", market_applicability="GLOBAL",
        )
        vehicle_source = self.source(
            display_name="Automotive Catalog", source_type="AFTERMARKET_CATALOG",
            launch_url="https://example.test/vehicle", asset_category_applicability="vehicle",
        )
        with closing(self.connection()) as c:
            deere_machine = list_sources_for_context(
                c, manufacturer="John Deere", asset_category="machine", market="UNKNOWN",
            )
            deere_heavy = list_sources_for_context(
                c, manufacturer="John Deere", asset_category="Heavy Equipment", market="UK",
            )
            jcb_heavy = list_sources_for_context(
                c, manufacturer="JCB", asset_category="heavy_equipment", market="UNKNOWN",
            )
            toyota_vehicle = list_sources_for_context(
                c, manufacturer="Toyota", asset_category="vehicle", market="UNKNOWN",
            )
        self.assertIn(deere_source, [row["id"] for row in deere_machine])
        self.assertIn(deere_source, [row["id"] for row in deere_heavy])
        self.assertNotIn(jcb_source, [row["id"] for row in deere_heavy])
        self.assertIn(jcb_source, [row["id"] for row in jcb_heavy])
        self.assertNotIn(vehicle_source, [row["id"] for row in deere_heavy])
        self.assertIn(vehicle_source, [row["id"] for row in toyota_vehicle])
        self.assertEqual(canonical_asset_category("heavy_equipment"), "machine")

    def test_migrated_cat_and_worldpac_sources_route_only_to_relevant_assets(self):
        with closing(self.connection()) as c:
            cat = c.execute("SELECT * FROM connector_profiles WHERE lower(display_name)='cat sis'").fetchone()
            worldpac = c.execute("SELECT * FROM connector_profiles WHERE lower(display_name)='worldpac'").fetchone()
            self.assertEqual(cat["source_type"], "OEM_CATALOG")
            self.assertEqual(worldpac["source_type"], "AFTERMARKET_CATALOG")
            cat_machine = list_sources_for_context(
                c, manufacturer="CAT", asset_category="machine", market="UNKNOWN",
            )
            caterpillar_machine = list_sources_for_context(
                c, manufacturer="Caterpillar", asset_category="equipment", market="UK",
            )
            toyota_vehicle = list_sources_for_context(
                c, manufacturer="Toyota", asset_category="vehicle", market="JDM",
            )
            deere_machine = list_sources_for_context(
                c, manufacturer="John Deere", asset_category="machine", market="UNKNOWN",
            )
        self.assertIn(cat["id"], [row["id"] for row in cat_machine])
        self.assertIn(cat["id"], [row["id"] for row in caterpillar_machine])
        self.assertIn(worldpac["id"], [row["id"] for row in toyota_vehicle])
        self.assertNotIn(worldpac["id"], [row["id"] for row in deere_machine])
        self.assertNotIn(cat["id"], [row["id"] for row in deere_machine])

    def test_source_url_safety_and_context_template(self):
        for unsafe in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.test"):
            with self.assertRaises(HTTPException):
                validate_source_url(unsafe)
        source = {"launch_url": "https://example.test/search?q={identifier}+{need}"}
        url = build_launch_url(source, {"identifier": "PIN 123", "need": "Fuel Filter"})
        self.assertEqual(url, "https://example.test/search?q=PIN+123+Fuel+Filter")

    def test_need_aware_session_snapshots_context_and_is_idempotent(self):
        job_id, deere, _ = self.make_job()
        fuel = self.need(job_id, deere["id"], "Fuel Filter Kit")
        oil = self.need(job_id, deere["id"], "Oil Filter")
        source_id = self.source(
            display_name="Deere Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere", manufacturer_applicability="John Deere",
            asset_category_applicability="machine",
        )
        revision = self.revision(job_id)
        with self.assertRaises(HTTPException):
            start_asset_research(
                job_id, deere["id"], source_id, requested_need_id=None,
                expected_revision_id=revision["id"], expected_version=revision["lock_version"],
            )
        first = start_asset_research(
            job_id, deere["id"], source_id, requested_need_id=fuel["id"],
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        second = start_asset_research(
            job_id, deere["id"], source_id, requested_need_id=fuel["id"],
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["need_wording_snapshot"], "Fuel Filter Kit")
        self.assertEqual(first["identifier_value_snapshot"], "TEST-3E-JD-350D")
        self.assertNotEqual(first["requested_need_id"], oil["id"])

    def test_need_creation_rendering_and_start_research_use_selected_asset_context(self):
        job_id, deere, _ = self.make_job()
        fuel = self.need(job_id, deere["id"], "Fuel Filter Kit")
        with closing(self.connection()) as c:
            stored = c.execute(
                "SELECT job_asset_id FROM requested_needs WHERE id=?", (fuel["id"],)
            ).fetchone()
        self.assertEqual(stored["job_asset_id"], deere["id"])
        request = Request({"type": "http", "method": "GET", "path": f"/jobs/{job_id}/basket",
                           "query_string": b"", "headers": [], "app": app, "router": app.router})
        html = basket_page(request, job_id, asset_id=deere["id"]).body.decode()
        self.assertIn(
            f"asset_id={deere['id']}&amp;need_id={fuel['id']}#research-results",
            html,
        )
        self.assertIn(f'name="requested_need_id" value="{fuel["id"]}"', html)
        self.assertIn("ACTIVE NEED", html)

        source_id = self.source(
            display_name="Deere Need Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere", manufacturer_applicability="John Deere",
            asset_category_applicability="machine",
        )
        revision = self.revision(job_id)
        session = start_asset_research(
            job_id, deere["id"], source_id, requested_need_id=None,
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertEqual(session["requested_need_id"], fuel["id"])
        self.assertEqual(session["need_wording_snapshot"], "Fuel Filter Kit")

        oil = self.need(job_id, deere["id"], "Oil Filter")
        html = basket_page(request, job_id, asset_id=deere["id"]).body.decode()
        self.assertIn(f"asset_id={deere['id']}&amp;need_id={fuel['id']}", html)
        self.assertIn(f"asset_id={deere['id']}&amp;need_id={oil['id']}", html)
        selected_html = basket_page(
            request, job_id, asset_id=deere["id"], need_id=oil["id"]
        ).body.decode()
        self.assertIn(f'name="requested_need_id" value="{oil["id"]}"', selected_html)
        self.assertIn("Oil Filter", selected_html)
        self.assertNotIn('id="customer-needs"', selected_html)

    def test_zero_need_session_remains_general_research(self):
        job_id, deere, _ = self.make_job()
        source_id = self.source(
            display_name="General Deere Research", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere", manufacturer_applicability="John Deere",
            asset_category_applicability="machine",
        )
        revision = self.revision(job_id)
        session = start_asset_research(
            job_id, deere["id"], source_id, requested_need_id=None,
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertIsNone(session["requested_need_id"])
        self.assertEqual(session["need_wording_snapshot"], "General research")

    def test_machine_tile_need_selection_drives_three_research_contexts(self):
        job_id, deere, _ = self.make_job()
        needs = [
            self.need(job_id, deere["id"], wording)
            for wording in ("STARTER", "TURBO", "RADIATOR")
        ]
        source_id = self.source(
            display_name="Deere Tile Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere-tile",
            manufacturer_applicability="John Deere",
            asset_category_applicability="machine",
        )
        request = Request({
            "type": "http", "method": "GET",
            "path": f"/jobs/{job_id}/basket", "query_string": b"",
            "headers": [], "app": app, "router": app.router,
        })
        for need in needs:
            html = basket_page(
                request, job_id, asset_id=deere["id"], need_id=need["id"]
            ).body.decode()
            self.assertIn(
                f'name="requested_need_id" value="{need["id"]}"', html
            )
            self.assertIn(f"<strong>{need['wording']}</strong>", html)
            revision = self.revision(job_id)
            session = start_asset_research(
                job_id, deere["id"], source_id,
                requested_need_id=need["id"],
                expected_revision_id=revision["id"],
                expected_version=revision["lock_version"],
            )
            self.assertEqual(session["requested_need_id"], need["id"])
            self.assertEqual(session["need_wording_snapshot"], need["wording"])

        with self.assertRaises(HTTPException) as missing:
            basket_page(request, job_id, asset_id=deere["id"], need_id=999999)
        self.assertEqual(missing.exception.status_code, 404)

    def test_open_source_creates_need_session_then_launches_safe_url(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Fuel Filter Kit")
        source_id = self.source(
            display_name="Context Deere Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/catalog/{identifier}?need={need}",
            manufacturer_applicability="John Deere", asset_category_applicability="machine",
        )
        revision = self.revision(job_id)
        setup = start_asset_research_route(
            job_id, deere["id"], source_id, requested_need_id=need["id"],
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertIn("/research-sessions/", setup.headers["location"])
        session_id = int(setup.headers["location"].split("/research-sessions/")[1].split("/")[0])
        launched = open_research_source(job_id, session_id)
        self.assertEqual(
            launched.headers["location"],
            "https://example.test/catalog/TEST-3E-JD-350D?need=Fuel+Filter+Kit",
        )
        with closing(self.connection()) as c:
            session = c.execute("SELECT * FROM verification_sessions WHERE id=?", (session_id,)).fetchone()
        self.assertEqual(session["requested_need_id"], need["id"])
        self.assertEqual(session["connector_profile_id"], source_id)
        revision = self.revision(job_id)
        result = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Identified Fuel Filter", manufacturer_part_number="RE-OPEN",
            research_session_id=None, expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )["items"][-1]
        self.assertEqual(result["research_session_id"], session_id)
        self.assertEqual(result["research_state"], "RESEARCH_RESULT")
        self.assertEqual(result["selected"], 0)

    def test_save_source_uses_authoritative_asset_scope_and_routes_immediately(self):
        job_id, deere, jcb = self.make_job()
        with closing(self.connection()) as c:
            c.execute("UPDATE job_assets SET asset_type='Heavy Equipment' WHERE id=?", (deere["id"],))
            c.commit()
        response = add_job_source(
            job_id, display_name="Saved Deere Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/saved-deere",
            manufacturer_applicability="Spoofed Manufacturer",
            asset_category_applicability="vehicle", market_applicability="GLOBAL",
            notes="Confirmed from Deere workspace", job_asset_id=deere["id"],
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn(f"asset_id={deere['id']}", response.headers["location"])
        with closing(self.connection()) as c:
            saved = c.execute(
                "SELECT * FROM connector_profiles WHERE display_name='Saved Deere Catalog'"
            ).fetchone()
            deere_sources = list_sources_for_context(
                c, manufacturer="John Deere", asset_category="Heavy Equipment", market="UNKNOWN",
            )
            jcb_sources = list_sources_for_context(
                c, manufacturer="JCB", asset_category="machine", market="UNKNOWN",
            )
        self.assertEqual(saved["manufacturer_applicability"], "John Deere")
        self.assertEqual(saved["asset_category_applicability"], "machine")
        self.assertEqual(saved["market_applicability"], "GLOBAL")
        self.assertEqual(saved["provenance"], f"JOB_WORKSPACE:{job_id}")
        self.assertIn(saved["id"], [row["id"] for row in deere_sources])
        self.assertNotIn(saved["id"], [row["id"] for row in jcb_sources])

    def test_concurrent_start_has_one_active_context(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Fuel Filter Kit")
        source_id = self.source(
            display_name="Deere Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere", manufacturer_applicability="John Deere",
            asset_category_applicability="machine",
        )
        revision = self.revision(job_id)
        def start():
            return start_asset_research(
                job_id, deere["id"], source_id, requested_need_id=need["id"],
                expected_revision_id=revision["id"], expected_version=revision["lock_version"],
            )["id"]
        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(lambda _: start(), range(2)))
        self.assertEqual(len(set(ids)), 1)
        with closing(self.connection()) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM verification_sessions WHERE status='ACTIVE'").fetchone()[0], 1)

    def test_machine_source_and_need_cannot_leak(self):
        job_id, deere, jcb = self.make_job()
        fuel = self.need(job_id, deere["id"], "Fuel Filter Kit")
        seal = self.need(job_id, jcb["id"], "Boom Cylinder Seal Kit")
        source_id = self.source(
            display_name="Deere Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere", manufacturer_applicability="John Deere",
            asset_category_applicability="machine",
        )
        revision = self.revision(job_id)
        with self.assertRaises(HTTPException):
            start_asset_research(
                job_id, jcb["id"], source_id, requested_need_id=seal["id"],
                expected_revision_id=revision["id"], expected_version=revision["lock_version"],
            )
        with self.assertRaises(HTTPException):
            start_asset_research(
                job_id, deere["id"], source_id, requested_need_id=seal["id"],
                expected_revision_id=revision["id"], expected_version=revision["lock_version"],
            )
        self.assertNotEqual(fuel["job_asset_id"], seal["job_asset_id"])

    def test_result_capture_evidence_and_explicit_promotion_preserve_lineage(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Fuel Filter Kit")
        source_id = self.source(
            display_name="Deere Catalog", source_type="OEM_CATALOG",
            launch_url="https://example.test/deere", manufacturer_applicability="John Deere",
            asset_category_applicability="machine",
        )
        revision = self.revision(job_id)
        session = start_asset_research(
            job_id, deere["id"], source_id, requested_need_id=need["id"],
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        basket = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Primary Fuel Filter", manufacturer_part_number="RE123456",
            research_session_id=session["id"], source_url="https://example.test/RE123456",
            verification_status="VERIFIED", research_evidence="Catalog diagram 12",
            research_notes="Exact PIN lookup", expected_revision_id=revision["id"],
            expected_version=revision["lock_version"],
        )
        item = basket["items"][-1]
        self.assertEqual(item["research_state"], "RESEARCH_RESULT")
        self.assertEqual(item["selected"], 0)
        self.assertEqual(item["research_session_id"], session["id"])
        self.assertEqual(item["research_evidence"], "Catalog diagram 12")
        revision = self.revision(job_id)
        promoted = set_quote_candidate(
            job_id, item["id"], candidate=True, requested_need_ids=[need["id"]],
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertEqual(promoted["research_session_id"], session["id"])
        self.assertEqual(promoted["research_evidence"], "Catalog diagram 12")

    def test_supplier_quote_updates_result_without_promoting_it(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Fuel Filter Kit")
        revision = self.revision(job_id)
        item = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Fuel Filter", manufacturer_part_number="RE-SUPPLIER",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )["items"][-1]
        revision = self.revision(job_id)
        quoted = add_supplier_quote_to_result(
            job_id, item["id"], supplier_name="Test Supplier", supplier_unit_cost=42.50,
            supplier_part_number="SUP-42", availability="In stock",
            source_url="https://example.test/quote/42", evidence="Supplier quote SQ-42",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertEqual(quoted["research_state"], "RESEARCH_RESULT")
        self.assertEqual(quoted["selected"], 0)
        self.assertEqual(quoted["supplier_name"], "Test Supplier")
        self.assertEqual(quoted["supplier_unit_cost"], 42.50)
        self.assertEqual(quoted["research_evidence"], "Supplier quote SQ-42")

    def test_multiple_results_remain_unselected_until_operator_choice(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Left Headlight")
        for description in ("LED Headlight", "Halogen Headlight"):
            revision = self.revision(job_id)
            create_manual_research_result(
                job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
                description=description, manufacturer_part_number=description.split()[0],
                expected_revision_id=revision["id"], expected_version=revision["lock_version"],
            )
        with closing(self.connection()) as c:
            rows = c.execute("SELECT research_state,selected FROM basket_items ORDER BY id").fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [("RESEARCH_RESULT", 0), ("RESEARCH_RESULT", 0)])

    @patch("plg_core.commercial.service._write_documents")
    def test_unverified_promoted_result_commits_and_reaches_quote_snapshot(self, _write_documents):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Fuel Filter Kit")
        revision = self.revision(job_id)
        basket = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Fuel Filter", manufacturer_part_number="RE-EVIDENCE",
            research_evidence="OEM diagram 8", research_notes="PIN confirmed",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        item = basket["items"][-1]
        self.assertEqual(item["verification_status"], "NEEDS_REVIEW")
        self.assertEqual(item["research_state"], "RESEARCH_RESULT")
        self.assertEqual(item["selected"], 0)
        revision = self.revision(job_id)
        other = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Unconfirmed Alternate", manufacturer_part_number="RE-ALT",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )["items"][-1]
        revision = self.revision(job_id)
        promoted = set_quote_candidate(
            job_id, item["id"], candidate=True, requested_need_ids=[need["id"]],
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertEqual(promoted["research_state"], "QUOTE_CANDIDATE")
        self.assertEqual(promoted["selected"], 1)
        revision = self.revision(job_id)
        response = update_basket_item_form(
            job_id, item["id"], quantity=2, supplier_unit_cost=25,
            markup_percent=30, customer_unit_price_override="",
            manufacturer_part_number="RE-EVIDENCE", alternate_part_number="",
            supplier_part_number="", part_status="", verification_status=None,
            verification_note=None, confidence=None,
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertEqual(response.status_code, 303)
        with closing(self.connection()) as c:
            saved = c.execute(
                "SELECT verification_status,verification_note FROM basket_items WHERE id=?",
                (item["id"],),
            ).fetchone()
        self.assertEqual(tuple(saved), ("NEEDS_REVIEW", "OEM diagram 8"))
        revision = self.revision(job_id)
        quote = create_selective_draft_quote(
            job_id, basket_item_ids=[item["id"]], bill_to_kind="CONTACT",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        with closing(self.connection()) as c:
            line = c.execute("SELECT * FROM quote_items WHERE quote_id=?", (quote["id"],)).fetchone()
            committed = c.execute(
                "SELECT verification_status FROM job_parts WHERE id=?", (line["part_id"],)
            ).fetchone()
            remaining = c.execute(
                "SELECT research_state,selected FROM basket_items WHERE requested_description=?",
                (other["requested_description"],),
            ).fetchone()
        self.assertEqual(line["research_evidence"], "OEM diagram 8")
        self.assertEqual(line["research_notes"], "PIN confirmed")
        self.assertEqual(line["primary_requested_need_id"], need["id"])
        self.assertEqual(committed["verification_status"], "NEEDS_REVIEW")
        self.assertEqual(tuple(remaining), ("RESEARCH_RESULT", 0))

    def test_provisional_quote_candidate_pricing_save_preserves_metadata(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Oil Filter")
        revision = self.revision(job_id)
        item = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=need["id"],
            description="Oil Filter", manufacturer_part_number="RE-PROVISIONAL",
            verification_status="PROVISIONAL", research_evidence="Supplier listing",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )["items"][-1]
        revision = self.revision(job_id)
        set_quote_candidate(
            job_id, item["id"], candidate=True, requested_need_ids=[need["id"]],
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        revision = self.revision(job_id)
        response = update_basket_item_form(
            job_id, item["id"], quantity=1, supplier_unit_cost=15,
            markup_percent=35, customer_unit_price_override="",
            manufacturer_part_number="RE-PROVISIONAL", alternate_part_number="",
            supplier_part_number="", part_status="", verification_status=None,
            verification_note=None, confidence=None,
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        self.assertEqual(response.status_code, 303)
        with closing(self.connection()) as c:
            saved = c.execute(
                "SELECT research_state,selected,verification_status,verification_note "
                "FROM basket_items WHERE id=?", (item["id"],),
            ).fetchone()
        self.assertEqual(tuple(saved), ("QUOTE_CANDIDATE", 1, "PROVISIONAL", "Supplier listing"))

    def test_legacy_verified_and_documented_override_candidates_still_load(self):
        job_id, deere, _ = self.make_job()
        get_basket(job_id)
        with closing(self.connection()) as c:
            basket_id = c.execute("SELECT id FROM baskets WHERE job_id=?", (job_id,)).fetchone()[0]
            c.execute(
                "INSERT INTO basket_items "
                "(basket_id,requested_description,job_asset_id,research_state,verification_status,selected) "
                "VALUES (?,?,?,'LEGACY_CANDIDATE','VERIFIED',1)",
                (basket_id, "Verified legacy part", deere["id"]),
            )
            c.execute(
                "INSERT INTO basket_items "
                "(basket_id,requested_description,job_asset_id,research_state,verification_status,verification_note,selected) "
                "VALUES (?,?,?,'LEGACY_CANDIDATE','OVERRIDE','Historical operator decision',1)",
                (basket_id, "Override legacy part", deere["id"]),
            )
            c.commit()
        loaded = get_basket(job_id)["items"]
        statuses = {
            row["requested_description"]: (row["research_state"], row["verification_status"])
            for row in loaded
        }
        self.assertEqual(statuses["Verified legacy part"], ("LEGACY_CANDIDATE", "VERIFIED"))
        self.assertEqual(statuses["Override legacy part"], ("LEGACY_CANDIDATE", "OVERRIDE"))
        legacy_routes = {
            route.path: route.endpoint.__name__ for route in legacy_app.app.routes
            if hasattr(route, "path") and hasattr(route, "endpoint")
        }
        self.assertEqual(
            legacy_routes["/parts/{part_id}/sources/{source_id}/verification"],
            "update_part_source_verification",
        )

    def test_shipping_optional_and_actual_cannot_be_downgraded(self):
        job_id, deere, _ = self.make_job()
        revision = self.revision(job_id)
        basket = create_manual_research_result(
            job_id, job_asset_id=deere["id"], requested_need_id=None,
            description="Filter", manufacturer_part_number="F-1",
            expected_revision_id=revision["id"], expected_version=revision["lock_version"],
        )
        item = basket["items"][-1]
        saved = save_shipping_data(
            job_id, item["id"], quality="ACTUAL", unit_weight=2.15,
            length=7, width=5, height=5, provenance="Physical measurement",
        )
        self.assertEqual(saved["quality"], "ACTUAL")
        with self.assertRaises(HTTPException):
            save_shipping_data(job_id, item["id"], quality="ESTIMATED_LOW", unit_weight=1.0)

    def test_future_firefox_capture_is_review_only(self):
        job_id, deere, _ = self.make_job()
        need = self.need(job_id, deere["id"], "Fuel Filter Kit")
        with closing(self.connection()) as c:
            proposal_id = create_capture_proposal(
                c, proposal_type="PART_RESULT", job_id=job_id,
                verification_session_id=None, job_asset_id=deere["id"],
                requested_need_id=need["id"], connector_profile_id=None,
                page_url="https://example.test/cart", payload={"part_number": "RE1"},
            )
            c.commit()
            row = c.execute("SELECT * FROM research_capture_proposals WHERE id=?", (proposal_id,)).fetchone()
            self.assertEqual(row["status"], "REVIEW")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM basket_items").fetchone()[0], 0)

    def test_migration_rerun_integrity_and_foreign_keys(self):
        isolated = Path(self.temp.name) / "migration-rehearsal.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", isolated)
        with patch.object(legacy_app, "DB_PATH", isolated):
            run_migrations()
            run_migrations()
            with closing(legacy_app.get_connection()) as c:
                self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(c.execute("SELECT COUNT(*) FROM connector_profiles WHERE connector_key='general_research'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
