(() => {
  if (window.__PLG_CAT_ASSISTANT_V05__) return;
  window.__PLG_CAT_ASSISTANT_V05__ = true;
  const DIAGRAM_KEY = "plgLastCatDiagramUrl";

  function isVisible(el){if(!el)return false;const r=el.getBoundingClientRect();const s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden"}
  function scan(root,cb){if(!root||!root.querySelectorAll)return null;const f=cb(root);if(f)return f;for(const el of root.querySelectorAll("*")){if(el.shadowRoot){const n=scan(el.shadowRoot,cb);if(n)return n}}return null}
  function findInput(ph){return scan(document,root=>{for(const i of root.querySelectorAll("input")){if(i.placeholder===ph&&isVisible(i))return i}return null})}
  function setValue(i,v){i.scrollIntoView({block:"center",inline:"center"});Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(i,v);i.dispatchEvent(new InputEvent("input",{bubbles:true,composed:true,inputType:"insertText",data:v}));i.dispatchEvent(new Event("change",{bubbles:true,composed:true}));i.focus()}
  function pressEnter(i){for(const t of ["keydown","keypress","keyup"]){i.dispatchEvent(new KeyboardEvent(t,{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,composed:true}))}}
  async function waitForInput(ph){for(let n=0;n<48;n++){const i=findInput(ph);if(i)return i;await new Promise(r=>setTimeout(r,250))}return null}
  async function addEquipment(pin){const b=document.querySelector('[data-testid="headerWidgetButton-native"]');if(!b)throw new Error("Add Equipment button was not found.");b.click();const i=await waitForInput("Enter Serial Number");if(!i)throw new Error("Serial/PIN field was not found.");setValue(i,pin);pressEnter(i);return `PIN submitted: ${pin}. Select the matching machine if CAT asks.`}
  async function rememberDiagram(){if(location.pathname.includes("/parts-diagram")){await browser.storage.local.set({[DIAGRAM_KEY]:location.href})}}
  function productNumber(url){const m=url.match(/\/product\/([^/?#]+)/i);return m?decodeURIComponent(m[1]).trim():""}
  function partText(){const exact=scan(document,root=>{for(const el of root.querySelectorAll('[data-part="part-default"][data-slot="default"]')){const t=(el.textContent||"").trim();if(/^[A-Z0-9-]+\s*:\s*.+/i.test(t))return t}return null});if(exact)return exact;const h=scan(document,root=>{for(const el of root.querySelectorAll("h1,h2,h3")){const t=(el.textContent||"").trim();if(t&&isVisible(el))return t}return null});return h||document.title||""}
  function extract(){const product_url=location.href;const oem_part_number=productNumber(product_url);if(!oem_part_number)throw new Error("Open the CAT product page using More Details first.");let text=partText();let oem_description=text;if(text.includes(":")){const [l,...r]=text.split(":");if(l.trim().toLowerCase()===oem_part_number.toLowerCase())oem_description=r.join(":").trim()}oem_description=oem_description.replace(/^Cat®\s*/i,"").replace(/\s*\|\s*Cat.*$/i,"").trim();return{oem_part_number,oem_description:oem_description||"CAT Product",product_url}}
  rememberDiagram();
  browser.runtime.onMessage.addListener(msg=>{
    if(msg?.type==="PLG_LOAD_PIN")return addEquipment(msg.pin).then(status=>({ok:true,status})).catch(e=>({ok:false,status:e.message}));
    if(msg?.type==="PLG_EXTRACT_CURRENT_PRODUCT")return browser.storage.local.get(DIAGRAM_KEY).then(s=>({ok:true,...extract(),diagram_url:s[DIAGRAM_KEY]||""})).catch(e=>({ok:false,status:e.message}));
  });
})();
