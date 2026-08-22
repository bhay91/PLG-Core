const MESSAGE_TYPE = "PPS_CHATGPT_INTAKE_PACKAGE_V1";
const QUEUE_KEY = "ppsFirefoxInboxQueue";
const TOKEN_KEY = "ppsFirefoxInboxToken";
const MAX_ATTEMPTS = 3;
const BASES = ["http://127.0.0.1:8000", "http://localhost:8000"];
const ENDPOINT = "/api/extension/v1/inbox/intake-proposals";
const ALLOWED_STATES = new Set(["PENDING", "SENDING", "SENT", "SENT_DUPLICATE", "RETRY", "AUTH_FAILED", "REJECTED"]);
const TOP_KEYS = ["schema_version", "source", "client_reference", "original_input", "customer", "machines", "requested_needs", "additional_notes", "research_evidence"];
const CHATGPT_MATCH = "https://chatgpt.com/*";

function isChatGPTUrl(value) {
  try { return new URL(value || "").origin === "https://chatgpt.com"; }
  catch (_) { return false; }
}

async function activateChatGPTBridge(tabId, url = "") {
  if (!Number.isInteger(tabId)) return false;
  let candidateUrl = url;
  if (!candidateUrl) {
    try { candidateUrl = (await browser.tabs.get(tabId))?.url || ""; }
    catch (_) { return false; }
  }
  if (!isChatGPTUrl(candidateUrl)) return false;
  try {
    await browser.scripting.executeScript({
      target: { tabId },
      files: ["chatgpt_bridge.js"]
    });
    return true;
  } catch (_) {
    return false;
  }
}

async function activateExistingChatGPTTabs() {
  let tabs = [];
  try { tabs = await browser.tabs.query({ url: CHATGPT_MATCH }); }
  catch (_) { return; }
  await Promise.all(tabs.map(tab => activateChatGPTBridge(tab.id, tab.url)));
}

function plainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exactKeys(value, keys) {
  return plainObject(value) && Object.keys(value).length === keys.length && Object.keys(value).every(key => keys.includes(key));
}

function boundedText(value, min, max) {
  return typeof value === "string" && value.length >= min && value.length <= max;
}

function validUrl(value) {
  if (!boundedText(value, 1, 2048)) return false;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && Boolean(url.hostname) && !url.username && !url.password;
  } catch (_) { return false; }
}

function validatePackage(value) {
  if (!exactKeys(value, TOP_KEYS)) throw new Error("Package fields do not match the PPS intake contract.");
  if (value.schema_version !== "1" || value.source !== "CHATGPT_FIREFOX") throw new Error("Unsupported PPS package source or version.");
  if (!boundedText(value.client_reference?.trim(), 1, 128)) throw new Error("Client reference is required.");
  if (!boundedText(value.original_input, 1, 10000) || !value.original_input.trim()) throw new Error("Original input is required.");
  if (!boundedText(value.additional_notes, 0, 4000)) throw new Error("Additional notes are invalid.");
  if (value.customer !== null) {
    if (!exactKeys(value.customer, ["name", "company"]) || !boundedText(value.customer.name, 0, 200) || !boundedText(value.customer.company, 0, 200)) throw new Error("Customer candidate is invalid.");
  }
  if (!Array.isArray(value.machines) || value.machines.length > 10) throw new Error("Machine candidates are invalid.");
  const references = new Set();
  for (const machine of value.machines) {
    if (!exactKeys(machine, ["reference", "manufacturer", "model", "year", "asset_type", "identifiers"])) throw new Error("Machine candidate fields are invalid.");
    if (!boundedText(machine.reference, 1, 128) || references.has(machine.reference)) throw new Error("Machine references must be unique.");
    references.add(machine.reference);
    if (!boundedText(machine.manufacturer, 0, 200) || !boundedText(machine.model, 0, 200) || !(machine.year === null || boundedText(machine.year, 0, 10))) throw new Error("Machine candidate text is invalid.");
    if (!["vehicle", "machine", "engine", "component", "other"].includes(machine.asset_type)) throw new Error("Machine asset type is invalid.");
    if (!Array.isArray(machine.identifiers) || machine.identifiers.length > 12) throw new Error("Machine identifiers are invalid.");
    for (const identifier of machine.identifiers) {
      if (!exactKeys(identifier, ["type", "value", "component_label", "primary"]) || !boundedText(identifier.value, 1, 128) || !boundedText(identifier.component_label, 0, 100) || typeof identifier.primary !== "boolean") throw new Error("Identifier candidate is invalid.");
      if (!["AUTOMOTIVE_VIN", "JDM_FRAME", "JDM_CHASSIS", "MODEL_CODE", "PIN", "MACHINE_SERIAL", "ENGINE_SERIAL", "COMPONENT_SERIAL", "OTHER_IDENTIFIER", "UNKNOWN"].includes(identifier.type)) throw new Error("Identifier type is invalid.");
    }
  }
  if (!Array.isArray(value.requested_needs) || value.requested_needs.length > 100) throw new Error("Requested needs are invalid.");
  for (const need of value.requested_needs) {
    if (!exactKeys(need, ["original_wording", "quantity", "machine_reference"]) || !boundedText(need.original_wording, 1, 1000)) throw new Error("Requested need is invalid.");
    if (!(need.quantity === null || (Number.isFinite(need.quantity) && need.quantity > 0 && need.quantity <= 1000000))) throw new Error("Requested quantity is invalid.");
    if (!(need.machine_reference === null || boundedText(need.machine_reference, 0, 128))) throw new Error("Requested need machine reference is invalid.");
  }
  if (value.research_evidence !== null) {
    const evidenceKeys = ["source_urls", "claims", "quoted_evidence", "options"];
    if (!plainObject(value.research_evidence) || Object.keys(value.research_evidence).some(key => !evidenceKeys.includes(key))) throw new Error("Research evidence fields are invalid.");
    const { source_urls: urls = [], claims = [], quoted_evidence: quotes = [], options = [] } = value.research_evidence;
    if (!Array.isArray(urls) || urls.length > 20 || urls.some(url => !validUrl(url))) throw new Error("Research evidence URL is invalid.");
    if (!Array.isArray(claims) || claims.length > 50 || claims.some(item => !boundedText(item, 1, 1000))) throw new Error("Research claims are invalid.");
    if (!Array.isArray(quotes) || quotes.length > 50 || quotes.some(item => !boundedText(item, 1, 2000))) throw new Error("Quoted evidence is invalid.");
    if (!Array.isArray(options) || options.length > 25) throw new Error("Research options are invalid.");
    const optionKeys = ["source_url", "source_name", "product_description", "part_number", "price", "currency", "research_notes", "confidence", "verification_status"];
    for (const option of options) {
      if (!plainObject(option) || Object.keys(option).some(key => !optionKeys.includes(key)) || !validUrl(option.source_url)) throw new Error("Research option source is invalid.");
      if (!boundedText(option.source_name || "", 0, 200) || !boundedText(option.product_description || "", 0, 1000) || !boundedText(option.part_number || "", 0, 200) || !boundedText(option.research_notes || "", 0, 2000)) throw new Error("Research option text is invalid.");
      if (!(option.price === null || option.price === undefined || (Number.isFinite(option.price) && option.price >= 0 && option.price <= 100000000))) throw new Error("Research option price is invalid.");
      if (!/^[A-Z]{3}$/.test(option.currency || "USD")) throw new Error("Research option currency is invalid.");
      if (!["UNKNOWN", "LOW", "MEDIUM", "HIGH"].includes(option.confidence || "UNKNOWN")) throw new Error("Research option confidence is invalid.");
      if (!["UNVERIFIED", "NEEDS_REVIEW", "VERIFIED"].includes(option.verification_status || "UNVERIFIED")) throw new Error("Research option verification status is invalid.");
    }
  }
  return JSON.parse(JSON.stringify(value));
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (plainObject(value)) return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

async function digest(value) {
  const copy = JSON.parse(JSON.stringify(value));
  delete copy.client_reference;
  const bytes = new TextEncoder().encode(canonical(copy));
  const result = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(result)].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

async function readQueue() {
  const stored = (await browser.storage.local.get(QUEUE_KEY))[QUEUE_KEY];
  return Array.isArray(stored) ? stored.filter(item => item && ALLOWED_STATES.has(item.status)) : [];
}

async function writeQueue(queue) {
  await browser.storage.local.set({ [QUEUE_KEY]: queue });
  await updateBadge(queue);
}

async function updateBadge(queue = null) {
  const items = queue || await readQueue();
  const pending = items.filter(item => ["PENDING", "SENDING", "RETRY", "AUTH_FAILED"].includes(item.status)).length;
  await browser.action.setBadgeText({ text: pending ? String(pending) : "" });
  if (pending) await browser.action.setBadgeBackgroundColor({ color: "#9a6700" });
}

async function notify(title, message) {
  await browser.notifications.create({ type: "basic", title, message });
}

function safeError(value) {
  return String(value || "Unknown error").replace(/Bearer\s+\S+/gi, "Bearer [redacted]").slice(0, 300);
}

async function updateItem(clientReference, changes) {
  const queue = await readQueue();
  const item = queue.find(entry => entry.client_reference === clientReference);
  if (!item) return null;
  Object.assign(item, changes, { updated_at: new Date().toISOString() });
  await writeQueue(queue);
  return item;
}

async function postPackage(payload, token) {
  let networkError = null;
  for (const base of BASES) {
    try {
      const response = await fetch(base + ENDPOINT, {
        method: "POST",
        headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      let data = {};
      try { data = await response.json(); } catch (_) {}
      return { response, data, base };
    } catch (error) { networkError = error; }
  }
  throw networkError || new Error("PPS is offline.");
}

function wait(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function sendQueuedItem(clientReference) {
  let queue = await readQueue();
  let item = queue.find(entry => entry.client_reference === clientReference);
  if (!item || ["SENT", "SENT_DUPLICATE", "AUTH_FAILED", "REJECTED"].includes(item.status)) return;
  const token = String((await browser.storage.local.get(TOKEN_KEY))[TOKEN_KEY] || "").trim();
  if (!token) {
    await updateItem(clientReference, { status: "AUTH_FAILED", last_error: "PPS Firefox Inbox token is not configured." });
    await notify("PPS Inbox authorization required", "Open the PPS extension and configure the local Inbox token.");
    return;
  }
  while (item.attempt_count < MAX_ATTEMPTS) {
    item = await updateItem(clientReference, { status: "SENDING", attempt_count: item.attempt_count + 1, last_error: "" });
    try {
      const { response, data, base } = await postPackage(item.payload, token);
      if (response.ok && data.status === "DRAFT" && Number.isFinite(Number(data.proposal_id))) {
        const status = data.duplicate ? "SENT_DUPLICATE" : "SENT";
        await updateItem(clientReference, { status, review_url: data.review_url || "", proposal_id: Number(data.proposal_id), last_error: "", pps_base: base });
        await notify("Sent to PPS Inbox", "DRAFT Smart Intake proposal created.");
        return;
      }
      if (response.status === 401 || response.status === 403) {
        await updateItem(clientReference, { status: "AUTH_FAILED", last_error: safeError(data.detail || "PPS authorization failed.") });
        await notify("PPS Inbox authorization failed", "Open the PPS extension and verify the local Inbox token.");
        return;
      }
      if ([400, 409, 413, 422].includes(response.status)) {
        await updateItem(clientReference, { status: response.status === 409 ? "REJECTED" : "REJECTED", last_error: safeError(data.detail || `PPS rejected the package (${response.status}).`) });
        await notify("PPS Inbox package rejected", safeError(data.detail || "Review the extension queue for details."));
        return;
      }
      throw new Error(data.detail || `PPS server error (${response.status}).`);
    } catch (error) {
      item = await updateItem(clientReference, { status: "RETRY", last_error: safeError(error?.message || error) });
      if (item.attempt_count < MAX_ATTEMPTS) await wait(500 * item.attempt_count);
    }
  }
  await notify("PPS Inbox submission pending", "PPS is unavailable. The DRAFT package remains queued for operator attention.");
}

async function queueAndSend(rawPayload) {
  let payload;
  try { payload = validatePackage(rawPayload); }
  catch (error) {
    await notify("PPS Inbox package rejected", safeError(error.message));
    return { ok: false, state: "REJECTED" };
  }
  const normalizedDigest = await digest(payload);
  const queue = await readQueue();
  const existing = queue.find(item => item.client_reference === payload.client_reference);
  if (existing) {
    if (existing.normalized_digest !== normalizedDigest) {
      existing.status = "REJECTED";
      existing.last_error = "Client reference was reused with different content.";
      existing.updated_at = new Date().toISOString();
      await writeQueue(queue);
      await notify("PPS Inbox package rejected", existing.last_error);
      return { ok: false, state: "REJECTED" };
    }
    if (["SENT", "SENT_DUPLICATE"].includes(existing.status)) {
      return { ok: true, state: existing.status };
    }
    await sendQueuedItem(existing.client_reference);
    const refreshed = (await readQueue()).find(item => item.client_reference === existing.client_reference);
    return { ok: true, state: refreshed?.status || existing.status };
  }
  const now = new Date().toISOString();
  queue.push({
    schema_version: "1", source: "CHATGPT_FIREFOX",
    client_reference: payload.client_reference, normalized_digest: normalizedDigest,
    payload, status: "PENDING", created_at: now, updated_at: now,
    attempt_count: 0, last_error: ""
  });
  await writeQueue(queue);
  await sendQueuedItem(payload.client_reference);
  return { ok: true, state: "PENDING" };
}

async function queueRejectedEnvelope(reason) {
  const now = new Date().toISOString();
  const clientReference = `rejected-${crypto.randomUUID()}`;
  const queue = await readQueue();
  queue.push({
    schema_version: "1", source: "CHATGPT_FIREFOX",
    client_reference: clientReference, normalized_digest: await digest({ rejection: reason }),
    payload: null, status: "REJECTED", created_at: now, updated_at: now,
    attempt_count: 0, last_error: safeError(reason)
  });
  await writeQueue(queue);
  await notify("PPS Inbox package rejected", safeError(reason));
  return { ok: false, state: "REJECTED" };
}

async function recoverQueue() {
  const queue = await readQueue();
  for (const item of queue) {
    if (item.status === "SENDING") item.status = "RETRY";
    if (["PENDING", "RETRY"].includes(item.status)) item.attempt_count = 0;
  }
  await writeQueue(queue);
  for (const item of queue.filter(entry => ["PENDING", "RETRY"].includes(entry.status))) {
    await sendQueuedItem(item.client_reference);
  }
}

async function resumeAuthorizedQueue() {
  const queue = await readQueue();
  const resumable = queue.filter(item => item.status === "AUTH_FAILED");
  if (!resumable.length) return;
  for (const item of resumable) {
    item.status = "RETRY";
    item.attempt_count = 0;
    item.last_error = "";
    item.updated_at = new Date().toISOString();
  }
  await writeQueue(queue);
  for (const item of resumable) await sendQueuedItem(item.client_reference);
}

let recoveryPromise = null;
function scheduleRecovery() {
  if (!recoveryPromise) {
    recoveryPromise = recoverQueue().finally(() => { recoveryPromise = null; });
  }
  return recoveryPromise;
}

browser.runtime.onMessage.addListener((message, sender) => {
  if (message?.type !== MESSAGE_TYPE) return undefined;
  if (sender.id !== browser.runtime.id) return Promise.resolve({ ok: false, state: "REJECTED" });
  try {
    const origin = new URL(sender.tab?.url || "").origin;
    if (origin !== "https://chatgpt.com") return Promise.resolve({ ok: false, state: "REJECTED" });
  } catch (_) { return Promise.resolve({ ok: false, state: "REJECTED" }); }
  if (message.rejection === "Malformed PPS intake envelope." || message.rejection === "PPS intake envelope failed schema validation.") {
    return queueRejectedEnvelope(message.rejection);
  }
  return queueAndSend(message.payload);
});

browser.runtime.onStartup.addListener(() => { scheduleRecovery().catch(() => {}); });
browser.runtime.onStartup.addListener(() => { activateExistingChatGPTTabs().catch(() => {}); });
browser.runtime.onInstalled.addListener(() => {
  scheduleRecovery().catch(() => {});
  activateExistingChatGPTTabs().catch(() => {});
});
browser.tabs.onActivated.addListener(({ tabId }) => {
  activateChatGPTBridge(tabId).catch(() => {});
});
browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url || changeInfo.status === "complete") {
    activateChatGPTBridge(tabId, changeInfo.url || tab?.url || "").catch(() => {});
  }
});
browser.storage.onChanged.addListener((changes, areaName) => {
  if (areaName === "local" && changes[TOKEN_KEY]?.newValue) {
    resumeAuthorizedQueue().catch(() => {});
  }
});
browser.windows.onFocusChanged.addListener(windowId => {
  if (windowId === browser.windows.WINDOW_ID_NONE) return;
  browser.tabs.query({ active: true, windowId }).then(tabs => {
    const tab = tabs[0];
    if (tab) return activateChatGPTBridge(tab.id, tab.url);
    return false;
  }).catch(() => {});
});
browser.notifications.onClicked.addListener(async () => {
  const queue = await readQueue();
  const latest = [...queue].reverse().find(item => item.review_url && ["SENT", "SENT_DUPLICATE"].includes(item.status));
  if (latest) browser.tabs.create({ url: `${latest.pps_base || BASES[0]}${latest.review_url}` });
});

scheduleRecovery().catch(() => {});
activateExistingChatGPTTabs().catch(() => {});
