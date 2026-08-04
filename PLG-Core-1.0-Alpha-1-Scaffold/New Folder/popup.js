const statusEl=document.getElementById("status");
const pinEl=document.getElementById("pin");
const customerTermEl=document.getElementById("customerTerm");
const termSelect=document.getElementById("termSelect");

const terminologyRules = [
  {
    patterns: ["accelerator cable","throttle cable","gas pedal cable","accelerator control cable"],
    terms: [
      "Cable Assembly (Governor Control)",
      "governor control cable",
      "foot pedal governor cable",
      "foot throttle cable",
      "hand throttle cable",
      "engine speed control cable",
      "throttle control cable",
      "cable assembly"
    ]
  },
  {
    patterns: ["jack seal kit","hydraulic jack seal kit","cylinder seal kit","seal kit"],
    terms: [
      "hydraulic cylinder seal kit",
      "cylinder repair kit",
      "cylinder service kit",
      "seal kit",
      "repair kit"
    ]
  },
  {
    patterns: ["fuel pump"],
    terms: ["fuel transfer pump","fuel lift pump","fuel supply pump","injection pump"]
  },
  {
    patterns: ["turbo"],
    terms: ["turbocharger assembly","turbocharger","turbo service kit"]
  }
];

function normalize(value){return value.trim().toLowerCase().replace(/\s+/g," ");}

function buildTerms(customerTerm){
  const normalized=normalize(customerTerm);
  for(const rule of terminologyRules){
    if(rule.patterns.some(pattern=>normalized.includes(pattern))){
      return [...new Set([customerTerm,...rule.terms])];
    }
  }
  return [customerTerm];
}

function populateTerms(terms){
  termSelect.innerHTML="";
  terms.forEach((term,index)=>{
    const option=document.createElement("option");
    option.value=term;
    option.textContent=`${index+1}. ${term}`;
    termSelect.appendChild(option);
  });
}

function setStatus(message,ok=true){
  statusEl.textContent=message;
  statusEl.className=ok?"ok":"error";
}

async function getActiveCatTab(){
  const [tab]=await browser.tabs.query({active:true,currentWindow:true});
  if(!tab?.id) throw new Error("No active tab was found.");
  if(!tab.url?.startsWith("https://parts.cat.com/")){
    throw new Error("Open parts.cat.com in the active tab first.");
  }
  return tab;
}

async function send(message){
  const tab=await getActiveCatTab();
  try{
    return await browser.tabs.sendMessage(tab.id,message);
  }catch(error){
    await browser.scripting.executeScript({target:{tabId:tab.id},files:["content.js"]});
    return browser.tabs.sendMessage(tab.id,message);
  }
}

async function searchCurrent(){
  const term=termSelect.value;
  if(!term){setStatus("Build or select a search term first.",false);return;}
  setStatus(`Searching CAT for: ${term}`);
  try{
    const response=await send({type:"PLG_SEARCH_PART",term});
    setStatus(response?.status||"Search submitted.",Boolean(response?.ok));
  }catch(error){setStatus(error.message,false);}
}

document.getElementById("addEquipment").addEventListener("click",async()=>{
  const pin=pinEl.value.trim();
  if(!pin){setStatus("Enter a PIN or serial number.",false);return;}
  setStatus("Submitting PIN…");
  try{
    const response=await send({type:"PLG_ADD_EQUIPMENT",pin});
    setStatus(response?.status||"PIN submitted.",Boolean(response?.ok));
  }catch(error){setStatus(error.message,false);}
});

document.getElementById("buildTerms").addEventListener("click",()=>{
  const customerTerm=customerTermEl.value.trim();
  if(!customerTerm){setStatus("Enter the customer/mechanic term.",false);return;}
  const terms=buildTerms(customerTerm);
  populateTerms(terms);
  setStatus(`Built ${terms.length} search terms. Start with CAT's OEM wording.`);
});

document.getElementById("searchSelected").addEventListener("click",searchCurrent);

document.getElementById("searchNext").addEventListener("click",async()=>{
  if(termSelect.options.length===0){
    populateTerms(buildTerms(customerTermEl.value.trim()));
  }else{
    termSelect.selectedIndex=(termSelect.selectedIndex+1)%termSelect.options.length;
  }
  await searchCurrent();
});

document.getElementById("copySummary").addEventListener("click",async()=>{
  const summary=[
    "PLG Lookup",
    "",
    "Manufacturer: CAT",
    `PIN / Serial: ${pinEl.value.trim()}`,
    `Customer term: ${customerTermEl.value.trim()}`,
    `CAT search term: ${termSelect.value||""}`
  ].join("\n");
  await navigator.clipboard.writeText(summary);
  setStatus("PLG lookup summary copied.");
});

populateTerms(buildTerms(customerTermEl.value));
