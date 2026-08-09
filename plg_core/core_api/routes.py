from contextlib import closing
from fastapi import APIRouter
from legacy_app import get_connection

router = APIRouter(prefix="/api/v1/core", tags=["alpha19-core"])

@router.get("/ready")
def ready():
    required = {
        "customers", "machines", "jobs", "quotes", "invoices",
        "supplier_orders", "supplier_order_items",
        "receiving_events", "deliveries", "audit_logs",
    }
    with closing(get_connection()) as connection:
        existing = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    missing = sorted(required - existing)
    return {"ok": not missing, "missing_tables": missing, "roadmap": "alpha-12-19-starter"}

@router.get("/capabilities")
def capabilities():
    return {
        "alpha_12_13": ["quote tracker", "quote status", "quote conversion adapter", "invoice tracker"],
        "alpha_14_15": ["supplier orders", "receiving", "delivery"],
        "alpha_16_18": ["customer API", "machine API", "admin dashboard", "audit logs"],
        "alpha_19": ["versioned API", "readiness", "API-key template", "optional hardening middleware"],
    }
