from __future__ import annotations

import json
import base64
import hashlib
from pathlib import Path
import unittest
import zipfile

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extensions" / "firefox" / "PLG-Firefox-Extension-v0.15-Connector-SDK"


def payload(**overrides):
    value = {
        "schema_version": "1", "source": "CHATGPT_FIREFOX",
        "client_reference": "bridge-test-0001",
        "original_input": "Synthetic Customer needs a hydraulic cylinder seal kit.",
        "customer": {"name": "Synthetic Customer", "company": ""},
        "machines": [{
            "reference": "machine-1", "manufacturer": "Caterpillar", "model": "420D",
            "year": "2004", "asset_type": "machine",
            "identifiers": [{"type": "PIN", "value": "TEST420D3D3", "component_label": "", "primary": True}],
        }],
        "requested_needs": [{"original_wording": "Hydraulic cylinder seal kit", "quantity": 1, "machine_reference": "machine-1"}],
        "additional_notes": "<img src=x onerror=alert(1)>",
        "research_evidence": {"source_urls": ["https://example.test/part"], "claims": [], "quoted_evidence": []},
    }
    value.update(overrides)
    return value


def envelope(value):
    return f"[PPS_INTAKE_PACKAGE_V1]\n{json.dumps(value)}\n[/PPS_INTAKE_PACKAGE_V1]"


def research_package_envelope(package_id="research-bridge-001", pdf_bytes=b"synthetic-pdf-bytes"):
    package = {
        "schema_version": "1", "package_id": package_id,
        "source_pdf": {"filename": "research.pdf", "sha256": hashlib.sha256(pdf_bytes).hexdigest()},
        "target": {"mode": "NEW_JOB"},
        "customer": {"name": "Synthetic Customer", "company": "Synthetic Co"},
        "machine": {"reference": "truck", "manufacturer": "International", "model": "5600i", "year": "2003", "asset_type": "vehicle", "identifiers": []},
        "requested_needs": [{"reference": "need-1", "original_wording": "Synthetic part", "quantity": 2, "machine_reference": "truck"}],
        "research_options": [],
    }
    value = {"schema_version": "1", "package": package, "pdf_base64": base64.b64encode(pdf_bytes).decode()}
    return f"[PPS_RESEARCH_IMPORT_PACKAGE_V1]\n{json.dumps(value)}\n[/PPS_RESEARCH_IMPORT_PACKAGE_V1]"


class FirefoxChatGPTBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(executable_path="/usr/bin/chromium-browser", headless=True)
        cls.addClassCleanup(cls.browser.close)

    def page(self):
        page = self.browser.new_page()
        page.route("https://chatgpt.com/**", lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><html><body></body></html>",
        ))
        page.goto("https://chatgpt.com/c/test")
        return page

    def test_01_startup_assistant_envelope_uses_exact_message_type_once(self):
        page = self.page()
        page.set_content('<div data-message-author-role="assistant"></div>')
        page.evaluate("text => document.querySelector('[data-message-author-role=assistant]').textContent=text", envelope(payload()))
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.wait_for_timeout(50)
        messages = page.evaluate("window.__messages")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["type"], "PPS_CHATGPT_INTAKE_PACKAGE_V1")
        self.assertEqual(messages[0]["payload"]["client_reference"], "bridge-test-0001")
        page.evaluate("() => document.querySelector('[data-message-author-role=assistant]').append(document.createTextNode(' later mutation'))")
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("window.__messages.length"), 1)
        page.evaluate("text => { const n=document.createElement('div'); n.dataset.messageAuthorRole='user'; n.textContent=text; document.body.append(n); }", envelope(payload(client_reference="user-marker")))
        page.evaluate("text => { const n=document.createElement('aside'); n.textContent=text; document.body.append(n); }", envelope(payload(client_reference="sidebar-marker")))
        page.evaluate("text => { const n=document.createElement('div'); n.dataset.messageAuthorRole='assistant'; n.textContent=text; document.body.append(n); }", envelope(payload(client_reference="new-assistant")))
        page.wait_for_timeout(100)
        messages = page.evaluate("window.__messages")
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1]["payload"]["client_reference"], "new-assistant")
        page.close()

    def test_02_empty_assistant_streams_split_envelope_once(self):
        page = self.page()
        page.set_content("<main></main>")
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.evaluate("""() => {
          const node=document.createElement('div');
          node.id='streamed-assistant';
          node.dataset.messageAuthorRole='assistant';
          document.querySelector('main').append(node);
        }""")
        page.wait_for_timeout(30)
        self.assertEqual(page.evaluate("window.__messages.length"), 0)
        text = envelope(payload(client_reference="streamed-bridge-0001"))
        splits = [text[:24], text[24:91], text[91:-12], text[-12:]]
        for part in splits:
            page.evaluate("part => document.querySelector('#streamed-assistant').append(document.createTextNode(part))", part)
            page.wait_for_timeout(25)
        page.wait_for_timeout(100)
        messages = page.evaluate("window.__messages")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"]["client_reference"], "streamed-bridge-0001")
        for suffix in (" trailing", " text", " after completion"):
            page.evaluate("value => document.querySelector('#streamed-assistant').append(document.createTextNode(value))", suffix)
        page.wait_for_timeout(100)
        self.assertEqual(page.evaluate("window.__messages.length"), 1)
        page.close()

    def test_03_two_streamed_assistants_submit_independently(self):
        page = self.page()
        page.set_content("<main></main>")
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        for index in (1, 2):
            page.evaluate("index => { const n=document.createElement('div'); n.id=`assistant-${index}`; n.dataset.messageAuthorRole='assistant'; document.querySelector('main').append(n); }", index)
            text = envelope(payload(client_reference=f"two-message-{index}"))
            page.evaluate("({index,text}) => document.querySelector(`#assistant-${index}`).textContent=text.slice(0,-10)", {"index": index, "text": text})
            page.wait_for_timeout(20)
            page.evaluate("({index,text}) => document.querySelector(`#assistant-${index}`).append(document.createTextNode(text.slice(-10)))", {"index": index, "text": text})
        page.wait_for_timeout(100)
        messages = page.evaluate("window.__messages")
        self.assertEqual([item["payload"]["client_reference"] for item in messages], ["two-message-1", "two-message-2"])
        page.close()

    def test_04_malformed_is_rejected_once_and_non_envelope_text_is_ignored(self):
        page = self.page()
        page.set_content("<main></main>")
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        texts = [
            "ordinary assistant response",
            "[PPS_INTAKE_PACKAGE_V1]{bad json}[/PPS_INTAKE_PACKAGE_V1]",
            envelope(payload(extra="rejected")),
            json.dumps(payload()),
        ]
        for text in texts:
            page.evaluate("text => { const n=document.createElement('div'); n.dataset.messageAuthorRole='assistant'; n.textContent=text; document.body.append(n); }", text)
        page.wait_for_timeout(100)
        messages = page.evaluate("window.__messages")
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(message["type"] == "PPS_CHATGPT_INTAKE_PACKAGE_V1" for message in messages))
        self.assertTrue(all("rejection" in message for message in messages))
        page.evaluate("() => { for (const node of document.querySelectorAll('[data-message-author-role=assistant]')) node.append(document.createTextNode(' later mutation')); }")
        page.wait_for_timeout(100)
        self.assertEqual(page.evaluate("window.__messages.length"), 2)
        page.close()

    def background_page(self, response_status=200, response_body=None, offline=False,
                        token="local-test-token", api_base=None, web_base=None,
                        cloudflare_client_id="", cloudflare_client_secret=""):
        page = self.page()
        body = response_body or {"status": "DRAFT", "proposal_id": 44, "review_url": "/requests/smart-intake/proposals/44", "duplicate": False}
        page.add_init_script(f"""
          window.__store = {{
            ppsFirefoxToken:{json.dumps(token)},
            ppsApiBase:{json.dumps(api_base)},
            ppsWebBase:{json.dumps(web_base)},
            cloudflareAccessClientId:{json.dumps(cloudflare_client_id)},
            cloudflareAccessClientSecret:{json.dumps(cloudflare_client_secret)}
          }};
          window.__listeners = {{message:[], startup:[], installed:[], notification:[], activated:[], updated:[], focused:[], storageChanged:[]}};
          window.__notifications=[]; window.__fetches=[]; window.__tabs=[]; window.__executions=[];
          window.__tabQuery = [];
          window.browser = {{
            runtime: {{id:'pps-extension-id', onMessage:{{addListener:f=>__listeners.message.push(f)}}, onStartup:{{addListener:f=>__listeners.startup.push(f)}}, onInstalled:{{addListener:f=>__listeners.installed.push(f)}}}},
            storage: {{local: {{get: async key => {{
              const keys=Array.isArray(key) ? key : [key];
              return Object.fromEntries(keys.map(item => [item,__store[item]]));
            }}, set: async values => Object.assign(__store, values)}}, onChanged:{{addListener:f=>__listeners.storageChanged.push(f)}}}},
            action: {{setBadgeText:async()=>{{}}, setBadgeBackgroundColor:async()=>{{}}}},
            notifications: {{create:async value=>{{__notifications.push(value);return String(__notifications.length)}}, onClicked:{{addListener:f=>__listeners.notification.push(f)}}}},
            tabs: {{
              create:value=>__tabs.push(value),
              query:async()=>__tabQuery,
              get:async id=>__tabQuery.find(tab=>tab.id===id),
              onActivated:{{addListener:f=>__listeners.activated.push(f)}},
              onUpdated:{{addListener:f=>__listeners.updated.push(f)}}
            }},
            scripting: {{executeScript:async value=>__executions.push(value)}}
            ,windows: {{WINDOW_ID_NONE:-1,onFocusChanged:{{addListener:f=>__listeners.focused.push(f)}}}}
          }};
          window.fetch = async (url, options) => {{
            __fetches.push({{url, options}});
            {'throw new Error("offline")' if offline else f'return new Response({json.dumps(json.dumps(body))}, {{status:{response_status}, headers:{{"Content-Type":"application/json"}}}})'};
          }};
        """)
        page.route("https://example.test/**", lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><html><body></body></html>",
        ))
        page.goto("https://example.test")
        page.add_script_tag(path=str(EXT / "background.js"))
        page.wait_for_timeout(50)
        return page

    def invoke(self, page, value, url="https://chatgpt.com/c/test"):
        return page.evaluate("async ({payload,url}) => await __listeners.message[0]({type:'PPS_CHATGPT_INTAKE_PACKAGE_V1',payload},{id:'pps-extension-id',tab:{url}})", {"payload": value, "url": url})

    def invoke_research(self, page, envelope_text, url="https://chatgpt.com/c/test"):
        page.goto(url)
        page.set_content(f'<div data-message-author-role="assistant">{envelope_text}</div>')
        page.evaluate("window.__messages=[]; window.browser.runtime.sendMessage = m => { __messages.push(m); return Promise.resolve(); }")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.wait_for_timeout(100)
        return page.evaluate("__messages[0]")

    def test_05b_research_import_envelope_transports_pdf_and_sidecar_as_multipart(self):
        page = self.background_page(response_body={"status": "DRAFT", "proposal_id": 77, "package_id": "research-bridge-001", "review_url": "/requests/smart-intake/proposals/77", "duplicate": False})
        message = self.invoke_research(page, research_package_envelope())
        self.assertEqual(message["type"], "PPS_CHATGPT_RESEARCH_IMPORT_PACKAGE_V1")
        page.evaluate("async m => await __listeners.message[0](m,{id:'pps-extension-id',tab:{url:'https://chatgpt.com/c/test'}})", message)
        page.wait_for_timeout(100)
        item = page.evaluate("__store.ppsFirefoxInboxQueue[0]")
        self.assertEqual(item["status"], "SENT")
        self.assertEqual(item["proposal_id"], 77)
        fetch = page.evaluate("__fetches[0]")
        self.assertTrue(fetch["options"]["body"])
        self.assertEqual(fetch["options"]["method"], "POST")
        self.assertNotIn("Content-Type", fetch["options"]["headers"])
        self.assertIn("/api/extension/v1/research-import/packages", fetch["url"])
        fields = page.evaluate("async () => ({pdf: __fetches[0].options.body.get('research_pdf').name, pdfType: __fetches[0].options.body.get('research_pdf').type, sidecar: __fetches[0].options.body.get('sidecar').name, sidecarType: __fetches[0].options.body.get('sidecar').type, sidecarText: await __fetches[0].options.body.get('sidecar').text()})")
        self.assertEqual(fields["pdf"], "research.pdf")
        self.assertEqual(fields["pdfType"], "application/pdf")
        self.assertEqual(fields["sidecar"], "research-import.json")
        self.assertEqual(fields["sidecarType"], "application/json")
        self.assertIn('"package_id":"research-bridge-001"', fields["sidecarText"])
        self.assertIn("no Job was created", page.evaluate("__notifications.at(-1).message"))
        page.close()

    def test_05c_research_import_duplicate_and_malformed_or_oversized_packages_are_safe(self):
        page = self.background_page(response_body={"status": "DRAFT", "proposal_id": 78, "package_id": "research-bridge-002", "review_url": "/requests/smart-intake/proposals/78", "duplicate": True})
        message = self.invoke_research(page, research_package_envelope("research-bridge-002"))
        page.evaluate("async m => await __listeners.message[0](m,{id:'pps-extension-id',tab:{url:'https://chatgpt.com/c/test'}})", message)
        page.wait_for_timeout(100)
        self.assertEqual(page.evaluate("__store.ppsFirefoxInboxQueue[0].status"), "SENT_DUPLICATE")
        before = page.evaluate("__fetches.length")
        page.evaluate("async m => await __listeners.message[0](m,{id:'pps-extension-id',tab:{url:'https://chatgpt.com/c/test'}})", message)
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("__fetches.length"), before)
        page.close()

        malformed = self.background_page()
        self.assertEqual(self.invoke_research(malformed, "[PPS_RESEARCH_IMPORT_PACKAGE_V1]{bad}[/PPS_RESEARCH_IMPORT_PACKAGE_V1]")["type"], "PPS_CHATGPT_RESEARCH_IMPORT_PACKAGE_V1")
        malformed.wait_for_timeout(50)
        self.assertEqual(malformed.evaluate("(__store.ppsFirefoxInboxQueue||[]).length"), 0)
        malformed.close()

    def test_05_background_sender_validation_queue_success_and_token_boundary(self):
        page = self.background_page()
        rejected = self.invoke(page, payload(client_reference="wrong-site"), "https://evil.example/")
        self.assertFalse(rejected["ok"])
        self.invoke(page, payload())
        page.wait_for_timeout(100)
        queue = page.evaluate("__store.ppsFirefoxInboxQueue")
        self.assertEqual(queue[0]["status"], "SENT")
        self.assertEqual(queue[0]["proposal_id"], 44)
        self.assertNotIn("local-test-token", json.dumps(queue))
        fetches = page.evaluate("__fetches")
        self.assertEqual(len(fetches), 1)
        self.assertEqual(fetches[0]["url"], "https://api.pinpointsourcing.com/api/extension/v1/inbox/intake-proposals")
        self.assertEqual(fetches[0]["options"]["headers"]["Authorization"], "Bearer local-test-token")
        self.assertNotIn("CF-Access-Client-Id", fetches[0]["options"]["headers"])
        self.assertEqual(queue[0]["pps_web_base"], "https://pinpointsourcing.com")
        self.assertEqual(page.evaluate("__notifications.at(-1).title"), "Sent to PPS Inbox")
        page.evaluate("__listeners.notification[0]('1')")
        page.wait_for_timeout(25)
        self.assertEqual(
            page.evaluate("__tabs.at(-1).url"),
            "https://pinpointsourcing.com/requests/smart-intake/proposals/44",
        )
        page.close()

    def test_05a_remote_headers_local_override_review_base_and_secret_redaction(self):
        page = self.background_page(
            response_status=500,
            response_body={"detail": "failure cf-client-id-value cf-secret-value firefox-secret-value"},
            token="firefox-secret-value",
            api_base="http://127.0.0.1:8000",
            web_base="http://localhost:8000",
            cloudflare_client_id="cf-client-id-value",
            cloudflare_client_secret="cf-secret-value",
        )
        self.invoke(page, payload(client_reference="configured-remote"))
        page.wait_for_timeout(1800)
        fetch = page.evaluate("__fetches[0]")
        self.assertEqual(fetch["url"], "http://127.0.0.1:8000/api/extension/v1/inbox/intake-proposals")
        self.assertEqual(fetch["options"]["headers"]["CF-Access-Client-Id"], "cf-client-id-value")
        self.assertEqual(fetch["options"]["headers"]["CF-Access-Client-Secret"], "cf-secret-value")
        self.assertEqual(fetch["options"]["headers"]["Authorization"], "Bearer firefox-secret-value")
        item = page.evaluate("__store.ppsFirefoxInboxQueue[0]")
        self.assertNotIn("cf-client-id-value", json.dumps(item))
        self.assertNotIn("cf-secret-value", json.dumps(item))
        self.assertNotIn("firefox-secret-value", json.dumps(item))
        page.close()

    def test_06_duplicate_auth_failure_and_strict_rejection(self):
        duplicate = self.background_page(response_body={"status": "DRAFT", "proposal_id": 9, "review_url": "/requests/smart-intake/proposals/9", "duplicate": True})
        self.invoke(duplicate, payload())
        duplicate.wait_for_timeout(100)
        self.assertEqual(duplicate.evaluate("__store.ppsFirefoxInboxQueue[0].status"), "SENT_DUPLICATE")
        self.invoke(duplicate, payload())
        duplicate.wait_for_timeout(50)
        self.assertEqual(duplicate.evaluate("__fetches.length"), 1, "known terminal references must not be retransmitted")
        conflict = self.invoke(duplicate, payload(additional_notes="changed content"))
        duplicate.wait_for_timeout(50)
        self.assertFalse(conflict["ok"])
        self.assertEqual(duplicate.evaluate("__store.ppsFirefoxInboxQueue[0].status"), "REJECTED")
        self.assertEqual(duplicate.evaluate("__fetches.length"), 1)
        duplicate.close()

        unauthorized = self.background_page(response_status=401, response_body={"detail": "bad token"})
        self.invoke(unauthorized, payload())
        unauthorized.wait_for_timeout(100)
        self.assertEqual(unauthorized.evaluate("__store.ppsFirefoxInboxQueue[0].status"), "AUTH_FAILED")
        unauthorized.close()

        strict = self.background_page()
        result = self.invoke(strict, payload(job_id=1))
        strict.wait_for_timeout(50)
        self.assertFalse(result["ok"])
        self.assertEqual(strict.evaluate("(__store.ppsFirefoxInboxQueue||[]).length"), 0)
        strict.close()

    def test_07_offline_retry_is_bounded_and_durable(self):
        page = self.background_page(offline=True)
        self.invoke(page, payload())
        page.wait_for_timeout(1800)
        item = page.evaluate("__store.ppsFirefoxInboxQueue[0]")
        self.assertEqual(item["status"], "RETRY")
        self.assertEqual(item["attempt_count"], 3)
        self.assertEqual(page.evaluate("__fetches.length"), 3, "each attempt uses the configured API base once")
        self.assertIn("offline", item["last_error"])
        page.close()

    def test_08_manifest_permissions_and_connector_separation(self):
        manifest = json.loads((EXT / "manifest.json").read_text())
        self.assertEqual(manifest["version"], "0.17.3")
        self.assertIn("https://chatgpt.com/*", manifest["host_permissions"])
        self.assertIn("https://api.pinpointsourcing.com/*", manifest["host_permissions"])
        self.assertEqual(manifest["background"]["scripts"], ["background.js"])
        self.assertIn("notifications", manifest["permissions"])
        forbidden = {"clipboardRead", "nativeMessaging", "webRequest", "cookies", "history", "<all_urls>"}
        self.assertFalse(forbidden.intersection(manifest["permissions"] + manifest["host_permissions"]))
        self.assertNotIn("externally_connectable", manifest)
        self.assertNotIn("web_accessible_resources", manifest)
        chatgpt = next(item for item in manifest["content_scripts"] if item["matches"] == ["https://chatgpt.com/*"])
        self.assertEqual(chatgpt["js"], ["chatgpt_bridge.js"])
        configured = next(item for item in manifest["content_scripts"] if "https://sis2.cat.com/*" in item["matches"])
        self.assertEqual(configured["js"], ["sdk.js", "connectors.js", "content.js"])
        background = (EXT / "background.js").read_text()
        bridge = (EXT / "chatgpt_bridge.js").read_text()
        self.assertNotIn("ppsFirefoxInboxToken", bridge)
        for forbidden_source in ("window.postMessage", "eval(", "clipboard", "localStorage", "document.cookie"):
            self.assertNotIn(forbidden_source, bridge)
        self.assertIn("browser.runtime.sendMessage", bridge)
        self.assertIn("sender.tab?.url", background)

    def test_09_non_chatgpt_origin_is_inert(self):
        page = self.browser.new_page()
        page.set_content(f'<div data-message-author-role="assistant">{envelope(payload(client_reference="wrong-origin"))}</div>')
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("window.__messages.length"), 0)
        page.close()

    def test_10_multiple_startup_assistants_each_submit_once(self):
        page = self.page()
        first = envelope(payload(client_reference="startup-multiple-1"))
        second = envelope(payload(client_reference="startup-multiple-2"))
        page.set_content('<div data-message-author-role="assistant"></div><div data-message-author-role="assistant"></div>')
        page.evaluate("values => document.querySelectorAll('[data-message-author-role=assistant]').forEach((node,index) => { node.textContent=values[index]; })", [first, second])
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.wait_for_timeout(100)
        self.assertEqual(
            page.evaluate("window.__messages.map(item => item.payload.client_reference)"),
            ["startup-multiple-1", "startup-multiple-2"],
        )
        page.evaluate("() => { for (const node of document.querySelectorAll('[data-message-author-role=assistant]')) node.append(' later'); }")
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("window.__messages.length"), 2)
        page.close()

    def test_11_legacy_guard_allows_existing_document_attachment(self):
        page = self.page()
        page.set_content(
            f'<div data-message-author-role="assistant">'
            f'{envelope(payload(client_reference="legacy-guard-existing"))}</div>'
        )
        page.evaluate("""() => {
          window.__PPS_CHATGPT_BRIDGE_V1__ = true;
          window.__messages=[];
          window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}};
        }""")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.wait_for_timeout(100)
        self.assertEqual(
            page.evaluate("window.__messages.map(item => item.payload.client_reference)"),
            ["legacy-guard-existing"],
        )
        self.assertEqual(page.evaluate("typeof window.__PPS_CHATGPT_BRIDGE_V1__.activate"), "function")
        page.close()

    def test_12_spa_navigation_and_return_use_bounded_activation(self):
        page = self.page()
        page.set_content("<main></main>")
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.evaluate("""({first}) => {
          history.pushState({}, '', '/c/first');
          document.querySelector('main').innerHTML=`<div data-message-author-role="assistant">${first}</div>`;
        }""", {"first": envelope(payload(client_reference="spa-first"))})
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.wait_for_timeout(100)
        page.evaluate("""({second}) => {
          history.pushState({}, '', '/c/second');
          document.querySelector('main').innerHTML=`<div data-message-author-role="assistant">${second}</div>`;
        }""", {"second": envelope(payload(client_reference="spa-second"))})
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.wait_for_timeout(100)
        page.evaluate("history.pushState({}, '', '/c/first')")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        page.wait_for_timeout(50)
        self.assertEqual(
            page.evaluate("window.__messages.map(item => item.payload.client_reference)"),
            ["spa-first", "spa-second"],
        )
        page.close()

    def test_13_background_activates_existing_spa_and_returned_tabs(self):
        page = self.background_page()
        page.evaluate("""() => {
          __tabQuery.push({id:21,url:'https://chatgpt.com/c/existing'});
          __listeners.installed[0]({reason:'update'});
        }""")
        page.wait_for_timeout(100)
        page.evaluate("__listeners.updated[0](21,{url:'https://chatgpt.com/c/next'},{id:21,url:'https://chatgpt.com/c/next'})")
        page.evaluate("__listeners.activated[0]({tabId:21})")
        page.wait_for_timeout(100)
        executions = page.evaluate("__executions")
        self.assertEqual(len(executions), 3)
        self.assertTrue(all(item["target"] == {"tabId": 21} for item in executions))
        self.assertTrue(all(item["files"] == ["chatgpt_bridge.js"] for item in executions))
        page.evaluate("__listeners.updated[0](99,{url:'https://evil.example/'},{id:99,url:'https://evil.example/'})")
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("__executions.length"), 3)
        page.evaluate("__listeners.focused[0](4)")
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("__executions.length"), 4)
        page.evaluate("__listeners.focused[0](-1)")
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("__executions.length"), 4)
        page.close()

    def test_14_pairing_resumes_auth_failed_queue_without_new_message(self):
        page = self.background_page(token="")
        self.invoke(page, payload(client_reference="pairing-resume"))
        page.wait_for_timeout(100)
        self.assertEqual(page.evaluate("__store.ppsFirefoxInboxQueue[0].status"), "AUTH_FAILED")
        page.evaluate("""async () => {
          __store.ppsFirefoxToken='configured-test-token';
          __listeners.storageChanged[0]({ppsFirefoxToken:{oldValue:'',newValue:'configured-test-token'}},'local');
        }""")
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("__store.ppsFirefoxInboxQueue[0].status"), "SENT")
        self.assertEqual(page.evaluate("__fetches.length"), 1)
        page.close()

    def test_15_markdown_code_block_and_normal_whitespace(self):
        page = self.page()
        page.set_content("<main></main>")
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        text = envelope(payload(client_reference="markdown-code-block"))
        page.evaluate("""text => {
          const assistant=document.createElement('div');
          assistant.dataset.messageAuthorRole='assistant';
          const pre=document.createElement('pre');
          const code=document.createElement('code');
          code.textContent=`  \n${text}\n  `;
          pre.append(code); assistant.append(pre); document.querySelector('main').append(assistant);
        }""", text)
        page.wait_for_timeout(100)
        self.assertEqual(
            page.evaluate("window.__messages.map(item => item.payload.client_reference)"),
            ["markdown-code-block"],
        )
        page.close()

    def test_16_markers_split_across_nested_dom_nodes(self):
        page = self.page()
        page.set_content("<main></main>")
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        value = json.dumps(payload(client_reference="nested-marker-nodes"))
        page.evaluate("""value => {
          const assistant=document.createElement('div');
          assistant.dataset.messageAuthorRole='assistant';
          for (const character of '[PPS_INTAKE_PACKAGE_V1]') {
            const span=document.createElement('span'); span.textContent=character; assistant.append(span);
          }
          assistant.append(document.createTextNode(`\n${value}\n`));
          for (const character of '[/PPS_INTAKE_PACKAGE_V1]') {
            const span=document.createElement('span'); span.textContent=character; assistant.append(span);
          }
          document.querySelector('main').append(assistant);
        }""", value)
        page.wait_for_timeout(100)
        self.assertEqual(
            page.evaluate("window.__messages.map(item => item.payload.client_reference)"),
            ["nested-marker-nodes"],
        )
        page.close()

    def test_17_known_zero_width_rendering_characters_only(self):
        page = self.page()
        page.set_content("<main></main>")
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        for index, character in enumerate(("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff")):
            opening = "[PPS_INTAKE_PACKAGE_V1]".replace("_", f"_{character}", 1)
            closing = "[/PPS_INTAKE_PACKAGE_V1]".replace("_", f"_{character}", 1)
            text = f"{opening}{character}\n{json.dumps(payload(client_reference=f'zero-width-{index}'))}\n{character}{closing}"
            page.evaluate("""text => {
              const node=document.createElement('div'); node.dataset.messageAuthorRole='assistant';
              node.textContent=text; document.querySelector('main').append(node);
            }""", text)
        page.wait_for_timeout(150)
        self.assertEqual(
            page.evaluate("window.__messages.map(item => item.payload.client_reference)"),
            [f"zero-width-{index}" for index in range(5)],
        )
        page.close()

    def test_18_unicode_lookalike_markers_are_not_accepted(self):
        page = self.page()
        page.set_content("<main></main>")
        page.evaluate("window.__messages=[]; window.browser={runtime:{sendMessage:m=>{window.__messages.push(m);return Promise.resolve();}}}")
        page.add_script_tag(path=str(EXT / "chatgpt_bridge.js"))
        valid = envelope(payload(client_reference="lookalike-rejected"))
        lookalikes = [
            valid.replace("PPS_INTAKE", "\u03a1PS_INTAKE"),
            valid.replace("[PPS_INTAKE_PACKAGE_V1]", "\uff3bPPS_INTAKE_PACKAGE_V1\uff3d"),
        ]
        for text in lookalikes:
            page.evaluate("""text => {
              const node=document.createElement('div'); node.dataset.messageAuthorRole='assistant';
              node.textContent=text; document.querySelector('main').append(node);
            }""", text)
        page.wait_for_timeout(100)
        self.assertEqual(page.evaluate("window.__messages.length"), 0)
        page.close()

    def test_19_release_package_matches_tested_runtime_source(self):
        package = ROOT / "artifacts" / "pps-firefox-extension-0.17.3-unsigned.xpi"
        runtime_files = {
            "background.js", "chatgpt_bridge.js", "connectors.js", "content.js",
            "manifest.json", "popup.html", "popup.js", "sdk.js", "style.css",
        }
        if not package.is_file():
            self.skipTest(
                "0.17.3 release artifact not built; run release packaging before parity verification"
            )
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(set(archive.namelist()), runtime_files)
            for name in runtime_files:
                self.assertEqual(archive.read(name), (EXT / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
