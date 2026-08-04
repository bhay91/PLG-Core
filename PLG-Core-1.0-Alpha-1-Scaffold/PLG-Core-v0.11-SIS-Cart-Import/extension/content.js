(() => {
  if (window.__PLG_VERIFY_V011__) return;
  window.__PLG_VERIFY_V011__ = true;

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden";
  }

  function scan(root, cb) {
    if (!root || !root.querySelectorAll) return null;
    const found = cb(root);
    if (found) return found;
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) {
        const nested = scan(el.shadowRoot, cb);
        if (nested) return nested;
      }
    }
    return null;
  }

  function allRoots() {
    const roots = [document];
    scan(document, root => {
      for (const el of root.querySelectorAll("*")) {
        if (el.shadowRoot) roots.push(el.shadowRoot);
      }
      return null;
    });
    return roots;
  }

  function parseMoney(text) {
    const match = String(text || "").match(/([0-9][0-9,]*\.\d{2})\s*\(USD\)/i);
    return match ? Number(match[1].replace(/,/g, "")) : null;
  }

  function parseSisCart() {
    const items = [];
    const seen = new Set();

    for (const root of allRoots()) {
      for (const row of root.querySelectorAll("tr")) {
        const partLink = row.querySelector(
          'a[data-track-attr-nltext^="Part Number | Same Tab |"]'
        );
        if (!partLink) continue;

        const partNumber = (partLink.textContent || "").trim();
        if (!partNumber || seen.has(partNumber)) continue;

        const partCell = partLink.closest("td");
        const descriptionCell = partCell?.nextElementSibling;
        const quantityInput = row.querySelector("input.quantity-input");
        const priceElement = row.querySelector(".priceText");
        const availabilityElement = row.querySelector(
          "._ECommerceAvailability .findme-label"
        );

        const description = (descriptionCell?.textContent || "").trim();
        const quantity = Math.max(1, Number(quantityInput?.value || 1));
        const price = parseMoney(priceElement?.textContent || "");
        const availabilityRaw = (availabilityElement?.textContent || "").trim();
        const availability = /^yes$/i.test(availabilityRaw)
          ? "In Stock"
          : /^no$/i.test(availabilityRaw)
            ? "Out of Stock"
            : availabilityRaw;

        items.push({
          oem_part_number: partNumber,
          oem_description: description || "CAT SIS Part",
          quantity,
          oem_price: price,
          availability,
          source_url: partLink.href || location.href
        });
        seen.add(partNumber);
      }
    }

    if (!items.length) {
      throw new Error("No SIS cart line items were found. Make sure the cart table is visible.");
    }
    return items;
  }

  function candidateContainers() {
    const candidates = [];
    scan(document, root => {
      for (const el of root.querySelectorAll("div,section,article,aside")) {
        if (!visible(el)) continue;
        const text = (el.innerText || "").trim();
        if (
          text.includes("Group Part") &&
          /In Store:\s*(Yes|No)/i.test(text) &&
          /\d+\.\d{2}\s*\(USD\)\s*ea\.\s*Per Unit/i.test(text)
        ) {
          candidates.push({el, text});
        }
      }
      return null;
    });
    candidates.sort((a, b) => a.text.length - b.text.length);
    return candidates;
  }

  function parseCurrentSisPart() {
    const candidates = candidateContainers();
    if (!candidates.length) {
      throw new Error("Could not find the selected SIS part. Open the part detail panel first.");
    }
    const text = candidates[0].text.replace(/\u00a0/g, " ");
    const pnMatch = text.match(/\b(\d{3}-\d{4})\b/);
    const priceMatch = text.match(/(\d+\.\d{2})\s*\(USD\)\s*ea\.\s*Per Unit/i);
    const availabilityMatch = text.match(/In Store:\s*(Yes|No)/i);
    if (!pnMatch) throw new Error("SIS part number was not detected.");

    const partNumber = pnMatch[1];
    const lines = text.split("\n").map(x => x.trim()).filter(Boolean);
    const pnIndex = lines.findIndex(line => line.includes(partNumber));
    let description = "";
    if (pnIndex >= 0) {
      const sameLine = lines[pnIndex].replace(partNumber, "").trim();
      if (sameLine && !sameLine.match(/^\d/)) description = sameLine;
      else if (lines[pnIndex + 1]) description = lines[pnIndex + 1];
    }
    description = description.replace(/^[-:]\s*/, "").replace(/\s+/g, " ").trim();

    return {
      oem_part_number: partNumber,
      oem_description: description || "CAT SIS Part",
      oem_dealer_name: "CAT SIS",
      oem_dealer_price: priceMatch ? Number(priceMatch[1]) : null,
      oem_dealer_availability: availabilityMatch
        ? `In Store: ${availabilityMatch[1]}`
        : "",
      oem_dealer_lead_time: "",
      verification_source: "CAT SIS",
      source_url: location.href,
      product_url: location.href,
      diagram_url: location.href
    };
  }

  browser.runtime.onMessage.addListener(message => {
    try {
      if (message?.type === "PLG_READ_SIS_CART") {
        return Promise.resolve({ok: true, items: parseSisCart()});
      }
      if (message?.type === "PLG_READ_SIS_PART") {
        return Promise.resolve({ok: true, ...parseCurrentSisPart()});
      }
    } catch (error) {
      return Promise.resolve({ok: false, status: error.message});
    }
    return undefined;
  });
})();
