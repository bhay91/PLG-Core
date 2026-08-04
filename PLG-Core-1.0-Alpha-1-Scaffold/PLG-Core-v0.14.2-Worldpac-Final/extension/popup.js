const BASES=["http://127.0.0.1:8000","http://localhost:8000"];
let active=null,cart=null;
const $=id=>document.getElementById(id);

function status(message,ok=true){
  $("status").textContent=message;
  $("status").className=ok?"ok":"error";
}

async function api(path,options={}){
  for(const base of BASES){
    try{return await fetch(base+path,options);}
    catch(error){}
  }
  throw new Error("PLG Core unavailable.");
}

async function refresh(){
  const response=await api("/api/active-source-import");

  if(!response.ok){
    active=null;
    status("No active source import job.",false);
    return;
  }

  active=await response.json();
  $("jobNumber").textContent=active.job_number;
  $("customer").textContent=`Customer: ${active.customer}`;
  $("source").textContent=`Selected source: ${active.source_name}`;
  $("machine").textContent=`Machine: ${active.manufacturer} ${active.machine}`;
  $("pin").textContent=`VIN / PIN: ${active.pin_serial}`;
  status("Active source loaded.");
}

async function send(message){
  const [tab]=await browser.tabs.query({
    active:true,
    currentWindow:true
  });

  try{
    return await browser.tabs.sendMessage(tab.id,message);
  }catch(error){
    await browser.scripting.executeScript({
      target:{tabId:tab.id},
      files:["content.js"]
    });
    return browser.tabs.sendMessage(tab.id,message);
  }
}

function money(value){
  return value==null?"—":`$${Number(value).toFixed(2)}`;
}

function render(){
  $("count").textContent=`${cart.items.length} cart item(s)`;
  $("items").innerHTML=cart.items.map(item=>`
    <div class="row">
      <strong>${item.description}</strong>
      <span>${item.brand} ${item.supplier_part_number}</span>
      <span>${money(item.supplier_cost)} · Qty ${item.quantity}</span>
      <span>${item.fitment||""}</span>
      <span>${item.warehouse||""} ${item.shipping_method||""}</span>
    </div>
  `).join("");

  $("subtotal").textContent=money(cart.subtotal);
  $("shipping").textContent=money(cart.shipping);
  $("supplierTotal").textContent=money(cart.supplier_total);
}

$("refresh").onclick=refresh;

$("read").onclick=async()=>{
  const result=await send({type:"PLG_READ_CURRENT_CART"});

  if(!result?.ok){
    cart=null;
    status(result?.status||"Could not read cart.",false);
    return;
  }

  cart=result;
  render();
  status("Cart read successfully.");
};

$("import").onclick=async()=>{
  if(!active){
    status("No active source import job.",false);
    return;
  }

  if(!cart){
    status("Read the current cart first.",false);
    return;
  }

  const response=await api("/api/import-source-cart",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      job_id:active.job_id,
      source_key:cart.source_key,
      source_name:cart.source_name,
      trust_level:cart.trust_level,
      source_url:cart.source_url,
      currency:cart.currency,
      items:cart.items,
      charges:cart.charges
    })
  });

  const data=await response.json();

  if(!response.ok){
    status(data.detail||"Import failed.",false);
    return;
  }

  status(
    `Imported ${data.imported_count} item(s). ` +
    `Shipping ${money(data.shipping_total)}.`
  );

  setTimeout(()=>{
    browser.tabs.create({
      url:`http://127.0.0.1:8000/jobs/${data.job_id}?refresh=${Date.now()}`
    });
  },500);
};

refresh().catch(error=>status(error.message,false));