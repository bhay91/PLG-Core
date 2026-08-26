from pathlib import Path
import unittest

from plg_core.intake.service import _review_summary
from plg_core.research.service import derive_result_visibility


ROOT = Path(__file__).resolve().parents[1]


class WorkStateVisibilityAlpha371Tests(unittest.TestCase):
    def test_capture_details_renderer_exposes_required_provenance(self):
        popup = (ROOT / "extensions/firefox/PLG-Firefox-Extension-v0.15-Connector-SDK/popup.js").read_text()
        for value in ("Capture Details", "MERGED", "Product URL", "Cart URL", "Identifiers", "Extracted → edited", "evidence_fields"):
            self.assertIn(value, popup)

    def test_import_payload_preserves_capture_mode(self):
        popup = (ROOT / "extensions/firefox/PLG-Firefox-Extension-v0.15-Connector-SDK/popup.js").read_text()
        service = (ROOT / "plg_core/basket/service.py").read_text()
        self.assertIn("capture_mode:", popup)
        self.assertIn('f"Capture mode: {capture_mode}"', service)
        self.assertIn('"Field evidence: "', service)

    def test_review_summary_displays_customer_create_and_machine_reuse(self):
        summary = _review_summary({"contact_name":"Synthetic Review Customer", "company_name":"", "matched_customer_id":None,
            "review_state":"CONFIDENT", "assets":[{"manufacturer":"CAT", "model":"420D", "matched_machine_id":7,
            "review_state":"CONFIDENT", "identifiers":[], "needs":[{"review_state":"CONFIDENT"}]}], "unassigned_needs":[]})
        self.assertEqual(summary["customer"]["action"], "CREATE")
        self.assertEqual(summary["machines"][0], {"action":"REUSE", "label":"CAT 420D"})
        self.assertEqual((summary["need_count"], summary["assigned_need_count"]), (1, 1))

    def test_review_summary_displays_customer_reuse_and_machine_create(self):
        summary = _review_summary({"contact_name":"Synthetic Review Customer", "matched_customer_id":4, "review_state":"CONFIDENT",
            "assets":[{"manufacturer":"CAT", "model":"420D", "matched_machine_id":None, "review_state":"CONFIDENT", "identifiers":[], "needs":[]}], "unassigned_needs":[]})
        self.assertEqual(summary["customer"]["action"], "REUSE")
        self.assertEqual(summary["machines"][0]["action"], "CREATE")

    def test_review_summary_reports_missing_and_unassigned(self):
        summary = _review_summary({"contact_name":"", "company_name":"", "review_state":"REVIEW", "assets":[],
                                   "unassigned_needs":[{"review_state":"REVIEW"}]})
        self.assertIn("Customer name or company", summary["missing"])
        self.assertNotIn("Machine assignment", " ".join(summary["missing"]))
        self.assertGreater(summary["review_count"], 0)

    def test_next_action_derivation_uses_existing_state_only(self):
        self.assertEqual(derive_result_visibility({"supplier_unit_cost":None})["next_action"], "ADD SUPPLIER PRICE")
        self.assertEqual(derive_result_visibility({"supplier_unit_cost":10})["next_action"], "REVIEW FITMENT / EVIDENCE")
        self.assertEqual(derive_result_visibility({"supplier_unit_cost":10, "research_evidence":"fitment"})["next_action"], "CONFIRM FOR QUOTE")
        self.assertEqual(derive_result_visibility({"supplier_unit_cost":10, "selected":1, "research_state":"QUOTE_CANDIDATE"})["next_action"], "READY FOR QUOTE")

    def test_source_origin_and_shipping_provenance_are_derived(self):
        result = derive_result_visibility({"supplier_unit_cost":10, "research_evidence":"Capture mode: CART | evidence"},
                                          {"source_name":"Amazon", "source_key":"amazon"},
                                          {"provenance":"Amazon listing"})
        self.assertEqual((result["source_display_name"], result["capture_origin"], result["shipping_provenance"]),
                         ("Amazon", "CART", "Amazon listing"))

    def test_one_time_origin_is_named_without_new_state(self):
        result = derive_result_visibility({}, {"source_name":"One-time Website", "source_key":"one_time_website"})
        self.assertEqual(result["capture_origin"], "One-time Website")

    def test_operator_templates_show_summary_and_next_action(self):
        intake = (ROOT / "templates/smart_intake_proposal.html").read_text()
        command = (ROOT / "templates/job_command_center.html").read_text()
        self.assertIn("What PPS will create or reuse", intake)
        self.assertIn("Missing important information", intake)
        self.assertIn("item.next_action", command)
        self.assertIn("item.source_display_name", command)


if __name__ == "__main__":
    unittest.main()
