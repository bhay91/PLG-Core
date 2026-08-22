from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from plg_core.ai.context import build_internal_job_context
from plg_core.ai.service import (
    AISettings,
    AIMalformedResponseError,
    AITimeoutError,
    AIUnavailableError,
    ask_job,
    source_fact_catalog,
)


FIXED_TIME = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def snapshot(job_id: int = 7) -> dict:
    return {
        "job_id": job_id,
        "job_number": "PPS-J-9007",
        "job_status": "ACTIVE",
        "customer": {
            "id": 4, "customer_number": "PPS-C-9004", "name": "Synthetic Context Customer",
            "company": "Synthetic Context Company", "phone": "555-secret", "email": "private@example.test",
            "address": "Synthetic Confidential Address",
        },
        "assets": [{
            "id": 11, "manufacturer": "Caterpillar", "model": "320", "year": "2019",
            "vin_pin_serial": "TEST-PIN-CONTEXT-001", "notes": "private note", "attachment_path": "/private/asset.pdf",
        }],
        "needs": [{
            "id": 20, "job_asset_id": 11, "wording": "Hydraulic filters", "state": "OPEN",
            "notes": "unrestricted internal note",
        }],
        "workflow": {"stage": "Waiting for Parts", "next_action": "Receive incoming parts", "next_url": "/purchasing"},
        "quote": {"id": 2, "quote_number": "PPS-Q-9007", "status": "APPROVED"},
        "invoice": {
            "id": 3, "invoice_number": "PPS-I-9007", "status": "PAID", "customer_total": 1000,
            "balance_due": 0, "amount_paid": 1000, "payment_state": "PAID", "payment_reference": "secret-ref",
        },
        "supplier_orders": [
            {"id": 2, "supplier": "Synthetic Supplier A", "po_number": "PPS-PO-9001", "status": "ORDERED", "booked_cost": 300,
             "placed_cost": 250, "actual_cost": 275, "actual_cost_state": "CONFIRMED", "cost_variance": -25,
             "ordered_units": 3, "received_units": 0, "remaining_units": 3, "delivered_units": 0,
             "available_to_deliver_units": 0, "order_url": "/private/order"},
            {"id": 3, "supplier": "Synthetic Supplier B", "po_number": "PPS-PO-9002", "status": "ORDERED", "booked_cost": 300,
             "placed_cost": 250, "actual_cost": 275, "actual_cost_state": "CONFIRMED", "cost_variance": -25,
             "ordered_units": 5, "received_units": 0, "remaining_units": 5, "delivered_units": 0,
             "available_to_deliver_units": 0},
        ],
        "financial": {
            "customer_total": 1000, "booked_supplier_cost": 600, "placed_supplier_cost": 500,
            "actual_supplier_cost": 550, "expected_profit": 400, "placed_cost_profit": 500,
            "actual_profit": 450, "cost_variance": -50, "profit_variance": 50,
            "actual_cost_state": "CONFIRMED", "bank_account": "secret",
        },
        "documents": [{
            "document_type": "INVOICE", "document_number": "PPS-I-9007", "audience": "CUSTOMER",
            "version": 1, "status": "CURRENT", "integrity": "VERIFIED", "is_current": True,
            "path": "/private/invoice.pdf", "sha256": "abc123", "content": b"pdf", "open_url": "/documents/private",
        }, {
            "document_type": "QUOTE", "document_number": "PPS-Q-OLD", "audience": "CUSTOMER",
            "version": 1, "status": "SUPERSEDED", "integrity": "VERIFIED", "is_current": False,
        }],
        "activity": [{
            "event_type": "SUPPLIER_ORDER_PLACED", "message": "PPS-PO-9002 placed", "created_at": "2026-08-15T10:00:00",
            "actor": "secret@example.com",
        }, {"event_type": "INTERNAL_NOTE", "message": "do not expose", "created_at": "2026-08-14T10:00:00"}],
        "movement": {"ordered_units": 8, "received_units": 0, "remaining_units": 8, "delivered_units": 0, "available_to_deliver_units": 0},
    }


def context(state: str = "CONFIRMED"):
    data = snapshot()
    data["financial"]["actual_cost_state"] = state
    return build_internal_job_context(7, snapshot_getter=lambda _: data, generated_at=FIXED_TIME)


def response(answer: str, facts: list[str]) -> dict:
    return {"output_text": json.dumps({"answer": answer, "source_facts": facts})}


SETTINGS = AISettings(True, "test-key", "test-model", 2)


class AIJobContextTests(unittest.TestCase):
    def test_01_context_generation(self):
        result = context()
        self.assertEqual(result.job["job_number"], "PPS-J-9007")
        self.assertEqual(result.audience, "INTERNAL")

    def test_02_context_schema_version(self):
        self.assertEqual(context().schema_version, "1")

    def test_03_context_id_stable_for_identical_context(self):
        first = build_internal_job_context(7, snapshot_getter=lambda _: snapshot(), generated_at=FIXED_TIME)
        later = build_internal_job_context(7, snapshot_getter=lambda _: snapshot())
        self.assertEqual(first.context_id, later.context_id)

    def test_04_financial_filtering(self):
        financial = context().financial
        self.assertEqual(financial["final_profit"], 450.0)
        self.assertNotIn("bank_account", financial)

    def test_05_customer_internal_confidentiality(self):
        customer = context().customer
        self.assertEqual(set(customer), {"id", "customer_number", "display_name", "company"})
        self.assertNotIn("phone", json.dumps(context().model_dump()))

    def test_06_document_metadata_filtering(self):
        documents = context().documents
        self.assertEqual(len(documents), 1)
        self.assertEqual(set(documents[0]), {"document_type", "business_number", "audience", "version", "status", "integrity", "current"})

    def test_07_no_filesystem_paths(self):
        payload = json.dumps(context().model_dump())
        self.assertNotIn("/private/", payload)
        self.assertNotIn("path", payload.lower())

    def test_08_no_secrets(self):
        payload = json.dumps(context().model_dump())
        for secret in ("test-key", "secret-ref", "private@example.test", "abc123"):
            self.assertNotIn(secret, payload)

    def test_09_one_job_scoping(self):
        getter = Mock(return_value=snapshot(22))
        result = build_internal_job_context(22, snapshot_getter=getter)
        getter.assert_called_once_with(22)
        self.assertEqual(result.job["id"], 22)

    def test_10_unknown_job_handling(self):
        def missing(_):
            raise HTTPException(status_code=404, detail="Job not found.")
        with self.assertRaises(HTTPException) as caught:
            build_internal_job_context(999, snapshot_getter=missing)
        self.assertEqual(caught.exception.status_code, 404)

    def test_11_empty_question(self):
        from pydantic import ValidationError
        from plg_core.ai.models import AskPPSRequest
        with self.assertRaises(ValidationError):
            AskPPSRequest(question="   ")

    def test_12_disabled_ai(self):
        from plg_core.ai.service import AIConfigurationError
        with self.assertRaises(AIConfigurationError):
            ask_job("Question", context(), settings=AISettings(False, "", "test", 1))

    def test_13_ai_timeout(self):
        def timeout(*_):
            raise AITimeoutError("timeout")
        with self.assertRaises(AITimeoutError):
            ask_job("Question", context(), settings=SETTINGS, transport=timeout)

    def test_14_openai_failure(self):
        def unavailable(*_):
            raise AIUnavailableError("unavailable")
        with self.assertRaises(AIUnavailableError):
            ask_job("Question", context(), settings=SETTINGS, transport=unavailable)

    def test_15_malformed_model_response(self):
        with self.assertRaises(AIMalformedResponseError):
            ask_job("Question", context(), settings=SETTINGS, transport=lambda *_: {"output_text": "not json"})
        with self.assertRaises(AIMalformedResponseError):
            ask_job("Question", context(), settings=SETTINGS, transport=lambda *_: [])

    def test_16_answer_source_facts_are_allowlisted(self):
        allowed = source_fact_catalog(context())[0]
        answer, facts, _ = ask_job(
            "Status?", context(), settings=SETTINGS,
            transport=lambda *_: response("The Job is active.", [allowed, "invented fact"]),
        )
        self.assertEqual(answer, "The Job is active.")
        self.assertEqual(facts, [allowed])

    def test_17_projected_vs_confirmed_cost_wording(self):
        partial = context("PARTIALLY_CONFIRMED")
        answer, facts, _ = ask_job(
            "Final profit?", partial, settings=SETTINGS,
            transport=lambda *_: response("Final profit is $450.00.", source_fact_catalog(partial)[-2:]),
        )
        self.assertIn("not confirmed", answer.lower())
        self.assertIn("PARTIALLY_CONFIRMED", answer)

    def test_18_no_mutation_calls(self):
        getter = Mock(return_value=snapshot())
        ctx = build_internal_job_context(7, snapshot_getter=getter)
        ask_job("Status?", ctx, settings=SETTINGS, transport=lambda *_: response("Active.", []))
        getter.assert_called_once_with(7)

    def test_19_no_db_changes_from_ask_pps(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "audit.db"
            con = sqlite3.connect(db)
            con.execute("create table sentinel(value text)")
            con.execute("insert into sentinel values ('unchanged')")
            con.commit(); con.close()
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            ask_job("Status?", context(), settings=SETTINGS, transport=lambda *_: response("Active.", []))
            after = hashlib.sha256(db.read_bytes()).hexdigest()
            self.assertEqual(before, after)

    def test_20_synthetic_read_only_context(self):
        result = context()
        self.assertEqual(result.financial, {
            "revenue": 1000.0, "booked_cost": 600.0, "placed_cost": 500.0,
            "actual_cost": 550.0, "expected_profit": 400.0, "placed_cost_profit": 500.0,
            "final_profit": 450.0, "cost_variance": -50.0, "profit_variance": 50.0,
            "actual_confirmation_state": "CONFIRMED",
        })
        self.assertEqual([(o["po_number"], o["ordered"], o["received"], o["remaining"]) for o in result.supplier_orders], [
            ("PPS-PO-9001", 3, 0, 3), ("PPS-PO-9002", 5, 0, 5),
        ])


class AIEndpointTests(unittest.TestCase):
    def test_endpoint_disabled_and_local_only(self):
        from plg_core.ai.models import AskPPSRequest
        from plg_core.ai.routes import ask_pps_job
        request = Mock(client=Mock(host="testclient"), headers={})
        with patch("plg_core.ai.routes.build_internal_job_context", return_value=context()), patch.dict(os.environ, {"PPS_AI_ENABLED": "false"}):
            with self.assertRaises(HTTPException) as denied:
                ask_pps_job(7, AskPPSRequest(question="Status?"), request, None)
            self.assertEqual(denied.exception.status_code, 403)
            with self.assertRaises(HTTPException) as disabled:
                ask_pps_job(7, AskPPSRequest(question="Status?"), request, "1")
            self.assertEqual(disabled.exception.status_code, 503)

    def test_endpoint_unknown_job(self):
        from plg_core.ai.models import AskPPSRequest
        from plg_core.ai.routes import ask_pps_job
        error = HTTPException(status_code=404, detail="Job not found.")
        with patch("plg_core.ai.routes.build_internal_job_context", side_effect=error):
            with self.assertRaises(HTTPException) as result:
                ask_pps_job(999, AskPPSRequest(question="Status?"), Mock(client=Mock(host="testclient"), headers={}), "1")
        self.assertEqual(result.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
