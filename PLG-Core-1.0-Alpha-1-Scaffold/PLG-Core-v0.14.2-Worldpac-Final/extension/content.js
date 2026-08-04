(() => {
if (window.__PLG_WORLDPAC_V0142__) return;
window.__PLG_WORLDPAC_V0142__ = true;

const clean = el => (el?.textContent || "").replace(/\s+/g, " ").trim();

function money(value) {
  const match = String(value || "").match(/\$?\s*([0-9][0-9,]*\.\d{2})/);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

function summaryValue(labelName) {
  const labels = document.querySelectorAll(
    ".order-total-summary .order-summary-item"
  );

  for (const label of labels) {
    if (!new RegExp(`^${labelName}`, "i").test(clean(label))) continue;

    let sibling = label.nextElementSibling;
    while (
      sibling &&
      !sibling.classList.contains("order-summary-value")
    ) {
      sibling = sibling.nextElementSibling;
    }

    return money(clean(sibling));
  }

  return null;
}

function parseWorldpacCart() {
  const items = [];

  for (const row of document.querySelectorAll(".order-line.cart-checkout")) {
    let fitment = "";
    let warehouse = "";
    let delivery = "";

    for (const pair of row.querySelectorAll(".name-value-row")) {
      const name = clean(pair.querySelector(".name-column")).replace(/:$/, "");
      const value = clean(pair.querySelector(".value-column"));

      if (name === "Warehouse") warehouse = value;

      if (/^(ETA|Ready|Submit by)$/i.test(name) && value) {
        delivery += `${delivery ? " | " : ""}${name}: ${value}`;
      }

      if (!name && /^For:/i.test(value)) {
        fitment = value.replace(/^For:\s*/i, "");
      }
    }

    const supplierPartNumber = clean(row.querySelector(".product-id"));
    if (!supplierPartNumber) continue;

    items.push({
      description:
        clean(
          row.querySelector(
            ".product-info .bold-text .product-detail-link"
          )
        ) || "Imported Part",
      brand:
        row.querySelector("img.sd-brand-image")?.getAttribute("alt")?.trim() ||
        "",
      supplier_part_number: supplierPartNumber,
      manufacturer_part_number: supplierPartNumber,
      quantity: Math.max(
        1,
        Number(
          row.querySelector(
            'input[aria-label="Qty"], input.spinner-up-down-qty-input'
          )?.value || 1
        )
      ),
      supplier_cost: money(
        clean(row.querySelector(".order-line-pricing .font-16-bolder"))
      ),
      availability: "In Stock",
      lead_time: delivery,
      fitment,
      warehouse,
      delivery,
      shipping_method:
        clean(row.querySelector(".selected-shipping-single")) ||
        clean(row.querySelector('[role="combobox"]')),
      source_url: location.href
    });
  }

  if (!items.length) {
    throw new Error("No Worldpac cart items were found.");
  }

  const subtotal = summaryValue("Subtotal");
  const shipping = summaryValue("Shipping");
  const supplierTotal =
    subtotal != null
      ? subtotal + (shipping || 0)
      : items.reduce(
          (sum, item) =>
            sum + (Number(item.supplier_cost) || 0) * item.quantity,
          0
        ) + (shipping || 0);

  return {
    source_key: "worldpac",
    source_name: "Worldpac",
    trust_level: "SUPPLIER_VERIFIED",
    source_url: location.href,
    currency: "USD",
    subtotal,
    shipping,
    supplier_total: supplierTotal,
    items,
    charges:
      shipping == null
        ? []
        : [
            {
              charge_type: "SHIPPING",
              amount: shipping,
              currency: "USD"
            }
          ]
  };
}

browser.runtime.onMessage.addListener(message => {
  if (message?.type !== "PLG_READ_CURRENT_CART") return undefined;

  try {
    return Promise.resolve({
      ok: true,
      ...parseWorldpacCart()
    });
  } catch (error) {
    return Promise.resolve({
      ok: false,
      status: error.message
    });
  }
});
})();