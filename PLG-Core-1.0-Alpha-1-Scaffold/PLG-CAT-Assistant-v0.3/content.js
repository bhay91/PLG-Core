(() => {
  if (window.__PLG_CAT_ASSISTANT_V03__) return;
  window.__PLG_CAT_ASSISTANT_V03__ = true;

  function isVisible(element) {
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 &&
      style.display !== "none" && style.visibility !== "hidden";
  }

  function scan(root, callback) {
    if (!root || !root.querySelectorAll) return null;
    const found = callback(root);
    if (found) return found;

    for (const element of root.querySelectorAll("*")) {
      if (element.shadowRoot) {
        const nested = scan(element.shadowRoot, callback);
        if (nested) return nested;
      }
    }
    return null;
  }

  function findInput(placeholder) {
    return scan(document, (root) => {
      for (const input of root.querySelectorAll("input")) {
        if (input.placeholder === placeholder && isVisible(input)) return input;
      }
      return null;
    });
  }

  function setValue(input, value) {
    input.scrollIntoView({block:"center",inline:"center"});
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value"
    ).set;

    setter.call(input,value);
    input.dispatchEvent(new InputEvent("input",{
      bubbles:true,
      composed:true,
      inputType:"insertText",
      data:value
    }));
    input.dispatchEvent(new Event("change",{bubbles:true,composed:true}));
    input.focus();
  }

  function pressEnter(input) {
    for (const type of ["keydown","keypress","keyup"]) {
      input.dispatchEvent(new KeyboardEvent(type,{
        key:"Enter",
        code:"Enter",
        keyCode:13,
        which:13,
        bubbles:true,
        composed:true
      }));
    }
  }

  async function waitForInput(placeholder) {
    for (let i=0;i<48;i+=1) {
      const input = findInput(placeholder);
      if (input) return input;
      await new Promise(resolve=>setTimeout(resolve,250));
    }
    return null;
  }

  async function addEquipment(pin) {
    const button = document.querySelector(
      '[data-testid="headerWidgetButton-native"]'
    );

    if (!button) throw new Error("Add Equipment button was not found.");
    button.click();

    const input = await waitForInput("Enter Serial Number");
    if (!input) throw new Error("Serial/PIN field was not found.");

    setValue(input,pin);
    pressEnter(input);

    return `PIN submitted: ${pin}. Select the matching machine result if CAT asks.`;
  }

  browser.runtime.onMessage.addListener((message) => {
    if (message?.type === "PLG_LOAD_PIN") {
      return addEquipment(message.pin)
        .then(status=>({ok:true,status}))
        .catch(error=>({ok:false,status:error.message}));
    }
    return undefined;
  });
})();
