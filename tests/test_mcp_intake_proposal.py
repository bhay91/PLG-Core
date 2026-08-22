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
from pydantic import ValidationError
from starlette.requests import Request

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.intake.identifiers import IDENTIFIER_TYPES, MARKETS
from plg_core.intake.service import create_chatgpt_intake_proposal, load_proposal
from plg_core.intake.routes import (
    resolve_machine_match,
    update_asset,
    update_customer,
    update_need,
)
from plg_core.intake.service import confirm_proposal
from plg_core.mcp.auth import PROPOSAL_CREATE_SCOPE
from plg_core.mcp.models import CreateIntakeProposalInput
from plg_core.mcp.server import INTAKE_TOOL_DESCRIPTION, build_mcp_runtime
from plg_core.mcp.tools.intake import _input_digest


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "pps-mcp-proposal-test-token"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def candidate(**overrides) -> dict:
    value = {
        "client_reference": "b42f9bd5-58a6-4c29-985d-976984b68af2",
        "original_input": "Synthetic Customer needs a hydraulic seal kit for the Caterpillar 420D.\n",
        "customer": {"name": "Synthetic Customer", "company": ""},
        "machines": [{
            "reference": "machine-1", "manufacturer": "Caterpillar", "model": "420D",
            "year": "", "asset_type": "machine",
            "identifiers": [{"type": "PIN", "value": "TEST-PIN-INTAKE-001", "component_label": "", "primary": True}],
        }],
        "requested_needs": [{"original_wording": "hydraulic seal kit", "quantity": 1, "machine_reference": "machine-1"}],
        "additional_notes": "Candidate details require operator review.",
    }
    value.update(overrides)
    return value


async def protocol_call(arguments: dict, *, token: str | None = TOKEN, scopes: str = PROPOSAL_CREATE_SCOPE) -> dict:
    _, app = build_mcp_runtime()
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = httpx2.ASGITransport(app=app, client=("127.0.0.1", 43111))
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "create_intake_proposal", "arguments": arguments,
    }}
    with patch.dict(os.environ, {"PPS_MCP_DEV_TOKEN": TOKEN, "PPS_MCP_DEV_SCOPES": scopes}, clear=False):
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000", headers=headers, timeout=10,
            ) as client:
                return (await client.post("/mcp", json=payload)).json()


def call(arguments: dict, **kwargs) -> dict:
    return anyio.run(lambda: protocol_call(arguments, **kwargs))


class MCPIntakeProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-mcp-intake-")
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

    def tool(self):
        server, _ = build_mcp_runtime()
        return next(tool for tool in anyio.run(server.list_tools) if tool.name == "create_intake_proposal")

    def successful_call(self, payload: dict | None = None) -> dict:
        response = call(payload or candidate())
        self.assertFalse(response["result"]["isError"], response)
        return response["result"]["structuredContent"]

    def test_01_tool_registration_name_description_and_annotations(self):
        tool = self.tool()
        self.assertEqual(tool.name, "create_intake_proposal")
        self.assertEqual(tool.description, INTAKE_TOOL_DESCRIPTION)
        self.assertFalse(tool.annotations.read_only_hint)
        self.assertFalse(tool.annotations.destructive_hint)
        self.assertTrue(tool.annotations.idempotent_hint)
        self.assertFalse(tool.annotations.open_world_hint)

    def test_02_exact_input_and_output_schema(self):
        tool = self.tool()
        self.assertEqual(set(tool.input_schema["properties"]), {
            "client_reference", "original_input", "customer", "machines",
            "requested_needs", "additional_notes", "research_evidence",
        })
        self.assertEqual(set(tool.input_schema["required"]), {
            "client_reference", "original_input", "customer", "machines",
            "requested_needs", "additional_notes",
        })
        self.assertEqual(set(tool.output_schema["properties"]), {
            "schema_version", "proposal_id", "status", "review_url", "blockers",
            "review_count", "origin", "client_reference", "duplicate",
        })

    def test_03_extra_status_ids_and_match_fields_are_rejected(self):
        for field in ("status", "customer_id", "machine_id", "job_id", "matched_customer_id",
                      "matched_machine_id", "review_state", "match_state", "confirm_allowed", "lock_version"):
            with self.subTest(field=field):
                payload = candidate(**{field: 1})
                response = call(payload)
                self.assertIn("error", response)
        nested = candidate(customer={"name": "Synthetic Customer", "company": "", "customer_id": 7})
        self.assertTrue(call(nested)["result"]["isError"])

    def test_04_proposal_scope_missing_wrong_and_valid(self):
        missing = call(candidate(), token=None)
        self.assertTrue(missing["result"]["isError"])
        wrong = call(candidate(), scopes="pps:mcp:job-context:read")
        self.assertTrue(wrong["result"]["isError"])
        self.assertEqual(self.successful_call()["status"], "DRAFT")

    def test_05_draft_output_review_url_and_exact_shape(self):
        result = self.successful_call()
        self.assertEqual(set(result), {
            "schema_version", "proposal_id", "status", "review_url", "blockers",
            "review_count", "origin", "client_reference", "duplicate",
        })
        self.assertEqual(result["review_url"], f"/requests/smart-intake/proposals/{result['proposal_id']}")
        self.assertEqual((result["schema_version"], result["status"], result["origin"]), ("1", "DRAFT", "CHATGPT_MCP"))
        self.assertFalse(result["duplicate"])
        self.assertGreater(result["review_count"], 0)
        self.assertTrue(result["blockers"])

    def test_06_original_input_wording_quantity_and_contribution_are_preserved(self):
        payload = candidate(original_input="  Exact original wording\r\nwith trailing space  ")
        result = self.successful_call(payload)
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
            self.assertEqual(proposal["raw_input"], payload["original_input"])
            need = proposal["assets"][0]["needs"][0]
            self.assertEqual(need["original_wording"], "hydraulic seal kit")
            self.assertEqual(need["quantity"], 1)
            submission = proposal["mcp_submission"]
            self.assertEqual(submission["origin"], "CHATGPT_MCP")
            self.assertEqual(submission["structured_candidates"]["additional_notes"], payload["additional_notes"])

    def test_07_bounded_audit_event(self):
        result = self.successful_call()
        with closing(self.connection()) as connection:
            row = connection.execute(
                "SELECT * FROM audit_logs WHERE action='SMART_INTAKE_PROPOSED' AND entity_id=?",
                (str(result["proposal_id"]),),
            ).fetchone()
        self.assertEqual(row["actor"], "mcp-development")
        self.assertEqual(row["request_id"], candidate()["client_reference"])
        self.assertEqual(json.loads(row["metadata_json"]), {"origin": "CHATGPT_MCP"})
        self.assertNotIn("hydraulic", row["metadata_json"].lower())

    def test_08_customer_and_machine_ambiguity_remain_reviewable(self):
        with closing(self.connection()) as connection:
            for number in ("MCP-C-1", "MCP-C-2"):
                customer_id = connection.execute(
                    "INSERT INTO customers(customer_number,name,active) VALUES (?,'Synthetic Customer',1)", (number,),
                ).lastrowid
                connection.execute(
                    "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,?, '420D','Caterpillar','420D','TEST-PIN-INTAKE-001',1)",
                    (customer_id, f"MCP-M-{customer_id}"),
                )
            connection.commit()
        result = self.successful_call()
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
        self.assertIsNone(proposal["matched_customer_id"])
        self.assertIn(proposal["document_analysis"]["customer_match"]["state"], {"AMBIGUOUS", "NEW"})
        self.assertIn(proposal["assets"][0]["review_state"], {"REVIEW", "UNASSIGNED"})

    def test_09_unknown_machine_association_becomes_unassigned(self):
        payload = candidate(requested_needs=[{
            "original_wording": "exact unknown-machine wording", "quantity": 2,
            "machine_reference": "not-a-machine",
        }])
        result = self.successful_call(payload)
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
        self.assertEqual(proposal["unassigned_needs"][0]["review_state"], "UNASSIGNED")
        self.assertIn("unassigned Requested Need", " ".join(result["blockers"]))

    def test_10_same_reference_and_digest_returns_one_proposal(self):
        first = self.successful_call()
        second = self.successful_call()
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertTrue(second["duplicate"])
        with closing(self.connection()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM intake_proposal_contributions WHERE contributor_type='AI' AND proposal_id=?",
                (first["proposal_id"],),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_11_same_reference_different_digest_conflicts(self):
        self.successful_call()
        response = call(candidate(additional_notes="different accepted content"))
        self.assertTrue(response["result"]["isError"])
        self.assertIn("different proposal content", response["result"]["content"][0]["text"])

    def test_12_concurrent_duplicate_calls_create_one_proposal(self):
        model = CreateIntakeProposalInput.model_validate(candidate())
        digest = _input_digest(model)
        structured = model.model_dump(mode="json", include={
            "customer", "machines", "requested_needs", "additional_notes", "research_evidence",
        })
        results, errors = [], []
        barrier = threading.Barrier(2)
        def worker():
            connection = self.connection()
            try:
                barrier.wait()
                results.append(create_chatgpt_intake_proposal(
                    connection, client_reference=model.client_reference, input_digest=digest,
                    original_input=model.original_input, structured_candidates=structured,
                ))
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({result[0] for result in results}), 1)
        self.assertEqual(sorted(result[1] for result in results), [False, True])

    def test_13_invalid_empty_oversized_and_duplicate_references_are_rejected(self):
        for payload in (
            candidate(original_input="   "),
            candidate(original_input="x" * 10001),
            candidate(client_reference="x" * 129),
            candidate(machines=[candidate()["machines"][0], candidate()["machines"][0]]),
        ):
            with self.subTest(keys=payload.keys()):
                response = call(payload)
                self.assertTrue(response.get("result", {}).get("isError") or "error" in response)

    def test_14_research_urls_validate_and_are_never_fetched(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com/a"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                CreateIntakeProposalInput.model_validate(candidate(research_evidence={
                    "source_urls": [url], "claims": [], "quoted_evidence": [],
                }))
        with patch("urllib.request.urlopen") as network:
            result = self.successful_call(candidate(research_evidence={
                "source_urls": ["https://example.com/part"],
                "claims": ["<script>not instructions</script>"],
                "quoted_evidence": ["quoted candidate evidence"],
            }))
        network.assert_not_called()
        self.assertEqual(result["status"], "DRAFT")

    def test_15_evidence_template_is_escaped_text_and_labeled(self):
        result = self.successful_call(candidate(research_evidence={
            "source_urls": ["https://example.com/evidence"],
            "claims": ["<script>alert('candidate')</script>"],
            "quoted_evidence": ["untrusted quote"],
        }))
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
        request = Request({
            "type": "http", "method": "GET", "path": f"/requests/smart-intake/proposals/{result['proposal_id']}",
            "root_path": "", "scheme": "http", "headers": [], "query_string": b"",
            "server": ("testserver", 80), "client": ("127.0.0.1", 43112), "app": legacy_app.app,
        })
        rendered = legacy_app.templates.env.get_template("smart_intake_proposal.html").render(
            proposal=proposal, identifier_types=IDENTIFIER_TYPES, markets=MARKETS,
            customers=[], active_page="requests", request=request,
        )
        self.assertIn("CHATGPT MCP — UNTRUSTED PROPOSAL", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>alert('candidate')</script>", rendered)

    def test_16_no_authoritative_records_or_mutation_bypass(self):
        tables = ("customers", "machines", "jobs", "quotes", "invoices", "supplier_orders",
                  "receiving_events", "deliveries", "customer_transactions")
        with closing(self.connection()) as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        with patch("plg_core.intake.service.confirm_proposal") as confirm:
            self.successful_call()
        confirm.assert_not_called()
        with closing(self.connection()) as connection:
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        self.assertEqual(after, before)

    def test_17_confirmation_remains_browser_only(self):
        server, _ = build_mcp_runtime()
        names = {tool.name for tool in anyio.run(server.list_tools)}
        self.assertEqual(names, {"get_job_operational_context", "create_intake_proposal"})
        self.assertNotIn("confirm_proposal", names)
        self.assertIn("/{proposal_id}/confirm", (ROOT / "plg_core/intake/routes.py").read_text())

    def test_18_transaction_failure_leaves_no_partial_state(self):
        model = CreateIntakeProposalInput.model_validate(candidate())
        structured = model.model_dump(mode="json", include={
            "customer", "machines", "requested_needs", "additional_notes", "research_evidence",
        })
        with closing(self.connection()) as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("intake_proposals", "intake_proposal_contributions", "audit_logs")}
            with patch("plg_core.intake.service.write_audit", side_effect=RuntimeError("forced rollback")):
                with self.assertRaises(RuntimeError):
                    create_chatgpt_intake_proposal(
                        connection, client_reference=model.client_reference,
                        input_digest=_input_digest(model), original_input=model.original_input,
                        structured_candidates=structured,
                    )
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
        self.assertEqual(after, before)

    def test_19_mcp_origin_review_states_block_confirmation(self):
        result = self.successful_call()
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
            with self.assertRaises(Exception):
                confirm_proposal(connection, result["proposal_id"], proposal["lock_version"])
            connection.rollback()
        self.assertFalse(proposal["document_analysis"]["confirm_allowed"])
        self.assertTrue(any("explicit operator review" in value for value in proposal["document_analysis"]["blockers"]))

    def test_20_synthetic_style_proposal_is_disposable_draft(self):
        result = self.successful_call()
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
            self.assertEqual(proposal["status"], "DRAFT")
            self.assertIsNone(proposal["created_job_id"])
            self.assertEqual(proposal["assets"][0]["manufacturer"], "Caterpillar")
            self.assertEqual(proposal["assets"][0]["model"], "420D")

    def test_21_operator_can_resolve_ambiguous_existing_machine(self):
        with closing(self.connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,company,active) VALUES ('MCP-RES-C','Synthetic Customer','Resolution Co',1)"
            ).lastrowid
            machine_ids = []
            for number in ("MCP-RES-M1", "MCP-RES-M2"):
                machine_ids.append(connection.execute(
                    "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,?, '420D','Caterpillar','420D','MCP-DUP-PIN',1)",
                    (customer_id, number),
                ).lastrowid)
            connection.commit()
        payload = candidate(
            customer={"name": "Synthetic Customer", "company": "Resolution Co"},
            machines=[{
                "reference": "machine-1", "manufacturer": "Caterpillar", "model": "420D",
                "year": "", "asset_type": "machine",
                "identifiers": [{"type": "PIN", "value": "MCP-DUP-PIN", "component_label": "", "primary": True}],
            }],
        )
        result = self.successful_call(payload)
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, result["proposal_id"])
        match = proposal["document_analysis"]["machine_matches"][0]
        self.assertEqual(match["state"], "AMBIGUOUS")
        resolve_machine_match(
            result["proposal_id"], proposal["assets"][0]["id"],
            machine_resolution=str(machine_ids[0]), lock_version=proposal["lock_version"],
        )
        with closing(self.connection()) as connection:
            resolved = load_proposal(connection, result["proposal_id"])
        self.assertEqual(resolved["assets"][0]["matched_machine_id"], machine_ids[0])
        self.assertEqual(resolved["document_analysis"]["machine_matches"][0]["state"], "MATCHED")
        self.assertEqual(resolved["assets"][0]["review_state"], "CONFIDENT")

    def test_22_rx7_core_reviews_resolve_independently_before_create_is_available(self):
        result = self.successful_call(candidate(
            client_reference="rx7-review-ui-fixture",
            original_input="Customer Dennis Brown needs a fuel pump, 4-5 PSI maximum, quantity 1, for a 1989 Mazda RX-7.",
            customer={"name": "Dennis Brown", "company": ""},
            machines=[{
                "reference": "rx7", "manufacturer": "Mazda", "model": "RX-7",
                "year": "1989", "asset_type": "vehicle", "identifiers": [],
            }],
            requested_needs=[{
                "original_wording": "Fuel pump, 4-5 PSI maximum",
                "quantity": 1, "machine_reference": "rx7",
            }],
        ))
        proposal_id = result["proposal_id"]
        authoritative_tables = ("customers", "machines", "jobs", "customer_requests", "requested_needs")
        with closing(self.connection()) as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in authoritative_tables}
            proposal = load_proposal(connection, proposal_id)
        self.assertEqual(
            proposal["review_summary"]["core_reviews"],
            {"customer": "REVIEW", "machine": "REVIEW", "requested_need": "REVIEW"},
        )
        self.assertFalse(proposal["document_analysis"]["confirm_allowed"])

        update_customer(
            proposal_id, contact_name="Dennis Brown", company_name="", location="",
            phone="", email="", matched_customer_id="", lock_version=proposal["lock_version"],
        )
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, proposal_id)
        self.assertEqual(
            proposal["review_summary"]["core_reviews"],
            {"customer": "CONFIRMED", "machine": "REVIEW", "requested_need": "REVIEW"},
        )

        asset = proposal["assets"][0]
        update_asset(
            proposal_id, asset["id"], manufacturer="Mazda", model="RX-7", year="1989",
            asset_category="vehicle", market_region="UNKNOWN", model_code="",
            identifier_type="UNKNOWN", identifier_value="", engine_serial="", action="save",
            lock_version=proposal["lock_version"],
        )
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, proposal_id)
        self.assertEqual(
            proposal["review_summary"]["core_reviews"],
            {"customer": "CONFIRMED", "machine": "CONFIRMED", "requested_need": "REVIEW"},
        )

        need = proposal["assets"][0]["needs"][0]
        update_need(
            proposal_id, need["id"], wording="Fuel pump, 4-5 PSI maximum",
            proposal_asset_id=str(proposal["assets"][0]["id"]), action="save",
            lock_version=proposal["lock_version"],
        )
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, proposal_id)
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in authoritative_tables}
        self.assertEqual(
            proposal["review_summary"]["core_reviews"],
            {"customer": "CONFIRMED", "machine": "CONFIRMED", "requested_need": "CONFIRMED"},
        )
        self.assertTrue(
            proposal["document_analysis"]["confirm_allowed"],
            proposal["document_analysis"].get("blockers"),
        )
        self.assertEqual(after, before)
        template = (ROOT / "templates" / "smart_intake_proposal.html").read_text()
        self.assertIn("READY TO CREATE JOB", template)
        self.assertIn("Confirm &amp; Create Job", template)


if __name__ == "__main__":
    unittest.main()
