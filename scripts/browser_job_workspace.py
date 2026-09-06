"""Real Chromium checks at phone widths using a fresh synthetic database only."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    with tempfile.TemporaryDirectory(prefix='pps-workspace-browser-') as directory:
        root = Path(directory)
        os.environ.update(PPS_DB_PATH=str(root / 'browser.db'), PPS_DOCUMENT_ROOT=str(root / 'documents'),
                          PPS_UPLOAD_ROOT=str(root / 'uploads'))
        import legacy_app
        from plg_core.database.migrations import run_migrations
        from plg_core.research.service import create_requested_need, create_manual_research_result, set_quote_candidate
        from playwright.sync_api import sync_playwright, expect
        legacy_app.initialize_database()
        run_migrations()
        c = legacy_app.get_connection()
        job = c.execute("INSERT INTO jobs(job_number,created_date,customer,status) VALUES ('PHONE-PHASE1','2026-09-06','Synthetic Equipment Customer','REQUESTED')").lastrowid
        asset = c.execute("INSERT INTO job_assets(job_id,name,manufacturer,model,vin_pin_serial,is_primary) VALUES (?,'Excavator','CAT','320','VIN' || ?,1)", (job, '1234567890' * 14)).lastrowid
        c.commit()
        need = create_requested_need(job, job_asset_id=asset, wording='Hydraulic Pump')
        empty = create_requested_need(job, job_asset_id=asset, wording='Seal Kit')
        other = create_requested_need(job, job_asset_id=asset, wording='Brake Assembly')
        offers = []
        for index in range(3):
            basket = create_manual_research_result(job, job_asset_id=asset, requested_need_id=need['id'],
                description='Hydraulic Pump', manufacturer_part_number='PART' + '1234567890' * 15,
                supplier_name='Supplier' + 'LongName' * 24 if index == 0 else f'Supplier {index}',
                supplier_unit_cost=420 + index * 20, availability='In stock', verification_status='VERIFIED',
                source_url='https://example.test/' + 'reference' * 30, research_evidence='Catalog evidence retained')
            offers.append(basket['items'][-1])
        for index in range(2):
            option = create_manual_research_result(job, job_asset_id=asset, requested_need_id=other['id'],
                description=f'Brake component {index}', supplier_name=f'Brake Supplier {index}', supplier_unit_cost=30)['items'][-1]
            set_quote_candidate(job, option['id'], candidate=True)
        create_manual_research_result(job, job_asset_id=None, requested_need_id=None, description='Unassigned legacy offer')
        c.execute("INSERT INTO job_parts(job_id,requested_description,quantity) VALUES (?,'Earlier saved filter',2)", (job,))
        c.commit()
        c.close()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        log = open(root / 'server.log', 'w')
        server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=log)
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    urlopen(base + '/health', timeout=1).close()
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError((root / 'server.log').read_text())
            screenshots = Path('/tmp/pps-phase1/screenshots')
            screenshots.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                executable = os.environ.get('PPS_BROWSER_EXECUTABLE', '/snap/chromium/current/usr/lib/chromium-browser/chrome')
                browser = p.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                page = browser.new_page(viewport={'width':390, 'height':844}, device_scale_factor=1, is_mobile=True, has_touch=True)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                url = f'{base}/jobs/{job}/basket'
                observations = []
                for width in (320,375,390,430,1440):
                    page.set_viewport_size({'width':width,'height':900 if width > 430 else 844})
                    page.goto(url)
                    page.evaluate("document.documentElement.dataset.theme='light'")
                    page.locator('.jcc').wait_for()
                    need_groups = page.locator('[data-need-workspace]')
                    assert need_groups.count() == 3
                    first_group = need_groups.nth(0)
                    second_group = need_groups.nth(1)
                    assert first_group.locator('[data-option-id]').count() == 3
                    assert second_group.locator('[data-option-id]').count() == 0
                    assert page.locator('#other-items').count() == 1
                    assert page.locator('#other-items [data-option-id]').count() == 1
                    assert page.locator('[data-advanced][open]').count() == 0
                    visible = page.locator('.jcc').inner_text().lower()
                    for phrase in ('source directory', 'connector profile', 'verification session', 'research result', 'quote candidate'):
                        assert phrase not in visible, phrase
                    for state in ('collapsed', 'expanded'):
                        if state == 'expanded':
                            page.locator('.jcc details').evaluate_all('(nodes) => nodes.forEach(node => node.open = true)')
                        dimensions = page.evaluate('({width: innerWidth, scroll: document.documentElement.scrollWidth})')
                        assert dimensions['scroll'] <= dimensions['width'], (width, state, dimensions)
                        overflowing = page.locator('.jcc *').evaluate_all('(nodes) => nodes.filter(n => n.getBoundingClientRect().width && n.getBoundingClientRect().right > innerWidth + 1).map(n => n.className).slice(0,10)')
                        assert not overflowing, (width, state, overflowing)
                        small_controls = page.locator('.jcc button, .jcc summary, .jcc input:not([type=hidden]):not([type=checkbox]), .jcc select, .jcc .button').evaluate_all('(nodes) => nodes.filter(n => n.checkVisibility() && n.getBoundingClientRect().height < 44).map(n => n.outerHTML.slice(0,100))')
                        assert not small_controls, (width, small_controls)
                        assert page.locator('.jcc h1').evaluate('(node) => getComputedStyle(node).color') in ('rgb(25, 50, 71)','rgb(16, 32, 56)')
                        observations.append({'viewport':width,'state':state,**dimensions})
                    page.evaluate("document.documentElement.dataset.theme='dark'")
                    dark = page.evaluate("""()=>{const q=s=>getComputedStyle(document.querySelector(s)); const h=q('.jcc h1'), p=q('.jcc-panel h2'), t=q('#quick-add-input'), e=q('.jcc-event'); return {heading:h.color, section:p.color, textarea:t.backgroundColor, textareaText:t.color, event:e.color, scroll:document.documentElement.scrollWidth}}""")
                    assert dark['heading'] == 'rgb(255, 255, 255)' and dark['section'] == 'rgb(255, 255, 255)', dark
                    assert dark['textarea'] != 'rgb(255, 255, 255)' and dark['textareaText'] == 'rgb(245, 248, 252)', dark
                    assert dark['event'] == 'rgb(255, 255, 255)' and dark['scroll'] <= width, dark
                    page.evaluate("document.documentElement.dataset.theme='light'")
                    page.goto(url + '#parts')
                    page.reload()
                    page.screenshot(path=str(screenshots / f'parts-{width}.png'), full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                page.goto(url + '#parts')
                card = page.locator(f'[data-requested-part="{need["id"]}"]')
                assert card.locator('[data-option-id]').count() == 3
                card.get_by_role('button', name='Use This Option').first.click()
                page.wait_for_url('**#part-*')
                card = page.locator(f'[data-requested-part="{need["id"]}"]')
                expect(card.locator('.jcc-selected')).to_have_count(1)
                assert card.locator('[data-option-id]').count() == 3
                assert card.get_by_role('button', name='Use This Option').count() == 0
                page.screenshot(path=str(screenshots / 'selected-390.png'), full_page=True)
                page.screenshot(path=str(screenshots / 'selected-viewport-390.png'))
                # A valid browser URL with a disallowed scheme fails server URL validation.
                card.get_by_role('link', name='Add Option', exact=True).click()
                form = card.locator('[action$="/research-results/manual"]')
                form.locator('details').evaluate('(node) => node.open = true')
                form.locator('[name="source_url"]').fill('ftp://example.test/offer')
                form.locator('[name="supplier_name"]').fill('Unsaved supplier entry')
                form.get_by_role('button', name='Save Option').click()
                form.locator('.jcc-error').wait_for()
                assert form.locator('[name="supplier_name"]').input_value() == 'Unsaved supplier entry'
                assert form.locator('[name="source_url"]').input_value() == 'ftp://example.test/offer'
                form.locator('[name="description"]').fill('Additional offer')
                form.locator('[name="source_url"]').fill('https://example.test/offer')
                form.get_by_role('button', name='Save Option').click()
                page.wait_for_url('**#part-*')
                page.wait_for_load_state()
                expect(page.locator(f'[data-requested-part="{need["id"]}"] [data-option-id]')).to_have_count(4)
                # Need-specific Find Part works; browser Back returns to this same card.
                page.locator(f'[data-requested-part="{need["id"]}"]').get_by_role('link', name='Find Part', exact=True).click()
                page.wait_for_url('**#find-part')
                assert page.locator('#find-part').inner_text().find('Hydraulic Pump') >= 0
                page.go_back()
                assert f'/jobs/{job}/basket' in page.url and '#part-' in page.url
                assert page.locator('#other-items').count() == 1
                assert not errors, errors
                browser.close()
                print(json.dumps({'layout_checks':observations,'selection':'passed','validation_retains_values':'passed','back_navigation':'passed','console_errors':errors,'screenshots':str(screenshots)}, indent=2))
        finally:
            server.terminate()
            server.wait(timeout=10)
            log.close()


if __name__ == '__main__':
    main()
