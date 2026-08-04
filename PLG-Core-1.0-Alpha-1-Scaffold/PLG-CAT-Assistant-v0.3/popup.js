const API_BASE = "http://127.0.0.1:8000";

let activeVerification = null;

const statusEl = document.getElementById("status");
const jobNumberEl = document.getElementById("jobNumber");
const customerEl = document.getElementById("customer");
const machineEl = document.getElementById("machine");
const pinEl = document.getElementById("pin");
const requestedPartEl = document.getElementById("requestedPart");

function setStatus(message, ok = true) {
  statusEl.textContent = message;
  statusEl.className = ok ? "ok" : "error";
}

async function getActiveCatTab() {
  const [tab] = await browser.tabs.query({
    active: true,
    currentWindow: true
  });

  if (!tab?.id || !tab.url?.startsWith("https://parts.cat.com/")) {
    throw new Error("Open CAT Parts in the active tab first.");
  }

  return tab;
}

async function sendToCat(message) {
  const tab = await getActiveCatTab();

  try {
    return await browser.tabs.sendMessage(tab.id, message);
  } catch (error) {
    await browser.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["content.js"]
    });

    return browser.tabs.sendMessage(tab.id, message);
  }
}

async function refreshActiveVerification() {
  setStatus("Loading active PLG verification…");

  const response = await fetch(`${API_BASE}/api/active-verification`);

  if (!response.ok) {
    activeVerification = null;
    jobNumberEl.textContent = "No active verification";
    customerEl.textContent = "";
    machineEl.textContent = "";
    pinEl.textContent = "";
    requestedPartEl.textContent = "";
    setStatus("Open a PLG job and click Verify in CAT.", false);
    return;
  }

  activeVerification = await response.json();

  jobNumberEl.textContent = activeVerification.job_number;
  customerEl.textContent = `Customer: ${activeVerification.customer}`;
  machineEl.textContent =
    `Machine: ${activeVerification.manufacturer} ${activeVerification.machine}`;
  pinEl.textContent = `PIN / Serial: ${activeVerification.pin_serial}`;
  requestedPartEl.textContent =
    `Requested part: ${activeVerification.requested_description}`;

  setStatus("Active PLG verification loaded.");
}

document.getElementById("refreshJob").addEventListener(
  "click",
  refreshActiveVerification
);

document.getElementById("loadMachine").addEventListener("click", async () => {
  if (!activeVerification) {
    setStatus("No active PLG verification.", false);
    return;
  }

  try {
    const response = await sendToCat({
      type: "PLG_LOAD_PIN",
      pin: activeVerification.pin_serial
    });

    setStatus(response?.status || "PIN submitted.", Boolean(response?.ok));
  } catch (error) {
    setStatus(error.message, false);
  }
});

document.getElementById("capturePart").addEventListener("click", async () => {
  if (!activeVerification) {
    setStatus("No active PLG verification.", false);
    return;
  }

  const oemPartNumber =
    document.getElementById("oemPartNumber").value.trim();
  const oemDescription =
    document.getElementById("oemDescription").value.trim();
  const diagramName =
    document.getElementById("diagramName").value.trim();
  const calloutNumber =
    document.getElementById("calloutNumber").value.trim();
  const verificationNotes =
    document.getElementById("verificationNotes").value.trim();

  if (!oemPartNumber) {
    setStatus("Enter the OEM part number before capturing.", false);
    return;
  }

  const [tab] = await browser.tabs.query({
    active: true,
    currentWindow: true
  });

  const response = await fetch(`${API_BASE}/api/capture-part`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      part_id: activeVerification.part_id,
      oem_part_number: oemPartNumber,
      oem_description: oemDescription,
      diagram_name: diagramName,
      callout_number: calloutNumber,
      source_url: tab?.url || "",
      verification_notes: verificationNotes
    })
  });

  const data = await response.json();

  if (!response.ok) {
    setStatus(data.detail || "Capture failed.", false);
    return;
  }

  setStatus("OEM part captured and saved to PLG Core.");
  activeVerification = null;
});

refreshActiveVerification().catch(error => {
  setStatus(error.message, false);
});
