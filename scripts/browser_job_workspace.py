"""Disposable 390px mutation smoke for the canonical Job Command Center.

This helper intentionally uses the current semantic Command Center selectors. It
stops if a required operator control is not exposed; it never calls mutation
endpoints directly to simulate a missing UI control.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pps-final-mobile-smoke-") as directory:
        root = Path(directory)
        os.environ.update(
            PPS_DB_PATH=str(root / "smoke.db"),
            PPS_DOCUMENT_ROOT=str(root / "documents"),
            PPS_UPLOAD_ROOT=str(root / "uploads"),
        )
        import legacy_app
        from plg_core.database.migrations import run_migrations
        from plg_core.basket.models import BasketItemCreate
        from plg_core.basket.service import add_item

        legacy_app.initialize_database()
        run_migrations()
        with legacy_app.get_connection() as c:
            customer = c.execute(
                "INSERT INTO customers(customer_number,name,active) VALUES (?,?,1)",
                ("FINAL-MOBILE-C", "DISPOSABLE FINAL MOBILE SMOKE"),
            ).lastrowid
            job = c.execute(
                """INSERT INTO jobs(job_number,created_date,customer_id,customer,company,status)
                   VALUES (?,?,?,?,?,?)""",
                ("FINAL-MOBILE-J", "2026-09-11", customer,
                 "DISPOSABLE FINAL MOBILE SMOKE", "Disposable Test Co", "REQUESTED"),
            ).lastrowid
            c.execute("UPDATE customers SET email='final-mobile@example.test' WHERE id=?", (customer,))
            c.execute(
                """INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,
                   vin_pin_serial,active) VALUES (?,?,?,'TestCo','Model R','FINAL-MOBILE-ASSET',1)""",
                (customer, "FINAL-MOBILE-M", "Disposable Test Asset"),
            )
            asset = c.execute(
                """INSERT INTO job_assets(job_id,customer_id,name,manufacturer,model,
                   vin_pin_serial,is_primary) VALUES (?,?,?,'TestCo','Model R','FINAL-MOBILE-ASSET',1)""",
                (job, customer, "Disposable Test Asset"),
            ).lastrowid
            c.commit()
        add_item(int(job), BasketItemCreate(
            job_asset_id=int(asset), requested_description="Synthetic filter",
            supplier_name="Disposable Supplier", supplier_part_number="FINAL-FILTER",
            quantity=1, supplier_unit_cost=100, markup_percent=30,
            verification_status="VERIFIED", selected=True,
        ))

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log_path = root / "server.log"
        log = log_path.open("w")
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=log,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    urlopen(base + "/health", timeout=1).close()
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError(log_path.read_text())

            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                executable = os.environ.get(
                    "PPS_BROWSER_EXECUTABLE",
                    "/snap/chromium/current/usr/lib/chromium-browser/chrome",
                )
                browser = p.chromium.launch(
                    executable_path=executable, headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                page = browser.new_page(viewport={"width": 390, "height": 844},
                                        device_scale_factor=1, is_mobile=True, has_touch=True)
                page.goto(f"{base}/jobs/{job}/basket?view=advanced", wait_until="networkidle")
                assert page.locator(".cc-shell").count() == 1

                def layout(label: str) -> None:
                    dims = page.evaluate("({width:innerWidth, scroll:document.documentElement.scrollWidth})")
                    if dims["scroll"] > dims["width"]:
                        raise AssertionError(f"{label}: horizontal overflow {dims}")
                    print(f"{label}: width={dims['width']} scrollWidth={dims['scroll']}")

                layout("initial")
                need_form = page.locator('form[action$="/needs"]').last
                if need_form.count() != 1:
                    raise AssertionError("Add Need control is not exposed in the canonical Command Center")
                need_form.evaluate("""el => { let n = el; while (n) { if (n.tagName === 'DETAILS') n.open = true; n = n.parentElement; } }""")
                need_form.locator('input[name="wording"]').fill("Disposable final smoke need")
                need_form.get_by_role("button", name="Add Need").click()
                page.wait_for_load_state("networkidle")
                if "/basket?view=advanced" not in page.url:
                    raise AssertionError(f"Add Need returned non-canonical URL: {page.url}")
                layout("add-need")
                assert page.locator("#customer-needs").count() == 1
                # Research anchors are valid while this job is editable and has
                # an active requested need/research context.
                for anchor in ("customer-needs", "research-results"):
                    page.goto(f"{base}/jobs/{job}/basket?view=advanced#{anchor}", wait_until="networkidle")
                    page.wait_for_timeout(100)
                    target = page.locator(f"#{anchor}")
                    assert target.count() == 1, anchor
                    assert target.evaluate("el => !!el.closest('details') ? el.closest('details').open : true"), anchor
                    layout(f"anchor-{anchor}")

                # Fixture preparation for the quote correction portion is done by
                # the local service, while all correction actions below are UI-driven.
                legacy_app.generate_quote(int(job))
                page.goto(f"{base}/jobs/{job}/basket?view=advanced", wait_until="networkidle")
                edit = page.get_by_role("button", name="Edit Draft")
                if edit.count() != 1:
                    raise AssertionError("Edit Draft control is not exposed in the current Command Center")
                edit.locator("xpath=ancestor::form").locator('input[name="reason"]').fill("Disposable mobile correction")
                edit.click()
                page.wait_for_load_state("networkidle")
                if "/basket?view=advanced" not in page.url:
                    back = page.get_by_role("link", name="Back to Job")
                    if back.count() != 1:
                        raise AssertionError(f"Edit Draft returned unexpected URL: {page.url}")
                    back.click()
                    page.wait_for_load_state("networkidle")
                layout("editable-revision")
                if page.get_by_role("button", name="Cancel Changes").count() != 1:
                    raise AssertionError("Cancel Changes control is not exposed after UI revision start")
                cancel = page.get_by_role("button", name="Cancel Changes")
                cancel.locator("xpath=ancestor::form").locator('input[name="reason"]').fill("Disposable mobile cancel")
                page.once("dialog", lambda dialog: dialog.accept())
                cancel.click()
                page.wait_for_load_state("networkidle")
                if "/basket?view=advanced" not in page.url:
                    back = page.get_by_role("link", name="Back to Job")
                    if back.count() != 1:
                        raise AssertionError(f"Cancel Changes returned unexpected URL: {page.url}")
                    back.click()
                    page.wait_for_load_state("networkidle")
                layout("cancelled-revision")

                # Start the second correction through the visible UI. The next
                # required step must also be exposed by the current page; do not
                # fall back to a direct commit endpoint if it is absent.
                edit_again = page.get_by_role("button", name="Edit Draft")
                if edit_again.count() != 1:
                    raise AssertionError("Second correction Edit Draft control is not exposed")
                edit_again.locator("xpath=ancestor::form").locator('input[name="reason"]').fill("Disposable mobile second correction")
                edit_again.click()
                page.wait_for_load_state("networkidle")
                if "/basket?view=advanced" not in page.url:
                    back = page.get_by_role("link", name="Back to Job")
                    if back.count() != 1:
                        raise AssertionError(f"Second correction returned unexpected URL: {page.url}")
                    back.click()
                    page.wait_for_load_state("networkidle")
                layout("second-editable-revision")
                commit_forms = page.locator('form[action$="/basket/commit"], form[action$="/basket/checkout"]')
                if commit_forms.count() != 1:
                    raise AssertionError("Commit Revision control is not exposed in the current canonical Command Center")
                commit_forms.first.get_by_role("button", name="Finish Changes").click()
                page.wait_for_load_state("networkidle")
                if "/basket?view=advanced" not in page.url:
                    raise AssertionError(f"Finish Changes returned non-canonical URL: {page.url}")
                layout("committed-pending")
                assert page.get_by_role("button", name="Finish Changes").count() == 0
                assert page.get_by_role("button", name="Cancel Changes").count() == 0
                assert page.locator('.job-command-next').count() == 1
                next_button = page.locator('.job-command-next').get_by_role("button", name="Generate Revised Quote")
                if next_button.count() != 1:
                    raise AssertionError("Generate Revised Quote is not exposed as the sole committed-pending action")
                assert next_button.locator("xpath=ancestor::form").locator('[name="expected_version"]').count() == 1
                next_button.click()
                page.wait_for_load_state("networkidle")
                if "/basket?view=advanced" not in page.url:
                    back = page.get_by_role("link", name="Back to Job")
                    if back.count() == 1:
                        back.click(); page.wait_for_load_state("networkidle")
                layout("post-projection")
                assert page.get_by_role("button", name="Generate Revised Quote").count() == 0
                # Quote review page exposes the governed SENT transition.
                docs_group = page.locator("#documents-activity-group")
                if docs_group.count() == 1 and not docs_group.get_attribute("open"):
                    docs_group.locator("> summary").click()
                quote_link = page.locator('a[href^="/quotes/"][href$="/documents"]').first
                if quote_link.count() != 1:
                    raise AssertionError("Projected quote link is not exposed")
                quote_link.click(); page.wait_for_load_state("networkidle")
                sent = page.get_by_role("button", name="Mark Quote as Sent")
                if sent.count() != 1:
                    raise AssertionError("Mark Quote as Sent is not exposed")
                sent.click(); page.wait_for_load_state("networkidle")
                layout("sent-quote")

                # Smart Intake review is exercised only through its visible form;
                # no confirmation is submitted.
                page.goto(f"{base}/requests/smart-intake", wait_until="networkidle")
                page.locator('form[action="/requests/smart-intake/analyze"] textarea[name="raw_text"]').fill(
                    "DISPOSABLE FINAL MOBILE SMOKE\nfinal-mobile@example.test\nTestCo Model R\nPIN FINAL-MOBILE-ASSET\nNeeds disposable final smoke filter"
                )
                page.get_by_role("button", name="Analyze Request").click()
                page.wait_for_load_state("networkidle")
                layout("smart-intake-review")
                if "/requests/smart-intake/proposals/" not in page.url:
                    raise AssertionError(f"Smart Intake review did not render at {page.url}")
                assert "Confirm what PPS understood" in page.locator("body").inner_text()

                print("RESULT: 390px smoke passed through projection, quote SENT, Smart Intake review, and editable research anchors")
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
            log.close()


if __name__ == "__main__":
    main()
