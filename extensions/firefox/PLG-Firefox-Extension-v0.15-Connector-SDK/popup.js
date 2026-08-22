const BASES = [
  "http://127.0.0.1:8000",
  "http://localhost:8000"
];
const FIREFOX_INBOX_TOKEN_KEY = "ppsFirefoxInboxToken";
const FIREFOX_INBOX_QUEUE_KEY = "ppsFirefoxInboxQueue";

let active = null;
let cart = null;
let detected = null;
let extractedCart = null;
let captureContext = null;

const $ = id => document.getElementById(id);

function status(message, ok = true) {
  $("status").textContent = message;
  $("status").className = ok ? "ok" : "error";
}

async function renderFirefoxInboxQueue() {
  const queue = (await browser.storage.local.get(FIREFOX_INBOX_QUEUE_KEY))[FIREFOX_INBOX_QUEUE_KEY];
  const container = $("firefoxInboxQueue");
  container.replaceChildren();
  if (!Array.isArray(queue) || !queue.length) {
    container.textContent = "No queued packages.";
    return;
  }
  for (const item of [...queue].reverse().slice(0, 10)) {
    const row = document.createElement("div");
    row.className = "queue-item";
    const heading = document.createElement("strong");
    heading.textContent = `${item.status || "UNKNOWN"} · ${item.client_reference || "No reference"}`;
    row.append(heading);
    if (item.last_error) {
      const error = document.createElement("span");
      error.className = "error";
      error.textContent = String(item.last_error).slice(0, 300);
      row.append(error);
    }
    if (item.review_url) {
      const link = document.createElement("a");
      link.href = `${item.pps_base || BASES[0]}${item.review_url}`;
      link.target = "_blank";
      link.textContent = "Open Smart Intake review";
      row.append(link);
    }
    container.append(row);
  }
}

async function initializeFirefoxInboxSettings() {
  const configured = String((await browser.storage.local.get(FIREFOX_INBOX_TOKEN_KEY))[FIREFOX_INBOX_TOKEN_KEY] || "").trim();
  $("firefoxInboxTokenStatus").textContent = configured ? "Local token configured." : "Local token not configured.";
  await renderFirefoxInboxQueue();
}

async function saveFirefoxInboxToken() {
  const input = $("firefoxInboxToken");
  const token = input.value.trim();
  if (!token) {
    $("firefoxInboxTokenStatus").textContent = "Enter the dedicated PPS Firefox Inbox token.";
    return;
  }
  await browser.storage.local.set({ [FIREFOX_INBOX_TOKEN_KEY]: token });
  input.value = "";
  $("firefoxInboxTokenStatus").textContent = "Local token saved.";
}

function money(value) {
  return value == null ? "—" : `${cart?.currency || "USD"} ${Number(value).toFixed(2)}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

function captureMode(item) {
  if (item?.product_page_url && item?.cart_page_url) return "MERGED";
  return String(cart?.capture_mode || "PAGE").toUpperCase();
}

function captureDetails(item, index) {
  const original = extractedCart?.items?.[index] || {};
  const identifiers = [["MPN", item.manufacturer_part_number], ["Supplier part", item.supplier_part_number], ["SKU", item.sku], ["ASIN", item.asin], ["Listing ID", item.listing_id], ["Item ID", item.item_id]].filter(([, value]) => value);
  const edited = ["supplier_name", "manufacturer_part_number", "supplier_part_number", "sku", "asin", "listing_id", "item_id", "description", "supplier_cost", "quantity", "weight", "weight_unit", "length", "width", "height", "dimension_unit", "availability"]
    .filter(field => String(original[field] ?? "") !== String(item[field] ?? ""))
    .map(field => `<li>${escapeHtml(field.replaceAll("_", " "))}: <del>${escapeHtml(original[field] ?? "blank")}</del> → <ins>${escapeHtml(item[field] ?? "blank")}</ins></li>`).join("");
  const fieldEvidence = item.evidence_fields && Object.keys(item.evidence_fields).length ? Object.entries(item.evidence_fields).map(([field, value]) => `${field}: ${typeof value === "string" ? value : JSON.stringify(value)}`).join("; ") : "";
  return `<details class="capture-details"><summary>Capture Details</summary><dl><dt>Mode</dt><dd>${captureMode(item)}</dd><dt>Source</dt><dd>${escapeHtml(item.source_name || cart.source_name)}${item.source_domain || cart.source_domain ? ` · ${escapeHtml(item.source_domain || cart.source_domain)}` : ""}</dd>${item.product_page_url ? `<dt>Product URL</dt><dd><a href="${escapeHtml(item.product_page_url)}" target="_blank">${escapeHtml(item.product_page_url)}</a></dd>` : ""}${item.cart_page_url ? `<dt>Cart URL</dt><dd><a href="${escapeHtml(item.cart_page_url)}" target="_blank">${escapeHtml(item.cart_page_url)}</a></dd>` : ""}${identifiers.length ? `<dt>Identifiers</dt><dd>${identifiers.map(([label, value]) => `${label}: ${escapeHtml(value)}`).join(" · ")}</dd>` : ""}${item.evidence ? `<dt>Evidence</dt><dd>${escapeHtml(item.evidence)}</dd>` : ""}${fieldEvidence ? `<dt>Field evidence</dt><dd>${escapeHtml(fieldEvidence)}</dd>` : ""}</dl>${edited ? `<div class="capture-edits"><strong>Extracted → edited</strong><ul>${edited}</ul></div>` : `<p class="capture-unchanged">No operator edits.</p>`}</details>`;
}

async function api(path, options = {}) {
  let lastError = null;

  for (const base of BASES) {
    try {
      return await fetch(base + path, options);
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError || new Error("PLG Core is unavailable.");
}

async function responseJson(response, fallback) {
  let data;
  try {
    data = await response.json();
  } catch (_) {
    throw new Error(fallback || `PPS returned an unreadable response (${response.status}).`);
  }
  if (!data || typeof data !== "object") {
    throw new Error(fallback || "PPS returned an unexpected response.");
  }
  return data;
}

function contextIdentity(value) {
  const normalize = item => item == null || item === "" ? null : String(item);
  return {
    job_id: normalize(value?.job_id),
    job_asset_id: normalize(value?.job_asset_id),
    requested_need_id: normalize(value?.requested_need_id),
    verification_session_id: normalize(value?.verification_session_id)
  };
}

function sameWorkContext(left, right) {
  const a = contextIdentity(left);
  const b = contextIdentity(right);
  return Object.keys(a).every(key => a[key] === b[key]);
}

async function currentTab() {
  const [tab] = await browser.tabs.query({
    active: true,
    currentWindow: true
  });

  if (!tab?.id) throw new Error("No active browser tab.");
  return tab;
}

async function send(message) {
  const tab = await currentTab();

  try {
    return await browser.tabs.sendMessage(tab.id, message);
  } catch (error) {
    await browser.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["sdk.js", "connectors.js", "content.js"]
    });

    return browser.tabs.sendMessage(tab.id, message);
  }
}

async function detectSite() {
  const result = await send({ type: "PLG_DETECT_SITE" });

  if (!result?.ok) {
    detected = null;
    $("detectedSite").textContent = "Unavailable page";
    status(result?.status || "Could not detect supplier site.", false);
    return;
  }

  detected = result;
  $("detectedSite").textContent = result.configured
    ? `Configured source: ${result.source_name}`
    : `One-time Website: ${result.domain}`;
}

function sameOrigin(left, right) {
  try {
    return new URL(left).origin === new URL(right).origin;
  } catch (_) {
    return false;
  }
}

function cacheKey() {
  if (!active || !detected?.domain) return "";
  return `pps-page:${active.job_id}:${active.job_asset_id || 0}:${active.requested_need_id || 0}:${detected.domain}`;
}

async function cachePageCapture(capture) {
  const key = cacheKey();
  if (key) await browser.storage.local.set({ [key]: capture });
}

async function cachedPageCapture() {
  const key = cacheKey();
  if (!key) return null;
  return (await browser.storage.local.get(key))[key] || null;
}

function applyConfiguredContext(capture) {
  if (detected?.configured && detected?.generic_capture) {
    return {
      ...capture,
      source_key: detected.source_key,
      source_name: detected.source_name,
      configured_source: true
    };
  }
  return capture;
}

async function refresh() {
  const response = await api("/api/active-source-import");

  if (!response.ok) {
    active = null;
    status("No active source import job.", false);
    return;
  }

  active = await response.json();
  $("jobNumber").textContent = active.job_number;
  $("customer").textContent = `Customer: ${active.customer}`;
  $("machine").textContent =
    `Machine: ${active.manufacturer || ""} ${active.machine || ""}`.trim();
  $("pin").textContent = `VIN / PIN: ${active.pin_serial || ""}`;
  $("need").textContent = `Need: ${active.requested_need || "General machine research"}`;

  await detectSite();

  if (
    detected && !detected.configured &&
    active.source_name !== "One-time Website" &&
    sameOrigin(detected.source_url, active.source_url_snapshot)
  ) {
    detected = {
      ...detected,
      configured: true,
      source_key: active.source_key,
      source_name: active.source_name,
      connector_profile_id: active.connector_profile_id,
      generic_capture: true
    };
    $("detectedSite").textContent = `Configured source: ${active.source_name}`;
  }

  if (detected?.source_name) {
    status(`Active job loaded. ${detected.source_name} detected.`);
  }
}

function render() {
  $("count").textContent =
    `${cart.items.length} item(s) · ${cart.capture_mode} · ${cart.source_name}`;

  const identifier = item => item.manufacturer_part_number || item.supplier_part_number || item.sku || item.asin || item.listing_id || "";
  const identifierField = item => item.manufacturer_part_number ? "manufacturer_part_number" : item.supplier_part_number ? "supplier_part_number" : item.sku ? "sku" : item.asin ? "asin" : item.listing_id ? "listing_id" : "supplier_part_number";
  $("items").innerHTML = cart.items.map((item, index) => `
    <div class="row">
      <div class="edit-grid">
        <label>Supplier<input data-item="${index}" data-field="supplier_name" value="${escapeHtml(item.supplier_name || cart.suggested_supplier || cart.source_domain)}"></label>
        <label>Part number / SKU / ASIN / listing ID<input data-item="${index}" data-field="${identifierField(item)}" value="${escapeHtml(identifier(item))}"></label>
        <label>Description<input data-item="${index}" data-field="description" value="${escapeHtml(item.description)}" required></label>
        <label>Supplier cost<input data-item="${index}" data-field="supplier_cost" type="number" step="0.01" value="${item.supplier_cost ?? ""}"></label>
        <label>Quantity<input data-item="${index}" data-field="quantity" type="number" min="1" step="1" value="${item.quantity || 1}"></label>
        <label>Weight<input data-item="${index}" data-field="weight" type="number" step="0.001" value="${item.weight ?? ""}"></label>
        <label>Weight unit<input data-item="${index}" data-field="weight_unit" value="${escapeHtml(item.weight_unit)}"></label>
        <label>Length<input data-item="${index}" data-field="length" type="number" step="0.001" value="${item.length ?? ""}"></label>
        <label>Width<input data-item="${index}" data-field="width" type="number" step="0.001" value="${item.width ?? ""}"></label>
        <label>Height<input data-item="${index}" data-field="height" type="number" step="0.001" value="${item.height ?? ""}"></label>
        <label>Dimension unit<input data-item="${index}" data-field="dimension_unit" value="${escapeHtml(item.dimension_unit)}"></label>
        <label>Availability<input data-item="${index}" data-field="availability" value="${escapeHtml(item.availability)}"></label>
      </div>
      <span class="price">${money(item.supplier_cost)} · Qty ${item.quantity}</span>
      <span class="meta">${escapeHtml(item.availability)}</span>
      <span class="meta">${escapeHtml(item.fitment)}</span>
      <span class="meta">${escapeHtml(item.warehouse)} ${escapeHtml(item.shipping_method)}</span>
      <span class="meta">${item.weight == null ? "" : `${item.weight} ${escapeHtml(item.weight_unit)}`} ${item.length == null ? "" : `· ${item.length} × ${item.width} × ${item.height} ${escapeHtml(item.dimension_unit)}`}</span>
      ${captureDetails(item, index)}
    </div>
  `).join("");

  $("subtotal").textContent = money(cart.subtotal);
  $("shipping").textContent = money(cart.shipping);
  $("supplierTotal").textContent = money(cart.supplier_total);
  $("items").onchange = () => { applyEdits(); render(); };
}

function applyEdits() {
  for (const input of document.querySelectorAll("[data-item][data-field]")) {
    const item = cart.items[Number(input.dataset.item)];
    if (!item) continue;
    item[input.dataset.field] = ["supplier_cost","weight","length","width","height"].includes(input.dataset.field)
      ? (input.value === "" ? null : Number(input.value))
      : input.dataset.field === "quantity" ? Math.max(1, Number(input.value) || 1)
      : input.value.trim();
  }
}

async function readCapture(mode) {
  if (!active) {
    status("No active source import job.", false);
    return;
  }

  const result = await send({ type: mode === "PAGE" ? "PLG_READ_CURRENT_PAGE" : "PLG_READ_CURRENT_CART" });

  if (!result?.ok) {
    cart = null;
    status(result?.status || "Could not read cart.", false);
    return;
  }

  if (!result.items?.length) {
    cart = null;
    status(`No product items were found on the current ${mode.toLowerCase()}.`, false);
    return;
  }
  const normalized = applyConfiguredContext(result);
  if (mode === "PAGE") {
    cart = normalized;
    await cachePageCapture(cart);
  } else {
    const pageCapture = await cachedPageCapture();
    cart = pageCapture
      ? window.PLGConnectorSDK?.mergeCaptures?.(pageCapture, normalized) || normalized
      : normalized;
  }
  extractedCart = JSON.parse(JSON.stringify(cart));
  captureContext = contextIdentity(active);
  render();
  status(`${cart.source_name} ${mode.toLowerCase()} read successfully.`);
}

async function importCart() {
  try {
    if (!active) {
      status("No active source import job.", false);
      return;
    }

    if (!cart) {
      status("Read the current cart first.", false);
      return;
    }

    applyEdits();
    if (cart.items.some(item => !item.manufacturer_part_number && !item.supplier_part_number && !item.sku && !item.asin && !item.listing_id && !item.item_id)) {
      status("Add an identifier before sending this part to PPS.", false);
      return;
    }

    const contextResponse = await api("/api/active-source-import");
    const currentContext = await responseJson(
      contextResponse,
      "Could not refresh the current PPS work context."
    );
    if (!contextResponse.ok) {
      status(currentContext.detail || currentContext.message || "Could not refresh the current PPS work context.", false);
      return;
    }
    if (!sameWorkContext(captureContext || active, currentContext)) {
      status("The active Job, machine, Need, or research session changed. Review the current PPS context and read the page again before sending.", false);
      return;
    }
    active = {
      ...active,
      expected_revision_id: currentContext.expected_revision_id,
      expected_version: currentContext.expected_version
    };

    if (cart.source_key === "one_time_website") {
      const oneTimeResponse = await api("/api/research/extension/one-time-context", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: active.job_id,
        job_asset_id: active.job_asset_id,
        requested_need_id: active.requested_need_id,
        page_url: cart.source_url,
        expected_revision_id: active.expected_revision_id,
        expected_version: active.expected_version
      })
    });
      const context = await responseJson(
        oneTimeResponse,
        "Could not establish one-time website context."
      );
      if (!oneTimeResponse.ok) {
      status(context.detail || "Could not establish one-time website context.", false);
      return;
      }
      active = { ...active, ...context };
    }

    const response = await api("/api/basket/import-source-cart", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_id: active.job_id,
      job_asset_id: active.job_asset_id,
      requested_need_id: active.requested_need_id,
      verification_session_id: active.verification_session_id,
      source_key: cart.source_key,
      source_name: cart.source_name,
      trust_level: cart.trust_level,
      source_url: cart.source_url,
      capture_mode: cart.items.some(item => captureMode(item) === "MERGED") ? "MERGED" : cart.capture_mode,
      expected_revision_id: active.expected_revision_id,
      expected_version: active.expected_version,
      currency: cart.currency,
      items: cart.items,
      charges: cart.charges
    })
  });

    const data = await responseJson(response, "PPS returned an unreadable import response.");

    if (!response.ok) {
      status(data.detail || "Cart import failed.", false);
      return;
    }
    if (data.ok !== true || !Number.isFinite(Number(data.imported_count))) {
      status("PPS returned an unexpected import response.", false);
      return;
    }

    status(`Added ${data.imported_count} part(s) for review in PPS.`);

    setTimeout(() => {
      browser.tabs.create({
        url:
          `http://127.0.0.1:8000/jobs/${data.job_id}/basket` +
          `?refresh=${Date.now()}`
      });
    }, 500);
  } catch (error) {
    status(error?.message || "Could not send parts to PPS.", false);
  }
}

$("refresh").onclick = refresh;
$("readPage").onclick = () => readCapture("PAGE");
$("readCart").onclick = () => readCapture("CART");
$("import").onclick = importCart;
$("saveFirefoxInboxToken").onclick = () => saveFirefoxInboxToken().catch(error => {
  $("firefoxInboxTokenStatus").textContent = error.message;
});
$("refreshFirefoxInboxQueue").onclick = () => renderFirefoxInboxQueue().catch(error => {
  $("firefoxInboxQueue").textContent = error.message;
});

initializeFirefoxInboxSettings().catch(() => {});
refresh().catch(error => status(error.message, false));
