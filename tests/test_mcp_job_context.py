from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import anyio
import httpx2
from fastapi import HTTPException

from plg_core.mcp.auth import JOB_CONTEXT_READ_SCOPE
from plg_core.mcp.models import normalize_job_number
from plg_core.mcp.server import MCP_INSTRUCTIONS, build_mcp_runtime
from tests.test_ai_job_context import context


TOKEN = "pps-mcp-test-token"
HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


async def protocol_request(
    method: str,
    params: dict,
    *,
    token: str | None = TOKEN,
    client_host: str = "127.0.0.1",
) -> tuple[int, dict]:
    _, app = build_mcp_runtime()
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = httpx2.ASGITransport(app=app, client=(client_host, 43110))
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    with patch.dict(os.environ, {
        "PPS_MCP_DEV_TOKEN": TOKEN,
        "PPS_MCP_DEV_SCOPES": JOB_CONTEXT_READ_SCOPE,
    }, clear=False):
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8000",
                headers=headers,
                timeout=5,
            ) as client:
                response = await client.post("/mcp", json=payload)
    return response.status_code, response.json()


def request(method: str, params: dict, **kwargs) -> tuple[int, dict]:
    return anyio.run(lambda: protocol_request(method, params, **kwargs))


def call_arguments(arguments: dict, **kwargs) -> tuple[int, dict]:
    return request("tools/call", {
        "name": "get_job_operational_context",
        "arguments": arguments,
    }, **kwargs)


class MCPJobContextTests(unittest.TestCase):
    def setUp(self):
        self.server, _ = build_mcp_runtime()

    def tools(self):
        return anyio.run(self.server.list_tools)

    def tool(self):
        return next(tool for tool in self.tools() if tool.name == "get_job_operational_context")

    def successful_call(self, value: str = "PPS-J-9007") -> dict:
        with patch("plg_core.mcp.tools.jobs.resolve_job_id_by_number", return_value=7), patch(
            "plg_core.mcp.tools.jobs.build_internal_job_context", return_value=context()
        ):
            status, payload = call_arguments({"job_number": value})
        self.assertEqual(status, 200)
        self.assertFalse(payload["result"]["isError"])
        return payload["result"]["structuredContent"]

    def test_01_mcp_server_initializes(self):
        status, payload = request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "pps-tests", "version": "1"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["serverInfo"]["name"], "pps-read-only")
        self.assertEqual(payload["result"]["instructions"], MCP_INSTRUCTIONS)

    def test_02_tool_is_registered(self):
        self.assertIn("get_job_operational_context", {tool.name for tool in self.tools()})

    def test_03_exact_tool_name(self):
        self.assertEqual(self.tool().name, "get_job_operational_context")

    def test_04_input_schema(self):
        schema = self.tool().input_schema
        self.assertEqual(schema["required"], ["job_number"])
        self.assertEqual(set(schema["properties"]), {"job_number"})
        self.assertEqual(schema["properties"]["job_number"]["pattern"], r"^PPS-J-[0-9]{4,12}$")

    def test_05_output_schema(self):
        properties = set(self.tool().output_schema["properties"])
        self.assertTrue({"schema_version", "audience", "context_id", "job", "movement", "financial"} <= properties)

    def test_06_read_only_hint(self):
        self.assertTrue(self.tool().annotations.read_only_hint)

    def test_07_open_world_hint(self):
        self.assertFalse(self.tool().annotations.open_world_hint)

    def test_08_destructive_hint(self):
        self.assertFalse(self.tool().annotations.destructive_hint)

    def test_09_valid_authorization(self):
        self.assertEqual(self.successful_call()["audience"], "INTERNAL")

    def test_10_missing_authorization(self):
        _, payload = call_arguments({"job_number": "PPS-J-9007"}, token=None)
        self.assertTrue(payload["result"]["isError"])
        self.assertIn("authorization is required", payload["result"]["content"][0]["text"])

    def test_11_invalid_authorization(self):
        _, payload = call_arguments({"job_number": "PPS-J-9007"}, token="wrong-token")
        self.assertTrue(payload["result"]["isError"])
        self.assertIn("authorization is invalid", payload["result"]["content"][0]["text"])

    def test_12_valid_job_number(self):
        resolver = Mock(return_value=7)
        with patch("plg_core.mcp.tools.jobs.resolve_job_id_by_number", resolver), patch(
            "plg_core.mcp.tools.jobs.build_internal_job_context", return_value=context()
        ):
            call_arguments({"job_number": "PPS-J-9007"})
        resolver.assert_called_once_with("PPS-J-9007")

    def test_13_unknown_job(self):
        with patch("plg_core.mcp.tools.jobs.resolve_job_id_by_number", side_effect=HTTPException(404, "private")):
            _, payload = call_arguments({"job_number": "PPS-J-9999"})
        self.assertTrue(payload["result"]["isError"])
        self.assertIn("PPS Job not found.", payload["result"]["content"][0]["text"])

    def test_14_whitespace_normalization(self):
        resolver = Mock(return_value=7)
        with patch("plg_core.mcp.tools.jobs.resolve_job_id_by_number", resolver), patch(
            "plg_core.mcp.tools.jobs.build_internal_job_context", return_value=context()
        ):
            call_arguments({"job_number": "  pps-j-9007  "})
        resolver.assert_called_once_with("PPS-J-9007")

    def test_15_invalid_and_oversized_job_number(self):
        for invalid in ("", "21", "PPS-J-1", "../PPS-J-9007", "PPS-J-" + "9" * 40):
            with self.subTest(invalid=invalid):
                _, payload = call_arguments({"job_number": invalid})
                self.assertTrue(payload["result"]["isError"])

    def test_16_synthetic_context(self):
        result = self.successful_call()
        self.assertEqual(result["job"]["job_number"], "PPS-J-9007")
        self.assertEqual(result["movement"], {"ordered": 8, "received": 0, "remaining": 8, "delivered": 0, "available": 0})
        self.assertEqual([order["po_number"] for order in result["supplier_orders"]], ["PPS-PO-9001", "PPS-PO-9002"])

    def test_17_financial_values_preserved(self):
        financial = self.successful_call()["financial"]
        self.assertEqual(financial["revenue"], 1000.0)
        self.assertEqual(financial["booked_cost"], 600.0)
        self.assertEqual(financial["placed_cost"], 500.0)
        self.assertEqual(financial["actual_cost"], 550.0)
        self.assertEqual(financial["expected_profit"], 400.0)
        self.assertEqual(financial["final_profit"], 450.0)
        self.assertEqual(financial["profit_variance"], 50.0)
        self.assertEqual(financial["actual_confirmation_state"], "CONFIRMED")

    def test_18_confidentiality_filtering(self):
        serialized = json.dumps(self.successful_call()).lower()
        for forbidden in ("private@example.test", "555-secret", "synthetic confidential address", "payment_reference", "bank_account", "internal note"):
            self.assertNotIn(forbidden, serialized)

    def test_19_no_filesystem_paths(self):
        serialized = json.dumps(self.successful_call())
        self.assertNotIn("/private/", serialized)
        self.assertNotIn("/home/", serialized)
        self.assertNotIn("stored_path", serialized)

    def test_20_no_pdfs(self):
        serialized = json.dumps(self.successful_call()).lower()
        self.assertNotIn(".pdf", serialized)
        self.assertNotIn("pdf bytes", serialized)

    def test_21_no_raw_database_rows(self):
        result = self.successful_call()
        self.assertEqual(set(result["customer"]), {"id", "customer_number", "display_name", "company"})
        self.assertNotIn("order_url", json.dumps(result))

    def test_22_no_database_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sentinel.db"
            connection = sqlite3.connect(path)
            connection.execute("create table sentinel(value text)")
            connection.execute("insert into sentinel values ('unchanged')")
            connection.commit(); connection.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            self.successful_call()
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_23_same_context_id_for_identical_context(self):
        self.assertEqual(self.successful_call()["context_id"], self.successful_call()["context_id"])

    def test_24_stable_schema_version(self):
        self.assertEqual(self.successful_call()["schema_version"], "1")

    def test_25_no_mutation_service_calls(self):
        resolver = Mock(return_value=7)
        builder = Mock(return_value=context())
        with patch("plg_core.mcp.tools.jobs.resolve_job_id_by_number", resolver), patch(
            "plg_core.mcp.tools.jobs.build_internal_job_context", builder
        ):
            call_arguments({"job_number": "PPS-J-9007"})
        resolver.assert_called_once_with("PPS-J-9007")
        builder.assert_called_once_with(7)

    def test_26_tool_accepts_no_audience_parameter(self):
        _, payload = call_arguments({"job_number": "PPS-J-9007", "audience": "CUSTOMER"})
        self.assertEqual(payload["error"]["code"], -32602)
        self.assertEqual(payload["error"]["message"], "The tool accepts only the job_number argument.")

    def test_27_tool_accepts_no_database_id(self):
        _, payload = call_arguments({"job_number": 21})
        self.assertTrue(payload["result"]["isError"])
        self.assertEqual(set(self.tool().input_schema["properties"]), {"job_number"})


if __name__ == "__main__":
    unittest.main()
