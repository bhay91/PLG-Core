from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import anyio
import httpx2

import legacy_app
from plg_core.application import app
from plg_core.database.migrations import run_migrations
from plg_core.intake.service import (
    confirm_proposal, load_proposal, refresh_proposal_analysis, submit_structured_intake,
)
from plg_core.intake.submission_models import FirefoxIntakePackage
from plg_core.requests.extension_auth import FIREFOX_INBOX_CREATE_SCOPE
from plg_core.requests.extension_routes import router as firefox_inbox_router
from plg_core.requests.routes import list_requests
from plg_core.intake.routes import review as review_proposal
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "firefox-inbox-test-token"


def package(**overrides) -> dict:
    result = {
        "schema_version": "1",
        "source": "CHATGPT_FIREFOX",
        "client_reference": "c17e923e-372a-4e75-b49f-a83dfbb30a73",
        "original_input": "Synthetic Customer needs a hydraulic cylinder seal kit for a Caterpillar 420D.",
        "customer": {"name": "Synthetic Customer", "company": ""},
        "machines": [{
            "reference": "machine-1", "manufacturer": "Caterpillar", "model": "420D",
            "year": "2004", "asset_type": "machine",
            "identifiers": [{"type": "PIN", "value": "TEST420D3D3", "component_label": "", "primary": True}],
        }],
        "requested_needs": [{
            "original_wording": "Hydraulic cylinder seal kit", "quantity": 1,
            "machine_reference": "machine-1",
        }],
        "additional_notes": "<script>alert('proposal data only')</script>; DROP TABLE jobs;",
        "research_evidence": {
            "source_urls": ["https://example.test/parts/420d"],
            "claims": ["Candidate seal kit needs operator verification."],
            "quoted_evidence": ["Untrusted quoted evidence."],
        },
    }
    result.update(overrides)
    return result


async def request_endpoint(payload: object, *, token: str | None = TOKEN,
                           scopes: str = FIREFOX_INBOX_CREATE_SCOPE,
                           client_host: str = "127.0.0.1",
                           remote_enabled: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = httpx2.ASGITransport(app=app, client=(client_host, 45211))
    environment = {"PPS_FIREFOX_INBOX_TOKEN": TOKEN, "PPS_FIREFOX_INBOX_SCOPES": scopes}
    environment["PPS_FIREFOX_REMOTE_ENABLED"] = remote_enabled or ""
    with patch.dict(os.environ, environment, clear=False):
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
            return await client.post("/api/extension/v1/inbox/intake-proposals", headers=headers, json=payload)


def call(payload: object, **kwargs):
    return anyio.run(lambda: request_endpoint(payload, **kwargs))


async def get_page(path: str):
    transport = httpx2.ASGITransport(app=app, client=("127.0.0.1", 45212))
    async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        return await client.get(path)


class FirefoxInboxIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-firefox-inbox-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def successful(self, payload=None):
        response = call(payload or package())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def counts(self):
        with closing(self.connection()) as connection:
            tables = [
                "customers", "machines", "jobs", "quotes", "invoices", "supplier_orders",
                "receiving_events", "deliveries",
            ]
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}

    def test_01_endpoint_registered_and_authorization_boundary(self):
        self.assertEqual([route.path for route in firefox_inbox_router.routes], [
            "/api/extension/v1/inbox/intake-proposals",
            "/api/extension/v1/research-import/packages",
            "/api/extension/v1/jobs/payment-received",
            "/api/extension/v1/jobs/order-placed",
            "/api/extension/v1/jobs/item-received",
            "/api/extension/v1/jobs/item-delivered",
        ])
        self.assertEqual(call(package(), token=None).status_code, 401)
        self.assertEqual(call(package(), token="wrong").status_code, 401)
        self.assertEqual(call(package(), scopes="wrong:scope").status_code, 403)
        self.assertEqual(call(package(), client_host="198.51.100.9").status_code, 403)
        self.assertEqual(call(package()).status_code, 200)

    def test_remote_inbox_requires_explicit_enable_token_and_create_scope(self):
        remote = {"client_host": "198.51.100.9", "remote_enabled": "true"}
        self.assertEqual(call(package(), **remote).status_code, 200)
        self.assertEqual(call(package(), token=None, **remote).status_code, 401)
        self.assertEqual(call(package(), token="wrong", **remote).status_code, 401)
        self.assertEqual(call(package(), scopes="wrong:scope", **remote).status_code, 403)
        self.assertEqual(
            call(package(), scopes="pps:firefox:jobs:update", **remote).status_code,
            403,
        )

    def test_02_valid_package_creates_only_draft_and_appears_in_inbox(self):
        before = self.counts()
        with patch("plg_core.intake.service.confirm_proposal") as confirm:
            result = self.successful()
        confirm.assert_not_called()
        self.assertEqual((result["status"], result["origin"], result["duplicate"]), ("DRAFT", "CHATGPT_FIREFOX", False))
        self.assertEqual(result["review_url"], f"/requests/smart-intake/proposals/{result['proposal_id']}")
        self.assertGreater(result["review_count"], 0)
        with closing(self.connection()) as connection:
            row = connection.execute("SELECT status,created_request_id,created_job_id FROM intake_proposals WHERE id=?", (result["proposal_id"],)).fetchone()
        self.assertEqual(tuple(row), ("DRAFT", None, None))
        self.assertEqual(before, self.counts())
        scope = {"type": "http", "method": "GET", "path": "/requests", "headers": [], "query_string": b"", "server": ("testserver", 80), "client": ("127.0.0.1", 1), "scheme": "http", "app": app}
        inbox = list_requests(Request(scope))
        self.assertIn(result["review_url"], inbox.body.decode())

    def test_03_original_wording_provenance_and_bounded_audit(self):
        payload = package(original_input="  Exact original input\r\nwith spacing  ")
        result = self.successful(payload)
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
            audit = connection.execute("SELECT * FROM audit_logs WHERE action='SMART_INTAKE_PROPOSED' AND entity_id=?", (str(result["proposal_id"]),)).fetchone()
            contribution = connection.execute("SELECT * FROM intake_proposal_contributions WHERE proposal_id=? AND contributor_type='AI'", (result["proposal_id"],)).fetchone()
        self.assertEqual(proposal["raw_input"], payload["original_input"])
        self.assertEqual(proposal["assets"][0]["needs"][0]["original_wording"], "Hydraulic cylinder seal kit")
        self.assertEqual(proposal["firefox_submission"]["origin"], "CHATGPT_FIREFOX")
        stored = json.loads(contribution["payload_json"])
        self.assertEqual(stored["origin"], "CHATGPT_FIREFOX")
        self.assertEqual(contribution["evidence"], "Submitted through the authenticated PPS Firefox Inbox bridge.")
        self.assertEqual((audit["actor"], audit["request_id"]), ("firefox-extension-local", payload["client_reference"]))
        self.assertEqual(json.loads(audit["metadata_json"]), {"origin": "CHATGPT_FIREFOX"})
        self.assertNotIn("hydraulic", audit["metadata_json"].lower())

    def test_04_strict_schema_source_ids_financial_and_unsafe_urls_rejected(self):
        invalid = [
            package(source="CHATGPT_MCP"), package(extra="no"), package(customer_id=1),
            package(revenue=100), package(job_id=7), package(document_path="/tmp/a.pdf"),
            package(research_evidence={"source_urls": ["file:///etc/passwd"], "claims": [], "quoted_evidence": []}),
            package(customer={"name": "Synthetic Customer", "company": "", "matched_customer_id": 1}),
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(call(payload).status_code, 422)

    def test_05_same_reference_duplicate_and_changed_content_conflict(self):
        first = self.successful()
        second = self.successful()
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertTrue(second["duplicate"])
        conflict = call(package(additional_notes="different"))
        self.assertEqual(conflict.status_code, 409)
        with closing(self.connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM intake_proposals WHERE id=?", (first["proposal_id"],)).fetchone()[0], 1)

    def test_06_concurrent_duplicate_service_creates_one_proposal(self):
        model = FirefoxIntakePackage.model_validate(package())
        results, errors = [], []
        barrier = threading.Barrier(2)
        def worker():
            connection = self.connection()
            try:
                barrier.wait()
                results.append(submit_structured_intake(
                    connection, origin="CHATGPT_FIREFOX", client_reference=model.client_reference,
                    input_digest=model.normalized_digest(), original_input=model.original_input,
                    structured_candidates=model.structured_candidates(), actor="firefox-extension-local",
                    evidence="Submitted through the authenticated PPS Firefox Inbox bridge.",
                ))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(len({proposal_id for proposal_id, _ in results}), 1)
        self.assertEqual(sorted(duplicate for _, duplicate in results), [False, True])

    def test_07_text_is_data_urls_are_not_fetched_and_review_label_is_safe(self):
        result = self.successful()
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
        self.assertIn("<script>", proposal["firefox_submission"]["structured_candidates"]["additional_notes"])
        scope = {"type": "http", "method": "GET", "path": result["review_url"], "headers": [], "query_string": b"", "server": ("testserver", 80), "client": ("127.0.0.1", 1), "scheme": "http", "app": app}
        rendered = review_proposal(Request(scope), result["proposal_id"]).body.decode()
        self.assertIn("CHATGPT FIREFOX — UNTRUSTED PROPOSAL", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>alert", rendered)

    def test_08_transaction_failure_leaves_no_partial_state(self):
        before = self.counts()
        with closing(self.connection()) as connection:
            before_contributions = connection.execute(
                "SELECT COUNT(*) FROM intake_proposal_contributions "
                "WHERE payload_json LIKE '%CHATGPT_FIREFOX%'"
            ).fetchone()[0]
        with closing(self.connection()) as connection:
            with patch("plg_core.intake.service.write_audit", side_effect=RuntimeError("forced")):
                with self.assertRaises(RuntimeError):
                    submit_structured_intake(
                        connection, origin="CHATGPT_FIREFOX", client_reference=package()["client_reference"],
                        input_digest=FirefoxIntakePackage.model_validate(package()).normalized_digest(),
                        original_input=package()["original_input"],
                        structured_candidates=FirefoxIntakePackage.model_validate(package()).structured_candidates(),
                        actor="firefox-extension-local", evidence="bounded",
                    )
        self.assertEqual(before, self.counts())
        with closing(self.connection()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM intake_proposal_contributions "
                    "WHERE payload_json LIKE '%CHATGPT_FIREFOX%'"
                ).fetchone()[0],
                before_contributions,
            )

    def test_09_structured_research_options_are_stored_and_rendered_review_only(self):
        option = {
            "source_url": "https://supplier.example.test/mazda-rx7/fuel-pump",
            "source_name": "Example Parts Supplier",
            "product_description": "Low-pressure electric fuel pump, 4–5 PSI",
            "part_number": "FP-RX7-45",
            "price": 89.95,
            "currency": "USD",
            "research_notes": "Candidate only; verify fitment and pressure before selection.",
            "confidence": "MEDIUM",
            "verification_status": "NEEDS_REVIEW",
        }
        payload = package(research_evidence={
            "source_urls": [], "claims": [], "quoted_evidence": [],
            "options": [option],
        })
        before = self.counts()
        result = self.successful(payload)
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
        stored = proposal["firefox_submission"]["structured_candidates"]["research_evidence"]
        self.assertEqual(stored["options"], [option])
        scope = {"type": "http", "method": "GET", "path": result["review_url"], "headers": [], "query_string": b"", "server": ("testserver", 80), "client": ("127.0.0.1", 1), "scheme": "http", "app": app}
        rendered = review_proposal(Request(scope), result["proposal_id"]).body.decode()
        for value in ("Example Parts Supplier", "FP-RX7-45", "USD 89.95", "Needs Review", "Open research source"):
            self.assertIn(value, rendered)
        self.assertIn(option["source_url"], rendered)
        self.assertEqual(before, self.counts())

    def test_10_confirmed_proposal_preserves_research_as_unselected_reference_material(self):
        option = {
            "source_url": "https://supplier.example.test/mazda-rx7/fuel-pump",
            "source_name": "Example Parts Supplier",
            "product_description": "Low-pressure electric fuel pump, 4–5 PSI",
            "part_number": "FP-RX7-45", "price": 89.95, "currency": "USD",
            "research_notes": "Verify RX-7 fitment before selection.",
            "confidence": "MEDIUM", "verification_status": "NEEDS_REVIEW",
        }
        payload = package(
            client_reference="research-continuity-confirm",
            research_evidence={"source_urls": [option["source_url"]],
                               "claims": ["Candidate only."], "quoted_evidence": [],
                               "options": [option]},
        )
        result = self.successful(payload)
        with closing(self.connection()) as connection:
            proposal_id = result["proposal_id"]
            connection.execute("UPDATE intake_proposals SET review_state='CONFIDENT' WHERE id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_assets SET review_state='CONFIDENT' WHERE proposal_id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_identifiers SET review_state='CONFIDENT' WHERE proposal_id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_needs SET review_state='CONFIDENT' WHERE proposal_id=?", (proposal_id,))
            refresh_proposal_analysis(connection, proposal_id)
            connection.commit()
            version = connection.execute("SELECT lock_version FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
            job_id = confirm_proposal(connection, proposal_id, version)
        with closing(self.connection()) as connection:
            item = connection.execute(
                "SELECT bi.* FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id WHERE b.job_id=?",
                (job_id,),
            ).fetchone()
            source = connection.execute("SELECT * FROM basket_sources WHERE basket_id=?", (item["basket_id"],)).fetchone()
            authoritative = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table} WHERE job_id=?", (job_id,)).fetchone()[0]
                for table in ("quotes", "invoices", "supplier_orders")
            }
        self.assertEqual((item["research_state"], item["selected"]), ("RESEARCH_RESULT", 0))
        self.assertEqual((item["supplier_name"], item["supplier_part_number"], item["supplier_unit_cost"]),
                         (option["source_name"], option["part_number"], option["price"]))
        preserved = json.loads(item["research_evidence"])
        for key in ("source_url", "source_name", "product_description", "part_number", "price",
                    "currency", "research_notes", "confidence", "verification_status"):
            self.assertEqual(preserved[key], option[key])
        self.assertEqual((source["source_url"], source["trust_level"]), (option["source_url"], "NEEDS_REVIEW"))
        self.assertEqual(authoritative, {"quotes": 0, "invoices": 0, "supplier_orders": 0})

    def test_11_disposable_dennis_rx7_end_to_end_stays_draft(self):
        before = self.counts()
        payload = package(
            client_reference="disposable-dennis-rx7-control-pass",
            original_input="Dennis Brown; 1989 Mazda RX-7; Fuel pump, 4–5 PSI maximum; quantity 1.",
            customer={"name": "Dennis Brown", "company": ""},
            machines=[{"reference": "rx7", "manufacturer": "Mazda", "model": "RX-7",
                       "year": "1989", "asset_type": "vehicle", "identifiers": []}],
            requested_needs=[{"original_wording": "Fuel pump, 4–5 PSI maximum", "quantity": 1,
                              "machine_reference": "rx7"}],
            additional_notes="Disposable isolated Smart Intake bridge verification.",
            research_evidence={
                "source_urls": ["https://example.test/rx7/fuel-pump"],
                "claims": ["Candidate requires operator fitment review."],
                "quoted_evidence": [],
                "options": [{
                    "source_url": "https://example.test/rx7/fuel-pump",
                    "source_name": "Disposable Research Source",
                    "product_description": "12V low-pressure fuel pump, 4–5 PSI",
                    "part_number": "TEST-RX7-PUMP", "price": 67.79, "currency": "USD",
                    "research_notes": "Research evidence only; verify configuration and fittings.",
                    "confidence": "MEDIUM", "verification_status": "NEEDS_REVIEW",
                }],
            },
        )
        result = self.successful(payload)
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
        self.assertEqual((proposal["status"], proposal["created_job_id"]), ("DRAFT", None))
        self.assertEqual(proposal["contact_name"], "Dennis Brown")
        self.assertEqual((proposal["assets"][0]["manufacturer"], proposal["assets"][0]["model"],
                          proposal["assets"][0]["year"]), ("Mazda", "RX-7", "1989"))
        self.assertEqual(proposal["assets"][0]["needs"][0]["wording"], "Fuel pump, 4–5 PSI maximum")
        scope = {"type": "http", "method": "GET", "path": result["review_url"], "headers": [],
                 "query_string": b"", "server": ("testserver", 80), "client": ("127.0.0.1", 1),
                 "scheme": "http", "app": app}
        rendered = review_proposal(Request(scope), result["proposal_id"]).body.decode()
        for value in ("Dennis Brown", "Mazda", "RX-7", "Fuel pump, 4–5 PSI maximum",
                      "Disposable Research Source", "TEST-RX7-PUMP"):
            self.assertIn(value, rendered)
        self.assertEqual(before, self.counts())


if __name__ == "__main__":
    unittest.main()
