from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class GlobalDeleteArchivePolicyTests(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text()

    def test_shared_confirmation_contract_is_non_destructive(self):
        base = self.read("templates/base.html")
        for text in (
            "Delete this item?",
            "This will archive it and remove it from active PPS views.",
            "Historical records will be preserved.",
            ">Cancel<", ">Delete<",
        ):
            self.assertIn(text, base)
        self.assertIn("data-archive-delete", base)
        self.assertIn("requestSubmit()", base)

    def test_operator_delete_forms_use_existing_soft_states(self):
        cases = {
            "templates/requests.html": ("remove_url", "data-archive-delete"),
            "templates/request_detail.html": ("/archive", "data-archive-delete"),
            "templates/edit_job.html": ("/archive", "data-archive-delete"),
            "templates/job_command_center.html": ('value="ARCHIVED"', "data-archive-delete"),
            "templates/quote_documents.html": ("/archive", "data-archive-delete"),
            "templates/connectors.html": ("/archive", "data-archive-delete"),
            "templates/customers.html": ("/deactivate", "data-archive-delete"),
            "templates/suppliers.html": ('value="INACTIVE"', "data-archive-delete"),
        }
        for filename, expected in cases.items():
            source = self.read(filename)
            for value in expected:
                self.assertIn(value, source, filename)

    def test_permanent_delete_is_visibly_separate(self):
        self.assertIn("Permanently Delete", self.read("templates/disposable_delete_review.html"))
        self.assertIn("Permanently Delete", self.read("templates/requests.html"))
        self.assertIn("Permanently Delete", self.read("templates/request_detail.html"))

    def test_authoritative_records_have_no_generic_delete(self):
        for filename in (
            "templates/invoice_documents.html",
            "templates/supplier_order_detail.html",
            "templates/job_delivery.html",
        ):
            source = self.read(filename)
            self.assertNotIn("data-archive-delete", source, filename)
        self.assertIn("Void Invoice", self.read("templates/invoice_documents.html"))
        self.assertIn("Receive Parts", self.read("templates/supplier_order_detail.html"))
        self.assertIn("Mark Delivered", self.read("templates/job_delivery.html"))

    def test_specialized_research_result_removal_remains_specialized(self):
        source = self.read("templates/job_command_center.html")
        self.assertIn("Remove Result", source)
        self.assertIn("Remove this identified result?", source)
        self.assertIn("item.research_state == 'RESEARCH_RESULT'", source)

    def test_historical_request_attachments_are_server_protected(self):
        routes = self.read("plg_core/requests/routes.py")
        template = self.read("templates/request_detail.html")
        self.assertIn("attachment_removable", routes)
        self.assertIn("Historical Request attachments are preserved", routes)
        self.assertIn("{% if attachment_removable %}", template)

    def test_archive_and_deactivation_write_audit(self):
        app = self.read("legacy_app.py")
        for action in ("CUSTOMER_DEACTIVATED", "SUPPLIER_DEACTIVATED"):
            self.assertIn(action, app)
        lifecycle = self.read("plg_core/lifecycle/service.py")
        for action in ("JOB_ARCHIVED", "JOB_RESTORED"):
            self.assertIn(action, lifecycle)
        requests = self.read("plg_core/requests/routes.py")
        self.assertIn("REQUEST_ARCHIVED", requests)


if __name__ == "__main__":
    unittest.main()
