(() => {
  if (window.__PLG_CONNECTOR_CONTENT_V015__) return;
  window.__PLG_CONNECTOR_CONTENT_V015__ = true;

  function currentWebsite() {
    if (!/^https?:$/.test(location.protocol)) return null;
    return {
      key: "one_time_website",
      name: "One-time Website",
      source_url: location.href,
      domain: location.hostname,
      configured: false
    };
  }

  function oneTimeCapture(captured, page) {
    return {
      ...captured,
      suggested_supplier: captured.source_name === page.name ? page.domain : captured.source_name,
      source_key: page.key,
      source_name: page.name,
      source_domain: page.domain,
      configured_source: false
    };
  }

  function readCurrentPage() {
    const page = currentWebsite();
    if (!page) {
      throw new Error("Only HTTP or HTTPS supplier pages can be captured.");
    }
    return oneTimeCapture(window.PLGConnectorSDK.readProductPage({
      source_key: page.key,
      source_name: page.name
    }), page);
  }

  function readCurrentCart() {
    const page = currentWebsite();
    if (!page) throw new Error("Only HTTP or HTTPS supplier pages can be captured.");
    return oneTimeCapture(window.PLGConnectorSDK.readCartPage({
      source_key: page.key, source_name: page.name
    }), page);
  }

  browser.runtime.onMessage.addListener(message => {
    const connector = window.PLGConnectors.detectConnector();

    if (message?.type === "PLG_DETECT_SITE") {
      if (!connector) {
        const page = currentWebsite();
        if (page) {
          return Promise.resolve({ ok: true, ...page });
        }
        return Promise.resolve({
          ok: false,
          status: "Only HTTP or HTTPS supplier pages can be used."
        });
      }

      return Promise.resolve({
        ok: true,
        source_key: connector.key,
        source_name: connector.name,
        source_url: location.href,
        domain: location.hostname,
        configured: true
      });
    }

    if (!["PLG_READ_CURRENT_PAGE", "PLG_READ_CURRENT_CART"].includes(message?.type)) {
      return undefined;
    }

    try {
      const capture = message.type === "PLG_READ_CURRENT_PAGE"
        ? (connector?.readPage ? connector.readPage() : readCurrentPage())
        : (connector?.readCart ? connector.readCart() : readCurrentCart());
      return Promise.resolve({
        ok: true,
        ...capture
      });
    } catch (error) {
      return Promise.resolve({
        ok: false,
        status: error.message
      });
    }
  });
})();
