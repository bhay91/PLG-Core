(() => {
  if (window.location.origin !== "https://chatgpt.com") return;
  const existingBridge = window.__PPS_CHATGPT_BRIDGE_V1__;
  if (existingBridge && typeof existingBridge.activate === "function") {
    existingBridge.activate();
    return;
  }

  const OPEN = "[PPS_INTAKE_PACKAGE_V1]";
  const CLOSE = "[/PPS_INTAKE_PACKAGE_V1]";
  const MESSAGE_TYPE = "PPS_CHATGPT_INTAKE_PACKAGE_V1";
  const RESEARCH_OPEN = "[PPS_RESEARCH_IMPORT_PACKAGE_V1]";
  const RESEARCH_CLOSE = "[/PPS_RESEARCH_IMPORT_PACKAGE_V1]";
  const RESEARCH_MESSAGE_TYPE = "PPS_CHATGPT_RESEARCH_IMPORT_PACKAGE_V1";
  const MAX_ENVELOPE_CHARS = 48 * 1024;
  const MAX_RESEARCH_ENVELOPE_CHARS = 14 * 1024 * 1024;
  // ChatGPT may render a marker through nested Markdown spans or insert a
  // format-only character at a span boundary. These are the only characters
  // ignored while locating markers; payload JSON is never normalized.
  const MARKER_RENDERING_CHARS = new Set(["\u200B", "\u200C", "\u200D", "\u2060", "\uFEFF"]);
  const EXACT_KEYS = new Set([
    "schema_version", "source", "client_reference", "original_input", "customer",
    "machines", "requested_needs", "additional_notes", "research_evidence"
  ]);
  const messageStates = new WeakMap();

  function isAssistantMessage(node) {
    return node instanceof Element && node.matches('[data-message-author-role="assistant"]');
  }

  function shallowValid(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const keys = Object.keys(value);
    if (keys.length !== EXACT_KEYS.size || keys.some(key => !EXACT_KEYS.has(key))) return false;
    return value.schema_version === "1" && value.source === "CHATGPT_FIREFOX" &&
      typeof value.client_reference === "string" && value.client_reference.trim().length > 0 &&
      typeof value.original_input === "string" && value.original_input.trim().length > 0 &&
      (value.customer === null || (typeof value.customer === "object" && !Array.isArray(value.customer))) &&
      Array.isArray(value.machines) && Array.isArray(value.requested_needs) &&
      typeof value.additional_notes === "string" &&
      (value.research_evidence === null || typeof value.research_evidence === "object");
  }

  function shallowValidResearch(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const keys = Object.keys(value);
    if (keys.length !== 3 || !["schema_version", "package", "pdf_base64"].every(key => keys.includes(key))) return false;
    if (value.schema_version !== "1" || !value.package || typeof value.package !== "object" || Array.isArray(value.package)) return false;
    const source = value.package.source_pdf;
    if (!source || typeof source.filename !== "string" || !/\.pdf$/i.test(source.filename)) return false;
    if (typeof value.pdf_base64 !== "string" || value.pdf_base64.length < 1 || value.pdf_base64.length > MAX_RESEARCH_ENVELOPE_CHARS || !/^[A-Za-z0-9+/]*={0,2}$/.test(value.pdf_base64)) return false;
    return true;
  }

  function rejectOnce(container, reason) {
    const state = messageStates.get(container);
    if (!state || state.processed || state.rejected) return;
    state.rejected = true;
    browser.runtime.sendMessage({ type: MESSAGE_TYPE, rejection: reason }).catch(() => {});
  }

  function markerProjection(text) {
    let rendered = "";
    const sourceOffsets = [];
    for (let index = 0; index < text.length;) {
      const codePoint = text.codePointAt(index);
      const character = String.fromCodePoint(codePoint);
      if (!MARKER_RENDERING_CHARS.has(character)) {
        rendered += character;
        sourceOffsets.push(index);
      }
      index += character.length;
    }
    return { rendered, sourceOffsets };
  }

  function extractEnvelope(container, open = OPEN, close = CLOSE, maxChars = MAX_ENVELOPE_CHARS) {
    // textContent concatenates nested Markdown/code spans without introducing
    // layout whitespace. innerText is retained only as a defensive fallback.
    const text = String(container.textContent || container.innerText || "");
    const projection = markerProjection(text);
    const start = projection.rendered.indexOf(open);
    if (start < 0) return { state: "PENDING" };
    const contentStart = start + open.length;
    const end = projection.rendered.indexOf(close, contentStart);
    if (projection.rendered.indexOf(open, contentStart) >= 0) {
      return { state: "REJECTED", reason: "Malformed PPS intake envelope." };
    }
    if (end < 0) return { state: "PENDING" };

    const sourceStart = projection.sourceOffsets[contentStart - 1] + 1;
    const sourceEnd = projection.sourceOffsets[end];
    const boundaryRenderingChars = /^[\s\u200B\u200C\u200D\u2060\uFEFF]+|[\s\u200B\u200C\u200D\u2060\uFEFF]+$/g;
    const encoded = text.slice(sourceStart, sourceEnd).replace(boundaryRenderingChars, "");
    if (!encoded || encoded.length > maxChars) {
      return { state: "REJECTED", reason: "Malformed PPS intake envelope." };
    }
    return { state: "COMPLETE", encoded };
  }

  function inspectAssistantMessage(container) {
    const state = messageStates.get(container);
    if (!state || state.processed || state.rejected) return;
    const extracted = extractEnvelope(container, RESEARCH_OPEN, RESEARCH_CLOSE, MAX_RESEARCH_ENVELOPE_CHARS);
    const research = extracted.state !== "PENDING" ? extracted : extractEnvelope(container);
    if (research.state === "PENDING") return;
    if (research.state === "REJECTED") {
      rejectOnce(container, research.reason);
      return;
    }
    try {
      const payload = JSON.parse(research.encoded);
      if (research === extracted && shallowValidResearch(payload)) {
        state.processed = true;
        browser.runtime.sendMessage({ type: RESEARCH_MESSAGE_TYPE, payload }).catch(() => {});
        return;
      }
      if (research === extracted && !shallowValidResearch(payload)) {
        rejectOnce(container, "PPS Research Import package failed envelope validation.");
        return;
      }
      if (!shallowValid(payload)) {
        rejectOnce(container, "PPS intake envelope failed schema validation.");
        return;
      }
      state.processed = true;
      browser.runtime.sendMessage({ type: MESSAGE_TYPE, payload }).catch(() => {});
    } catch (_) {
      rejectOnce(container, "Malformed PPS intake envelope.");
    }
  }

  function registerAssistant(container) {
    if (!messageStates.has(container)) {
      messageStates.set(container, { processed: false, rejected: false });
    }
    inspectAssistantMessage(container);
  }

  function assistantForNode(node) {
    const element = node instanceof Element ? node : node.parentElement;
    return element?.closest?.('[data-message-author-role="assistant"]') || null;
  }

  const observer = new MutationObserver(records => {
    for (const record of records) {
      const containingAssistant = assistantForNode(record.target);
      if (containingAssistant) {
        registerAssistant(containingAssistant);
      }
      for (const node of record.addedNodes) {
        if (!(node instanceof Element)) {
          const assistant = assistantForNode(node);
          if (assistant) registerAssistant(assistant);
          continue;
        }
        if (isAssistantMessage(node)) registerAssistant(node);
        for (const assistant of node.querySelectorAll?.('[data-message-author-role="assistant"]') || []) {
          registerAssistant(assistant);
        }
      }
    }
  });
  observer.observe(document.documentElement, { childList: true, characterData: true, subtree: true });

  function scanCurrentConversation() {
    // Lifecycle activation is bounded to the assistant messages currently
    // rendered by ChatGPT. WeakMap state prevents a previously handled DOM
    // message from being submitted again.
    for (const assistant of document.querySelectorAll('[data-message-author-role="assistant"]')) {
      registerAssistant(assistant);
    }
  }

  window.__PPS_CHATGPT_BRIDGE_V1__ = Object.freeze({
    activate: scanCurrentConversation
  });

  // ChatGPT may finish rendering a response before an installed, updated, or
  // restored content script attaches. Scan once now; later lifecycle events
  // call the same bounded scanner, while streaming remains observer-driven.
  scanCurrentConversation();
})();
