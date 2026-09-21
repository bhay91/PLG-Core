from __future__ import annotations

import os
from contextlib import closing

from fastapi import APIRouter

from legacy_app import get_connection
from plg_core.version import PPS_PHASE, PPS_VERSION


router = APIRouter(
    prefix="/api/v1/core",
    tags=["alpha19-core"],
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@router.get("/ready")
def ready():
    required = {
        "customers",
        "machines",
        "jobs",
        "quotes",
        "quote_events",
        "invoices",
        "invoice_events",
        "supplier_orders",
        "supplier_order_items",
        "receiving_events",
        "receiving_event_items",
        "deliveries",
        "delivery_items",
        "audit_logs",
        "api_idempotency_keys",
    }

    database_check = "unknown"
    missing = []

    try:
        with closing(
            get_connection()
        ) as connection:
            existing = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table'
                    """
                ).fetchall()
            }

            missing = sorted(
                required - existing
            )

            quick_check = connection.execute(
                "PRAGMA quick_check"
            ).fetchone()

            database_check = str(
                quick_check[0]
                if quick_check
                else "unknown"
            ).lower()

    except Exception:
        database_check = "error"

    hardening_enabled = _truthy(
        os.getenv(
            "PPS_ENABLE_API_HARDENING"
        )
    )

    api_key_configured = bool(
        os.getenv(
            "PPS_API_KEY",
            "",
        ).strip()
    )

    database_ok = (
        database_check == "ok"
    )

    security_ok = (
        not hardening_enabled
        or api_key_configured
    )

    return {
        "ok": (
            database_ok
            and not missing
            and security_ok
        ),
        "database_check": database_check,
        "missing_tables": missing,
        "security": {
            "hardening_enabled": (
                hardening_enabled
            ),
            "api_key_configured": (
                api_key_configured
            ),
            "write_api_protected": (
                hardening_enabled
                and api_key_configured
            ),
        },
        "roadmap": PPS_PHASE,
        "version": PPS_VERSION,
    }


@router.get("/capabilities")
def capabilities():
    return {
        "sales": [
            "quote lifecycle",
            "quote approval",
            "invoice conversion",
            "payments",
            "payment reversals",
            "invoice voiding",
        ],
        "purchasing": [
            "supplier purchase orders",
            "supplier cost adjustments",
            "receiving",
            "partial receiving",
            "customer delivery",
        ],
        "records": [
            "customers",
            "machines",
            "global search",
            "document center",
        ],
        "operations": [
            "workflow dashboard",
            "follow-up center",
            "accounting snapshot",
            "audit trail",
        ],
        "api": [
            "versioned API",
            "readiness",
            "request IDs",
            "security headers",
            "optional API-key write protection",
            "idempotency storage",
        ],
    }
