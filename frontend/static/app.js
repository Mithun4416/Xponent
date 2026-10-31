"use strict";
// Xponent seller dashboard — talks only to real backend endpoints, scoped to the
// selected restaurant via the X-Dev-Tenant-ID header (server enforces tenant scope).

const $ = id => document.getElementById(id);
const state = { tenant: null, range: "last_30_days", currency: "INR", locale: "en-IN", recent: [] };

function moneyFmt(v){
  try { return new Intl.NumberFormat(state.locale, {style:"currency", currency:state.currency, maximumFractionDigits:0}).format(Number(v)||0); }
  catch { return (state.currency+" ")+ (Number(v)||0).toLocaleString(); }
}
const intf = n => (Number(n)||0).toLocaleString(state.locale);

function sellerHdr(extra={}){ return Object.assign({"X-Dev-Tenant-ID": String(state.tenant)}, extra); }
async function jx(path, opts={}){
  const r = await fetch(path, opts);
  if(!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
function toast(msg, type=""){
  const t=document.createElement("div"); t.className="toast "+type; t.textContent=msg;
  $("toast").appendChild(t); setTimeout(()=>t.remove(), 3600);
}

// ----- date ranges (mirror server range_dates; browser clock ~ data epoch) -----
function rangeDates(label){
  const now=new Date(); const DAY=864e5;
  const fd=d=>{const x=new Date(d); x.setUTCHours(0,0,0,0); return x;};
  let from, to=now;
  if(label==="last_7_days") from=new Date(now-7*DAY);
  else if(label==="this_month"){ from=fd(now); from.setUTCDate(1); }
  else if(label==="all_time"){ from=new Date(Date.UTC(2026,6,1)); to=new Date(now.getTime()+DAY); }
  else from=new Date(now-30*DAY);
  return [from.toISOString(), to.toISOString()];
}
const rangeLabel = ()=>({last_7_days:"last 7 days",last_30_days:"last 30 days",this_month:"this month",all_time:"all time"})[state.range];

// ----- sellers -----
async function loadSellers(){
  let ids=[];
  try{ const p=await jx("/api/v1/status/pipeline");
    ids=(p.stages?.[0]?.detail||[]).map(s=>s.tenant_id).filter(x=>x!=null);
  }catch{}
  if(!ids.length) ids=[1,2];
  ids=[...new Set(ids)].sort((a,b)=>a-b);
  $("seller").innerHTML=ids.map(i=>`<option value="${i}">Restaurant #${i}</option>`).join("");
  state.tenant=ids[0];
  syncSeller();
}
function syncSeller(){
  const n=`Restaurant #${state.tenant}`;
  $("acctName").textContent=n; $("avatar").textContent=String(state.tenant);
  const hr=new Date().getHours();
  const greet=hr<12?"Good morning":hr<18?"Good afternoon":"Good evening";
  $("greeting").textContent=`${greet}, ${n}! 👋`;
}

// ----- dashboard -----
async function loadDashboard(){
  const [from,to]=rangeDates(state.range);
  $("kpiPeriod1").textContent=$("kpiPeriod2").textContent=$("kpiPeriod3").textContent=`${rangeLabel()}`;
  $("trendSub").textContent=`Your daily net sales for the ${rangeLabel()}.`;
  // current + previous period (for deltas) — both real queries.
  const span=new Date(to)-new Date(from);
  const prevFrom=new Date(new Date(from)-span).toISOString(), prevTo=from;
  const t0=performance.now();
  let cur, prev, trend, fresh;
  try{
    cur=await jx(`/api/v1/tenants/${state.tenant}/orders/summary?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,{headers:sellerHdr()});
    state.currency=cur.data.currency||state.currency; state.locale=state.currency==="INR"?"en-IN":"en-US";
    fresh=cur.freshness;
  }catch(e){ toast("Couldn't load your numbers: "+e.message,"err"); return; }
  const qms=Math.round(performance.now()-t0);
  try{ prev=await jx(`/api/v1/tenants/${state.tenant}/orders/summary?from=${encodeURIComponent(prevFrom)}&to=${encodeURIComponent(prevTo)}`,{headers:sellerHdr()}); }catch{ prev=null; }
  try{ trend=await jx(`/api/v1/tenants/${state.tenant}/orders/trends?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&granularity=day`,{headers:sellerHdr()}); }catch{ trend={series:[]}; }

  $("kpiOrders").textContent=intf(cur.data.order_count);
  $("kpiGross").textContent=moneyFmt(cur.data.gross_sales);
  $("kpiNet").textContent=moneyFmt(cur.data.net_sales);
  setDelta("dOrders", cur.data.order_count, prev?.data.order_count);
  setDelta("dGross", cur.data.gross_sales, prev?.data.gross_sales);
  setDelta("dNet", cur.data.net_sales, prev?.data.net_sales);

  drawTrend(trend.series);
  $("insights").innerHTML=
    `<tr><td>Orders</td><td class="num">${intf(cur.data.order_count)}</td></tr>`+
    `<tr><td>Gross sales</td><td class="num">${moneyFmt(cur.data.gross_sales)}</td></tr>`+
    `<tr><td>Net sales</td><td class="num">${moneyFmt(cur.data.net_sales)}</td></tr>`+
    `<tr><td>Avg. order value</td><td class="num">${moneyFmt(cur.data.order_count?cur.data.gross_sales/cur.data.order_count:0)}</td></tr>`;

  const ok=fresh&&fresh.status==="FRESH";
  $("dataStatus").innerHTML=
    `<div><span class="badge ${ok?"fresh":"stale"}"><span class="dotp"></span>${ok?"Up to date":"May be delayed"}</span></div>`+
    `<div style="margin-top:8px">Last updated: <b>${fresh?fresh.human_readable:"—"}</b></div>`+
    `<div>Query time: <b>${qms} ms</b></div>`;
}
function setDelta(id, cur, prev){
  const el=$(id);
  if(prev==null||prev===0){ el.textContent=""; return; }
  const pct=Math.round(((cur-prev)/prev)*100);
  el.className="delta "+(pct>=0?"up":"down");
  el.textContent=(pct>=0?"↑ +":"↓ ")+pct+"%";
}
function drawTrend(series){
  const svg=$("trend"); if(!series||!series.length){ svg.innerHTML=`<text x="12" y="30" fill="#9ca3af" font-size="13">No data for this period.</text>`; return; }
  const vals=series.map(p=>p.net_sales||0), max=Math.max(1,...vals), W=600,H=210,n=vals.length;
  const x=i=>n<=1?0:(i/(n-1))*W, y=v=>H-(v/max)*(H-24)-8;
  let d=""; vals.forEach((v,i)=>{d+=(i?"L":"M")+x(i).toFixed(1)+","+y(v).toFixed(1)+" ";});
  svg.innerHTML=`<path d="${d} L${W},${H} L0,${H} Z" fill="rgba(229,56,59,.10)"/><path d="${d}" fill="none" stroke="#e5383b" stroke-width="2.5"/>`;
}

// ----- NL query -----
const EXAMPLES=["My sales this week","Order count last 7 days","Compare this month vs last month","Order status breakdown last 30 days","Top products this month"];
function renderChips(el, input, ans){
  el.innerHTML=EXAMPLES.map(e=>`<span class="chip">${e}</span>`).join("");
  el.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{input.value=c.textContent; askQuery(input, ans, el===$("chips"));});
}
function answerText(res){
  const r=res.result||{}, cur=state.currency;
  if(r.data) return `You had <b>${intf(r.data.order_count)}</b> orders, <b>${moneyFmt(r.data.gross_sales)}</b> gross and <b>${moneyFmt(r.data.net_sales)}</b> net.`;
  if(r.series){ const o=r.series.reduce((a,p)=>a+(p.order_count||0),0); return `<b>${intf(o)}</b> orders across <b>${r.series.length}</b> day(s).`; }
  if(r.categories) return r.categories.map(c=>`${c.status}: <b>${intf(c.order_count)}</b>`).join(" · ");
  if(r.items) return `Top: `+r.items.slice(0,3).map(p=>`${p.product_name||("#"+p.product_id)} (${moneyFmt(p.gross_sales)})`).join(", ");
  if(r.leaderboard) return r.leaderboard.map(s=>`R#${s.tenant_id}: ${moneyFmt(s.gross_sales)}`).join(" · ");
  return "No matching data for that question.";
}
async function askQuery(input, ansEl, isFull){
  const q=input.value.trim(); if(!q) return;
  ansEl.innerHTML=`<span class="spin"></span> Thinking…`;
  try{
    const res=await jx("/api/v1/nlq",{method:"POST",headers:sellerHdr({"Content-Type":"application/json"}),body:JSON.stringify({question:q})});
    const f=res.freshness, empty=(res.result?.data && res.result.data.order_count===0);
    const fb=res.fallback?` <span class="badge stale">simple mode</span>`:"";
    const head=isFull?`<div class="lead">${answerText(res)}</div>`:`<div>${answerText(res)}</div>`;
    ansEl.innerHTML=head+
      `<div class="small muted" style="margin-top:8px">${res.interpreted.label}${fb}`+
      (f?` · <span class="badge ${f.status==="FRESH"?"fresh":"stale"}"><span class="dotp"></span>${f.human_readable}</span>`:"")+
      ` · answered in ${res.execution_ms} ms from your live sales data.</div>`+
      (empty?`<div class="small muted" style="margin-top:6px">No orders in that period — try “All time” or create an order in the Orders tab.</div>`:"");
    // recent + diagnostics
    state.recent.unshift(q); state.recent=state.recent.slice(0,6); renderRecent();
    $("diagSql").textContent=`engine: ${res.engine}  (requested: ${res.provider_requested}${res.fallback?", fell back":""})\nscope_locked: ${res.scope_locked}\n\n${res.sql}`;
  }catch(e){ ansEl.innerHTML=`<span class="err">Sorry — that didn’t work: ${e.message}</span>`; }
}
function renderRecent(){
  $("recent").innerHTML=state.recent.length? state.recent.map(q=>`<div style="padding:5px 0;border-bottom:1px solid var(--canvas)"><a href="#" data-q="${encodeURIComponent(q)}">${q}</a></div>`).join(""):"None yet.";
  $("recent").querySelectorAll("a").forEach(a=>a.onclick=ev=>{ev.preventDefault(); const q=decodeURIComponent(a.dataset.q); $("q").value=q; askQuery($("q"),$("answer"),true);});
}

// ----- orders CRUD -----
async function loadOrders(){
  try{
    const r=await jx(`/api/v1/tenants/${state.tenant}/orders?limit=20`,{headers:sellerHdr()});
    $("ordersBody").innerHTML=r.orders.map(o=>{
      const cls={placed:"s-placed",shipped:"s-shipped",cancelled:"s-cancelled"}[o.status]||"s-placed";
      const actions=o.status==="cancelled"
        ? `<button class="btn sm" data-act="del" data-id="${o.order_id}">Delete</button>`
        : `<button class="btn sm" data-act="upd" data-id="${o.order_id}">Edit</button> `+
          `<button class="btn sm danger" data-act="cancel" data-id="${o.order_id}">Cancel</button> `+
          `<button class="btn sm" data-act="del" data-id="${o.order_id}">Delete</button>`;
      return `<tr><td>#${o.order_id}</td><td><span class="statuspill ${cls}">${o.status}</span></td>`+
        `<td class="num">${moneyFmt(o.amount_cents/100)}</td><td class="small muted">${o.created_at.slice(0,10)}</td>`+
        `<td class="num">${actions}</td></tr>`;
    }).join("")||`<tr><td colspan="5" class="muted">No orders yet. Create one above.</td></tr>`;
    $("ordersBody").querySelectorAll("button").forEach(b=>b.onclick=()=>orderAction(b.dataset.act, b.dataset.id));
  }catch(e){ $("ordersBody").innerHTML=`<tr><td colspan="5" class="err">${e.message}</td></tr>`; }
}
async function createOrder(){
  const amt=parseInt($("newAmount").value||"0",10);
  const body=amt>0?{amount_cents:amt}:{};
  try{
    const r=await jx(`/api/v1/tenants/${state.tenant}/orders`,{method:"POST",headers:sellerHdr({"Content-Type":"application/json"}),body:JSON.stringify(body)});
    toast(`Order #${r.order_id} created (${moneyFmt(r.amount_cents/100)})`,"ok");
    $("newAmount").value=""; refreshAfterWrite();
  }catch(e){ toast("Create failed: "+e.message,"err"); }
}
async function orderAction(act, id){
  try{
    if(act==="cancel"){ await jx(`/api/v1/tenants/${state.tenant}/orders/${id}/cancel`,{method:"POST",headers:sellerHdr()}); toast(`Order #${id} cancelled`,"ok"); }
    else if(act==="del"){ if(!confirm(`Delete order #${id}? This cannot be undone.`)) return;
      await jx(`/api/v1/tenants/${state.tenant}/orders/${id}`,{method:"DELETE",headers:sellerHdr()}); toast(`Order #${id} deleted`,"ok"); }
    else if(act==="upd"){ const v=prompt("New amount in paise (e.g. 25000 = ₹250):"); if(!v) return; const amt=parseInt(v,10);
      if(!(amt>0)){ toast("Enter a positive amount","err"); return; }
      await jx(`/api/v1/tenants/${state.tenant}/orders/${id}`,{method:"PATCH",headers:sellerHdr({"Content-Type":"application/json"}),body:JSON.stringify({amount_cents:amt})}); toast(`Order #${id} updated`,"ok"); }
    refreshAfterWrite();
  }catch(e){ toast(act+" failed: "+e.message,"err"); }
}
function refreshAfterWrite(){ loadOrders(); setTimeout(()=>{loadDashboard();},1500); }

// ----- diagnostics (admin) -----
async function recon(kind){
  const key=$("adminKey").value; const out=$("diagRecon");
  if(!key){ out.textContent="Enter the admin key to use Receipts."; return; }
  try{
    if(kind==="verify"){ const r=await jx(`/api/v1/admin/verification?tenant_id=${state.tenant}&run=true&sample=200`,{headers:{"X-Admin-Key":key}}); out.textContent="VERIFY → "+JSON.stringify(r.digest_health,null,2); }
    else{ const r=await jx(`/api/v1/admin/rebuild`,{method:"POST",headers:{"X-Admin-Key":key,"Content-Type":"application/json"},body:JSON.stringify({mode:"TARGETED",tenant_id:state.tenant,day_bucket:$("reconDay").value})}); out.textContent="REPAIR → "+JSON.stringify(r,null,2); }
  }catch(e){ out.textContent="Error: "+e.message; }
}

// ----- nav + wiring -----
function showSection(sec){
  document.querySelectorAll(".section").forEach(s=>s.classList.remove("on"));
  $("sec-"+sec).classList.add("on");
  document.querySelectorAll("#nav a").forEach(a=>a.classList.toggle("active", a.dataset.sec===sec));
  if(sec==="orders") loadOrders();
}
function wire(){
  document.querySelectorAll("#nav a").forEach(a=>a.onclick=e=>{e.preventDefault(); showSection(a.dataset.sec);});
  $("seller").onchange=()=>{ state.tenant=Number($("seller").value); syncSeller(); loadDashboard(); if($("sec-orders").classList.contains("on")) loadOrders(); };
  $("range").onchange=()=>{ state.range=$("range").value; loadDashboard(); };
  $("diagBtn").onclick=()=>{ const d=$("diag"); d.style.display=d.style.display==="none"?"block":"none"; d.open=true; };
  renderChips($("chips"), $("q"), $("answer"));
  renderChips($("chipsMini"), $("qMini"), $("ansMini"));
  $("run").onclick=()=>askQuery($("q"),$("answer"),true);
  $("runMini").onclick=()=>askQuery($("qMini"),$("ansMini"),false);
  $("q").addEventListener("keydown",e=>{if(e.key==="Enter")askQuery($("q"),$("answer"),true);});
  $("qMini").addEventListener("keydown",e=>{if(e.key==="Enter")askQuery($("qMini"),$("ansMini"),false);});
  $("createOrder").onclick=createOrder;
  $("verify").onclick=()=>recon("verify");
  $("repair").onclick=()=>recon("repair");
}

(async function init(){
  wire();
  await loadSellers();
  await loadDashboard();
  setInterval(()=>{ if($("sec-dashboard").classList.contains("on")) loadDashboard(); }, 8000);
})();
