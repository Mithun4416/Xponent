"use strict";
// Xponent dashboard — one app, two roles:
//   Organization (platform owner): cross-tenant overview, leaderboard, live CDC
//     activity, and a read-only viewer for every database. Uses the operator key.
//   Restaurant (seller): own numbers only, scoped on the server by X-Dev-Tenant-ID.
// Talks only to real backend endpoints — nothing here is mocked.

const $ = id => document.getElementById(id);
const ico = (n, cls = "") => `<svg class="ico ${cls}"><use href="#i-${n}"/></svg>`;
const state = {
  mode: "org", tenant: null, sellerIds: [], range: "last_30_days",
  currency: "INR", locale: "en-IN", recent: [],
  db: { selected: null, meta: null, table: null, offset: 0, limit: 25 },
};

function moneyFmt(v){
  try { return new Intl.NumberFormat(state.locale, {style:"currency", currency:state.currency, maximumFractionDigits:0}).format(Number(v)||0); }
  catch { return (state.currency+" ")+ (Number(v)||0).toLocaleString(); }
}
const intf = n => (Number(n)||0).toLocaleString(state.locale);
function fmtMetric(value, format){
  if(value==null) return "—";
  if(format==="currency") return moneyFmt(value);
  if(format==="percent") return (Math.round(value*10)/10)+"%";
  return intf(value);
}

const adminKey = () => ($("adminKey").value || "").trim();
const sellerHdr = (extra={}) => Object.assign({"X-Dev-Tenant-ID": String(state.tenant)}, extra);
const adminHdr  = (extra={}) => Object.assign({"X-Admin-Key": adminKey()}, extra);
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
const RANGE_OPTS=[{value:"last_7_days",label:"Last 7 days"},{value:"last_30_days",label:"Last 30 days"},
  {value:"this_month",label:"This month"},{value:"all_time",label:"All time"}];
const rangeLabel = ()=>(RANGE_OPTS.find(o=>o.value===state.range)||{}).label||"";

// ----- custom dropdown -----
function makeDropdown(mount, {options, value, onChange}){
  mount.classList.add("dd");
  mount.innerHTML=`<button class="dd-btn" type="button"><span class="dd-cur"></span>${ico("chev","chev")}</button><div class="dd-menu"></div>`;
  const btn=mount.querySelector(".dd-btn"), cur=mount.querySelector(".dd-cur"), menu=mount.querySelector(".dd-menu");
  let val=value, opts=options;
  function render(){
    cur.textContent=(opts.find(o=>String(o.value)===String(val))||opts[0]||{}).label||"";
    menu.innerHTML=opts.map(o=>`<div class="dd-opt ${String(o.value)===String(val)?'sel':''}" data-v="${o.value}">${o.label}${ico("check")}</div>`).join("");
    menu.querySelectorAll(".dd-opt").forEach(el=>el.onclick=()=>{ val=el.dataset.v; mount.classList.remove("open"); render(); onChange(val); });
  }
  btn.onclick=e=>{ e.stopPropagation(); document.querySelectorAll(".dd.open").forEach(d=>{if(d!==mount)d.classList.remove("open");}); mount.classList.toggle("open"); };
  render();
  return { set(v){val=v;render();}, setOptions(o){opts=o;render();} };
}
document.addEventListener("click", ()=>document.querySelectorAll(".dd.open").forEach(d=>d.classList.remove("open")));

// ----- navigation + mode -----
const NAV={
  org:[{sec:"org-overview",label:"Overview",icon:"home"},{sec:"org-activity",label:"Live activity",icon:"activity"},{sec:"org-db",label:"Databases",icon:"db"}],
  seller:[{sec:"dashboard",label:"Dashboard",icon:"home"},{sec:"ask",label:"Ask a question",icon:"chat"},{sec:"orders",label:"Orders",icon:"receipt"}],
};
function renderNav(){
  $("nav").innerHTML=NAV[state.mode].map((n,i)=>
    `<a href="#" class="${i===0?'active':''}" data-sec="${n.sec}">${ico(n.icon)} ${n.label}</a>`).join("");
  $("nav").querySelectorAll("a").forEach(a=>a.onclick=e=>{e.preventDefault(); showSection(a.dataset.sec);});
}
function showSection(sec){
  document.querySelectorAll(".section").forEach(s=>s.classList.remove("on"));
  const el=$("sec-"+sec); if(el) el.classList.add("on");
  $("nav").querySelectorAll("a").forEach(a=>a.classList.toggle("active", a.dataset.sec===sec));
  if(sec==="dashboard") loadDashboard();
  else if(sec==="orders") loadOrders();
  else if(sec==="org-overview") loadOrg();
  else if(sec==="org-activity") loadActivity();
  else if(sec==="org-db") loadDatabases();
}
function setMode(role){
  state.mode=role;
  document.querySelectorAll("#roleSeg button").forEach(b=>b.classList.toggle("active", b.dataset.role===role));
  renderNav();
  if(role==="org"){
    $("avatar").textContent="O"; $("acctName").textContent="Organization"; $("acctSub").textContent="Platform owner";
    $("sellerSwitchWrap").style.display="none";
    $("greeting").textContent="Organization overview"; $("greetSub").textContent="Every restaurant on the platform, live.";
    showSection("org-overview");
  }else{
    if(state.tenant==null) state.tenant=state.sellerIds[0]||1;
    syncSeller();
    $("sellerSwitchWrap").style.display="block";
    showSection("dashboard");
  }
}
function syncSeller(){
  const n=`Restaurant #${state.tenant}`;
  $("avatar").textContent=String(state.tenant); $("acctName").textContent=n; $("acctSub").textContent="Seller account";
  const hr=new Date().getHours();
  const greet=hr<12?"Good morning":hr<18?"Good afternoon":"Good evening";
  $("greeting").textContent=`${greet}, ${n}`; $("greetSub").textContent="Here's how your business is doing.";
}

// ===================== auth gate (demo sign-in) =====================
// Routes each role to its own dashboard. A restaurant that signs in is LOCKED to
// its own data — no org views, no restaurant switcher, no operator key. The demo
// per-restaurant passkey below would be an opaque, server-issued credential in
// production; here the real isolation is enforced server-side by Row-Level
// Security regardless of what is typed on this screen.
const sellerPass = id => "restaurant" + id;
let loginSellerTid = null;

function showLogin(){
  state.authRole = null;
  $("appRoot").style.display = "none";
  $("login").style.display = "flex";
  $("signOut").style.display = "none";
  $("loginErr").textContent = "";
  $("loginSellerKey").value = "";
  $("loginRoles").querySelectorAll("button").forEach(b=>b.classList.toggle("active", b.dataset.r==="org"));
  $("loginOrg").style.display = "flex"; $("loginSeller").style.display = "none";
}
async function signInOrg(key){
  $("loginErr").textContent = "";
  if(!key){ $("loginErr").textContent = "Enter the operator key."; return; }
  $("adminKey").value = key;
  try{
    const r = await fetch(`/api/v1/org/activity?limit=1`, {headers:{"X-Admin-Key":key}});
    if(r.status === 403){ $("loginErr").textContent = "Invalid operator key."; return; }
    // Any other reachable response (200, or 503 if owner pool off) means the key passed.
  }catch(e){ $("loginErr").textContent = "Can't reach the server — is it running?"; return; }
  enterApp("org");
}
function signInSeller(tid, key){
  $("loginErr").textContent = "";
  if(tid==null){ $("loginErr").textContent = "Pick a restaurant."; return; }
  if(key !== sellerPass(tid)){ $("loginErr").textContent = "Wrong passkey for that restaurant."; return; }
  state.tenant = Number(tid);
  enterApp("seller");
}
function enterApp(role){
  state.authRole = role;
  $("login").style.display = "none";
  $("appRoot").style.display = "flex";
  $("signOut").style.display = "flex";
  if(role === "seller"){
    // Lock the whole UI to this one restaurant.
    $("adminKey").value = "";                 // no operator powers for a seller
    $("roleSeg").style.display = "none";       // can't flip to the org view
    $("adminWrap").style.display = "none";     // no operator key field
    if(sellerDD) sellerDD.set(String(state.tenant));
    setMode("seller");
    $("sellerSwitchWrap").style.display = "none";  // can't switch to another restaurant
  }else{
    $("roleSeg").style.display = "";
    $("adminWrap").style.display = "";
    setMode("org");
  }
}
function signOut(){ showLogin(); }

// ===================== ORGANIZATION: overview =====================
async function loadOrg(){
  const [from,to]=rangeDates(state.range);
  $("oPeriod1").textContent=$("oPeriod2").textContent=rangeLabel();
  let o;
  try{ o=await jx(`/api/v1/org/overview?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`,{headers:adminHdr()}); }
  catch(e){ $("leaderboard").innerHTML=`<tr><td colspan="5" class="err">${e.message.includes("403")?"Enter a valid operator key in the sidebar to load the organization view.":e.message}</td></tr>`; return; }
  state.currency=o.currency||state.currency; state.locale=state.currency==="INR"?"en-IN":"en-US";
  $("oTenants").textContent=intf(o.tenants);
  $("oOrders").textContent=intf(o.totals.order_count);
  $("oGross").textContent=moneyFmt(o.totals.gross_sales);
  const v=o.verification||{};
  const ok=v.status==="VERIFIED";
  $("oVerify").innerHTML=`<span class="badge ${ok?'fresh':'stale'}"><span class="dotp"></span>${ok?"Verified":"Drift"}</span>`;
  $("oVerifySub").textContent=`${intf(v.total_buckets||0)} buckets · ${intf(v.corrupted_buckets||0)} corrupted`;
  $("leaderboard").innerHTML=(o.leaderboard||[]).map((s,i)=>
    `<tr data-tid="${s.tenant_id}"><td><span class="rankdot">${i+1}</span>Restaurant #${s.tenant_id}</td>`+
    `<td class="num">${intf(s.order_count)}</td><td class="num">${moneyFmt(s.gross_sales)}</td>`+
    `<td class="num">${moneyFmt(s.net_sales)}</td><td class="num"><span class="viewlink">Open ${ico("chev")}</span></td></tr>`
  ).join("")||`<tr><td colspan="5" class="muted">No sellers with data in this period.</td></tr>`;
  $("leaderboard").querySelectorAll("tr[data-tid]").forEach(tr=>tr.onclick=()=>enterSeller(Number(tr.dataset.tid)));
}
function enterSeller(tid){
  state.tenant=tid; if(sellerDD) sellerDD.set(String(tid));
  setMode("seller");
  toast(`Viewing Restaurant #${tid} — its own scoped dashboard`,"ok");
}

// ===================== ORGANIZATION: live activity =====================
async function loadActivity(){
  $("actMsg").textContent="Loading…";
  try{
    const r=await jx(`/api/v1/org/activity?limit=50`,{headers:adminHdr()});
    $("activityBody").innerHTML=(r.events||[]).map(e=>
      `<tr><td class="small muted">${e.id}</td><td>Restaurant #${e.tenant_id}</td>`+
      `<td><span class="oppill op-${e.op}">${e.op}</span></td><td class="small">${e.table}</td>`+
      `<td class="small">${e.order_id??"—"}</td><td class="small muted">${e.lsn??"—"}</td>`+
      `<td class="small muted">${e.applied_at?e.applied_at.slice(11,19):"—"}</td></tr>`
    ).join("")||`<tr><td colspan="7" class="muted">No events yet — create or change an order to see CDC flow.</td></tr>`;
    $("actMsg").textContent=`${(r.events||[]).length} most recent events`;
  }catch(e){ $("activityBody").innerHTML=`<tr><td colspan="7" class="err">${e.message.includes("403")?"Operator key required.":e.message}</td></tr>`; $("actMsg").textContent=""; }
}
// Generate real CDC traffic on demand. Writes test orders into tenants' OWN source
// DBs via the admin demo endpoint; each write flows source → WAL → worker → rollup
// and appends a row to cdc_activity, so the feed below fills in live.
async function simulateActivity(){
  if(!adminKey()){ $("actMsg").textContent="Enter the operator key in the sidebar first."; return; }
  const btn=$("actSim"); btn.disabled=true;
  const ids=state.sellerIds.length?state.sellerIds:[1,2];
  const N=8; let made=0;
  $("actMsg").innerHTML=`<span class="spin"></span> Writing ${N} test orders across ${ids.length} restaurant(s)…`;
  for(let i=0;i<N;i++){
    try{ await jx(`/api/v1/demo/order`,{method:"POST",headers:adminHdr({"Content-Type":"application/json"}),body:JSON.stringify({tenant_id:ids[i%ids.length]})}); made++; }
    catch(e){ $("actMsg").textContent=(e.message.includes("403")?"Operator key required.":"Simulate failed: "+e.message); btn.disabled=false; return; }
  }
  $("actMsg").textContent=`Wrote ${made} orders — the worker is applying them. Refreshing…`;
  setTimeout(()=>{ loadActivity(); btn.disabled=false; }, 1400);
}

// ===================== ORGANIZATION: database explorer =====================
async function loadDatabases(){
  if(state.db.meta) { renderDbList(); return; }
  try{
    const r=await jx(`/api/v1/explore/databases`,{headers:adminHdr()});
    state.db.meta=r.databases;
    state.db.selected=(r.databases.find(d=>d.kind==="summary")||r.databases[0]||{}).id;
    renderDbList(); selectTablesFor(state.db.selected); loadRows();
  }catch(e){ $("dbList").innerHTML=`<div class="err small">${e.message.includes("403")?"Operator key required.":e.message}</div>`; }
}
function renderDbList(){
  $("dbList").innerHTML=state.db.meta.map(d=>
    `<div class="dbitem ${d.id===state.db.selected?'sel':''}" data-id="${d.id}">`+
    `<div class="dbname">${ico(d.kind==="summary"?"db":"store")} ${d.label}</div>`+
    `<div class="dbsub">${d.host} · ${d.dbname}</div>`+
    `<span class="dbtag ${d.kind}">${d.kind.toUpperCase()}</span>`+
    `<div class="dbsub">${d.role}</div></div>`
  ).join("");
  $("dbList").querySelectorAll(".dbitem").forEach(el=>el.onclick=()=>{
    state.db.selected=el.dataset.id; state.db.offset=0; renderDbList(); selectTablesFor(el.dataset.id); loadRows();
  });
}
function selectTablesFor(id){
  const d=state.db.meta.find(x=>x.id===id)||{tables:[]};
  state.db.table=d.tables[0];
  $("dbTables").innerHTML=d.tables.map(t=>`<div class="dbtab ${t===state.db.table?'sel':''}" data-t="${t}">${t}</div>`).join("");
  $("dbTables").querySelectorAll(".dbtab").forEach(el=>el.onclick=()=>{
    state.db.table=el.dataset.t; state.db.offset=0;
    $("dbTables").querySelectorAll(".dbtab").forEach(x=>x.classList.toggle("sel",x.dataset.t===el.dataset.t));
    loadRows();
  });
}
async function loadRows(){
  if(!state.db.selected||!state.db.table) return;
  const q=`database=${encodeURIComponent(state.db.selected)}&table=${encodeURIComponent(state.db.table)}&limit=${state.db.limit}&offset=${state.db.offset}`;
  $("dbBody").innerHTML=`<tr><td class="muted">Loading…</td></tr>`;
  try{
    const r=await jx(`/api/v1/explore/rows?${q}`,{headers:adminHdr()});
    $("dbHead").innerHTML=`<tr>${r.columns.map(c=>`<th>${c.name}<div class="coltype">${c.type}</div></th>`).join("")}</tr>`;
    $("dbBody").innerHTML=r.rows.map(row=>`<tr>${row.map(v=>`<td>${v==null?'<span class="muted">null</span>':String(v)}</td>`).join("")}</tr>`).join("")
      ||`<tr><td class="muted">No rows.</td></tr>`;
    const start=r.offset+ (r.row_count?1:0), end=r.offset+r.row_count;
    $("dbMeta").innerHTML=`Table <b>${r.table}</b> · ${r.columns.length} columns · ~${intf(r.estimated_total)} rows (estimated)`;
    $("dbPage").textContent=`Rows ${start}–${end}`;
  }catch(e){ $("dbBody").innerHTML=`<tr><td class="err">${e.message}</td></tr>`; }
}

// ===================== SELLER: dashboard =====================
async function loadDashboard(){
  const [from,to]=rangeDates(state.range);
  $("kpiPeriod1").textContent=$("kpiPeriod2").textContent=$("kpiPeriod3").textContent=rangeLabel();
  $("trendSub").textContent=`Your daily net sales for the ${rangeLabel()}.`;
  const span=new Date(to)-new Date(from);
  const prevFrom=new Date(new Date(from)-span).toISOString(), prevTo=from;
  const H=sellerHdr(), t0=performance.now();
  const S=(p)=>jx(p,{headers:H});
  // Parallel fetches — the previous version awaited these one after another.
  const [curR,prevR,trendR]=await Promise.allSettled([
    S(`/api/v1/tenants/${state.tenant}/orders/summary?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`),
    S(`/api/v1/tenants/${state.tenant}/orders/summary?from=${encodeURIComponent(prevFrom)}&to=${encodeURIComponent(prevTo)}`),
    S(`/api/v1/tenants/${state.tenant}/orders/trends?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&granularity=day`),
  ]);
  if(curR.status!=="fulfilled"){ toast("Couldn't load your numbers: "+curR.reason.message,"err"); return; }
  const cur=curR.value, prev=prevR.status==="fulfilled"?prevR.value:null, trend=trendR.status==="fulfilled"?trendR.value:{series:[]};
  const qms=Math.round(performance.now()-t0);
  state.currency=cur.data.currency||state.currency; state.locale=state.currency==="INR"?"en-IN":"en-US";

  $("kpiOrders").textContent=intf(cur.data.order_count);
  $("kpiGross").textContent=moneyFmt(cur.data.gross_sales);
  $("kpiNet").textContent=moneyFmt(cur.data.net_sales);
  setDelta("dOrders", cur.data.order_count, prev?.data.order_count);
  setDelta("dGross", cur.data.gross_sales, prev?.data.gross_sales);
  setDelta("dNet", cur.data.net_sales, prev?.data.net_sales);

  drawTrend(trend.series);
  let rows=
    `<tr><td>Orders</td><td class="num">${intf(cur.data.order_count)}</td></tr>`+
    `<tr><td>Gross sales</td><td class="num">${moneyFmt(cur.data.gross_sales)}</td></tr>`+
    `<tr><td>Net sales</td><td class="num">${moneyFmt(cur.data.net_sales)}</td></tr>`;
  // Config-defined derived metrics (Bonus B-4) — appear here with no UI code change.
  (cur.data.derived||[]).forEach(m=>{ rows+=`<tr><td>${m.label}</td><td class="num">${fmtMetric(m.value,m.format)}</td></tr>`; });
  $("insights").innerHTML=rows;

  const fresh=cur.freshness, fok=fresh&&fresh.status==="FRESH";
  $("dataStatus").innerHTML=
    `<div><span class="badge ${fok?"fresh":"stale"}"><span class="dotp"></span>${fok?"Up to date":"May be delayed"}</span></div>`+
    `<div style="margin-top:8px">Last updated: <b>${fresh?fresh.human_readable:"—"}</b></div>`+
    `<div>Query time: <b>${qms} ms</b></div>`;
}
function setDelta(id, cur, prev){
  const el=$(id);
  if(prev==null||prev===0){ el.textContent=""; el.className="delta"; return; }
  const pct=Math.round(((cur-prev)/prev)*100);
  el.className="delta "+(pct>=0?"up":"down");
  el.innerHTML=`${ico(pct>=0?"up":"down")} ${pct>=0?"+":""}${pct}%`;
}
function drawTrend(series){
  const svg=$("trend"); if(!series||!series.length){ svg.innerHTML=`<text x="12" y="30" fill="#9ca3af" font-size="13">No data for this period.</text>`; return; }
  const vals=series.map(p=>p.net_sales||0), max=Math.max(1,...vals), W=600,H=210,n=vals.length;
  const x=i=>n<=1?0:(i/(n-1))*W, y=v=>H-(v/max)*(H-24)-8;
  let d=""; vals.forEach((v,i)=>{d+=(i?"L":"M")+x(i).toFixed(1)+","+y(v).toFixed(1)+" ";});
  svg.innerHTML=`<path d="${d} L${W},${H} L0,${H} Z" fill="rgba(229,56,59,.10)"/><path d="${d}" fill="none" stroke="#e5383b" stroke-width="2.5"/>`;
}

// ===================== NL query (seller + org) =====================
const EXAMPLES=["My sales this week","Order count last 7 days","Order status breakdown last 30 days","Top products this month"];
const ORG_EXAMPLES=["Compare restaurants this month","Total orders across all restaurants last 7 days","Which restaurant sold most this month"];
function renderChips(el, input, ans, isFull){
  const ex=(state.mode==="org"&&el===$("ochips"))?ORG_EXAMPLES:EXAMPLES;
  el.innerHTML=ex.map(e=>`<span class="chip">${e}</span>`).join("");
  el.querySelectorAll(".chip").forEach(c=>c.onclick=()=>{input.value=c.textContent; askQuery(input, ans, isFull, el===$("ochips"));});
}
function answerText(res){
  const r=res.result||{};
  if(r.leaderboard){
    const list=r.leaderboard.map(s=>`R#${s.tenant_id}: <b>${moneyFmt(s.gross_sales)}</b> (${intf(s.order_count)} orders)`).join(" · ");
    if(r.ranked){ const w=r.top||r.leaderboard[0];
      return `Top: <b>Restaurant #${w.tenant_id}</b> — <b>${moneyFmt(w.gross_sales)}</b> gross${r.leaderboard.length>1?` · then ${list}`:``}`; }
    return list;
  }
  if(r.totals) return `<b>${intf(r.totals.order_count)}</b> orders · <b>${moneyFmt(r.totals.gross_sales)}</b> gross across <b>${r.tenants}</b> restaurants.`;
  if(r.data) return `<b>${intf(r.data.order_count)}</b> orders, <b>${moneyFmt(r.data.gross_sales)}</b> gross and <b>${moneyFmt(r.data.net_sales)}</b> net.`;
  if(r.series){ const o=r.series.reduce((a,p)=>a+(p.order_count||0),0); return `<b>${intf(o)}</b> orders across <b>${r.series.length}</b> day(s).`; }
  if(r.categories) return r.categories.map(c=>`${c.status}: <b>${intf(c.order_count)}</b>`).join(" · ");
  if(r.items) return `Top: `+r.items.slice(0,3).map(p=>`${p.product_name||("#"+p.product_id)} (${moneyFmt(p.gross_sales)})`).join(", ");
  return "No matching data for that question.";
}
async function askQuery(input, ansEl, isFull, isOrg){
  const q=input.value.trim(); if(!q) return;
  ansEl.innerHTML=`<span class="spin"></span> Thinking…`;
  try{
    const headers=isOrg?adminHdr({"Content-Type":"application/json"}):sellerHdr({"Content-Type":"application/json"});
    const body=isOrg?{question:q,tenant_id:null}:{question:q};
    const res=await jx("/api/v1/nlq",{method:"POST",headers,body:JSON.stringify(body)});
    const f=res.freshness;
    const badge=res.fallback?` <span class="badge stale">simple mode</span>`:(res.fast_path?` <span class="badge fresh"><span class="dotp"></span>instant</span>`:` <span class="badge lock">${res.engine}</span>`);
    const head=isFull?`<div class="lead">${answerText(res)}</div>`:`<div>${answerText(res)}</div>`;
    ansEl.innerHTML=head+
      `<div class="small muted" style="margin-top:8px">${res.interpreted.label}${badge}`+
      (f?` · <span class="badge ${f.status==="FRESH"?"fresh":"stale"}"><span class="dotp"></span>${f.human_readable}</span>`:"")+
      ` · answered in ${res.execution_ms} ms.</div>`;
    if(!isOrg){ state.recent.unshift(q); state.recent=state.recent.slice(0,6); renderRecent(); }
    $("diagSql").textContent=`engine: ${res.engine}  (requested: ${res.provider_requested}${res.fast_path?", fast-path":""}${res.fallback?", fell back":""})\nscope_locked: ${res.scope_locked}\n\n${res.sql}`;
  }catch(e){ ansEl.innerHTML=`<span class="err">Sorry — that didn’t work: ${e.message}</span>`; }
}
function renderRecent(){
  $("recent").innerHTML=state.recent.length? state.recent.map(q=>`<div style="padding:5px 0;border-bottom:1px solid var(--canvas)"><a href="#" data-q="${encodeURIComponent(q)}">${q}</a></div>`).join(""):"None yet.";
  $("recent").querySelectorAll("a").forEach(a=>a.onclick=ev=>{ev.preventDefault(); const q=decodeURIComponent(a.dataset.q); $("q").value=q; askQuery($("q"),$("answer"),true,false);});
}

// ===================== recompute vs Xponent =====================
function cmpBox(kind, d, work){
  const label=kind==="naive"?"Recompute from scratch":"Xponent (rollups)";
  return `<div class="cmpbox ${kind==="naive"?"naive":"xpo"}">`+
    `<div class="cmptag">${ico(kind==="naive"?"refresh":"spark")} ${label}</div>`+
    `<div class="cmptime">${intf(d.ms)}<span class="u">ms</span></div>`+
    `<div class="cmpwork">${work}</div>`+
    `<div class="cmpres">${intf(d.order_count)} orders · ${moneyFmt(d.gross_sales)} gross</div></div>`;
}
function mult(x){ return x>=10?Math.round(x):x.toFixed(1); }
function renderCompare(grid, r){
  const nv=r.naive, xp=r.xponent;
  const speed=(xp.ms>0)?(nv.ms/xp.ms):0;
  // Work gap is the scale-independent truth: rows SCANNED vs rollup rows READ.
  const workRatio=(xp.buckets_read>0)?(nv.rows_scanned/xp.buckets_read):0;
  const workN=r.scope==="org"
    ? `scanned ${intf(nv.rows_scanned)} raw orders across ${intf(r.source_dbs)} source DB(s)`
    : `scanned ${intf(nv.rows_scanned)} raw orders`;
  const workX=`read ${intf(xp.buckets_read)} pre-aggregated rollup row(s)`;
  let verdict;
  if(!r.match){
    verdict=`Results differ — recompute and rollups disagree. This flags real drift; verify before trusting the numbers.`;
  }else{
    // Headline = the work Xponent avoids (always true). Time is secondary:
    // meaningful only once the scan is big enough to cost more than the overhead.
    const head=`${ico("check")} Identical result · Xponent read <span class="big">${mult(workRatio)}× less data</span> (${intf(xp.buckets_read)} rollup rows vs ${intf(nv.rows_scanned)} raw orders)`;
    const tail=speed>=1.3
      ? ` <span class="ok">— and ${mult(speed)}× faster (${intf(nv.ms)} ms → ${intf(xp.ms)} ms)</span>`
      : ` <span class="note">— both are a few ms at this data size; the <b>time</b> gap opens up as rows grow (re-seed larger to see it widen).</span>`;
    verdict=head+tail;
  }
  grid.innerHTML=cmpBox("naive",nv,workN)+cmpBox("xpo",xp,workX)+
    `<div class="cmpwin ${r.match?"":"mismatch"}">${verdict}</div>`;
}
async function runCompare(scope){
  const grid=scope==="org"?$("cmpOrgGrid"):$("cmpSelGrid");
  const msg =scope==="org"?$("cmpOrgMsg"):$("cmpSelMsg");
  const btn =scope==="org"?$("cmpOrgRun"):$("cmpSelRun");
  const [from,to]=rangeDates(state.range);
  btn.disabled=true; msg.innerHTML=`<span class="spin"></span> Running both engines over ${rangeLabel().toLowerCase()}…`;
  try{
    const path=scope==="org"
      ? `/api/v1/org/compare?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`
      : `/api/v1/tenants/${state.tenant}/compare?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
    const r=await jx(path,{headers:scope==="org"?adminHdr():sellerHdr()});
    renderCompare(grid,r);
    msg.textContent=`Over ${rangeLabel().toLowerCase()} · identical numbers · best of ${r.runs||5} warm runs.`;
  }catch(e){ grid.innerHTML=`<div class="err small">${e.message.includes("403")?"Operator key required.":e.message}</div>`; msg.textContent=""; }
  finally{ btn.disabled=false; }
}

// ===================== SELLER: orders CRUD =====================
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
    if(act==="cancel"){ if(!confirm(`Cancel order #${id}?`)) return;
      await jx(`/api/v1/tenants/${state.tenant}/orders/${id}/cancel`,{method:"POST",headers:sellerHdr()}); toast(`Order #${id} cancelled`,"ok"); }
    else if(act==="del"){ if(!confirm(`Delete order #${id}? This cannot be undone.`)) return;
      await jx(`/api/v1/tenants/${state.tenant}/orders/${id}`,{method:"DELETE",headers:sellerHdr()}); toast(`Order #${id} deleted`,"ok"); }
    else if(act==="upd"){ const v=prompt("New amount in paise (e.g. 25000 = ₹250):"); if(!v) return; const amt=parseInt(v,10);
      if(!(amt>0)){ toast("Enter a positive amount","err"); return; }
      await jx(`/api/v1/tenants/${state.tenant}/orders/${id}`,{method:"PATCH",headers:sellerHdr({"Content-Type":"application/json"}),body:JSON.stringify({amount_cents:amt})}); toast(`Order #${id} updated`,"ok"); }
    refreshAfterWrite();
  }catch(e){ toast(act+" failed: "+e.message,"err"); }
}
function refreshAfterWrite(){ loadOrders(); setTimeout(()=>{loadDashboard();},1500); }

// ===================== diagnostics (Receipts) =====================
async function recon(kind){
  const out=$("diagRecon");
  if(!adminKey()){ out.textContent="Enter the operator key in the sidebar to use Receipts."; return; }
  const tid=state.tenant||state.sellerIds[0]||1;
  try{
    if(kind==="verify"){ const r=await jx(`/api/v1/admin/verification?tenant_id=${tid}&run=true&sample=200`,{headers:adminHdr()}); out.textContent="VERIFY: "+JSON.stringify(r.digest_health,null,2); }
    else{ const r=await jx(`/api/v1/admin/rebuild`,{method:"POST",headers:adminHdr({"Content-Type":"application/json"}),body:JSON.stringify({mode:"TARGETED",tenant_id:tid,day_bucket:$("reconDay").value})}); out.textContent="REPAIR: "+JSON.stringify(r,null,2); }
  }catch(e){ out.textContent="Error: "+e.message; }
}

// ===================== sellers + wiring =====================
let sellerDD=null, rangeDD=null;
async function loadSellers(){
  let ids=[];
  try{ const p=await jx("/api/v1/status/pipeline");
    ids=(p.stages?.[0]?.detail||[]).map(s=>s.tenant_id).filter(x=>x!=null);
  }catch{}
  if(!ids.length) ids=[1,2];
  state.sellerIds=[...new Set(ids)].sort((a,b)=>a-b);
  state.tenant=state.sellerIds[0];
  sellerDD=makeDropdown($("sellerDD"), {
    options:state.sellerIds.map(i=>({value:i,label:`Restaurant #${i}`})), value:state.tenant,
    onChange:v=>{ state.tenant=Number(v); syncSeller(); loadDashboard(); if($("sec-orders").classList.contains("on")) loadOrders(); },
  });
  // Restaurant picker on the sign-in screen.
  loginSellerTid=state.sellerIds[0];
  loginSellerDD=makeDropdown($("loginSellerDD"), {
    options:state.sellerIds.map(i=>({value:i,label:`Restaurant #${i}`})), value:loginSellerTid,
    onChange:v=>{ loginSellerTid=Number(v); },
  });
}
let loginSellerDD=null;
function wire(){
  document.querySelectorAll("#roleSeg button").forEach(b=>b.onclick=()=>setMode(b.dataset.role));
  rangeDD=makeDropdown($("rangeDD"), {options:RANGE_OPTS, value:state.range,
    onChange:v=>{ state.range=v; state.mode==="org"?loadOrg():loadDashboard(); }});
  $("diagBtn").onclick=()=>{ const d=$("diag"); d.style.display=d.style.display==="none"?"block":"none"; d.open=true; };
  renderChips($("chips"), $("q"), $("answer"), true);
  renderChips($("chipsMini"), $("qMini"), $("ansMini"), false);
  renderChips($("ochips"), $("oq"), $("oanswer"), true);
  $("run").onclick=()=>askQuery($("q"),$("answer"),true,false);
  $("runMini").onclick=()=>askQuery($("qMini"),$("ansMini"),false,false);
  $("orun").onclick=()=>askQuery($("oq"),$("oanswer"),true,true);
  $("q").addEventListener("keydown",e=>{if(e.key==="Enter")askQuery($("q"),$("answer"),true,false);});
  $("qMini").addEventListener("keydown",e=>{if(e.key==="Enter")askQuery($("qMini"),$("ansMini"),false,false);});
  $("oq").addEventListener("keydown",e=>{if(e.key==="Enter")askQuery($("oq"),$("oanswer"),true,true);});
  $("createOrder").onclick=createOrder;
  $("actRefresh").onclick=loadActivity;
  $("actSim").onclick=simulateActivity;
  $("cmpOrgRun").onclick=()=>runCompare("org");
  $("cmpSelRun").onclick=()=>runCompare("seller");
  $("dbPrev").onclick=()=>{ state.db.offset=Math.max(0,state.db.offset-state.db.limit); loadRows(); };
  $("dbNext").onclick=()=>{ state.db.offset+=state.db.limit; loadRows(); };
  $("verify").onclick=()=>recon("verify");
  $("repair").onclick=()=>recon("repair");
  // --- sign-in gate ---
  $("loginRoles").querySelectorAll("button").forEach(b=>b.onclick=()=>{
    $("loginRoles").querySelectorAll("button").forEach(x=>x.classList.toggle("active", x===b));
    const r=b.dataset.r;
    $("loginOrg").style.display=r==="org"?"flex":"none";
    $("loginSeller").style.display=r==="seller"?"flex":"none";
    $("loginErr").textContent="";
  });
  $("loginOrg").addEventListener("submit", e=>{ e.preventDefault(); signInOrg(($("loginOrgKey").value||"").trim()); });
  $("loginSeller").addEventListener("submit", e=>{ e.preventDefault(); signInSeller(loginSellerTid, ($("loginSellerKey").value||"").trim()); });
  $("signOut").onclick=signOut;
}

(async function init(){
  wire();
  await loadSellers();
  showLogin();   // gate the app behind sign-in; enterApp() reveals the right dashboard
  setInterval(()=>{
    if($("sec-dashboard").classList.contains("on")) loadDashboard();
    else if($("sec-org-overview").classList.contains("on")) loadOrg();
    else if($("sec-org-activity").classList.contains("on")) loadActivity();
  }, 8000);
})();
