const T=document.querySelector('meta[name="board-token"]').content;
const el=id=>document.getElementById(id);
const esc=value=>String(value==null?"":value).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ACTIVE=["star","yes","new","maybe"];
const RUNNING=["evaluating","queued","drafting","reviewing","compiling","inspecting"];
const PHASE_STEP={evaluating:1,awaiting_approval:1,queued:2,drafting:2,reviewing:3,compiling:5,inspecting:6,done:6};
let JOBS=[],STATUSES=[],FILTERS=[],filter="active",q="",sel=0;
let RUNS=[],QUEUE=[],LEDGER=null,BUDGET=null,RUNPOLL=null,ACTIVE_RUN=null;
let EV=[],EPOCH=null,SEQ=0,actFilter="all",COUNTS={};
const HISTORY=[];
let toastTimer=null,undoTimer=null,POLL=null;

const media=matchMedia("(prefers-color-scheme: dark)");
const setTheme=()=>el("app").classList.toggle("dark",media.matches);
setTheme();media.addEventListener("change",setTheme);
el("buildstamp").textContent="build "+new Date(document.lastModified).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});

function toast(msg,opts={}){
  const box=el("toast"),btn=el("toastundo");el("toastmsg").textContent=msg;
  clearTimeout(toastTimer);clearInterval(undoTimer);box.classList.toggle("warn",!!opts.warn);box.classList.add("on");
  if(opts.undo){let left=opts.seconds||6;btn.hidden=false;el("toastsec").textContent="("+left+")";
    undoTimer=setInterval(()=>{left--;el("toastsec").textContent="("+left+")";if(left<=0)clearInterval(undoTimer)},1000);
    toastTimer=setTimeout(()=>{box.classList.remove("on");btn.hidden=true},left*1000);
  }else{btn.hidden=true;toastTimer=setTimeout(()=>box.classList.remove("on"),opts.ms||1400)}
}

const buildFilters=()=>[["active","active"],...STATUSES.map(s=>[s,s]),["all","all"]];
function match(j){
  const inFilter=filter==="all"||filter==="active"&&ACTIVE.includes(j.status)||j.status===filter;
  if(!inFilter)return false;if(!q)return true;
  const hay=[j.title,j.company,j.location,j.why,j.note].join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).every(word=>hay.includes(word));
}
const shown=()=>JOBS.filter(match);
const selectedJob=()=>shown()[sel]||null;

function renderChips(){
  const counts={all:JOBS.length,active:JOBS.filter(j=>ACTIVE.includes(j.status)).length};
  STATUSES.forEach(s=>counts[s]=JOBS.filter(j=>j.status===s).length);
  el("chips").innerHTML=FILTERS.map(([key,label])=>`<button class="chip ${key===filter?"on":""}" data-filter="${esc(key)}">${esc(label)}<span class="n">${counts[key]||0}</span></button>`).join("");
}
function render(){
  renderChips();const rows=shown();if(sel>=rows.length)sel=Math.max(0,rows.length-1);
  el("tb").innerHTML=rows.map((j,i)=>`<tr class="${i===sel?"sel":""}" data-row="${i}">
    <td><span class="fitword ${esc(j.fit)}">${esc(j.fit||"—")}</span></td>
    <td class="role" title="${esc(j.title)}">${esc(j.title)}</td><td class="co">${esc(j.company)}</td>
    <td class="co">${esc(j.location)}</td><td class="co">${esc(j.posted).slice(5)}</td>
    <td><select data-url="${esc(j.url)}">${STATUSES.map(s=>`<option value="${esc(s)}" ${s===j.status?"selected":""}>${esc(s)}</option>`).join("")}</select></td>
    <td class="wide-only why" title="${esc(j.why)}">${esc(j.why)}${j.dupes?.length?'<span class="dupe"> · possible dupe</span>':""}</td>
    <td class="wide-only note" data-note="${esc(j.url)}">${esc(j.note)}</td></tr>`).join("");
  el("empty").hidden=rows.length>0;el("count").textContent=`${rows.length} shown · ${JOBS.length} total`;
  renderJob();document.querySelector("tr.sel")?.scrollIntoView({block:"nearest"});
}
function renderJob(){
  const j=selectedJob(),rows=shown();
  if(!j){el("jobdetail").innerHTML='<div class="panel-empty">Select a job.</div>';el("jobpos").textContent="";return}
  el("jobpos").textContent=`${sel+1} of ${rows.length}`;el("jobopen").href=j.open_url||j.url;
  const statusButtons=["star","yes","maybe","gate","no"].map(s=>`<button class="${j.status===s?"on":""}" data-status="${s}">${s}</button>`).join("");
  el("jobdetail").innerHTML=`<div class="jobsummary"><div class="jobtitle">${esc(j.title)}</div>
    <div class="jobmeta"><strong>${esc(j.company)}</strong><span>·</span><span>${esc(j.location)}</span><span>·</span><span>posted ${esc(j.posted).slice(5)}</span></div>
    <div class="badges"><span class="fitword ${esc(j.fit)}">${esc(j.fit||"unranked")}</span><span class="badge">${esc(j.portal||"source unknown")}</span>${j.score?`<span class="badge">prefit ${esc(j.score)}</span>`:""}</div></div>
    <div class="whybox"><span class="label">Why it is here</span>${esc(j.why||"No prefit reason was stored.")}</div>
    <div class="posting"><span class="label">Posting</span><div class="${j.description?"":"postingempty"}">${esc(j.description||"Posting text is not stored for this row. Open the original posting to read it.")}</div></div>
    <div class="jobactions"><div class="statusbuttons">${statusButtons}</div>
      <input class="noteinput" data-note-input="${esc(j.url)}" value="${esc(j.note)}" placeholder="+ note" aria-label="My note">
      <button class="primary tailor" data-tailor="${esc(j.url)}">✎&nbsp; Tailor CV + cover letter</button>
      <div class="hint">runs /apply in the background · you stay on the board</div></div>`;
}

async function update(url,patch,record=true){
  const j=JOBS.find(x=>x.url===url),prev=j?.status;
  const response=await fetch("/api/update?t="+T,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url,...patch})});
  if(!response.ok){toast("save failed",{warn:true,ms:2500});return false}
  if("status" in patch){if(record&&patch.status!==prev)HISTORY.push({url,prev,title:j.title,company:j.company});j.status=patch.status}
  if("note" in patch)j.note=patch.note;render();pollActivity();return true;
}
async function setStatus(status){
  const j=selectedJob();if(!j)return;const before=j.status;
  if(!await update(j.url,{status}))return;
  if(status==="no")toast((j.company||j.title)+" → no — it will not return in a scrape",{undo:true,seconds:8,warn:true});
  else if(status!==before)toast((j.company||j.title)+" → "+status);
}
async function undo(){
  const last=HISTORY.pop();if(!last){toast("nothing to undo");return}
  if(await update(last.url,{status:last.prev},false))toast("undone: "+(last.company||last.title)+" back to "+last.prev);
}
function editNote(j=selectedJob()){
  if(!j)return;const value=prompt("Note for "+j.company+" — "+j.title,j.note||"");
  if(value!==null)update(j.url,{note:value}).then(ok=>ok&&toast("note saved"));
}

function money(value){return "$"+(Math.round((value||0)*100)/100).toFixed(2)}
function elapsed(run){
  if(!run.started_at)return "";const end=run.ended_at?new Date(run.ended_at):new Date();
  const seconds=Math.max(0,Math.round((end-new Date(run.started_at))/1000));
  if(seconds<90)return seconds+" s";return Math.round(seconds/60)+" min";
}
function activeRuns(){return RUNS.filter(r=>RUNNING.includes(r.phase)||r.phase==="awaiting_approval"||r.phase==="orphaned")}
function renderRuns(){
  const live=activeRuns(),recent=RUNS.filter(r=>!live.includes(r)).slice(0,4),rows=live.concat(recent);
  el("runqueue").textContent=QUEUE.length?QUEUE.length+" queued":(live.length?live.length+" active":"");
  el("runledger").textContent=LEDGER?money(LEDGER.spent_today_usd)+" / "+money(LEDGER.daily_budget_usd):"";
  el("runlist").innerHTML=rows.length?rows.map(r=>{
    const step=PHASE_STEP[r.phase]||1,pct=Math.round(step/6*100),bad=["failed","orphaned"].includes(r.phase);
    return `<div class="runitem ${RUNNING.includes(r.phase)?"live":""} ${ACTIVE_RUN===r.id?"selected":""}" data-run="${esc(r.id)}">
      <div class="runwho"><strong>${esc(r.company)}</strong><span class="runrole">${esc(r.role)}</span></div>
      <div class="runmeta ${bad?"runerror":""}"><span>${esc(r.phase.replaceAll("_"," "))}</span><span>·</span><span>step ${step}/6</span>${elapsed(r)?`<span>·</span><span>${elapsed(r)}</span>`:""}</div>
      ${RUNNING.includes(r.phase)?`<div class="runprogress"><span style="width:${pct}%"></span></div>`:""}</div>`;
  }).join(""):'<div class="panel-empty">No runs yet.</div>';
  const active=live.find(r=>RUNNING.includes(r.phase))||live[0];el("striprunning").textContent=live.length?live.length+" running":"";
  if(active){el("runpill").hidden=false;el("runpill").innerHTML=`<span>Tailoring <strong>${esc(active.company)}</strong> · step ${PHASE_STEP[active.phase]||1} of 6</span><span class="elapsed">${elapsed(active)}</span>`;el("runpill").dataset.run=active.id}
  else el("runpill").hidden=true;
  renderApplications();
  if(el("app").classList.contains("tailor-mode")&&ACTIVE_RUN)renderTailor(RUNS.find(r=>r.id===ACTIVE_RUN));
}
function renderApplications(){
  const rows=RUNS.slice(0,20);el("applicationcount").textContent=`${rows.filter(r=>r.phase==="done").length} drafted · ${activeRuns().length} active`;
  el("applicationlist").innerHTML=rows.length?rows.map(r=>`<tr data-run="${esc(r.id)}"><td><span class="role">${esc(r.company)}</span> <span class="co">· ${esc(r.role)}</span></td><td><span class="phase ${esc(r.phase)}">${esc(r.phase.replaceAll("_"," "))}</span></td><td class="co">${r.cost?.total_usd?money(r.cost.total_usd):"—"}</td><td class="co">${esc((r.started_at||"").slice(0,16).replace("T"," "))}</td><td><button class="linkish">Watch run</button></td></tr>`).join(""):'<tr><td colspan="5" class="panel-empty">No applications yet.</td></tr>';
}
async function pollRuns(){
  let data;try{data=await(await fetch("/api/runs?t="+T)).json()}catch(_){return}
  const before=JSON.stringify(RUNS.map(r=>[r.id,r.phase]));RUNS=data.runs||[];QUEUE=data.queue||[];LEDGER=data.ledger;BUDGET=data.budget_usd;renderRuns();
  if(JSON.stringify(RUNS.map(r=>[r.id,r.phase]))!==before)render();
  const busy=activeRuns().length>0;if(busy&&!RUNPOLL)RUNPOLL=setInterval(pollRuns,2000);if(!busy&&RUNPOLL){clearInterval(RUNPOLL);RUNPOLL=null}
}
async function postRun(path,body){
  const response=await fetch(path+"?t="+T,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
  let data={};try{data=await response.json()}catch(_){}
  if(!response.ok)toast(data.error||("request failed ("+response.status+")"),{warn:true,ms:6000});
  await pollRuns();pollActivity();return response.ok;
}
function startTailor(url){
  const j=JOBS.find(x=>x.url===url);if(!j)return;
  const note=prompt("Anything to tell the drafter about "+j.company+"? (optional — leave empty for none)","");
  if(note===null)return;
  postRun("/api/runs",{job_url:url,kind:"apply",note}).then(ok=>ok&&toast("evaluating — you will be asked before it drafts",{ms:3000}));
}

const STEPS=[
  ["Evaluate fit","Posting fetched and scored against your profile. Pauses for your go-ahead."],
  ["Draft CV + cover letter","Tailors both documents and audits every factual claim."],
  ["Reviewer critique","A second pass attacks grounding first, then style."],
  ["Revise","Structured edits are applied; invented facts are skipped."],
  ["Compile PDFs","Runs the registered document toolchains and page checks."],
  ["Verify","ATS text extraction and the final verification checklist."]
];
function renderTailor(run){
  if(!run){restoreWorkspace();return}ACTIVE_RUN=run.id;const current=PHASE_STEP[run.phase]||1,fit=run.fit||{};
  const steps=STEPS.map((s,i)=>{const n=i+1,state=n<current||run.phase==="done"?"done":n===current?"live":"todo";return `<div class="step ${state}"><div class="steprow"><span class="stepdot">${n}</span><div><div class="steptitle">${s[0]}</div><div class="stepdetail">${s[1]}</div></div><span class="steptime">${state==="live"?esc(run.phase.replaceAll("_"," ")):""}</span></div></div>`}).join("");
  const matches=(fit.matches||[]).map(x=>`<div>${esc(x)}</div>`).join("")||"<div>No structured match list yet.</div>";
  const gaps=(fit.gaps||[]).map(x=>`<div>${esc(x)}</div>`).join("")||"<div>No structured gap list yet.</div>";
  const logs=EV.filter(e=>e.source==="claude"||e.source==="latex"||e.source==="verify").slice(-100).map(e=>`<div><span>${esc((e.ts||"").slice(11,19))}</span>&nbsp; ${esc(e.cmd?"$ "+e.cmd:e.msg)}</div>`).join("")||"<div>Waiting for run activity…</div>";
  el("tailor-view").innerHTML=`<div class="tailor-shell"><section class="pipeline"><div class="panelhead"><span class="label">Pipeline</span><span class="spacer"></span><span class="dim">step ${current} of 6</span></div>${steps}<div class="writing"><div class="label">Writing to</div><div>${esc(run.targets?.cv||"CV target pending")}</div><div>${esc(run.targets?.cover||"Cover-letter target pending")}</div></div></section>
    <section class="runoutput"><div class="panelhead"><span class="label">Run output</span><span class="spacer"></span><span class="phase ${esc(run.phase)}">${esc(run.phase.replaceAll("_"," "))}</span></div>
      <div class="fitcard"><div class="fithead"><span class="fitword ${fit.overall>=70?"high":fit.overall>=50?"medium":"low"}">✓</span><strong>Fit evaluation — ${esc(fit.verdict||"pending")}${fit.overall!=null?", "+esc(fit.overall):""}</strong><span class="spacer"></span><span class="dim">${run.phase==="awaiting_approval"?"waiting for your approval":""}</span></div>
      <div class="fitgrid"><div><span class="label">Matches</span>${matches}</div><div><span class="label">Gaps, stated not smoothed</span>${gaps}</div></div></div>
      <div class="runlog">${logs}</div><div class="runfooter"><button class="secondary" data-restore>Back to board</button><span class="dim">Closing this panel does not stop the run.</span><span class="spacer"></span>
      ${run.phase==="awaiting_approval"?`<button class="primary approve" data-run-id="${esc(run.id)}" data-phase="${esc(run.phase)}">Draft it — stops at about ${money(BUDGET?.pass_b)}</button>`:""}${RUNNING.includes(run.phase)?`<button class="secondary cancelrun" data-run-id="${esc(run.id)}">Cancel run</button>`:""}</div></section></div>`;
  openView("tailor",run.company+" · "+run.role);
}

function openView(kind,title=""){
  const app=el("app");app.classList.remove("expanded-board","expanded-applications","expanded-job","tailor-mode");app.classList.add("expanded",kind==="tailor"?"tailor-mode":"expanded-"+kind);
  el("tailor-view").hidden=kind!=="tailor";
  layout.expanded=kind;saveLayout();el("restore").hidden=false;el("brand").textContent=kind==="tailor"?(RUNS.find(r=>r.id===ACTIVE_RUN)?.company||"Tailoring"):"JobFlow";el("local").textContent=title||kind;
}
function restoreWorkspace(){
  const app=el("app");app.classList.remove("expanded","expanded-board","expanded-applications","expanded-job","tailor-mode");el("tailor-view").hidden=true;layout.expanded=null;saveLayout();el("restore").hidden=true;el("brand").textContent="JobFlow";el("local").textContent="local · 127.0.0.1:8765";
}

// Layout state: user collapses and automatic narrow-window collapses stay distinct.
const LAYOUT_KEY="jobflow.layout.v1";
const DEFAULT_LAYOUT={version:1,left:264,right:452,collect:300,applications:208,leftCollapsed:false,rightCollapsed:false,autoLeft:false,autoRight:false,expanded:null};
function loadLayout(){try{const value=JSON.parse(localStorage.getItem(LAYOUT_KEY));if(value?.version===1)return {...DEFAULT_LAYOUT,...value};localStorage.removeItem(LAYOUT_KEY)}catch(_){try{localStorage.removeItem(LAYOUT_KEY)}catch(__){}}return {...DEFAULT_LAYOUT}}
let layout=loadLayout();
function saveLayout(){try{localStorage.setItem(LAYOUT_KEY,JSON.stringify(layout))}catch(_){}}
function applyLayout(){
  const app=el("app");app.style.setProperty("--left",layout.left+"px");app.style.setProperty("--right",layout.right+"px");app.style.setProperty("--collect",layout.collect+"px");app.style.setProperty("--applications",layout.applications+"px");
  app.classList.toggle("left-collapsed",layout.leftCollapsed||layout.autoLeft);app.classList.toggle("right-collapsed",layout.rightCollapsed||layout.autoRight);
  el("left-rail").classList.toggle("collapsed",layout.leftCollapsed||layout.autoLeft);el("right-rail").classList.toggle("collapsed",layout.rightCollapsed||layout.autoRight);
  document.querySelector('[data-collapse="left"]').setAttribute("aria-expanded",String(!(layout.leftCollapsed||layout.autoLeft)));document.querySelector('[data-collapse="right"]').setAttribute("aria-expanded",String(!(layout.rightCollapsed||layout.autoRight)));
  updateSeparatorAria();
}
function toggleCollapse(side){
  const key=side+"Collapsed",auto="auto"+side[0].toUpperCase()+side.slice(1);layout[key]=!layout[key];layout[auto]=false;applyLayout();saveLayout();autoCollapse();
}
function resetLayout(){layout={...DEFAULT_LAYOUT};applyLayout();autoCollapse();saveLayout();toast("layout reset")}
function autoCollapse(){
  const width=el("workspace").clientWidth||innerWidth;
  layout.autoRight=!layout.rightCollapsed&&width<layout.left+layout.right+482;
  layout.autoLeft=!layout.leftCollapsed&&width<(layout.rightCollapsed||layout.autoRight?28:layout.right)+layout.left+482;
  applyLayout();saveLayout();
}
function updateSeparatorAria(){
  const specs={"left-split":[layout.left,200,420],"right-split":[layout.right,320,640],"left-row-split":[layout.collect,120,Math.max(120,el("left-rail").clientHeight-120)],"centre-row-split":[layout.applications,96,Math.max(96,el("centre").clientHeight-200)]};
  Object.entries(specs).forEach(([id,[now,min,max]])=>{const node=el(id);node.setAttribute("aria-valuenow",Math.round(now));node.setAttribute("aria-valuemin",min);node.setAttribute("aria-valuemax",Math.round(max))});
}
const splitSpecs={
  "left-split":{key:"left",axis:"x",sign:1,min:200,max:420,def:264},
  "right-split":{key:"right",axis:"x",sign:-1,min:320,max:640,def:452},
  "left-row-split":{key:"collect",axis:"y",sign:1,min:120,def:300,max:()=>Math.max(120,el("left-rail").clientHeight-120)},
  "centre-row-split":{key:"applications",axis:"y",sign:-1,min:96,def:208,max:()=>Math.max(96,el("centre").clientHeight-200)}
};
function clampSplit(spec,value){
  let max=typeof spec.max==="function"?spec.max():spec.max;
  if(spec.key==="left"){const right=layout.rightCollapsed||layout.autoRight?28:layout.right;max=Math.min(max,el("workspace").clientWidth-right-482)}
  if(spec.key==="right"){const left=layout.leftCollapsed||layout.autoLeft?28:layout.left;max=Math.min(max,el("workspace").clientWidth-left-482)}
  return Math.max(spec.min,Math.min(max,value));
}
Object.entries(splitSpecs).forEach(([id,spec])=>{
  const node=el(id);node.addEventListener("pointerdown",event=>{event.preventDefault();node.setPointerCapture(event.pointerId);node.classList.add("dragging");const start=event[spec.axis==="x"?"clientX":"clientY"],before=layout[spec.key];
    const move=e=>{layout[spec.key]=clampSplit(spec,before+(e[spec.axis==="x"?"clientX":"clientY"]-start)*spec.sign);applyLayout()};
    const up=()=>{node.classList.remove("dragging");node.removeEventListener("pointermove",move);saveLayout()};
    node.addEventListener("pointermove",move);node.addEventListener("pointerup",up,{once:true});node.addEventListener("pointercancel",up,{once:true});
  });
  node.addEventListener("dblclick",()=>{layout[spec.key]=spec.def;applyLayout();saveLayout()});
  node.addEventListener("keydown",event=>{const vertical=spec.axis==="x",minus=vertical?"ArrowLeft":"ArrowUp",plus=vertical?"ArrowRight":"ArrowDown";
    if(event.key==="Home"){event.preventDefault();layout[spec.key]=spec.def}
    else if(event.key===minus||event.key===plus){event.preventDefault();layout[spec.key]=clampSplit(spec,layout[spec.key]+(event.key===plus?1:-1)*(event.shiftKey?64:16))}
    else return;applyLayout();saveLayout();
  });
});
applyLayout();autoCollapse();addEventListener("resize",autoCollapse);

// Fetching
const chosenSources=()=>["ats","freehire","linkedin"].filter(s=>el("s-"+s).checked);
async function reloadJobs(){const data=await(await fetch("/api/jobs?t="+T)).json();JOBS=data.jobs;STATUSES=data.statuses;FILTERS=buildFilters();render()}
function renderFetchLog(status){
  const lines=status.log||[];el("collectlog").innerHTML=lines.length?lines.slice(-12).map(line=>`<div class="${/403|error|failed/i.test(line)?"bad":""}">${esc(line)}</div>`).join(""):'<span class="dim">Collection is running…</span>';
}
async function pollFetch(){
  const status=await(await fetch("/api/fetch/status?t="+T)).json();renderFetchLog(status);pollActivity();
  if(status.running)return;clearInterval(POLL);POLL=null;el("fetch").disabled=false;await reloadJobs();
  const failed=(status.sources||[]).flatMap(x=>x.failed||[]),banner=el("degraded");
  if(failed.length){banner.hidden=false;banner.textContent="Degraded run: "+failed.join(", ")+" did not answer. Everything else was kept."}else banner.hidden=true;
  el("collectlast").textContent=status.finished_at?"last run "+String(status.finished_at).slice(11,16):"";
}
el("fetch").addEventListener("click",async()=>{
  const sources=chosenSources();if(!sources.length){toast("select at least one source",{warn:true});return}
  el("fetch").disabled=true;const body={sources,max_companies:+el("mc").value||8,max_new_jobs:+el("mn").value||40,linkedin_detail_fetches:+el("md").value};
  const response=await fetch("/api/fetch?t="+T,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!response.ok){el("fetch").disabled=false;toast((await response.json()).error||"could not start",{warn:true});return}
  POLL=setInterval(pollFetch,2000);pollFetch();
});

// Activity strip and drawer
async function pollActivity(){
  const url="/api/activity?t="+T+"&since="+SEQ+(EPOCH?"&epoch="+EPOCH:"");let data;try{data=await(await fetch(url)).json()}catch(_){return}
  if(data.reset||EPOCH===null)EV=[];EPOCH=data.epoch;SEQ=data.seq;COUNTS=data.counts||{};if(data.events?.length)EV=EV.concat(data.events).slice(-500);
  renderStrip();if(!el("drawer").hidden)renderActivity();if(el("app").classList.contains("tailor-mode")&&ACTIVE_RUN)renderTailor(RUNS.find(r=>r.id===ACTIVE_RUN));
}
function renderStrip(){
  const last=EV.at(-1);el("stripcount").textContent=COUNTS.all||0;if(!last)return;
  el("striptag").textContent=last.source;el("striptag").className="tag "+last.source;el("striptime").textContent=(last.ts||"").slice(11,19);el("stripmsg").textContent=last.cmd?"$ "+last.cmd:last.msg;el("stripdur").textContent=last.ms!=null?(last.ms>=1000?(last.ms/1000).toFixed(2)+" s":last.ms+" ms"):"";
}
function renderActivity(){
  const cats=["all","collect","board","claude","latex","verify","errors"];el("actchips").innerHTML=cats.filter(c=>c==="all"||COUNTS[c]).map(c=>`<button class="chip ${c===actFilter?"on":""}" data-act-filter="${c}">${c}<span class="n">${COUNTS[c]||0}</span></button>`).join("");
  const rows=EV.filter(e=>actFilter==="all"||actFilter==="errors"&&e.level==="error"||e.source===actFilter);
  el("actlog").innerHTML=rows.map(e=>`<div class="ln ${esc(e.level)}"><span class="t">${esc((e.ts||"").slice(11,19))}</span><span class="s">${esc(e.source)}</span><span class="m">${esc(e.cmd?"$ "+e.cmd:e.msg)}${e.ms!=null?"  "+e.ms+" ms":""}</span></div>`).join("");
  if(el("follow").checked)el("actlog").scrollTop=el("actlog").scrollHeight;
}
function toggleDrawer(){el("drawer").hidden=!el("drawer").hidden;el("stripcaret").textContent=el("drawer").hidden?"⌃":"⌄";if(!el("drawer").hidden)renderActivity()}
function copyLog(){const text=EV.slice(-200).map(e=>[(e.ts||"").slice(11,19),e.source,e.cmd?"$ "+e.cmd:e.msg].join("  ")).join("\n");navigator.clipboard.writeText(text).then(()=>toast("copied "+Math.min(EV.length,200)+" lines"),()=>toast("could not copy",{warn:true}))}

document.addEventListener("click",event=>{
  if(event.target.closest("#toastundo"))return void undo();
  if(event.target.closest("#striptoggle"))return void toggleDrawer();
  if(event.target.closest("#copylog"))return void copyLog();
  if(event.target.closest("#restore,[data-restore]"))return void restoreWorkspace();
  const collapse=event.target.closest("[data-collapse]");if(collapse)return void toggleCollapse(collapse.dataset.collapse);
  const expand=event.target.closest("[data-expand]");if(expand){const kind=expand.dataset.expand;if(kind==="board"||kind==="applications"||kind==="job")openView(kind);return}
  const chip=event.target.closest("[data-filter]");if(chip){filter=chip.dataset.filter;sel=0;render();return}
  const act=event.target.closest("[data-act-filter]");if(act){actFilter=act.dataset.actFilter;renderActivity();return}
  const status=event.target.closest("[data-status]");if(status)return void setStatus(status.dataset.status);
  const note=event.target.closest("[data-note]");if(note)return void editNote(JOBS.find(j=>j.url===note.dataset.note));
  const tailor=event.target.closest("[data-tailor]");if(tailor)return void startTailor(tailor.dataset.tailor);
  const runNode=event.target.closest("[data-run]");if(runNode){const run=RUNS.find(r=>r.id===runNode.dataset.run);if(run)renderTailor(run);return}
  const pill=event.target.closest("#runpill");if(pill){const run=RUNS.find(r=>r.id===pill.dataset.run);if(run)renderTailor(run);return}
  const approve=event.target.closest(".approve");if(approve){approve.disabled=true;postRun("/api/runs/"+approve.dataset.runId+"/approve",{phase:approve.dataset.phase});return}
  const cancel=event.target.closest(".cancelrun");if(cancel){postRun("/api/runs/"+cancel.dataset.runId+"/cancel");return}
  const row=event.target.closest("tr[data-row]");if(row&&!event.target.closest("select")){sel=+row.dataset.row;render()}
});
document.addEventListener("change",event=>{
  if(event.target.matches("select[data-url]"))update(event.target.dataset.url,{status:event.target.value});
  if(event.target.matches("[data-note-input]"))update(event.target.dataset.noteInput,{note:event.target.value}).then(ok=>ok&&toast("note saved"));
});
document.addEventListener("keydown",event=>{
  const typing=/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
  if(event.key==="/"&&!typing){event.preventDefault();el("q").focus();return}
  if(typing){if(event.key==="Escape")event.target.blur();return}
  if(event.key==="Escape"&&!el("drawer").hidden){toggleDrawer();return}
  if(event.key==="Escape"&&el("app").classList.contains("expanded")){restoreWorkspace();return}
  if(event.key==="["){event.preventDefault();toggleCollapse("left");return}
  if(event.key==="]"){event.preventDefault();toggleCollapse("right");return}
  if(event.key==="\\"){event.preventDefault();resetLayout();return}
  const rows=shown(),move={j:1,ArrowDown:1,k:-1,ArrowUp:-1};
  if(event.key in move){event.preventDefault();sel=Math.min(rows.length-1,Math.max(0,sel+move[event.key]));render();return}
  if(event.key==="Enter"){const j=selectedJob();if(j)open(j.open_url||j.url,"_blank","noopener");return}
  if(event.key==="e"){event.preventDefault();editNote();return}if(event.key==="t"){event.preventDefault();const j=selectedJob();if(j)startTailor(j.url);return}
  if(event.key==="z"){event.preventDefault();undo();return}
  const map={s:"star",y:"yes",m:"maybe",n:"no",a:"applied",u:"new",g:"gate"};if(event.key in map){event.preventDefault();setStatus(map[event.key])}
});
el("q").addEventListener("input",event=>{q=event.target.value;sel=0;render()});

setInterval(pollActivity,3000);setInterval(pollRuns,6000);
Promise.all([reloadJobs(),pollActivity(),pollRuns()]).then(()=>{
  if(layout.expanded==="board"||layout.expanded==="applications"||layout.expanded==="job")openView(layout.expanded);
  else if(layout.expanded==="tailor"&&activeRuns()[0])renderTailor(activeRuns()[0]);
  else if(layout.expanded){layout.expanded=null;saveLayout()}
  fetch("/api/fetch/status?t="+T).then(r=>r.json()).then(status=>{renderFetchLog(status);if(status.running){el("fetch").disabled=true;POLL=setInterval(pollFetch,2000)}});
});
