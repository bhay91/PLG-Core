(() => {
  if (window.__PLG_CAT_ASSISTANT_V02__) return;
  window.__PLG_CAT_ASSISTANT_V02__ = true;

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
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set;
    setter.call(input,value);
    input.dispatchEvent(new InputEvent("input",{bubbles:true,composed:true,inputType:"insertText",data:value}));
    input.dispatchEvent(new Event("change",{bubbles:true,composed:true}));
    input.focus();
  }

  function pressEnter(input) {
    for (const type of ["keydown","keypress","keyup"]) {
      input.dispatchEvent(new KeyboardEvent(type,{
        key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,composed:true
      }));
    }
  }

  async function waitForInput(placeholder) {
    for (let i=0;i<48;i+=1) {
      const input = findInput(placeholder);
      if (input) return input;
      await new Promise(r=>setTimeout(r,250));
    }
    return null;
  }

  async function addEquipment(pin) {
    const button = document.querySelector('[data-testid="headerWidgetButton-native"]');
    if (!button) throw new Error("Add Equipment button was not found.");
    button.click();
    const input = await waitForInput("Enter Serial Number");
    if (!input) throw new Error("Serial/PIN field was not found.");
    setValue(input,pin);
    pressEnter(input);
    return `PIN submitted: ${pin}. Select the matching machine result if CAT asks.`;
  }

  async function searchPart(term) {
    const input = await waitForInput("Search for part number or name");
    if (!input) throw new Error("Part-search field was not found. Add/select equipment first.");
    setValue(input,term);
    const form = input.closest("form");
    if (form && typeof form.requestSubmit === "function") form.requestSubmit();
    else pressEnter(input);
    return `CAT search submitted: ${term}`;
  }

  browser.runtime.onMessage.addListener((message) => {
    if (message?.type === "PLG_ADD_EQUIPMENT") {
      return addEquipment(message.pin).then(status=>({ok:true,status}))
        .catch(error=>({ok:false,status:error.message}));
    }
    if (message?.type === "PLG_SEARCH_PART") {
      return searchPart(message.term).then(status=>({ok:true,status}))
        .catch(error=>({ok:false,status:error.message}));
    }
    return undefined;
  });
})();
