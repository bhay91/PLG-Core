from pathlib import Path
import unittest

import legacy_app
from plg_core.dashboard.service import WORK_QUEUE_CATEGORIES
from plg_core.research.branding import manufacturer_identity


ROOT = Path(__file__).resolve().parents[1]


class WorkQueueStageBrandUiTests(unittest.TestCase):
    def test_every_queue_category_has_text_and_semantic_badge_class(self):
        template = (ROOT / "templates/dashboard.html").read_text()
        css = (ROOT / "static/app.css").read_text()
        self.assertIn("work-queue-stage", template)
        for key, _label in WORK_QUEUE_CATEGORIES:
            self.assertIn(f".stage-{key.lower().replace('_', '-')}", css)
        self.assertIn(".stage-follow-ups", css)
        self.assertIn("item.need_label", template)
        self.assertIn("item.action_detail", template)
        self.assertIn("item.url", template)

    def test_known_aliases_resolve_without_changing_stored_text(self):
        cat = manufacturer_identity("CAT")
        caterpillar = manufacturer_identity("Caterpillar")
        self.assertEqual(cat["logo_url"], caterpillar["logo_url"])
        self.assertEqual(manufacturer_identity("Deere")["key"], "john-deere")
        self.assertEqual(manufacturer_identity("Detroit")["key"], "detroit-diesel")
        self.assertEqual(cat["canonical_name"], "Caterpillar")

    def test_unknown_is_clean_fallback_and_absent_has_no_logo(self):
        unknown = manufacturer_identity("Acme Equipment")
        self.assertFalse(unknown["has_logo"])
        self.assertNotIn("initials", unknown)
        absent = manufacturer_identity("")
        self.assertFalse(absent["has_logo"])
        macro = legacy_app.templates.env.get_template("_manufacturer_logo.html").module.manufacturer_logo
        self.assertEqual(str(macro("", False)).strip(), "")
        rendered = str(macro("Acme Equipment", False))
        self.assertIn("Acme Equipment", rendered)
        self.assertNotIn("manufacturer-logo-fallback", rendered)
        self.assertNotIn(">AE<", rendered)

    def test_real_local_image_and_responsive_contract(self):
        for manufacturer, filename in (
            ("CAT", "caterpillar.png"), ("John Deere", "john-deere.png"),
            ("BOMAG", "bomag.png"), ("HAMM", "hamm.png"),
            ("JCB", "jcb.png"), ("International", "international.png"),
            ("Cummins", "cummins.png"), ("Toyota", "toyota.png"),
        ):
            identity = manufacturer_identity(manufacturer)
            self.assertTrue(identity["has_logo"])
            self.assertEqual(identity["logo_url"], f"/static/manufacturer-logos/{filename}")
            self.assertGreater((ROOT / "static/manufacturer-logos" / filename).stat().st_size, 1000)
        macro = (ROOT / "templates/_manufacturer_logo.html").read_text()
        self.assertIn('<img class="manufacturer-logo"', macro)
        self.assertNotIn("<svg", macro)
        self.assertNotIn("initials", macro)
        self.assertNotIn("manufacturer-logo-fallback", macro)
        self.assertFalse((ROOT / "static/manufacturer-logos.svg").exists())
        css = (ROOT / "static/app.css").read_text()
        self.assertIn(".manufacturer-logo-box{", css)
        self.assertIn("object-fit:contain", css)
        self.assertNotIn("overflow-x:auto", css[css.index("/* Phase 1 operator Work Queue */"):css.index(".quote-review-table")])

    def test_registry_and_quote_use_single_logo_in_existing_identity_mark(self):
        macro = legacy_app.templates.env.get_template("_manufacturer_logo.html").module.manufacturer_logo
        logo_only = str(macro("CAT", True, False, True))
        fallback = str(macro("Acme Equipment", True, False, True))
        self.assertEqual(logo_only.count("manufacturer-mark-image"), 1)
        self.assertIn("caterpillar.png", logo_only)
        self.assertNotIn("CAT</span>", logo_only)
        self.assertIn("manufacturer-mark-fallback", fallback)
        for template_name in ("machines.html", "quotes.html", "invoices.html"):
            source = (ROOT / "templates" / template_name).read_text()
            mark = source[source.index('class="workspace-row-mark'):source.index('class="workspace-row-identity')]
            identity = source[source.index('class="workspace-row-identity'):source.index('class="workspace-row-status')]
            self.assertIn("manufacturer_logo(", mark)
            self.assertIn("false, true)", mark)
            self.assertNotIn("manufacturer_logo(", identity)
        css = (ROOT / "static/app.css").read_text()
        self.assertIn(".workspace-row-mark .manufacturer-mark-image", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("width: 86px", css)
        self.assertIn("height: 62px", css)
        self.assertIn("grid-template-columns: 76px minmax(0, 1fr)", css)
        self.assertIn("width: 68px", css)

    def test_jobs_workspace_uses_shared_logo_only_identity_mark(self):
        source = (ROOT / "templates/jobs.html").read_text()
        mark = source[source.index('class="smart-job-row-manufacturer"'):
                      source.index('class="smart-job-row-identity"')]
        self.assertIn("manufacturer_logo(job.manufacturer, true, false, true)", mark)
        self.assertNotIn("manufacturer_code", mark)
        self.assertEqual(source.count("manufacturer_logo(job.manufacturer"), 1)
        css = (ROOT / "static/app.css").read_text()
        self.assertIn(".smart-job-row-manufacturer .manufacturer-mark-image", css)
        self.assertIn("grid-template-columns: 86px minmax(0, 1fr)", css)

    def test_verified_john_deere_asset_is_not_previous_jimmy_dean_image(self):
        deere = ROOT / "static/manufacturer-logos/john-deere.png"
        self.assertGreater(deere.stat().st_size, 100_000)
        self.assertEqual(manufacturer_identity("John Deere")["logo_url"],
                         "/static/manufacturer-logos/john-deere.png")
        self.assertNotIn(b"Jimmy", deere.read_bytes())

    def test_templates_compile_and_use_shared_component(self):
        for name in ("dashboard.html", "machines.html", "machine_detail.html", "job_command_center.html",
                     "smart_intake_proposal.html", "quotes.html", "quote_documents.html", "invoices.html",
                     "invoice_documents.html", "job_delivery.html"):
            legacy_app.templates.env.get_template(name)
        self.assertIn("manufacturer_logo", (ROOT / "templates/machines.html").read_text())


if __name__ == "__main__":
    unittest.main()
