(() => {
  if (window.__PLG_CONNECTOR_CONTENT_V015__) return;
  window.__PLG_CONNECTOR_CONTENT_V015__ = true;

  browser.runtime.onMessage.addListener(message => {
    const connector = window.PLGConnectors.detectConnector();

    if (message?.type === "PLG_DETECT_SITE") {
      if (!connector) {
        return Promise.resolve({
          ok: false,
          status: "This supplier site is not supported yet."
        });
      }

      return Promise.resolve({
        ok: true,
        source_key: connector.key,
        source_name: connector.name
      });
    }

    if (message?.type !== "PLG_READ_CURRENT_CART") {
      return undefined;
    }

    if (!connector) {
      return Promise.resolve({
        ok: false,
        status: "This supplier site does not have a connector yet."
      });
    }

    try {
      return Promise.resolve({
        ok: true,
        ...connector.readCart()
      });
    } catch (error) {
      return Promise.resolve({
        ok: false,
        status: error.message
      });
    }
  });
})();