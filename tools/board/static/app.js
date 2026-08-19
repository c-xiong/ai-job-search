const T = document.querySelector('meta[name="board-token"]').content;
let JOBS=[], STATUSES=[], FILTERS=[], filter="active", q="", sel=0;
const ACTIVE=["star","yes","new","maybe"];
// The filter chips are the status vocabulary itself, verbatim: one chip per status
// in jobs_md.STATUSES, in the same order and spelled with the same word the row's
// dropdown shows. A chip with a prettier private name ("Excluded" for the `no`
// status, "Gated" for `gate`) reads like a separate concept - you press `n` and land
// in a bucket that never says `no` anywhere. A hand-written chip list also silently
// drops statuses added later, which is how `expired` ended up with no chip at all.
// `active` and `all` are the only extras, and they are groups, not statuses.
const buildFilters = () => [["active","active"], ...STATUSES.map(s=>[s,s]), ["all","all"]];

const esc = s => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const el = id => document.getElementById(id);
// Status changes write through immediately - no modal, no confirm - so `n`
// (which hides a job from every future scrape) needs a way back. Undo is kept
// available after the toast fades: the toast is the nudge, `z` is the guarantee.
const HISTORY=[];
let toastTimer=null, undoTimer=null;

function toast(msg, opts){
  opts = opts || {};
  const t=el("toast"), btn=el("toastundo");
  el("toastmsg").textContent=msg;
  clearTimeout(toastTimer); clearInterval(undoTimer);
  t.classList.toggle("warn", !!opts.warn);
  t.classList.add("on");
  if(opts.undo){
    let left=opts.seconds||6;
    btn.hidden=false; el("toastsec").textContent="("+left+")";
    undoTimer=setInterval(()=>{ left--; el("toastsec").textContent="("+left+")";
      if(left<=0) clearInterval(undoTimer); },1000);
    toastTimer=setTimeout(()=>{ t.classList.remove("on"); btn.hidden=true; },(opts.seconds||6)*1000);
  } else {
    btn.hidden=true;
    toastTimer=setTimeout(()=>t.classList.remove("on"), opts.ms||1200);
  }
}

function match(j){
  const inFilter = filter==="all" ? true
    : filter==="active" ? ACTIVE.includes(j.status) : j.status===filter;
  if(!inFilter) return false;
  if(!q) return true;
  const hay=(j.title+" "+j.company+" "+j.location+" "+j.why+" "+j.note).toLowerCase();
  return q.toLowerCase().split(/\s+/).every(w=>hay.includes(w));
}
const shown = () => JOBS.filter(match);

function renderChips(){
  const counts={all:JOBS.length, active:JOBS.filter(j=>ACTIVE.includes(j.status)).length};
  for(const s of STATUSES) counts[s]=JOBS.filter(j=>j.status===s).length;
  el("chips").innerHTML = FILTERS.map(([k,label])=>
    `<button class="chip ${k===filter?"on":""}" data-f="${k}">${label}<span class="n">${counts[k]||0}</span></button>`
  ).join("");
}

function render(){
  renderChips();
  const rows=shown();
  if(sel>=rows.length) sel=Math.max(0,rows.length-1);
  el("tb").innerHTML = rows.map((j,i)=>`
    <tr class="${i===sel?"sel":""}" data-i="${i}">
      <td><span class="fit ${j.fit}">${esc(j.fit)}</span></td>
      <td class="role">${esc(j.title)}</td>
      <td class="co">${esc(j.company)}</td>
      <td class="co">${esc(j.location)}</td>
      <td class="co">${esc(j.posted).slice(5)}</td>
      <td><select data-url="${esc(j.url)}">${STATUSES.map(s=>
          `<option value="${s}" ${s===j.status?"selected":""}>${s}</option>`).join("")}</select></td>
      <td class="why" title="${esc(j.why)}">${esc(j.why)}${j.dupes && j.dupes.length
          ? `<span class="dupe" title="possible duplicate of: ${esc(j.dupes.join(", "))}">· possible dupe</span>` : ""}</td>
      <td class="note" data-url="${esc(j.url)}" title="click to edit">${esc(j.note)}</td>
      <td><a class="open" href="${esc(j.open_url || j.url)}" target="_blank" rel="noopener">open ↗</a></td>
    </tr>`).join("");
  el("empty").hidden = rows.length>0;
  el("count").textContent = `${rows.length} shown · ${JOBS.length} total`;
  const s=document.querySelector("tr.sel"); if(s) s.scrollIntoView({block:"nearest"});
}

async function update(url, patch, record){
  const j = JOBS.find(x=>x.url===url);
  const prev = j ? j.status : null;
  const r = await fetch("/api/update?t="+T, {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({url, ...patch})});
  if(!r.ok){ toast("save failed", {warn:true, ms:2500}); return false; }
  if("status" in patch){
    if(record!==false && patch.status!==prev)
      HISTORY.push({url, prev, title:j.title, company:j.company});
    j.status=patch.status;
  }
  if("note" in patch) j.note=patch.note;
  render(); pollActivity();
  return true;
}

async function setStatus(s){
  const j=shown()[sel];
  if(!j) return;
  const label = (j.company||j.title||"job");
  const wasStatus = j.status;
  if(!await update(j.url,{status:s})) return;
  if(s==="no")
    // The only status that removes a job from every future scrape.
    toast(label+" -> no - it will not come back in a scrape", {undo:true, seconds:8, warn:true});
  else if(s!==wasStatus)
    toast(label+" -> "+s);
}

async function undo(){
  const last = HISTORY.pop();
  if(!last){ toast("nothing to undo", {ms:1400}); return; }
  if(await update(last.url,{status:last.prev}, false))
    toast("undone: "+(last.company||last.title)+" back to "+last.prev);
}
function editNote(){ const j=shown()[sel]; if(!j) return;
  const v=prompt("Note for "+j.company+" — "+j.title, j.note||"");
  if(v!==null) update(j.url,{note:v}).then(ok=>{ if(ok) toast("note saved"); }); }

document.addEventListener("click", e=>{
  if(e.target.closest("#toastundo")){ undo(); return; }
  if(e.target.closest("#striptoggle")){ toggleDrawer(); return; }
  if(e.target.closest("#copylog")){ copyLog(); return; }
  const actchip=e.target.closest("#actchips .chip");
  if(actchip){ actFilter=actchip.dataset.f; renderActivity(); return; }
  const chip=e.target.closest("#chips .chip");
  if(chip){ filter=chip.dataset.f; sel=0; render(); return; }
  const note=e.target.closest(".note");
  if(note){ const j=JOBS.find(x=>x.url===note.dataset.url);
    const v=prompt("Note for "+j.company+" — "+j.title, j.note||"");
    if(v!==null) update(j.url,{note:v}).then(ok=>{ if(ok) toast("note saved"); }); return; }
  const tr=e.target.closest("tr[data-i]");
  if(tr && !e.target.closest("a")){ sel=+tr.dataset.i; render(); }
});
document.addEventListener("change", e=>{
  if(e.target.tagName==="SELECT" && e.target.dataset.url)
    update(e.target.dataset.url,{status:e.target.value});
});
document.addEventListener("keydown", e=>{
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if(e.key==="/" && !typing){ e.preventDefault(); el("q").focus(); return; }
  if(typing){ if(e.key==="Escape") e.target.blur(); return; }
  if(e.key==="Escape" && !el("drawer").hidden){ toggleDrawer(); return; }
  const rows=shown();
  const K={j:1,ArrowDown:1,k:-1,ArrowUp:-1};
  if(e.key in K){ e.preventDefault(); sel=Math.min(rows.length-1,Math.max(0,sel+K[e.key])); render(); return; }
  if(e.key==="Enter"){ const j=rows[sel]; if(j) window.open(j.open_url||j.url,"_blank","noopener"); return; }
  if(e.key==="e"){ e.preventDefault(); editNote(); return; }
  if(e.key==="z"){ e.preventDefault(); undo(); return; }
  const M={s:"star",y:"yes",m:"maybe",n:"no",a:"applied",u:"new",g:"gate"};
  if(e.key in M){ e.preventDefault(); setStatus(M[e.key]); }
});
el("q").addEventListener("input", e=>{ q=e.target.value; sel=0; render(); });

// ---------------------------------------------------------------- fetching
// One button drives every source. The run happens in a background thread on the
// server and this polls a status endpoint, rather than holding an HTTP request
// open for the two minutes a five-vendor sweep can take.
let POLL=null;

const chosenSources = () =>
  ["ats","freehire","linkedin"].filter(s=>el("s-"+s).checked);

function fstat(msg, bad){
  const e=el("fstat");
  e.textContent=msg||""; e.classList.toggle("bad", !!bad);
}

async function reloadJobs(){
  const d = await (await fetch("/api/jobs?t="+T)).json();
  JOBS=d.jobs; STATUSES=d.statuses; FILTERS=buildFilters(); render();
}

async function pollFetch(){
  const s = await (await fetch("/api/fetch/status?t="+T)).json();
  pollActivity();
  if(s.running){
    const tail=(s.log||[]).slice(-1)[0]||"working…";
    fstat(tail.replace(/^\d{4}-\d\d-\d\d \d\d:\d\d\s+/,""));
    return;
  }
  clearInterval(POLL); POLL=null;
  el("fetch").disabled=false;
  await reloadJobs();
  const banner=el("degraded");
  const failed=(s.sources||[]).flatMap(x=>x.failed||[]);
  if(s.error){ fstat(s.error, true); }
  else{
    const added=(s.sources||[]).reduce((n,x)=>n+(x.added||0),0);
    const gated=(s.sources||[]).reduce((n,x)=>n+(x.gated||0),0);
    fstat(added+" new · "+gated+" auto-gated on German");
  }
  if(failed.length){
    banner.hidden=false;
    banner.textContent="Degraded run: "+failed.join(", ")+
      " did not answer. Their results are missing, everything else was kept, and they are retried first next time.";
  } else { banner.hidden=true; }
}

el("fetch").addEventListener("click", async ()=>{
  const sources=chosenSources();
  if(!sources.length){ fstat("select at least one source", true); return; }
  const btn=el("fetch");
  btn.disabled=true; fstat("starting…");
  const body={sources,
    max_companies:+el("mc").value||8,
    max_new_jobs:+el("mn").value||40,
    linkedin_detail_fetches:+el("md").value};
  const r=await fetch("/api/fetch?t="+T,{method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  if(!r.ok){ btn.disabled=false; fstat((await r.json()).error||"could not start", true); return; }
  POLL=setInterval(pollFetch, 2000);
  pollFetch();
});

// ---------------------------------------------------------------- activity
// What the system just did, with the command and the exit code. `(epoch, seq)`
// is what lets this survive a server restart: a new epoch means the counter
// reset, so the client takes the whole ring instead of silently suppressing
// every event whose seq looks older than the one it remembers.
let EV=[], EPOCH=null, SEQ=0, actFilter="all", COUNTS={};

async function pollActivity(){
  const url = "/api/activity?t="+T+"&since="+SEQ+(EPOCH?"&epoch="+EPOCH:"");
  let d;
  try { d = await (await fetch(url)).json(); } catch(_) { return; }
  if(d.reset || EPOCH===null){ EV=[]; }
  EPOCH=d.epoch; SEQ=d.seq; COUNTS=d.counts||{};
  if(d.events && d.events.length){
    EV = EV.concat(d.events);
    if(EV.length>500) EV = EV.slice(-500);
  }
  renderStrip();
  if(!el("drawer").hidden) renderActivity();
}

function renderStrip(){
  const last = EV[EV.length-1];
  el("stripcount").textContent = COUNTS.all || 0;
  if(!last) return;
  el("striptag").textContent = last.source;
  el("striptag").className = "tag "+last.source;
  el("striptime").textContent = (last.ts||"").slice(11,19);
  el("stripmsg").textContent = last.cmd ? "$ "+last.cmd : last.msg;
  el("stripdur").textContent = last.ms!=null ? (last.ms>=1000
    ? (last.ms/1000).toFixed(2)+" s" : last.ms+" ms") : "";
  el("strip").classList.toggle("err", last.level==="error");
}

function renderActivity(){
  const cats = ["all","collect","board","claude","latex","verify","errors"];
  el("actchips").innerHTML = cats
    .filter(c => c==="all" || COUNTS[c])
    .map(c=>`<button class="chip ${c===actFilter?"on":""}" data-f="${c}">${c}<span class="n">${COUNTS[c]||0}</span></button>`)
    .join("");
  const rows = EV.filter(e => actFilter==="all" ? true
    : actFilter==="errors" ? e.level==="error" : e.source===actFilter);
  el("actlog").innerHTML = rows.map(e=>{
    const head = `<div class="ln ${e.level} ${e.cmd?"cmd":""}">`
      + `<span class="t">${esc((e.ts||"").slice(11,19))}</span>`
      + `<span class="s ${e.source}">${esc(e.source)}</span>`
      + `<span class="m">${esc(e.cmd ? "$ "+e.cmd : e.msg)}`
      + (e.exit!=null && !e.cmd ? "" : "")
      + (e.ms!=null ? `   ${e.ms} ms` : "") + `</span></div>`;
    const sub = (e.detail||[]).map(d=>`<div class="ln sub"><span class="m">${esc(d)}</span></div>`).join("");
    const tail = e.cmd && e.msg ? `<div class="ln ${e.level}"><span class="t"></span>`
      + `<span class="s"></span><span class="m">${esc(e.msg)}</span></div>` : "";
    return head + tail + sub;
  }).join("");
  if(el("follow").checked) el("actlog").scrollTop = el("actlog").scrollHeight;
}

function toggleDrawer(){
  const d=el("drawer");
  d.hidden = !d.hidden;
  el("stripcaret").textContent = d.hidden ? "⌃" : "⌄";
  if(!d.hidden) renderActivity();
}

function copyLog(){
  const text = EV.slice(-200).map(e =>
    [(e.ts||"").slice(11,19), e.source, e.cmd ? "$ "+e.cmd : e.msg,
     e.exit!=null ? "exit="+e.exit : "", e.ms!=null ? e.ms+"ms" : ""]
    .filter(Boolean).join("  ")).join("\n");
  navigator.clipboard.writeText(text).then(
    ()=>toast("copied "+Math.min(EV.length,200)+" lines"),
    ()=>toast("could not copy", {warn:true}));
}

setInterval(pollActivity, 3000);

fetch("/api/jobs?t="+T).then(r=>r.json()).then(d=>{
  JOBS=d.jobs; STATUSES=d.statuses; FILTERS=buildFilters(); render();
  pollActivity();
  // A fetch started before this page loaded may still be running.
  fetch("/api/fetch/status?t="+T).then(r=>r.json()).then(s=>{
    if(s.running){ el("fetch").disabled=true; POLL=setInterval(pollFetch,2000); }
  });
});
