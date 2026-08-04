const BASES = [
  "http://127.0.0.1:8000",
  "http://localhost:8000"
];

let active = null;
let cart = null;
let detected = null;

const $ = id => document.getElementById(id);

function status(message, ok = true) {
  $("status").textContent = message;
  $("status").className = ok ? "ok" : "error";
}

function money(value) {
  return value == null ? "—" : `$${Number(value).toFixed(2)}`;
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
    $("detectedSite").textContent = "Unsupported site";
    status(result?.status || "Could not detect supplier site.", false);
    return;
  }

  detected = result;
  $("detectedSite").textContent = `Detected: ${result.source_name}`;
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

  await detectSite();

  if (detected?.source_name) {
    status(`Active job loaded. ${detected.source_name} detected.`);
  }
}

function render() {
  $("count").textContent =
    `${cart.items.length} ${cart.source_name} cart item(s)`;

  $("items").innerHTML = cart.items.map(item => `
    <div class="row">
      <strong>${item.description}</strong>
      <span>${item.brand || ""} ${item.supplier_part_number || ""}</span>
      <span class="price">${money(item.supplier_cost)} · Qty ${item.quantity}</span>
      <span class="meta">${item.availability || ""}</span>
      <span class="meta">${item.fitment || ""}</span>
      <span class="meta">${item.warehouse || ""} ${item.shipping_method || ""}</span>
    </div>
  `).join("");

  $("subtotal").textContent = money(cart.subtotal);
  $("shipping").textContent = money(cart.shipping);
  $("supplierTotal").textContent = money(cart.supplier_total);
}

async function readCart() {
  if (!active) {
    status("No active source import job.", false);
    return;
  }

  const result = await send({ type: "PLG_READ_CURRENT_CART" });

  if (!result?.ok) {
    cart = null;
    status(result?.status || "Could not read cart.", false);
    return;
  }

  cart = result;
  render();
  status(`${cart.source_name} cart read successfully.`);
}

async function importCart() {
  if (!active) {
    status("No active source import job.", false);
    return;
  }

  if (!cart) {
    status("Read the current cart first.", false);
    return;
  }

  const response = await api("/api/basket/import-source-cart", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      job_id: active.job_id,
      source_key: cart.source_key,
      source_name: cart.source_name,
      trust_level: cart.trust_level,
      source_url: cart.source_url,
      currency: cart.currency,
      items: cart.items,
      charges: cart.charges
    })
  });

  const data = await response.json();

  if (!response.ok) {
    status(data.detail || "Cart import failed.", false);
    return;
  }

  status(`Imported ${data.imported_count} item(s).`);

  setTimeout(() => {
    browser.tabs.create({
      url:
        `http://127.0.0.1:8000/jobs/${data.job_id}/basket` +
        `?refresh=${Date.now()}`
    });
  }, 500);
}

$("refresh").onclick = refresh;
$("read").onclick = readCart;
$("import").onclick = importCart;

refresh().catch(error => status(error.message, false));