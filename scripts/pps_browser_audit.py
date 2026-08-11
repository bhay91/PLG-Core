from __future__ import annotations

import shutil
import sqlite3
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from plg_core.application import app

BASE_URL = "http://127.0.0.1:8000"
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet": {"width": 900, "height": 1100},
    "phone": {"width": 390, "height": 844},
}
SKIP_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


def _browser_path():
    return (
        shutil.which("chromium-browser")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )


def _static_candidates():
    required_modular_pages = {
        "/requests",
        "/requests/new",
        "/requests/smart-intake",
        "/machines",
        "/machines/new",
    }

    discovered = {
        route.path
        for route in app.routes
        if "GET" in (getattr(route, "methods", set()) or set())
        and "{" not in route.path
        and route.path not in SKIP_PATHS
    }

    return sorted(discovered | required_modular_pages)


def _first_id(connection, table):
    exists = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table,),
    ).fetchone()

    if exists is None:
        return None

    row = connection.execute(
        f'SELECT id FROM "{table}" ORDER BY id LIMIT 1'
    ).fetchone()

    return int(row[0]) if row else None


def _dynamic_candidates():
    db_path = ROOT / "data" / "plg_core.db"
    if not db_path.exists():
        return []

    connection = sqlite3.connect(
        "file:" + db_path.resolve().as_posix() + "?mode=ro",
        uri=True,
    )

    try:
        candidates = []

        customer_id = _first_id(connection, "customers")
        if customer_id is not None:
            candidates += [
                f"/customers/{customer_id}",
                f"/customers/{customer_id}/edit",
            ]

        machine_id = _first_id(connection, "machines")
        if machine_id is not None:
            candidates += [
                f"/machines/{machine_id}",
                f"/machines/{machine_id}/edit",
                f"/machines/{machine_id}/transfer",
            ]

        job_id = _first_id(connection, "jobs")
        if job_id is not None:
            candidates += [
                f"/jobs/{job_id}",
                f"/jobs/{job_id}/basket",
                f"/jobs/{job_id}/delivery",
                f"/jobs/{job_id}/edit",
            ]

        request_id = _first_id(connection, "customer_requests")
        if request_id is not None:
            candidates += [
                f"/requests/{request_id}",
                f"/requests/{request_id}/edit",
            ]

        quote_id = _first_id(connection, "quotes")
        if quote_id is not None:
            candidates += [
                f"/quotes/{quote_id}/customer",
                f"/quotes/{quote_id}/internal",
                f"/quotes/{quote_id}/documents",
            ]

        invoice_id = _first_id(connection, "invoices")
        if invoice_id is not None:
            candidates += [
                f"/invoices/{invoice_id}/documents",
                f"/invoices/{invoice_id}/custom",
            ]

        supplier_id = _first_id(connection, "suppliers")
        if supplier_id is not None:
            candidates.append(f"/suppliers/{supplier_id}/edit")

        connector_id = _first_id(connection, "connectors")
        if connector_id is not None:
            candidates.append(f"/connectors/{connector_id}/edit")

        order_id = _first_id(connection, "supplier_orders")
        if order_id is not None:
            candidates.append(f"/purchasing/orders/{order_id}")

        return sorted(set(candidates))
    finally:
        connection.close()


def _probe_html(page, url):
    try:
        response = page.goto(url, wait_until="networkidle", timeout=15000)
    except Exception as exc:
        return False, f"navigation error: {exc}"

    if response is None:
        return False, "no response"

    content_type = response.headers.get("content-type", "").lower()

    if "text/html" not in content_type:
        return False, None

    if response.status >= 400:
        return False, f"HTTP {response.status}"

    return True, None


def run_browser_audit(base_url=BASE_URL):
    executable = _browser_path()
    if not executable:
        return {
            "tested_pages": [],
            "tested_viewports": 0,
            "warnings": [],
            "failures": ["No Chromium browser executable found"],
        }

    candidates = sorted(set(_static_candidates() + _dynamic_candidates()))
    shard = os.getenv("PPS_BROWSER_SHARD", "").strip()
    if shard:
        try:
            shard_index, shard_count = (int(value) for value in shard.split("/", 1))
            if shard_count < 1 or not 0 <= shard_index < shard_count:
                raise ValueError
            candidates = candidates[shard_index::shard_count]
        except ValueError as exc:
            raise ValueError("PPS_BROWSER_SHARD must use zero-based INDEX/COUNT format") from exc
    tested_pages = []
    warnings = []
    failures = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=executable,
        )

        try:
            for path in candidates:
                page = browser.new_page(viewport=VIEWPORTS["desktop"])
                page_errors = []
                console_errors = []
                resource_errors = []

                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text)
                    if msg.type == "error" else None,
                )
                page.on(
                    "response",
                    lambda response: resource_errors.append(
                        f"{response.status} {response.url}"
                    ) if response.status >= 400
                    and not response.url.endswith("/favicon.ico") else None,
                )

                is_html = False
                try:
                    for viewport_name, viewport in VIEWPORTS.items():
                        page.set_viewport_size(viewport)
                        page_errors.clear()
                        console_errors.clear()
                        resource_errors.clear()
                        try:
                            response = page.goto(
                                base_url + path,
                                wait_until="load",
                                timeout=15000,
                            )
                            status = response.status if response else None

                            if response and "text/html" not in response.headers.get(
                                "content-type", ""
                            ).lower():
                                break

                            is_html = True

                            if status != 200:
                                failures.append(
                                    f"{path} [{viewport_name}]: HTTP {status}"
                                )
                                continue

                            overflow = page.evaluate(
                                "() => document.documentElement.scrollWidth > "
                                "document.documentElement.clientWidth"
                            )

                            if overflow:
                                warnings.append(
                                    f"{path} [{viewport_name}]: horizontal page overflow"
                                )
                            if page_errors:
                                failures.append(
                                    f"{path} [{viewport_name}]: page errors: "
                                    + " | ".join(page_errors[:3])
                                )
                            if console_errors:
                                warnings.append(
                                    f"{path} [{viewport_name}]: console errors: "
                                    + " | ".join(console_errors[:3])
                                )
                            if resource_errors:
                                warnings.append(
                                    f"{path} [{viewport_name}]: resource errors: "
                                    + " | ".join(resource_errors[:3])
                                )
                        except Exception as exc:
                            failures.append(
                                f"{path} [{viewport_name}]: {exc}"
                            )
                finally:
                    page.close()

                if is_html:
                    tested_pages.append(path)
        finally:
            browser.close()

    return {
        "tested_pages": tested_pages,
        "tested_viewports": len(tested_pages) * len(VIEWPORTS),
        "warnings": warnings,
        "failures": failures,
    }


def main():
    result = run_browser_audit()

    tested_pages = result["tested_pages"]
    warnings = result["warnings"]
    failures = result["failures"]

    print("PPS BROWSER / RESPONSIVE AUDIT")
    print("=" * 72)
    print("HTML_PAGES_TESTED:", len(tested_pages))
    print("VIEWPORT_RUNS:", result["tested_viewports"])

    for path in tested_pages:
        print("PAGE:", path)
    for warning in warnings:
        print("WARN:", warning)
    for failure in failures:
        print("FAIL:", failure)

    print("=" * 72)
    print(
        "SUMMARY:",
        f"PAGES={len(tested_pages)}",
        f"WARN={len(warnings)}",
        f"FAIL={len(failures)}",
    )

    if failures:
        print("RESULT: FAIL")
        return 1
    if warnings:
        print("RESULT: PASS WITH WARNINGS")
    else:
        print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
