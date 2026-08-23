from __future__ import annotations

from contextlib import closing
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import anyio
import httpx2
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

import legacy_app
from plg_core.application import app
from plg_core.ai.routes import _require_local_operator
from plg_core.core_api.middleware import install_optional_api_hardening
from plg_core.database.migrations import run_migrations
from plg_core.intake.routes import router as intake_router
from plg_core.mcp.server import LoopbackOnlyMCP
from plg_core.requests.mobile_auth import MOBILE_INBOX_CREATE_SCOPE
from plg_core.requests.extension_auth import require_firefox_job_update_authorization
from plg_core.requests.mobile_routes import router as mobile_router
from plg_core.sales.routes import router as sales_router


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-mobile-inbox-token"
TEST_APP = FastAPI()
TEST_APP.include_router(mobile_router)
TEST_APP.include_router(intake_router)


def registered_routes(routes):
    for route in routes:
        if getattr(route, "path", None):
            yield route
        nested = getattr(route, "routes", None)
        if nested is None:
            nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested:
            yield from registered_routes(nested)


def package(**overrides) -> dict:
    value = {
        "schema_version": "1",
        "source": "CHATGPT_MOBILE",
        "client_reference": "mobile-intake-test-0001",
        "original_input": "Synthetic Mobile Customer needs a test seal kit.",
        "customer": {"name": "Synthetic Mobile Customer", "company": ""},
        "machines": [{
            "reference": "mobile-machine-1",
            "manufacturer": "Synthetic Manufacturer",
            "model": "Test Model",
            "year": "2026",
            "asset_type": "machine",
            "identifiers": [{
                "type": "PIN", "value": "TEST-MOBILE-PIN-001",
                "component_label": "", "primary": True,
            }],
        }],
        "requested_needs": [{
            "original_wording": "Synthetic test seal kit", "quantity": 1,
            "machine_reference": "mobile-machine-1",
        }],
        "additional_notes": "DRAFT mobile intake test only.",
        "research_evidence": None,
    }
    value.update(overrides)
    return value


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(self.format(record))


class MobileInboxIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-mobile-inbox-")
        root = Path(self.temp.name)
        self.db_path = root / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    async def request(
        self, payload=None, *, token=TOKEN, enabled="true",
        scopes=MOBILE_INBOX_CREATE_SCOPE, raw_body=None,
        content_type="application/json", extra_environment=None,
        path="/api/mobile/v1/inbox/intake-proposals", method="POST",
        mobile_token=None,
    ):
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if mobile_token is not None:
            headers["X-PPS-Mobile-Token"] = mobile_token
        environment = {
            "PPS_MOBILE_INBOX_ENABLED": enabled,
            "PPS_MOBILE_INBOX_TOKEN": TOKEN,
            "PPS_MOBILE_INBOX_SCOPES": scopes,
            "PPS_FIREFOX_INBOX_TOKEN": "synthetic-firefox-only-token",
            "PPS_FIREFOX_INBOX_SCOPES": "pps:firefox:jobs:update",
            "PPS_API_KEY": "synthetic-general-api-key",
        }
        environment.update(extra_environment or {})
        transport = httpx2.ASGITransport(app=TEST_APP, client=("198.51.100.20", 49111))
        with patch.dict(os.environ, environment, clear=False):
            async with httpx2.AsyncClient(
                transport=transport, base_url="https://api.pinpointsourcing.com",
            ) as client:
                if raw_body is not None:
                    return await client.request(method, path, headers=headers, content=raw_body)
                return await client.request(method, path, headers=headers, json=payload or package())

    def call(self, payload=None, **kwargs):
        return anyio.run(lambda: self.request(payload, **kwargs))

    def connection(self):
        return legacy_app.get_connection()

    def authoritative_counts(self):
        tables = [
            "customers", "machines", "customer_requests", "jobs", "quotes",
            "invoices", "invoice_events", "supplier_orders", "receiving_events", "deliveries",
        ]
        with closing(self.connection()) as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }

    def test_route_registration_is_exact(self):
        self.assertEqual(
            [route.path for route in mobile_router.routes],
            ["/api/mobile/v1/inbox/intake-proposals"],
        )
        self.assertIn(
            "/api/mobile/v1/inbox/intake-proposals",
            [route.path for route in registered_routes(app.routes)],
        )

    def test_disabled_missing_wrong_token_and_missing_scope_fail_closed(self):
        self.assertEqual(self.call(enabled="").status_code, 403)
        self.assertEqual(self.call(enabled="false").status_code, 403)
        self.assertEqual(self.call(token=None).status_code, 401)
        self.assertEqual(self.call(token="wrong-mobile-token").status_code, 401)
        self.assertEqual(self.call(scopes="wrong:scope").status_code, 403)
        self.assertEqual(
            self.call(extra_environment={"PPS_MOBILE_INBOX_TOKEN": ""}).status_code,
            503,
        )

    def test_firefox_token_and_pps_api_key_cannot_authorize_mobile(self):
        self.assertEqual(self.call(token="synthetic-firefox-only-token").status_code, 401)
        self.assertEqual(self.call(token="synthetic-general-api-key").status_code, 401)

    def test_mobile_token_header_and_bearer_compatibility(self):
        header_response = self.call(
            package(client_reference="mobile-header-success"),
            token=None,
            mobile_token=TOKEN,
        )
        self.assertEqual(header_response.status_code, 200, header_response.text)
        self.assertEqual(header_response.json()["status"], "DRAFT")

        bearer_response = self.call(
            package(client_reference="mobile-bearer-success"),
            token=TOKEN,
        )
        self.assertEqual(bearer_response.status_code, 200, bearer_response.text)
        self.assertEqual(bearer_response.json()["status"], "DRAFT")

        self.assertEqual(self.call(token=None, mobile_token=None).status_code, 401)
        self.assertEqual(self.call(token=None, mobile_token="wrong-mobile-token").status_code, 401)

    def test_supplying_both_mobile_authentication_methods_fails_closed(self):
        both_valid = self.call(token=TOKEN, mobile_token=TOKEN)
        self.assertEqual(both_valid.status_code, 401)
        bearer_valid_header_wrong = self.call(token=TOKEN, mobile_token="wrong-mobile-token")
        self.assertEqual(bearer_valid_header_wrong.status_code, 401)
        bearer_wrong_header_valid = self.call(token="wrong-mobile-token", mobile_token=TOKEN)
        self.assertEqual(bearer_wrong_header_valid.status_code, 401)

    def test_strict_json_markers_size_and_extra_fields_are_rejected(self):
        self.assertEqual(self.call(raw_body=b"{bad json").status_code, 422)
        wrapped = "[PPS_INTAKE_PACKAGE_V1]\n{}\n[/PPS_INTAKE_PACKAGE_V1]"
        self.assertEqual(self.call(raw_body=wrapped.encode()).status_code, 422)
        self.assertEqual(self.call(raw_body=b"{}", content_type="text/plain").status_code, 415)
        self.assertEqual(self.call(raw_body=b"x" * (64 * 1024 + 1)).status_code, 413)
        self.assertEqual(self.call(package(extra="forbidden")).status_code, 422)

    def test_wrapped_payload_string_creates_the_same_committed_draft(self):
        before = self.authoritative_counts()
        response = self.call({"payload": json.dumps(package())})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["status"], result["origin"], result["duplicate"]),
                         ("DRAFT", "CHATGPT_MOBILE", False))
        with closing(self.connection()) as connection:
            row = connection.execute(
                "SELECT status,created_request_id,created_job_id FROM intake_proposals WHERE id=?",
                (result["proposal_id"],),
            ).fetchone()
        self.assertEqual(tuple(row), ("DRAFT", None, None))
        self.assertEqual(before, self.authoritative_counts())

    def test_wrapped_payload_preserves_idempotency(self):
        first = self.call({"payload": json.dumps(package())}).json()
        duplicate = self.call({"payload": json.dumps(package())}).json()
        self.assertEqual(duplicate["proposal_id"], first["proposal_id"])
        self.assertTrue(duplicate["duplicate"])

    def test_wrapped_payload_validation_is_strict(self):
        invalid = [
            {"payload": "{bad json"},
            {"payload": "[PPS_INTAKE_PACKAGE_V1]\n{}\n[/PPS_INTAKE_PACKAGE_V1]"},
            {"payload": json.dumps({"not": "a mobile package"})},
            {"payload": json.dumps(package()), "extra": "forbidden"},
            {"payload": package()},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(self.call(value).status_code, 422)

    def test_wrapped_payload_uses_unchanged_mobile_authorization(self):
        wrapped = {"payload": json.dumps(package())}
        self.assertEqual(self.call(wrapped, enabled="").status_code, 403)
        self.assertEqual(self.call(wrapped, token=None).status_code, 401)
        self.assertEqual(self.call(wrapped, token="wrong-mobile-token").status_code, 401)
        self.assertEqual(self.call(wrapped, scopes="wrong:scope").status_code, 403)

    def test_valid_package_is_committed_draft_only_and_review_loads(self):
        before = self.authoritative_counts()
        response = self.call()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["status"], result["origin"], result["duplicate"]),
                         ("DRAFT", "CHATGPT_MOBILE", False))
        self.assertEqual(result["review_url"], f"/requests/smart-intake/proposals/{result['proposal_id']}")
        with closing(self.connection()) as connection:
            row = connection.execute(
                "SELECT status,created_request_id,created_job_id FROM intake_proposals WHERE id=?",
                (result["proposal_id"],),
            ).fetchone()
        self.assertEqual(tuple(row), ("DRAFT", None, None))
        self.assertEqual(before, self.authoritative_counts())
        review_route = next(
            route for route in registered_routes(TEST_APP.routes)
            if getattr(route, "path", None) == "/requests/smart-intake/proposals/{proposal_id}"
            and "GET" in getattr(route, "methods", set())
        )
        scope = {
            "type": "http", "method": "GET", "path": result["review_url"],
            "headers": [], "query_string": b"", "server": ("testserver", 80),
            "client": ("127.0.0.1", 1), "scheme": "http", "app": app,
        }
        review = review_route.endpoint(Request(scope), result["proposal_id"])
        self.assertEqual(review.status_code, 200)
        self.assertIn("Smart Intake", review.body.decode())

    def test_duplicate_and_changed_reference_behavior(self):
        first = self.call().json()
        duplicate = self.call().json()
        self.assertEqual(duplicate["proposal_id"], first["proposal_id"])
        self.assertTrue(duplicate["duplicate"])
        changed = self.call(package(additional_notes="Different normalized content"))
        self.assertEqual(changed.status_code, 409)

    def test_authenticated_transport_forces_mobile_internal_origin(self):
        response = self.call(package(source="CHATGPT_FIREFOX"))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        with closing(self.connection()) as connection:
            contribution = connection.execute(
                "SELECT payload_json,evidence FROM intake_proposal_contributions "
                "WHERE proposal_id=? AND contributor_type='AI'", (result["proposal_id"],),
            ).fetchone()
            audit = connection.execute(
                "SELECT actor,request_id,metadata_json FROM audit_logs "
                "WHERE action='SMART_INTAKE_PROPOSED' AND entity_id=? ORDER BY id DESC",
                (str(result["proposal_id"]),),
            ).fetchone()
        self.assertEqual(json.loads(contribution["payload_json"])["origin"], "CHATGPT_MOBILE")
        self.assertEqual(json.loads(audit["metadata_json"]), {"origin": "CHATGPT_MOBILE"})
        self.assertEqual((audit["actor"], audit["request_id"]),
                         ("mobile-shortcut", package()["client_reference"]))
        self.assertEqual(result["origin"], "CHATGPT_MOBILE")

    def test_credentials_are_absent_from_responses_audit_and_logs(self):
        secret = "mobile-secret-must-not-escape"
        handler = _CaptureHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        try:
            response = self.call(
                token=secret,
                extra_environment={"PPS_MOBILE_INBOX_TOKEN": "different-configured-secret"},
            )
        finally:
            root_logger.removeHandler(handler)
        self.assertEqual(response.status_code, 401)
        exposed = response.text + "\n" + "\n".join(handler.messages)
        self.assertNotIn(secret, exposed)
        self.assertNotIn("different-configured-secret", exposed)
        with closing(self.connection()) as connection:
            audit_text = "\n".join(
                str(value or "")
                for row in connection.execute("SELECT metadata_json,summary FROM audit_logs")
                for value in row
            )
        self.assertNotIn(secret, audit_text)
        self.assertNotIn("different-configured-secret", audit_text)

    def test_mobile_bearer_does_not_authorize_other_boundaries(self):
        scope = {
            "type": "http", "method": "POST", "path": "/protected",
            "query_string": b"", "server": ("api.test", 443),
            "client": ("198.51.100.20", 49111), "scheme": "https",
            "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
        }
        request = Request(scope)
        with patch.dict(os.environ, {
            "PPS_FIREFOX_INBOX_TOKEN": "synthetic-firefox-only-token",
            "PPS_FIREFOX_INBOX_SCOPES": "pps:firefox:jobs:update",
            "PPS_FIREFOX_REMOTE_ENABLED": "true",
        }):
            with self.assertRaises(HTTPException) as job_error:
                require_firefox_job_update_authorization(request)
        self.assertEqual(job_error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as ask_error:
            _require_local_operator(request, None)
        self.assertEqual(ask_error.exception.status_code, 403)
        downstream_called = False

        async def downstream(scope, receive, send):
            nonlocal downstream_called
            downstream_called = True

        async def remote_mcp_request():
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            await LoopbackOnlyMCP(downstream)(
                {
                    "type": "http", "method": "POST", "path": "/mcp",
                    "client": ("198.51.100.20", 49111),
                    "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
                },
                receive,
                send,
            )
            return sent

        mcp_messages = anyio.run(remote_mcp_request)
        self.assertFalse(downstream_called)
        self.assertEqual(mcp_messages[0]["status"], 403)

        protected_app = FastAPI()
        protected_app.include_router(sales_router)
        with patch.dict(os.environ, {
            "PPS_ENABLE_API_HARDENING": "true",
            "PPS_API_KEY": "synthetic-general-api-key",
        }):
            install_optional_api_hardening(protected_app)

        async def protected_request():
            transport = httpx2.ASGITransport(app=protected_app, client=("198.51.100.20", 49112))
            async with httpx2.AsyncClient(transport=transport, base_url="https://api.test") as client:
                return await client.post(
                    "/api/v1/sales/quotes/1/status",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                    json={"status": "APPROVED", "notes": "must not run"},
                )

        protected = anyio.run(protected_request)
        self.assertEqual(protected.status_code, 401)


if __name__ == "__main__":
    unittest.main()
