(() => {
  if (window.PLGConnectorSDK) return;

  function cleanText(value) {
    if (value == null) return "";
    if (value instanceof Element) value = value.textContent;
    return String(value).replace(/\s+/g, " ").trim();
  }

  function parseMoney(value) {
    const text = cleanText(value)
      .replace(/,/g, "")
      .replace(/\u00a0/g, " ");

    const match = text.match(/-?\d+(?:\.\d+)?/);
    if (!match) return null;

    const parsed = Number.parseFloat(match[0]);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function parseInteger(value, fallback = 1) {
    const text = cleanText(value);
    const match = text.match(/-?\d+/);
    if (!match) return fallback;

    const parsed = Number.parseInt(match[0], 10);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function firstText(root, selectors) {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      const text = cleanText(node);
      if (text) return text;
    }
    return "";
  }

  function firstMoney(root, selectors) {
    for (const selector of selectors) {
      for (const node of root.querySelectorAll(selector)) {
        const parsed = parseMoney(node);
        if (parsed != null) return parsed;
      }
    }
    return null;
  }

  function normalizeAvailability(value) {
    const text = cleanText(value);
    if (!text) return "";
    if (/^(yes|available|in stock)$/i.test(text)) return "In Stock";
    if (/^(no|unavailable|out of stock)$/i.test(text)) return "Out of Stock";
    if (/back.?order/i.test(text)) return "Back Order";
    return text;
  }

  function allRoots() {
    const roots = [document];
    const visit = root => {
      for (const element of root.querySelectorAll("*")) {
        if (element.shadowRoot) {
          roots.push(element.shadowRoot);
          visit(element.shadowRoot);
        }
      }
    };
    visit(document);
    return roots;
  }

  function sumItems(items) {
    return items.reduce(
      (sum, item) =>
        sum +
        (Number(item.supplier_cost) || 0) *
        Math.max(1, Number(item.quantity) || 1),
      0
    );
  }

  function normalizeCart(raw) {
    const items = (raw.items || []).map(item => ({
      description: cleanText(item.description) || "Imported Part",
      brand: cleanText(item.brand),
      supplier_part_number: cleanText(item.supplier_part_number),
      manufacturer_part_number: cleanText(
        item.manufacturer_part_number || item.supplier_part_number
      ),
      quantity: Math.max(1, parseInteger(item.quantity, 1)),
      supplier_cost:
        item.supplier_cost == null ? null : Number(item.supplier_cost),
      availability: normalizeAvailability(item.availability),
      lead_time: cleanText(item.lead_time),
      fitment: cleanText(item.fitment),
      warehouse: cleanText(item.warehouse),
      delivery: cleanText(item.delivery),
      shipping_method: cleanText(item.shipping_method),
      source_url: cleanText(item.source_url || location.href)
    }));

    const computedSubtotal = sumItems(items);
    const subtotal =
      raw.subtotal == null ? computedSubtotal : Number(raw.subtotal);
    const shipping =
      raw.shipping == null ? null : Number(raw.shipping);

    return {
      source_key: cleanText(raw.source_key),
      source_name: cleanText(raw.source_name),
      trust_level: cleanText(raw.trust_level),
      source_url: cleanText(raw.source_url || location.href),
      currency: cleanText(raw.currency || "USD"),
      subtotal,
      shipping,
      supplier_total:
        raw.supplier_total == null
          ? subtotal + (shipping || 0)
          : Number(raw.supplier_total),
      items,
      charges:
        shipping == null
          ? []
          : [{
              charge_type: "SHIPPING",
              amount: shipping,
              currency: cleanText(raw.currency || "USD")
            }]
    };
  }

  window.PLGConnectorSDK = {
    cleanText,
    parseMoney,
    parseInteger,
    firstText,
    firstMoney,
    normalizeAvailability,
    allRoots,
    sumItems,
    normalizeCart
  };
})();