(() => {
  if (window.PLGConnectors) return;

  const SDK = window.PLGConnectorSDK;

  const catSis = {
    key: "cat_sis",
    name: "CAT SIS",
    host: "sis2.cat.com",

    detect() {
      return location.hostname === this.host;
    },

    readPage() {
      return SDK.normalizeCapture({
        ...SDK.readProductPage({ source_key: this.key, source_name: this.name }),
        source_key: this.key, source_name: this.name, configured_source: true,
        capture_mode: "PAGE"
      });
    },

    readCart() {
      const items = [];
      const seen = new Set();

      for (const root of SDK.allRoots()) {
        for (const row of root.querySelectorAll("tr")) {
          const partLink = row.querySelector(
            'a[data-track-attr-nltext^="Part Number | Same Tab |"],' +
            'a[href*="#/refine?"][data-track-attr-context*="Part Table"]'
          );

          if (!partLink) continue;

          const partNumber = SDK.cleanText(partLink);
          if (!partNumber || seen.has(partNumber)) continue;

          const partCell = partLink.closest("td");
          const description =
            SDK.cleanText(partCell?.nextElementSibling) ||
            "CAT SIS Part";

          const quantityInput = row.querySelector(
            'input.quantity-input, input[type="number"]'
          );

          const price = SDK.firstMoney(row, [
            ".priceText",
            '[class*="priceText"]',
            'div[data-v-167ce84f]'
          ]);

          const availability = SDK.firstText(row, [
            "._ECommerceAvailability .findme-label",
            ".findme-label"
          ]);

          items.push({
            description,
            brand: "CAT",
            supplier_part_number: partNumber,
            manufacturer_part_number: partNumber,
            quantity: SDK.parseInteger(quantityInput?.value, 1),
            supplier_cost: price,
            availability,
            source_url: partLink.href || location.href
          });

          seen.add(partNumber);
        }
      }

      if (!items.length) {
        throw new Error(
          "No CAT SIS cart items were found. Make sure the cart table is visible."
        );
      }

      return SDK.normalizeCart({
        source_key: this.key,
        source_name: this.name,
        trust_level: "OEM_VERIFIED",
        source_url: location.href,
        currency: "USD",
        configured_source: true,
        items
      });
    }
  };

  const worldpac = {
    key: "worldpac",
    name: "Worldpac",
    host: "speeddial.worldpac.com",

    detect() {
      return location.hostname === this.host;
    },

    readPage() {
      return SDK.normalizeCapture({
        ...SDK.readProductPage({ source_key: this.key, source_name: this.name }),
        source_key: this.key, source_name: this.name, configured_source: true,
        capture_mode: "PAGE"
      });
    },

    summaryValue(labelName) {
      for (const label of document.querySelectorAll(
        ".order-total-summary .order-summary-item"
      )) {
        if (
          !new RegExp(`^${labelName}`, "i").test(
            SDK.cleanText(label)
          )
        ) {
          continue;
        }

        let sibling = label.nextElementSibling;

        while (
          sibling &&
          !sibling.classList.contains("order-summary-value")
        ) {
          sibling = sibling.nextElementSibling;
        }

        return SDK.parseMoney(sibling);
      }

      return null;
    },

    readCart() {
      const items = [];

      for (const row of document.querySelectorAll(
        ".order-line.cart-checkout"
      )) {
        let fitment = "";
        let warehouse = "";
        let delivery = "";

        for (const pair of row.querySelectorAll(".name-value-row")) {
          const name = SDK.cleanText(
            pair.querySelector(".name-column")
          ).replace(/:$/, "");

          const value = SDK.cleanText(
            pair.querySelector(".value-column")
          );

          if (name === "Warehouse") warehouse = value;

          if (/^(ETA|Ready|Submit by)$/i.test(name) && value) {
            delivery +=
              `${delivery ? " | " : ""}${name}: ${value}`;
          }

          if (!name && /^For:/i.test(value)) {
            fitment = value.replace(/^For:\s*/i, "");
          }
        }

        const partNumber = SDK.cleanText(
          row.querySelector(".product-id")
        );

        if (!partNumber) continue;

        items.push({
          description:
            SDK.firstText(row, [
              ".product-info .bold-text .product-detail-link"
            ]) || "Imported Part",
          brand:
            row
              .querySelector("img.sd-brand-image")
              ?.getAttribute("alt")
              ?.trim() || "",
          supplier_part_number: partNumber,
          manufacturer_part_number: partNumber,
          quantity: SDK.parseInteger(
            row.querySelector(
              'input[aria-label="Qty"],' +
              "input.spinner-up-down-qty-input"
            )?.value,
            1
          ),
          supplier_cost: SDK.firstMoney(row, [
            ".order-line-pricing .font-16-bolder"
          ]),
          availability: "In Stock",
          lead_time: delivery,
          fitment,
          warehouse,
          delivery,
          shipping_method:
            SDK.firstText(row, [
              ".selected-shipping-single",
              '[role="combobox"]'
            ]),
          source_url: location.href
        });
      }

      if (!items.length) {
        throw new Error("No Worldpac cart items were found.");
      }

      const subtotal =
        this.summaryValue("Subtotal") ?? SDK.sumItems(items);
      const shipping = this.summaryValue("Shipping");

      return SDK.normalizeCart({
        source_key: this.key,
        source_name: this.name,
        trust_level: "SUPPLIER_VERIFIED",
        source_url: location.href,
        currency: "USD",
        configured_source: true,
        subtotal,
        shipping,
        supplier_total: subtotal + (shipping || 0),
        items
      });
    }
  };

  const connectors = [catSis, worldpac];

  function detectConnector() {
    return connectors.find(connector => connector.detect()) || null;
  }

  window.PLGConnectors = {
    connectors,
    detectConnector
  };
})();
