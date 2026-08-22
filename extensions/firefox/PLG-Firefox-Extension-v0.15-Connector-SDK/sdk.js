(() => {
  if (window.PLGConnectorSDK) return;

  const text = value => {
    if (value == null) return "";
    if (typeof Element !== "undefined" && value instanceof Element) value = value.textContent;
    return String(value).replace(/\s+/g, " ").trim();
  };

  const first = (...values) => values.map(text).find(Boolean) || "";

  function parseMoney(value) {
    const match = text(value).replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
    const number = match ? Number.parseFloat(match[0]) : NaN;
    return Number.isFinite(number) ? number : null;
  }

  function parseInteger(value, fallback = 1) {
    const match = text(value).match(/-?\d+/);
    const number = match ? Number.parseInt(match[0], 10) : NaN;
    return Number.isFinite(number) ? number : fallback;
  }

  function firstText(root, selectors) {
    for (const selector of selectors || []) {
      const node = root.querySelector(selector);
      const value = node?.content || node?.value || node?.getAttribute?.("value") || node;
      if (text(value)) return text(value);
    }
    return "";
  }

  function firstMoney(root, selectors) {
    for (const selector of selectors || []) {
      for (const node of root.querySelectorAll(selector)) {
        const value = node?.content || node?.value || node;
        const parsed = parseMoney(value);
        if (parsed != null) return parsed;
      }
    }
    return null;
  }

  function normalizeAvailability(value) {
    const valueText = text(value).split("/").pop();
    if (!valueText) return "";
    if (/^(yes|available|instock|in stock)$/i.test(valueText)) return "In Stock";
    if (/^(no|unavailable|outofstock|out of stock)$/i.test(valueText)) return "Out of Stock";
    if (/back.?order/i.test(valueText)) return "Back Order";
    return valueText;
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
    return items.reduce((sum, item) => sum + (Number(item.supplier_cost) || 0) * Math.max(1, Number(item.quantity) || 1), 0);
  }

  function normalizeUnit(value, kind) {
    const unit = text(value).toLowerCase().replace(/\./g, "");
    if (kind === "weight") {
      if (/^(lb|lbs|pound|pounds)$/.test(unit)) return "lb";
      if (/^(kg|kgs|kilogram|kilograms)$/.test(unit)) return "kg";
      if (/^(oz|ounce|ounces)$/.test(unit)) return "oz";
      if (/^(g|gram|grams)$/.test(unit)) return "g";
    }
    if (/^(in|inch|inches)$/.test(unit)) return "in";
    if (/^(mm|millimeter|millimeters)$/.test(unit)) return "mm";
    if (/^(cm|centimeter|centimeters)$/.test(unit)) return "cm";
    return unit;
  }

  function parseWeight(value) {
    if (value && typeof value === "object") value = `${value.value || ""} ${value.unitCode || value.unitText || ""}`;
    const match = text(value).match(/(\d+(?:\.\d+)?)\s*(lb|lbs|pounds?|kg|kgs|kilograms?|oz|ounces?|g|grams?)\b/i);
    return match ? { weight: Number(match[1]), weight_unit: normalizeUnit(match[2], "weight") } : {};
  }

  function parseDimensions(value) {
    if (value && typeof value === "object") {
      const unit = normalizeUnit(value.unitCode || value.unitText || value.depth?.unitCode || value.width?.unitCode || value.height?.unitCode, "dimension");
      const numeric = part => Number(part?.value ?? part);
      const length = numeric(value.length ?? value.depth);
      const width = numeric(value.width);
      const height = numeric(value.height);
      if ([length, width, height].every(Number.isFinite)) return { length, width, height, dimension_unit: unit };
      value = value.value || "";
    }
    const match = text(value).match(/(\d+(?:\.\d+)?)\s*(?:x|×|by)\s*(\d+(?:\.\d+)?)\s*(?:x|×|by)\s*(\d+(?:\.\d+)?)\s*(in|inches?|mm|cm)\b/i);
    return match ? {
      length: Number(match[1]), width: Number(match[2]), height: Number(match[3]),
      dimension_unit: normalizeUnit(match[4], "dimension")
    } : {};
  }

  function normalizeItem(item, sourceUrl, captureMode = "PAGE", explicitCaptureUrl = "") {
    const sku = text(item.sku);
    const listing = text(item.listing_id || item.asin || item.item_id);
    const supplierPart = first(item.supplier_part_number, sku, listing);
    const weight = parseWeight(item.weight);
    const dimensions = parseDimensions(item.dimensions);
    return {
      description: first(item.description, "Identified Part"),
      brand: text(item.brand),
      supplier_name: text(item.supplier_name),
      manufacturer_part_number: text(item.manufacturer_part_number),
      supplier_part_number: supplierPart,
      sku,
      asin: text(item.asin),
      listing_id: listing,
      item_id: text(item.item_id),
      quantity: Math.max(1, parseInteger(item.quantity, 1)),
      supplier_cost: item.supplier_cost == null ? null : Number(item.supplier_cost),
      currency: text(item.currency),
      availability: normalizeAvailability(item.availability),
      weight: item.weight_value ?? (typeof item.weight === "number" ? item.weight : weight.weight) ?? null,
      weight_unit: text(item.weight_unit || weight.weight_unit),
      length: item.length ?? dimensions.length ?? null,
      width: item.width ?? dimensions.width ?? null,
      height: item.height ?? dimensions.height ?? null,
      dimension_unit: text(item.dimension_unit || dimensions.dimension_unit),
      shipping_cost: item.shipping_cost == null ? null : Number(item.shipping_cost),
      shipping_method: text(item.shipping_method),
      lead_time: text(item.lead_time),
      fitment: text(item.fitment),
      warehouse: text(item.warehouse),
      evidence: text(item.evidence),
      evidence_fields: item.evidence_fields && typeof item.evidence_fields === "object" ? { ...item.evidence_fields } : {},
      product_page_url: text(item.product_page_url || (captureMode === "PAGE" ? item.source_url || explicitCaptureUrl : "")),
      cart_page_url: text(item.cart_page_url || (captureMode === "CART" ? item.source_url || explicitCaptureUrl : "")),
      source_url: first(item.source_url, sourceUrl, location.href),
      weight_type: text(item.weight_type || "UNKNOWN").toUpperCase(),
      dimension_type: text(item.dimension_type || "UNKNOWN").toUpperCase()
    };
  }

  function normalizeCapture(raw) {
    const explicitCaptureUrl = text(raw.source_url);
    const sourceUrl = first(explicitCaptureUrl, location.href);
    const captureMode = text(raw.capture_mode || "PAGE").toUpperCase() === "CART" ? "CART" : "PAGE";
    const items = (raw.items || []).map(item => normalizeItem(item, sourceUrl, captureMode, explicitCaptureUrl));
    const subtotal = raw.subtotal == null ? sumItems(items) : Number(raw.subtotal);
    const shipping = raw.shipping == null ? null : Number(raw.shipping);
    const currency = first(raw.currency, items.map(item => item.currency).find(Boolean), "USD");
    return {
      source_key: text(raw.source_key), source_name: text(raw.source_name),
      source_url: sourceUrl, source_domain: text(raw.source_domain || location.hostname),
      domain: text(raw.source_domain || location.hostname),
      capture_mode: captureMode,
      configured_source: Boolean(raw.configured_source),
      trust_level: text(raw.trust_level || "NEEDS_REVIEW"), currency,
      subtotal, shipping,
      supplier_total: raw.supplier_total == null ? subtotal + (shipping || 0) : Number(raw.supplier_total),
      items,
      charges: shipping == null ? [] : [{ charge_type: "SHIPPING", amount: shipping, currency }]
    };
  }

  const normalizeCart = raw => normalizeCapture({ ...raw, capture_mode: raw.capture_mode || "CART" });

  function normalizedIdentity(value) {
    return text(value).toLowerCase().replace(/[^a-z0-9]+/g, "");
  }

  function normalizedUrl(value) {
    try {
      const url = new URL(value);
      url.hash = "";
      for (const key of [...url.searchParams.keys()]) {
        if (/^(utm_|ref|tracking|campaign)/i.test(key)) url.searchParams.delete(key);
      }
      return `${url.origin}${url.pathname.replace(/\/$/, "")}${url.search}`;
    } catch (_) { return ""; }
  }

  function itemsMatch(left, right) {
    for (const field of ["manufacturer_part_number", "supplier_part_number", "sku", "asin", "listing_id", "item_id"]) {
      const a = normalizedIdentity(left[field]);
      const b = normalizedIdentity(right[field]);
      if (a && b && a === b) return true;
    }
    const leftUrl = normalizedUrl(left.product_page_url);
    const rightUrl = normalizedUrl(right.product_page_url);
    return Boolean(leftUrl && rightUrl && leftUrl === rightUrl);
  }

  function combineEvidence(left, right) {
    return [...new Set([text(left), text(right)].filter(Boolean))].join(" | ");
  }

  function mergeItems(pageItem, cartItem) {
    const merged = { ...cartItem, ...pageItem };
    for (const key of Object.keys(cartItem)) {
      if (merged[key] == null || merged[key] === "") merged[key] = cartItem[key];
    }
    for (const key of ["quantity", "supplier_cost", "availability", "shipping_cost", "shipping_method", "lead_time", "warehouse"]) {
      if (cartItem[key] != null && cartItem[key] !== "") merged[key] = cartItem[key];
    }
    merged.product_page_url = first(pageItem.product_page_url, cartItem.product_page_url);
    merged.cart_page_url = first(cartItem.cart_page_url, pageItem.cart_page_url);
    merged.source_url = first(merged.product_page_url, merged.cart_page_url, pageItem.source_url, cartItem.source_url);
    merged.evidence = combineEvidence(pageItem.evidence, cartItem.evidence);
    merged.evidence_fields = { ...(cartItem.evidence_fields || {}), ...(pageItem.evidence_fields || {}) };
    return merged;
  }

  function mergeCaptures(pageCapture, cartCapture) {
    const page = normalizeCapture({ ...(pageCapture || {}), capture_mode: "PAGE" });
    const cart = normalizeCapture({ ...(cartCapture || {}), capture_mode: "CART" });
    const unusedPage = [...page.items];
    const items = cart.items.map(cartItem => {
      const index = unusedPage.findIndex(pageItem => itemsMatch(pageItem, cartItem));
      return index < 0 ? cartItem : mergeItems(unusedPage.splice(index, 1)[0], cartItem);
    });
    items.push(...unusedPage);
    return normalizeCapture({
      ...cart,
      source_key: first(cart.source_key, page.source_key),
      source_name: first(cart.source_name, page.source_name),
      source_url: first(cart.source_url, page.source_url),
      source_domain: first(cart.source_domain, page.source_domain),
      configured_source: cart.configured_source || page.configured_source,
      capture_mode: "CART",
      items
    });
  }

  async function normalizeWithProvider(rawCapture, provider = null) {
    const normalized = normalizeCapture(rawCapture);
    if (!provider || typeof provider.normalize !== "function") return normalized;
    const proposal = await provider.normalize(JSON.parse(JSON.stringify(normalized)));
    if (!proposal || !Array.isArray(proposal.items)) return normalized;
    const safe = JSON.parse(JSON.stringify(normalized));
    proposal.items.forEach((candidate, index) => {
      if (!safe.items[index] || !candidate?.evidence_fields) return;
      const evidenceHaystack = JSON.stringify({
        evidence: safe.items[index].evidence,
        evidence_fields: safe.items[index].evidence_fields
      }).toLowerCase();
      for (const [field, value] of Object.entries(candidate.values || {})) {
        const evidence = text(candidate.evidence_fields[field]);
        if (evidence && evidenceHaystack.includes(evidence.toLowerCase())) {
          safe.items[index][field] = value;
          safe.items[index].evidence_fields[field] = evidence;
        }
      }
    });
    return normalizeCapture(safe);
  }

  function structuredProducts() {
    const products = [];
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      try {
        const parsed = JSON.parse(node.textContent || "null");
        const queue = Array.isArray(parsed) ? [...parsed] : [parsed];
        while (queue.length) {
          const value = queue.shift();
          if (!value || typeof value !== "object") continue;
          const types = Array.isArray(value["@type"]) ? value["@type"] : [value["@type"]];
          if (types.some(type => String(type).toLowerCase() === "product")) products.push(value);
          for (const key of ["@graph", "itemListElement", "mainEntity"]) {
            if (Array.isArray(value[key])) queue.push(...value[key]);
            else if (value[key] && typeof value[key] === "object") queue.push(value[key]);
          }
        }
      } catch (_) { /* Ignore malformed third-party JSON-LD. */ }
    }
    return products;
  }

  function primaryStructuredProduct(scope) {
    const products = structuredProducts();
    if (products.length <= 1) return products[0] || {};
    const scopeTitle = normalizedIdentity(firstText(scope, ['h1','[itemprop="name"]']));
    const currentUrl = normalizedUrl(location.href);
    return products.map((product, index) => {
      const urls = [product.url, product.mainEntityOfPage, product.offers?.url]
        .flat().map(value => normalizedUrl(typeof value === "object" ? value?.["@id"] : value));
      let score = urls.some(url => url && url === currentUrl) ? 10 : 0;
      if (scopeTitle && normalizedIdentity(product.name) === scopeTitle) score += 8;
      const identity = normalizedIdentity(first(product.mpn, product.sku, product.productID));
      if (identity && normalizedIdentity(location.pathname).includes(identity)) score += 4;
      return { product, score, index };
    }).sort((left, right) => right.score - left.score || left.index - right.index)[0].product;
  }

  function attributeMap(root = document) {
    const values = new Map();
    const add = (label, value) => {
      const key = text(label).toLowerCase().replace(/\s+/g, " ").replace(/:$/, "");
      if (key && text(value) && !values.has(key)) values.set(key, text(value));
    };
    for (const row of root.querySelectorAll("tr")) {
      const cells = row.querySelectorAll("th,td");
      if (cells.length >= 2) add(cells[0], cells[1]);
    }
    for (const term of root.querySelectorAll("dt")) add(term, term.nextElementSibling);
    for (const node of root.querySelectorAll("li,div.product-attribute,p")) {
      const match = text(node).match(/^([^:]{2,40}):\s*(.+)$/);
      if (match) add(match[1], match[2]);
    }
    return values;
  }

  function attr(map, labels) {
    for (const label of labels) {
      const normalized = label.toLowerCase();
      if (map.has(normalized)) return map.get(normalized);
      for (const [key, value] of map) if (key.includes(normalized)) return value;
    }
    return "";
  }

  const meta = selectors => firstText(document, selectors);

  const EXCLUDED_COLLECTION_PATTERN = /(?:save(?:d)?[-_\s]*for[-_\s]*later|recommend|related|sponsor|advert|recent(?:ly)?[-_\s]*(?:view|browse)|also[-_\s]*(?:view|buy)|frequently[-_\s]*bought|wishlist|wish[-_\s]*list|suggest)/i;
  const CART_PATTERN = /(?:shopping[-_\s]*cart|cart[-_\s]*(?:items?|contents?|lines?|list)|basket[-_\s]*(?:items?|contents?|lines?|list)|bag[-_\s]*(?:items?|contents?|lines?|list)|active[-_\s]*cart)/i;

  function structuralText(node) {
    return [
      node.id, node.className, node.getAttribute?.("aria-label"),
      node.getAttribute?.("data-testid"), node.getAttribute?.("data-name"),
      node.getAttribute?.("role")
    ].map(text).join(" ");
  }

  function excludedCollection(node) {
    for (let current = node; current && current !== document.documentElement; current = current.parentElement) {
      if (EXCLUDED_COLLECTION_PATTERN.test(structuralText(current))) return true;
      const heading = current.matches?.("section,aside")
        ? firstText(current, [":scope > h1", ":scope > h2", ":scope > h3", ":scope > [role=heading]"])
        : "";
      if (EXCLUDED_COLLECTION_PATTERN.test(heading)) return true;
    }
    return false;
  }

  function cartScopeScore(node) {
    if (excludedCollection(node)) return -100;
    const identity = structuralText(node);
    const heading = firstText(node, [":scope > h1", ":scope > h2", ":scope > h3", ":scope > [role=heading]"]);
    let score = CART_PATTERN.test(identity) ? 8 : 0;
    if (/(?:shopping cart|your cart|cart items|your bag|shopping bag|basket)/i.test(heading)) score += 6;
    if (node.matches("main,[role=main],form")) score += 2;
    if (node.querySelector('input[name*="quantity" i],select[name*="quantity" i],[aria-label*="quantity" i]')) score += 2;
    if (node.querySelector('a[href*="/dp/"],a[href*="/itm/"],a[href*="product" i]')) score += 2;
    return score;
  }

  function cartScopes() {
    const candidates = [...document.querySelectorAll("main,form,section,article,[role=main],[role=region],[aria-label],[data-testid],[data-name],[id]")]
      .map(node => ({ node, score: cartScopeScore(node) }))
      .filter(candidate => candidate.score >= 6)
      .sort((left, right) => right.score - left.score || right.node.closest("body").querySelectorAll("*").length - left.node.closest("body").querySelectorAll("*").length);
    if (!candidates.length) {
      const pageSignalsCart = /cart|basket|shopping[-_\s]*bag/i.test(
        `${location.pathname} ${document.title} ${firstText(document,["h1"])}`
      );
      return pageSignalsCart ? [document] : [];
    }
    const best = candidates[0].score;
    return candidates.filter(candidate => candidate.score === best)
      .map(candidate => candidate.node)
      .filter((node, index, nodes) => !nodes.some((other, otherIndex) => otherIndex !== index && other.contains(node)));
  }

  function primaryProductScope() {
    const candidates = [...document.querySelectorAll('[itemtype*="schema.org/Product" i],[data-testid*="product" i],main,article,[id*="product" i],[class*="product-detail" i]')]
      .filter(node => !excludedCollection(node))
      .map(node => {
        let score = node.matches('[itemtype*="schema.org/Product" i]') ? 10 : 0;
        if (/(?:product[-_\s]*(?:detail|main|info)|item[-_\s]*detail)/i.test(structuralText(node))) score += 7;
        if (node.matches("main,article")) score += 3;
        if (node.querySelector('h1,[itemprop="name"]')) score += 2;
        if (node.querySelector('[itemprop="price"],.price,[data-price]')) score += 2;
        return { node, score };
      }).sort((left, right) => right.score - left.score);
    return candidates[0]?.score >= 4 ? candidates[0].node : document;
  }

  const SITE_PROFILES = [
    { key: "amazon", name: "Amazon", hosts: [/(^|\.)amazon\./], id: () => first(meta(['#ASIN','input[name="ASIN"]']), location.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i)?.[1]), idType: "asin",
      title: ['#productTitle'], price: ['.a-price .a-offscreen','#priceblock_ourprice','#priceblock_dealprice'] },
    { key: "ebay", name: "eBay", hosts: [/(^|\.)ebay\./], id: () => first(meta(['meta[itemprop="sku"]']), location.pathname.match(/\/(\d{9,15})(?:\?|$)/)?.[1]), idType: "listing_id",
      title: ['h1.x-item-title__mainTitle','h1[itemprop="name"]'], price: ['.x-price-primary [itemprop="price"]','meta[itemprop="price"]'] },
    { key: "fcp_euro", name: "FCP Euro", hosts: [/(^|\.)fcpeuro\.com$/], title: ['h1'], price: ['[itemprop="price"]','[data-testid="product-price"]'] },
    { key: "miami_star", name: "Miami Star", hosts: [/(^|\.)miamistar\.com$/], title: ['h1','.product-title'], price: ['[itemprop="price"]','.price'] },
    { key: "oem_parts_online", name: "OEM Parts Online", hosts: [/(^|\.)oempartsonline\.com$/], title: ['h1','.product-title'], price: ['[itemprop="price"]','.sale-price','.price'] }
  ];

  function siteProfile() {
    return SITE_PROFILES.find(profile => profile.hosts.some(pattern => pattern.test(location.hostname))) || null;
  }

  function readProductPage({ source_key = "one_time_website", source_name = "One-time Website" } = {}) {
    if (!/^https?:$/.test(location.protocol)) throw new Error("Only HTTP or HTTPS supplier pages can be captured.");
    const profile = siteProfile();
    const scope = primaryProductScope();
    const product = primaryStructuredProduct(scope);
    const offers = (Array.isArray(product.offers) ? product.offers[0] : product.offers) || {};
    const attributes = attributeMap(scope);
    const attributeLabels = [...attributes.keys()].join(" ");
    const profileId = profile?.id?.() || "";
    const sku = first(product.sku, meta(['[itemprop="sku"]','meta[property="product:retailer_item_id"]']), attr(attributes,["sku","stock number"]));
    const asin = first(profile?.idType === "asin" ? profileId : "", attr(attributes,["asin"]));
    const listingId = first(profile?.idType === "listing_id" ? profileId : "", attr(attributes,["listing id","item number","item id"]));
    const mpn = first(product.mpn, meta(['[itemprop="mpn"]','meta[property="product:mpn"]']), attr(attributes,["manufacturer part number","mpn","oem part number","part number"]));
    const supplierPart = first(attr(attributes,["supplier part number","part #"]), sku, asin, listingId);
    const rawPrice = first(offers.price, meta(['meta[property="product:price:amount"]','meta[itemprop="price"]']), attr(attributes,["price","our price","sale price"]), firstMoney(scope, profile?.price || ['[itemprop="price"]','.product-price','.sale-price','.price']));
    const currency = first(offers.priceCurrency, meta(['meta[property="product:price:currency"]','meta[itemprop="priceCurrency"]']), text(rawPrice).match(/\b(USD|EUR|GBP|CAD|AUD|JPY)\b/i)?.[1], "USD").toUpperCase();
    const weightText = product.weight || attr(attributes,["package weight","shipping weight","item weight","product weight","weight"]);
    const dimensionsText = (product.depth && product.width && product.height)
      ? product
      : attr(attributes,["package dimensions","shipping dimensions","item dimensions","product dimensions","dimensions"]);
    const parsedDimensions = parseDimensions(dimensionsText);
    const separateUnit = first(
      text(attr(attributes,["length"])).match(/(in|inches?|mm|cm)\b/i)?.[1],
      text(attr(attributes,["width"])).match(/(in|inches?|mm|cm)\b/i)?.[1],
      text(attr(attributes,["height"])).match(/(in|inches?|mm|cm)\b/i)?.[1]
    );
    const evidence = [...attributes.entries()].slice(0, 12).map(([key,value]) => `${key}: ${value}`).join(" | ");
    const shippingText = attr(attributes,["shipping","delivery"]);
    const shippingCost = parseMoney(first(offers.shippingDetails?.shippingRate?.value, attr(attributes,["shipping cost"])));
    const title = first(product.name, meta(profile?.title || []), meta(['meta[property="og:title"]','meta[name="twitter:title"]','[itemprop="name"]','h1']), document.title);
    return normalizeCapture({
      source_key: profile?.key || source_key,
      source_name: profile?.name || source_name,
      source_url: location.href,
      capture_mode: "PAGE",
      trust_level: "NEEDS_REVIEW",
      currency,
      shipping: shippingCost,
      items: [{
        description: title || "Identified Part",
        brand: first(product.brand?.name, product.brand, meta(['[itemprop="brand"]','meta[property="product:brand"]']), attr(attributes,["brand","manufacturer"])),
        manufacturer_part_number: mpn,
        supplier_part_number: supplierPart,
        sku, asin, listing_id: listingId,
        quantity: parseInteger(first(meta(['input[name="quantity"]','input[aria-label*="quantity" i]']),1),1),
        supplier_cost: parseMoney(rawPrice), currency,
        availability: first(offers.availability, meta(['link[itemprop="availability"]','meta[property="product:availability"]']), attr(attributes,["availability","stock status"])),
        weight: weightText,
        weight_type: /package weight/i.test(attributeLabels) ? "PACKAGE" : /shipping weight/i.test(attributeLabels) ? "SHIPPING" : /(?:item|product) weight/i.test(attributeLabels) ? "ITEM" : "UNKNOWN",
        dimensions: dimensionsText,
        length: parsedDimensions.length ?? parseMoney(attr(attributes,["length","depth"])),
        width: parsedDimensions.width ?? parseMoney(attr(attributes,["width"])),
        height: parsedDimensions.height ?? parseMoney(attr(attributes,["height"])),
        dimension_unit: parsedDimensions.dimension_unit || normalizeUnit(separateUnit, "dimension"),
        dimension_type: /package dimensions/i.test(attributeLabels) ? "PACKAGE" : /shipping dimensions/i.test(attributeLabels) ? "SHIPPING" : /(?:item|product) dimensions/i.test(attributeLabels) ? "ITEM" : "UNKNOWN",
        shipping_cost: shippingCost,
        shipping_method: first(offers.shippingDetails?.shippingDestination?.addressCountry, attr(attributes,["shipping method"])),
        lead_time: first(offers.deliveryLeadTime?.value, attr(attributes,["lead time","delivery time","estimated delivery"]), shippingText),
        fitment: attr(attributes,["fitment","vehicle fitment"]),
        warehouse: attr(attributes,["warehouse","location"]),
        evidence,
        evidence_fields: Object.fromEntries([...attributes.entries()]),
        product_page_url: location.href,
        source_url: location.href
      }]
    });
  }

  function readCartPage({ source_key = "one_time_website", source_name = "One-time Website" } = {}) {
    if (!/^https?:$/.test(location.protocol)) throw new Error("Only HTTP or HTTPS supplier pages can be captured.");
    const profile = siteProfile();
    const selectors = [
      ".sc-list-item[data-asin]", ".cart-bucket-lineitem", "[data-cart-item]",
      ".cart-item", ".cart-item-row", ".cart__row", ".order-line",
      "[data-testid*=cart-item]", "[role=listitem]"
    ];
    const scopes = cartScopes();
    const rows = [...new Set(scopes.flatMap(scope => [...scope.querySelectorAll(selectors.join(","))]))]
      .filter(row => !excludedCollection(row))
      .filter((row, index, all) => !all.some((other, otherIndex) => otherIndex !== index && row.contains(other)));
    const items = rows.map(row => {
      const attributes = attributeMap(row);
      const productLink = row.querySelector('a[href*="/dp/"],a[href*="/itm/"],a[href*="product"],a[href]');
      const href = productLink?.href || "";
      const asin = first(row.dataset.asin, href.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i)?.[1]);
      const listingId = first(row.dataset.itemId, href.match(/\/(\d{9,15})(?:\?|$)/)?.[1]);
      const sku = first(row.dataset.sku, attr(attributes,["sku","stock number"]), firstText(row,['[itemprop="sku"]','.sku','.product-id']));
      const mpn = first(row.dataset.mpn, attr(attributes,["manufacturer part number","mpn","oem part number"]));
      const supplierPart = first(row.dataset.partNumber, attr(attributes,["supplier part number","part number"]), sku, asin, listingId);
      const rawPrice = first(
        row.querySelector('[itemprop="price"]')?.content,
        firstText(row,['.sc-product-price','.item-price','.cart-price','.price','[data-price]']),
        attr(attributes,["price","unit price"])
      );
      return {
        description: first(firstText(row,['.sc-product-title','.item-title','.product-title','[itemprop="name"]','h2','h3']), productLink, "Cart Item"),
        brand: first(row.dataset.brand, attr(attributes,["brand","manufacturer"])),
        manufacturer_part_number: mpn,
        supplier_part_number: supplierPart,
        sku, asin, listing_id: listingId, item_id: first(row.dataset.itemId, listingId),
        quantity: parseInteger(first(row.querySelector('input[name*="quantity" i],input[aria-label*="quantity" i],select[name*="quantity" i]')?.value, row.dataset.quantity, 1),1),
        supplier_cost: parseMoney(rawPrice),
        currency: first(row.dataset.currency, row.querySelector('[itemprop="priceCurrency"]')?.content),
        availability: first(row.dataset.availability, attr(attributes,["availability","stock status"])),
        shipping_cost: parseMoney(attr(attributes,["shipping cost","shipping"])),
        shipping_method: attr(attributes,["shipping method"]),
        lead_time: attr(attributes,["lead time","delivery","estimated delivery"]),
        fitment: attr(attributes,["fitment"]), warehouse: attr(attributes,["warehouse","location"]),
        evidence: [...attributes.entries()].map(([key,value]) => `${key}: ${value}`).join(" | "),
        evidence_fields: Object.fromEntries(attributes),
        product_page_url: href,
        cart_page_url: location.href,
        source_url: href || location.href
      };
    }).filter(item => item.manufacturer_part_number || item.supplier_part_number || item.description !== "Cart Item");
    return normalizeCapture({
      source_key: profile?.key || source_key,
      source_name: profile?.name || source_name,
      source_url: location.href,
      capture_mode: "CART",
      trust_level: "NEEDS_REVIEW",
      items
    });
  }

  window.PLGConnectorSDK = {
    cleanText: text, parseMoney, parseInteger, firstText, firstMoney,
    normalizeAvailability, allRoots, sumItems, normalizeCapture, normalizeCart,
    parseWeight, parseDimensions, readProductPage, readGenericProductPage: readProductPage, readCartPage,
    cartScopes, primaryProductScope, excludedCollection,
    siteProfile, itemsMatch, mergeItems, mergeCaptures, normalizeWithProvider
  };
})();
