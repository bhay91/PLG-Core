from __future__ import annotations

import runpy
import sqlite3
import sys
from pathlib import Path

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def fail(message):
    raise SystemExit(
        f"PPS RELEASE CHECK FAILED: {message}"
    )


# ---------------------------------------------------------
# VERSION
# ---------------------------------------------------------

from plg_core.version import PPS_PHASE, PPS_VERSION

if PPS_VERSION != "1.0.0-alpha.19":
    fail("incorrect PPS version")

if PPS_PHASE != "alpha-19":
    fail("incorrect PPS phase")


# ---------------------------------------------------------
# CRITICAL PYTHON
# ---------------------------------------------------------

python_files = [
    "legacy_app.py",
    "plg_core/application.py",
    "plg_core/version.py",
    "plg_core/sales/routes.py",
    "plg_core/sales/service.py",
    "plg_core/supply/routes.py",
    "plg_core/supply/service.py",
    "plg_core/crm/routes.py",
    "plg_core/admin/routes.py",
    "plg_core/admin/service.py",
    "plg_core/core_api/routes.py",
    "plg_core/core_api/middleware.py",
    "plg_core/roadmap/migrations.py",
    "plg_core/documents/library.py",
]

for relative in python_files:
    path = ROOT / relative

    if not path.exists():
        fail(f"missing Python file: {relative}")

    try:
        compile(
            path.read_text(),
            str(path),
            "exec",
        )
    except Exception as exc:
        fail(f"{relative}: {exc}")


# ---------------------------------------------------------
# CRITICAL TEMPLATES
# ---------------------------------------------------------

env = Environment()

templates = [
    "templates/base.html",
    "templates/dashboard.html",
    "templates/follow_up.html",
    "templates/accounting.html",
    "templates/documents.html",
    "templates/customer_account.html",
    "templates/supplier_order_detail.html",
    "templates/job_delivery.html",
]

for relative in templates:
    path = ROOT / relative

    if not path.exists():
        fail(f"missing template: {relative}")

    try:
        env.parse(path.read_text())
    except Exception as exc:
        fail(f"{relative}: {exc}")


# ---------------------------------------------------------
# REQUIRED ROUTE DEFINITIONS
# ---------------------------------------------------------

route_checks = {
    "plg_core/application.py": [
        '@app.get("/health")',
        "PPS_VERSION",
    ],
    "plg_core/core_api/routes.py": [
        '@router.get("/ready")',
        '@router.get("/capabilities")',
        '"version": PPS_VERSION',
    ],
    "plg_core/sales/routes.py": [
        '@router.get("/invoices")',
        '/payments',
        '/void',
    ],
    "plg_core/supply/routes.py": [
        '@router.get("/orders")',
        '/receipts',
        '/deliveries/',
    ],
    "plg_core/crm/routes.py": [
        '@router.get("/customers")',
        '@router.get("/machines")',
        '@router.get("/search")',
    ],
    "plg_core/admin/routes.py": [
        '@router.get("/accounting")',
        '@router.get("/audit")',
    ],
    "legacy_app.py": [
        '"/search"',
        '"/follow-up"',
        '"/accounting"',
        '"/documents"',
        '"/purchasing"',
    ],
}

for relative, tokens in route_checks.items():
    text = (ROOT / relative).read_text()

    for token in tokens:
        if token not in text:
            fail(
                f"{relative} missing {token}"
            )


# ---------------------------------------------------------
# ROADMAP SYNC
# ---------------------------------------------------------

namespace = runpy.run_path(
    str(ROOT / "build_roadmap.py"),
    run_name="pps_release_gate",
)

managed = [
    "plg_core/core_api/routes.py",
    "plg_core/core_api/middleware.py",
]

for relative in managed:
    generated = (
        namespace["FILES"][relative].rstrip()
        + "\n"
    )

    live = (
        (ROOT / relative).read_text().rstrip()
        + "\n"
    )

    if generated != live:
        fail(
            f"roadmap mismatch: {relative}"
        )


# ---------------------------------------------------------
# DATABASE — STRICTLY READ ONLY
# ---------------------------------------------------------

db_path = ROOT / "data" / "plg_core.db"

if not db_path.exists():
    fail("data/plg_core.db not found")

connection = sqlite3.connect(
    "file:" + db_path.resolve().as_posix() + "?mode=ro",
    uri=True,
)

try:
    quick = connection.execute(
        "PRAGMA quick_check"
    ).fetchone()

    if (
        not quick
        or str(quick[0]).lower() != "ok"
    ):
        fail("SQLite quick_check failed")

    required_tables = {
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

    existing = {
        row[0]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            """
        ).fetchall()
    }

    missing = sorted(
        required_tables - existing
    )

    if missing:
        fail(
            "missing tables: "
            + ", ".join(missing)
        )

finally:
    connection.close()


print("PPS RELEASE CHECK: PASS")
print(f"VERSION: {PPS_VERSION}")
print(f"PHASE: {PPS_PHASE}")
print("DATABASE: PASS")
print("ROADMAP SYNC: PASS")
