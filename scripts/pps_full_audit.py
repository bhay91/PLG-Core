from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Finding:
    level: str
    area: str
    message: str


class Audit:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def pass_(self, area: str, message: str) -> None:
        self.findings.append(Finding("PASS", area, message))

    def warn(self, area: str, message: str) -> None:
        self.findings.append(Finding("WARN", area, message))

    def fail(self, area: str, message: str) -> None:
        self.findings.append(Finding("FAIL", area, message))

    @property
    def failures(self) -> int:
        return sum(
            finding.level == "FAIL"
            for finding in self.findings
        )

    @property
    def warnings(self) -> int:
        return sum(
            finding.level == "WARN"
            for finding in self.findings
        )

    @property
    def passes(self) -> int:
        return sum(
            finding.level == "PASS"
            for finding in self.findings
        )

    def print_report(self) -> None:
        print("PPS FULL PRODUCT AUDIT")
        print("=" * 72)

        for finding in self.findings:
            print(
                f"{finding.level}: "
                f"[{finding.area}] "
                f"{finding.message}"
            )

        print("=" * 72)
        print(
            "SUMMARY:",
            f"PASS={self.passes}",
            f"WARN={self.warnings}",
            f"FAIL={self.failures}",
        )

        if self.failures:
            print("RESULT: FAIL")
        elif self.warnings:
            print("RESULT: PASS WITH WARNINGS")
        else:
            print("RESULT: PASS")


audit = Audit()


# ---------------------------------------------------------------------
# PYTHON SOURCE
# ---------------------------------------------------------------------

python_files: list[Path] = [ROOT / "legacy_app.py"]
python_files.extend(
    sorted((ROOT / "plg_core").rglob("*.py"))
)
python_files.extend(
    sorted((ROOT / "scripts").glob("*.py"))
)

python_files = [
    path
    for path in python_files
    if "__pycache__" not in path.parts
]

python_errors: list[str] = []

for path in python_files:
    try:
        ast.parse(path.read_text())
    except Exception as exc:
        python_errors.append(
            f"{path.relative_to(ROOT)}: {exc}"
        )

if python_errors:
    for error in python_errors:
        audit.fail("Python", error)
else:
    audit.pass_(
        "Python",
        f"{len(python_files)} active Python files parse",
    )


# ---------------------------------------------------------------------
# JINJA TEMPLATES
# ---------------------------------------------------------------------

template_root = ROOT / "templates"
template_files = sorted(template_root.rglob("*.html"))

env = Environment()
template_errors: list[str] = []

for path in template_files:
    try:
        env.parse(path.read_text())
    except Exception as exc:
        template_errors.append(
            f"{path.relative_to(ROOT)}: {exc}"
        )

if template_errors:
    for error in template_errors:
        audit.fail("Templates", error)
else:
    audit.pass_(
        "Templates",
        f"{len(template_files)} Jinja templates parse",
    )


# ---------------------------------------------------------------------
# RUNTIME / OPENAPI
# ---------------------------------------------------------------------

try:
    from plg_core.application import app

    schema = app.openapi()
    paths = schema.get("paths", {})

    if not paths:
        audit.fail(
            "Runtime",
            "OpenAPI contains no application paths",
        )
    else:
        operation_ids: list[str] = []
        method_path_pairs: list[tuple[str, str]] = []

        for path, operations in paths.items():
            for method, operation in operations.items():
                method_lower = method.lower()

                if method_lower not in {
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "options",
                    "head",
                    "trace",
                }:
                    continue

                method_path_pairs.append(
                    (method_lower.upper(), path)
                )

                operation_id = operation.get("operationId")

                if operation_id:
                    operation_ids.append(operation_id)

        duplicate_operation_ids = sorted({
            operation_id
            for operation_id in operation_ids
            if operation_ids.count(operation_id) > 1
        })

        if duplicate_operation_ids:
            audit.fail(
                "Runtime",
                "duplicate OpenAPI operation IDs: "
                + ", ".join(duplicate_operation_ids),
            )
        else:
            audit.pass_(
                "Runtime",
                f"{len(method_path_pairs)} OpenAPI operations "
                "with no duplicate operation IDs",
            )

except Exception as exc:
    paths = {}
    audit.fail(
        "Runtime",
        f"OpenAPI generation failed: {exc}",
    )


# ---------------------------------------------------------------------
# INTERNAL TEMPLATE LINKS / FORM ACTIONS
# ---------------------------------------------------------------------

def route_regex(route_path: str) -> re.Pattern[str]:
    escaped = re.escape(route_path)

    escaped = re.sub(
        r"\\\{[^{}]+\\\}",
        r"[^/]+",
        escaped,
    )

    return re.compile(rf"^{escaped}$")


route_patterns = [
    route_regex(path)
    for path in paths
]

literal_target_pattern = re.compile(
    r'''(?:href|action)\s*=\s*["'](/[^"'{}]*?)["']'''
)

ignored_prefixes = (
    "/static/",
    "/favicon",
)

unmatched_targets: dict[str, set[str]] = {}

for template in template_files:
    text = template.read_text()

    for target in literal_target_pattern.findall(text):
        target = target.split("?", 1)[0].split("#", 1)[0]

        if not target:
            continue

        if target.startswith(ignored_prefixes):
            continue

        if any(
            pattern.match(target)
            for pattern in route_patterns
        ):
            continue

        unmatched_targets.setdefault(
            str(template.relative_to(ROOT)),
            set(),
        ).add(target)

if unmatched_targets:
    for template, targets in sorted(
        unmatched_targets.items()
    ):
        audit.warn(
            "Links",
            f"{template}: unmatched literal targets: "
            + ", ".join(sorted(targets)),
        )
else:
    audit.pass_(
        "Links",
        "literal internal template links/forms match runtime routes",
    )


# ---------------------------------------------------------------------
# DATABASE — READ ONLY
# ---------------------------------------------------------------------

db_path = ROOT / "data" / "plg_core.db"

if not db_path.exists():
    audit.fail(
        "Database",
        "data/plg_core.db not found",
    )
else:
    connection = sqlite3.connect(
        "file:"
        + db_path.resolve().as_posix()
        + "?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row

    try:
        integrity = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()

        if (
            integrity
            and str(integrity[0]).lower() == "ok"
        ):
            audit.pass_(
                "Database",
                "SQLite integrity_check = ok",
            )
        else:
            audit.fail(
                "Database",
                f"integrity_check returned {integrity}",
            )

        fk_rows = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        if fk_rows:
            audit.fail(
                "Database",
                f"{len(fk_rows)} foreign-key violations",
            )
        else:
            audit.pass_(
                "Database",
                "0 foreign-key violations",
            )

        existing_tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                """
            )
        }

        required_tables = {
            "customers",
            "machines",
            "customer_requests",
            "jobs",
            "quotes",
            "invoices",
            "baskets",
            "basket_items",
            "supplier_orders",
            "machine_parts_history",
            "schema_migrations",
            "pps_number_sequences",
        }

        missing_tables = sorted(
            required_tables - existing_tables
        )

        if missing_tables:
            audit.fail(
                "Database",
                "missing required tables: "
                + ", ".join(missing_tables),
            )
        else:
            audit.pass_(
                "Database",
                f"{len(required_tables)} required "
                "operational tables present",
            )

        # -------------------------------------------------------------
        # MIGRATION SYNC
        # -------------------------------------------------------------

        from plg_core.database.migrations import (
            MIGRATIONS as CORE_MIGRATIONS,
        )
        from plg_core.roadmap.migrations import (
            MIGRATIONS as ROADMAP_MIGRATIONS,
        )

        code_ids = [
            migration_id
            for migration_id, _ in CORE_MIGRATIONS
        ] + [
            migration_id
            for migration_id, _ in ROADMAP_MIGRATIONS
        ]

        db_ids = [
            row["migration_id"]
            for row in connection.execute(
                """
                SELECT migration_id
                FROM schema_migrations
                """
            )
        ]

        duplicate_code_ids = sorted({
            migration_id
            for migration_id in code_ids
            if code_ids.count(migration_id) > 1
        })

        if duplicate_code_ids:
            audit.fail(
                "Migrations",
                "duplicate migration IDs in code: "
                + ", ".join(duplicate_code_ids),
            )
        else:
            audit.pass_(
                "Migrations",
                f"{len(set(code_ids))} unique migration IDs",
            )

        code_only = sorted(
            set(code_ids) - set(db_ids)
        )
        db_only = sorted(
            set(db_ids) - set(code_ids)
        )

        if code_only or db_only:
            audit.fail(
                "Migrations",
                f"sync mismatch; code_only={code_only}, "
                f"db_only={db_only}",
            )
        else:
            audit.pass_(
                "Migrations",
                f"code and DB synchronized "
                f"({len(set(code_ids))} migrations)",
            )

        # -------------------------------------------------------------
        # BUSINESS NUMBER SEQUENCES
        # -------------------------------------------------------------

        expected_sequences = {
            "CUSTOMER": "PPS-C-",
            "MACHINE": "PPS-M-",
            "REQUEST": "PPS-R-",
            "JOB": "PPS-J-",
            "QUOTE": "PPS-Q-",
        }

        if "pps_number_sequences" in existing_tables:
            sequence_rows = {
                row["entity_type"]: (
                    row["prefix"],
                    row["last_number"],
                )
                for row in connection.execute(
                    """
                    SELECT
                        entity_type,
                        prefix,
                        last_number
                    FROM pps_number_sequences
                    """
                )
            }

            bad_sequences: list[str] = []

            for entity_type, prefix in (
                expected_sequences.items()
            ):
                row = sequence_rows.get(entity_type)

                if row is None:
                    bad_sequences.append(
                        f"{entity_type}=missing"
                    )
                    continue

                if row[0] != prefix:
                    bad_sequences.append(
                        f"{entity_type} prefix={row[0]!r}"
                    )

                if int(row[1]) < 0:
                    bad_sequences.append(
                        f"{entity_type} last_number={row[1]}"
                    )

            extras = sorted(
                set(sequence_rows)
                - set(expected_sequences)
            )

            if extras:
                bad_sequences.append(
                    "unexpected=" + ",".join(extras)
                )

            if bad_sequences:
                audit.fail(
                    "Numbering",
                    "; ".join(bad_sequences),
                )
            else:
                audit.pass_(
                    "Numbering",
                    "C/M/R/J/Q persistent sequences valid",
                )

        # -------------------------------------------------------------
        # EXISTING BUSINESS NUMBER FORMATS
        # -------------------------------------------------------------

        number_sources = (
            (
                "customers",
                "customer_number",
                re.compile(r"^PPS-C-\d{4}$"),
            ),
            (
                "machines",
                "machine_number",
                re.compile(r"^PPS-M-\d{4}$"),
            ),
            (
                "customer_requests",
                "request_number",
                re.compile(r"^PPS-R-\d{4}$"),
            ),
            (
                "jobs",
                "job_number",
                re.compile(r"^PPS-J-\d{4}$"),
            ),
            (
                "quotes",
                "quote_number",
                re.compile(r"^PPS-Q-\d{4}$"),
            ),
            (
                "invoices",
                "invoice_number",
                re.compile(r"^PPS-INV-\d{4}$"),
            ),
        )

        invalid_numbers: list[str] = []

        for table, column, pattern in number_sources:
            if table not in existing_tables:
                continue

            rows = connection.execute(
                f"""
                SELECT id, {column}
                FROM {table}
                WHERE COALESCE(TRIM({column}), '') != ''
                """
            ).fetchall()

            for row in rows:
                value = str(row[column] or "").strip()

                if not pattern.fullmatch(value):
                    invalid_numbers.append(
                        f"{table} id={row['id']} "
                        f"{column}={value!r}"
                    )

        if invalid_numbers:
            for message in invalid_numbers[:20]:
                audit.warn("Numbering", message)

            if len(invalid_numbers) > 20:
                audit.warn(
                    "Numbering",
                    f"{len(invalid_numbers) - 20} more "
                    "legacy/nonstandard numbers",
                )
        else:
            audit.pass_(
                "Numbering",
                "existing visible PPS numbers use "
                "standard formats",
            )

        # -------------------------------------------------------------
        # QUOTE -> INVOICE NUMBER PAIRING
        # -------------------------------------------------------------

        if {
            "quotes",
            "invoices",
        }.issubset(existing_tables):
            mismatches = connection.execute(
                """
                SELECT
                    invoices.id,
                    invoices.invoice_number,
                    quotes.quote_number
                FROM invoices
                JOIN quotes
                  ON quotes.id = invoices.quote_id
                WHERE
                    quotes.quote_number LIKE 'PPS-Q-%'
                    AND invoices.invoice_number LIKE 'PPS-INV-%'
                    AND SUBSTR(
                        quotes.quote_number,
                        LENGTH('PPS-Q-') + 1
                    ) != SUBSTR(
                        invoices.invoice_number,
                        LENGTH('PPS-INV-') + 1
                    )
                """
            ).fetchall()

            if mismatches:
                for row in mismatches:
                    audit.fail(
                        "Quote/Invoice",
                        f"invoice id={row['id']} "
                        f"{row['quote_number']} -> "
                        f"{row['invoice_number']}",
                    )
            else:
                audit.pass_(
                    "Quote/Invoice",
                    "existing PPS Quote/Invoice sequences pair",
                )

    except Exception as exc:
        audit.fail(
            "Database",
            f"audit query failed: {exc}",
        )
    finally:
        connection.close()


# ---------------------------------------------------------------------
# LIVE BROWSER / RESPONSIVE AUDIT
# ---------------------------------------------------------------------

try:
    from pps_browser_audit import run_browser_audit

    # Browser routes always run against a transaction-consistent disposable
    # copy. Never point the routine at a live PPS server/database.
    browser_result = run_browser_audit(source_db_path=db_path)

    for warning in browser_result["warnings"]:
        audit.warn("Browser", warning)

    for failure in browser_result["failures"]:
        audit.fail("Browser", failure)

    if (
        not browser_result["warnings"]
        and not browser_result["failures"]
    ):
        audit.pass_(
            "Browser",
            f"{len(browser_result['tested_pages'])} HTML pages / "
            f"{browser_result['tested_viewports']} viewport runs passed",
        )

except Exception as exc:
    audit.fail(
        "Browser",
        f"browser audit could not run: {exc}",
    )

audit.print_report()

if audit.failures:
    raise SystemExit(1)
