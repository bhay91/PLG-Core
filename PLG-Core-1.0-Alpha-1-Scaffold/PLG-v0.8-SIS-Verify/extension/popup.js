const BASES=["http://127.0.0.1:8000","http://localhost:8000"];
let active=null,current=null;
const $=id=>document.getElementById(id);

function setStatus(message,ok=true){
  $("status").textContent=message;
  $("status").className=ok?"ok":"error";
}

async function api(path,options={}){
  let lastError=null;
  for(const base of BASES){
    try{return await fetch(base+path,options);}
    catch(error){lastError=error;}
  }
  throw lastError||new Error("PLG Core is unavailable.");
}

async function activeSisTab(){
  const [tab]=await browser.tabs.query({active:true,currentWindow:true});
  if(!tab?.id||!tab.url?.startsWith("https://sis2.cat.com/")){
    throw new Error("Open CAT SIS in the active tab first.");
  }
  return tab;
}

async function sendToSis(message){
  const tab=await activeSisTab();
  try{return await browser.tabs.sendMessage(tab.id,message);}
  catch(error){
    await browser.scripting.executeScript({
      target:{tabId:tab.id},
      files:["content.js"]
    });
    return browser.tabs.sendMessage(tab.id,message);
  }
}

async function refreshJob(){
  setStatus("Loading active PLG verification…");
  const response=await api("/api/active-verification");

  if(!response.ok){
    active=null;
    $("jobNumber").textContent="No active verification";
    $("customer").textContent="";
    $("machine").textContent="";
    $("pin").textContent="";
    $("requestedPart").textContent="";
    setStatus("Open the PLG job and click Verify Parts.",false);
    return;
  }

  active=await response.json();
  $("jobNumber").textContent=active.job_number;
  $("customer").textContent=`Customer: ${active.customer}`;
  $("machine").textContent=`Machine: ${active.manufacturer} ${active.machine}`;
  $("pin").textContent=`PIN / Serial: ${active.pin_serial}`;
  $("requestedPart").textContent=`Requested part: ${active.requested_description}`;
  setStatus("Active PLG verification loaded.");
}

async function readPart(){
  setStatus("Reading current SIS part…");
  const result=await sendToSis({type:"PLG_READ_SIS_PART"});

  if(!result?.ok){
    current=null;
    setStatus(result?.status||"Could not read SIS part.",false);
    return;
  }

  current=result;
  $("partNumber").textContent=result.oem_part_number;
  $("description").textContent=result.oem_description;
  $("price").textContent=result.oem_dealer_price!=null
    ?`Price: $${Number(result.oem_dealer_price).toFixed(2)} USD`
    :"Price: not detected";
  $("availability").textContent=`Availability: ${result.oem_dealer_availability||"not detected"}`;
  $("source").textContent="Source: CAT SIS";
  setStatus("Review the detected information, then import.");
}

$("refreshJob").onclick=refreshJob;
$("readPart").onclick=readPart;

$("importPart").onclick=async()=>{
  if(!active){
    setStatus("No active PLG verification.",false);
    return;
  }

  if(!current){
    await readPart();
    if(!current) return;
  }

  const response=await api("/api/capture-part",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      part_id:active.part_id,
      oem_part_number:current.oem_part_number,
      oem_description:current.oem_description,
      diagram_name:"",
      callout_number:"",
      source_url:current.source_url,
      diagram_url:current.diagram_url,
      product_url:current.product_url,
      oem_dealer_name:current.oem_dealer_name,
      oem_dealer_price:current.oem_dealer_price,
      oem_dealer_availability:current.oem_dealer_availability,
      oem_dealer_lead_time:current.oem_dealer_lead_time,
      verification_source:current.verification_source,
      verification_notes:"Imported from CAT SIS."
    })
  });

  const data=await response.json();
  if(!response.ok){
    setStatus(data.detail||"Import failed.",false);
    return;
  }

  setStatus("CAT SIS part imported into PLG.");
  active=null;
};

refreshJob().catch(error=>setStatus(error.message,false));