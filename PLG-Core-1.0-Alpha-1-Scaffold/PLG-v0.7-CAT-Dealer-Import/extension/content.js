(() => {
if (window.__PLG_CAT_V07__) return;
window.__PLG_CAT_V07__ = true;
const DIAGRAM_KEY="plgLastCatDiagramUrl";
function vis(e){if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden";}
function scan(root,cb){if(!root||!root.querySelectorAll)return null;const f=cb(root);if(f)return f;for(const e of root.querySelectorAll("*"))if(e.shadowRoot){const n=scan(e.shadowRoot,cb);if(n)return n;}return null;}
function findInput(p){return scan(document,r=>{for(const i of r.querySelectorAll("input"))if(i.placeholder===p&&vis(i))return i;return null;});}
async function loadPin(pin){const b=document.querySelector('[data-testid="headerWidgetButton-native"]');if(!b)throw new Error("Add Equipment button not found.");b.click();let i=null;for(let x=0;x<48&&!i;x++){i=findInput("Enter Serial Number");if(!i)await new Promise(r=>setTimeout(r,250));}if(!i)throw new Error("PIN field not found.");const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set;set.call(i,pin);i.dispatchEvent(new InputEvent("input",{bubbles:true,composed:true,data:pin}));for(const t of ["keydown","keypress","keyup"])i.dispatchEvent(new KeyboardEvent(t,{key:"Enter",code:"Enter",keyCode:13,which:13,bubbles:true,composed:true}));return `PIN submitted: ${pin}`;}
if(location.pathname.includes("/parts-diagram"))browser.storage.local.set({[DIAGRAM_KEY]:location.href});
function productNumber(){const m=location.href.match(/\/product\/([^/?#]+)/i);return m?decodeURIComponent(m[1]).trim():"";}
function exactText(selector){return scan(document,r=>{for(const e of r.querySelectorAll(selector))if(vis(e)){const t=(e.textContent||"").trim();if(t)return t;}return null;});}
function partDescription(pn){const t=exactText('[data-part="part-default"][data-slot="default"]')||document.title||"";if(t.includes(":")){const [l,...rest]=t.split(":");if(l.trim().toLowerCase()===pn.toLowerCase())return rest.join(":").trim();}return t;}
function dealerAndLead(){const t=exactText("span.ps-1")||"";const m=t.match(/^(.*?)\s*-\s*(.+)$/);return m?{dealer:m[1].trim(),lead:m[2].trim()}:{dealer:t.trim(),lead:""};}
function price(){const t=exactText("span.fw-bold.px-1")||"";const m=t.match(/\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)/);return m?Number(m[1].replace(/,/g,"")):null;}
function availability(){return exactText("h2.cat-u-theme-typography-label.m-0.text-start.text-capitalize")||"";}
function extract(){const pn=productNumber();if(!pn)throw new Error("Open the CAT product page using More Details.");const dl=dealerAndLead();return {oem_part_number:pn,oem_description:partDescription(pn),product_url:location.href,oem_dealer_name:dl.dealer,oem_dealer_lead_time:dl.lead,oem_dealer_price:price(),oem_dealer_availability:availability()};}
browser.runtime.onMessage.addListener(m=>{
 if(m?.type==="PLG_LOAD_PIN")return loadPin(m.pin).then(status=>({ok:true,status})).catch(e=>({ok:false,status:e.message}));
 if(m?.type==="PLG_EXTRACT_CURRENT_PRODUCT")return browser.storage.local.get(DIAGRAM_KEY).then(s=>({ok:true,...extract(),diagram_url:s[DIAGRAM_KEY]||""})).catch(e=>({ok:false,status:e.message}));
});
})();