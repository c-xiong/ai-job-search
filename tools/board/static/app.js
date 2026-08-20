const T=document.querySelector('meta[name="board-token"]').content;
const el=id=>document.getElementById(id);
const esc=value=>String(value==null?"":value).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ACTIVE=["star","yes","new","maybe"];
const RUNNING=["evaluating","queued","drafting","reviewing","compiling","inspecting"];
const PHASE_STEP={evaluating:1,awaiting_approval:1,queued:2,drafting:2,reviewing:3,compiling:5,inspecting:6,done:6};
let JOBS=[],STATUSES=[],FILTERS=[],filter="active",q="",sel=0;
let RUNS=[],QUEUE=[],LEDGER=null,BUDGET=null,RUNPOLL=null,ACTIVE_RUN=null;
let PREVIEW_RUN=null;
let REVISE_RUN=null;
let COMPANIES=null,COMPANY_FILTER="all",COMPANY_SELECTED=null,SUGGESTIONS=[];
let EV=[],EPOCH=null,SEQ=0,actFilter="all",COUNTS={};
const HISTORY=[];
let toastTimer=null,undoTimer=null,POLL=null;
let MODAL_RESOLVE=null;

// Theme: Auto -> Light -> Dark -> Auto. Auto is the old behaviour (follow the
// OS) and stays the default; only an explicit choice is persisted. Kept out of
// the layout blob so resetting the layout does not flip the user's theme.
const media=matchMedia("(prefers-color-scheme: dark)");
const THEME_KEY="jobflow.theme.v1";
const THEMES=[
  {mode:"system",name:"Auto",glyph:"\u25d0",hint:"Theme follows the system \u2014 click for light"},
  {mode:"light",name:"Light",glyph:"\u2600",hint:"Light theme \u2014 click for dark"},
  {mode:"dark",name:"Dark",glyph:"\u263e",hint:"Dark theme \u2014 click to follow the system"}
];
let themeMode=(()=>{try{const saved=localStorage.getItem(THEME_KEY);return THEMES.some(t=>t.mode===saved)?saved:"system"}catch(_){return "system"}})();
function applyTheme(){
  const spec=THEMES.find(t=>t.mode===themeMode)||THEMES[0];
  el("app").classList.toggle("dark",themeMode==="dark"||(themeMode==="system"&&media.matches));
  el("theme-glyph").textContent=spec.glyph;el("theme-name").textContent=spec.name;
  el("theme-toggle").title=spec.hint;el("theme-toggle").setAttribute("aria-label",spec.hint);
}
function cycleTheme(){
  themeMode=THEMES[(THEMES.findIndex(t=>t.mode===themeMode)+1)%THEMES.length].mode;
  try{localStorage.setItem(THEME_KEY,themeMode)}catch(_){}
  applyTheme();
}
applyTheme();media.addEventListener("change",()=>{if(themeMode==="system")applyTheme()});

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
const SOURCE_LABELS={"linkedin-search":"LinkedIn search","linkedin-browser":"LinkedIn browser","ats-search":"Company ATS","company-careers":"Company careers","freehire-search":"freehire"};
const sourceLabel=value=>SOURCE_LABELS[value]||String(value||"Other website").replace(/-search$/,"").replaceAll("-"," ");
const sourceTitle=job=>[...new Set([job.primary_source,...(job.sources||[])].filter(Boolean))].map(sourceLabel).join(" · ");

function renderChips(){
  const counts={all:JOBS.length,active:JOBS.filter(j=>ACTIVE.includes(j.status)).length};
  STATUSES.forEach(s=>counts[s]=JOBS.filter(j=>j.status===s).length);
  el("chips").innerHTML=FILTERS.map(([key,label])=>`<button class="chip ${key===filter?"on":""}" data-filter="${esc(key)}">${esc(label)}<span class="n">${counts[key]||0}</span></button>`).join("");
}
function render(){
  renderChips();const rows=shown();if(sel>=rows.length)sel=Math.max(0,rows.length-1);
  el("tb").innerHTML=rows.map((j,i)=>`<tr class="${i===sel?"sel":""}" data-row="${i}">
    <td><span class="fitword ${esc(j.fit)}">${esc(j.fit||"—")}</span></td>
    <td class="role" title="${esc(j.title)}">${esc(j.title)}</td><td class="co">${esc(j.company)}</td><td class="co sourcecell" title="${esc(sourceTitle(j))}">${esc(sourceLabel(j.primary_source||j.portal))}</td>
    <td class="co">${esc(j.location)}</td><td class="co">${esc(j.posted).slice(5)}</td>
    <td><select data-url="${esc(j.url)}">${STATUSES.map(s=>`<option value="${esc(s)}" ${s===j.status?"selected":""}>${esc(s)}</option>`).join("")}</select></td>
    <td class="wide-only why" title="${esc(j.why)}">${esc(j.why)}${j.dupes?.length?'<span class="dupe"> · possible dupe</span>':""}</td>
    <td class="wide-only note" data-note="${esc(j.url)}">${esc(j.note)}</td></tr>`).join("");
  el("empty").hidden=rows.length>0;el("count").textContent=`${rows.length} shown · ${JOBS.length} total`;
  renderJob();document.querySelector("tr.sel")?.scrollIntoView({block:"nearest"});
}
// Which number the row is actually sorted on. Calling a rank score a "prefit"
// was wrong in both directions: it understated an LLM judgement and overstated
// a keyword prior.
const SCORE_LABEL={rank:"rank",fit:"fit",prefit:"prefit",band:"band"};
const scoreTitle=j=>({
  rank:"Scored by /rank, which read the posting.",
  fit:"Computed by tools/fit_score.py from the title, seniority, skills, company affinity and location.",
  prefit:"An older collector-side prior, not a fit assessment.",
  band:"Carried over from a coarse band with no number behind it.",
}[j.score_source]||"");

// Gates, read from what was actually decided rather than guessed at again here.
//
// This used to run `/german required|deutsch/i` over the description in the
// browser. That was wrong twice over: far looser than collectors.GERMAN_RE,
// which distinguishes German stated as a job *condition* from German mentioned
// in passing, and it ran against a description that is now only an excerpt - so
// it would have missed a requirement further down the posting and reported "no
// blocking requirement detected" with confidence.
function gateLines(j){
  const gated=j.status==="gate";
  const note=(j.why||"").includes("German")?j.why:"";
  const language=gated&&note
    ? {mark:"✕",text:"Language — German stated as a job condition"}
    : gated
      ? {mark:"✕",text:"Language — auto-screened out; see the note"}
      : {mark:"✓",text:"Language — no blocking requirement was screened"};
  const location={mark:"✓",text:"Location — "+(j.location||"not stated")};
  return [language,location].map(g=>
    `<div class="gate-line"><span class="gate-mark">${g.mark}</span><span>${esc(g.text)}</span></div>`
  ).join("");
}

// Full posting bodies live in sidecars and are not in the list payload - every
// row of it goes to the browser on every reload. The excerpt paints instantly;
// the body arrives from /api/job and is cached so re-selecting a row is free.
const POSTING_CACHE=new Map();
function postingText(j){
  if(POSTING_CACHE.has(j.url))return POSTING_CACHE.get(j.url);
  if(j.description)return j.description;
  return j.has_posting?"Loading the posting…"
    :"Posting text is not stored for this row. Open the original posting to read it.";
}
async function loadPosting(j){
  if(!j||!j.has_posting||POSTING_CACHE.has(j.url))return;
  try{
    const full=await(await fetch("/api/job?t="+T+"&url="+encodeURIComponent(j.url))).json();
    POSTING_CACHE.set(j.url,full.description||"");
  }catch(error){return}
  // Only repaint when the row is still the selected one: a fast arrow-key walk
  // down the list would otherwise drop an old response into the new row.
  if(selectedJob()?.url===j.url){
    const box=el("postingbody");
    if(box){box.textContent=POSTING_CACHE.get(j.url);box.classList.remove("postingempty")}
  }
}

function renderJob(){
  const j=selectedJob(),rows=shown();
  if(!j){el("jobdetail").innerHTML='<div class="panel-empty">Select a job.</div>';el("jobpos").textContent="";return}
  el("jobpos").textContent=`${sel+1} of ${rows.length}`;el("jobopen").href=j.open_url||j.url;
  const statusButtons=["star","yes","maybe","gate","no"].map(s=>`<button class="${j.status===s?"on":""}" data-status="${s}">${s}</button>`).join("");
  el("jobdetail").innerHTML=`<div class="jobsummary"><div class="jobtitle">${esc(j.title)}</div>
    <div class="jobmeta"><strong>${esc(j.company)}</strong><span>·</span><span>${esc(j.location)}</span><span>·</span><span>posted ${esc(j.posted).slice(5)}</span></div>
    <div class="badges"><span class="fitword ${esc(j.fit)}">${esc(j.fit||"unranked")}</span><span class="badge">${esc(j.portal||"source unknown")}</span>${j.score?`<span class="badge" title="${esc(scoreTitle(j))}">${esc(SCORE_LABEL[j.score_source]||"score")} ${esc(Math.round(j.score))}</span>`:""}${j.fit_evidence==="title-only"?'<span class="badge" title="No posting text is stored, so the skills component could not be scored and the band is capped at medium.">title only</span>':""}</div></div>
    <div class="whybox"><span class="label">Why it is here</span>${esc(j.why||"No reason was stored.")}</div>
    <div class="posting"><span class="label">Posting</span><div id="postingbody" class="${j.description?"":"postingempty"}">${esc(postingText(j))}</div></div>
    <div class="jobactions"><div class="statusbuttons">${statusButtons}</div>
      <input class="noteinput" data-note-input="${esc(j.url)}" value="${esc(j.note)}" placeholder="+ note" aria-label="My note">
      <button class="primary tailor" data-tailor="${esc(j.url)}">✎&nbsp; Tailor CV + cover letter</button>
      <div class="hint">runs /apply in the background · you stay on the board</div></div>`;
  loadPosting(j);
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
function openTextModal({eyebrow="Edit",title,label="Text",value="",placeholder="",hint="",submit="Save"}){
  if(MODAL_RESOLVE)MODAL_RESOLVE(null);const modal=el("text-modal"),input=el("text-modal-input");
  el("text-modal-eyebrow").textContent=eyebrow;el("text-modal-title").textContent=title;el("text-modal-label").textContent=label;el("text-modal-hint").textContent=hint;el("text-modal-submit").textContent=submit;input.value=value;input.placeholder=placeholder;modal.hidden=false;document.body.classList.add("modal-open");
  requestAnimationFrame(()=>{input.focus();input.setSelectionRange(input.value.length,input.value.length)});
  return new Promise(resolve=>MODAL_RESOLVE=resolve);
}
function closeTextModal(save=false){
  if(!MODAL_RESOLVE)return;const resolve=MODAL_RESOLVE;MODAL_RESOLVE=null;el("text-modal").hidden=true;document.body.classList.remove("modal-open");resolve(save?el("text-modal-input").value:null);
}
async function editNote(j=selectedJob()){
  if(!j)return;const value=await openTextModal({eyebrow:"My note",title:j.company+" — "+j.title,label:"Private board note",value:j.note||"",placeholder:"Add context, a contact, or your next action…",hint:"Saved locally to your job board. Multiple lines are supported.",submit:"Save note"});
  if(value!==null)update(j.url,{note:value}).then(ok=>ok&&toast("note saved"));
}

function elapsed(run){
  if(!run.started_at)return "";const end=run.ended_at?new Date(run.ended_at):new Date();
  const seconds=Math.max(0,Math.round((end-new Date(run.started_at))/1000));
  if(seconds<90)return seconds+" s";return Math.round(seconds/60)+" min";
}
function activeRuns(){return RUNS.filter(r=>RUNNING.includes(r.phase)||r.phase==="awaiting_approval"||r.phase==="orphaned")}
function renderRuns(){
  const live=activeRuns(),recent=RUNS.filter(r=>!live.includes(r)).slice(0,4),rows=live.concat(recent);
  el("runqueue").textContent=QUEUE.length?QUEUE.length+" queued":(live.length?live.length+" active":"");
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
  if(el("app").classList.contains("tailor-mode")&&!el("app").classList.contains("reader-mode")&&!el("app").classList.contains("preview-mode")&&!el("app").classList.contains("revise-mode")&&ACTIVE_RUN)renderTailor(RUNS.find(r=>r.id===ACTIVE_RUN));
}
function renderApplications(){
  const rows=RUNS.slice(0,20);el("applicationcount").textContent=`${rows.filter(r=>r.phase==="done").length} drafted · ${activeRuns().length} active`;
  el("applicationlist").innerHTML=rows.length?rows.map(r=>`<tr data-run="${esc(r.id)}"><td><span class="role">${esc(r.company)}</span> <span class="co">· ${esc(r.role)}</span></td><td><span class="phase ${esc(r.phase)}">${esc(r.phase.replaceAll("_"," "))}</span></td><td class="co">${esc((r.started_at||"").slice(0,16).replace("T"," "))}</td><td>${r.phase==="done"?`<button class="linkish previewrun" data-preview="${esc(r.id)}">Preview</button> <button class="linkish" data-revise="${esc(r.id)}">Revise</button>`:`<button class="linkish">Watch run</button>`}</td></tr>`).join(""):'<tr><td colspan="4" class="panel-empty">No applications yet.</td></tr>';
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
async function startTailor(url){
  const j=JOBS.find(x=>x.url===url);if(!j)return;
  const note=await openTextModal({eyebrow:"Tailor application",title:j.company+" — "+j.title,label:"One-off instruction (optional)",placeholder:"Emphasize a project, explain a transition, or leave this empty…",hint:"This instruction applies only to this run unless you later add it as a standing preference.",submit:"Start tailoring"});
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
      ${run.phase==="awaiting_approval"?`<button class="primary approve" data-run-id="${esc(run.id)}" data-phase="${esc(run.phase)}">Draft CV + cover letter</button>`:""}${RUNNING.includes(run.phase)?`<button class="secondary cancelrun" data-run-id="${esc(run.id)}">Cancel run</button>`:""}${run.phase==="done"?`<button class="primary" data-preview="${esc(run.id)}">Preview PDFs</button>`:""}</div></section></div>`;
  openView("tailor",run.company+" · "+run.role,"/run/"+encodeURIComponent(run.id));
}

function renderReader(){
  const rows=shown(),j=selectedJob();if(!j)return;
  const queue=rows.slice(0,80).map((row,index)=>`<div class="queue-row ${index===sel?"selected":""}" data-reader-row="${index}"><div class="queue-top"><span class="fitdot ${esc(row.fit)}"></span><span class="queue-company">${esc(row.company)}</span><span class="queue-mark">${esc(row.status==="new"?"":row.status)}</span></div><div class="queue-title">${esc(row.title)}</div></div>`).join("");
  const marks=["star","yes","maybe","gate","no","applied"].map(s=>`<button class="${j.status===s?"on":""}" data-status="${s}">${s}</button>`).join("");
  el("tailor-view").innerHTML=`<div class="reader-shell"><section class="reader-queue"><div class="panelhead"><span class="label">Queue</span><span class="spacer"></span><span class="dim">${rows.length} active</span></div><div class="queue-list">${queue}</div></section>
    <section class="reader-main"><div class="reader-scroll"><article class="reader-copy"><div class="reader-badges"><span class="fitword ${esc(j.fit)}">${esc(j.fit||"unranked")}</span><span class="badge">${esc(j.portal||"source unknown")}</span>${j.score?`<span class="badge" title="${esc(scoreTitle(j))}">${esc(SCORE_LABEL[j.score_source]||"score")} ${esc(Math.round(j.score))}</span>`:""}${j.fit_evidence==="title-only"?'<span class="badge">title only</span>':""}<span class="spacer"></span><a class="open" href="${esc(j.open_url||j.url)}" target="_blank" rel="noopener">open posting ↗</a></div><h1>${esc(j.title)}</h1><div class="reader-meta"><strong>${esc(j.company)}</strong><span>·</span><span>${esc(j.location)}</span><span>·</span><span>posted ${esc(j.posted).slice(5)}</span></div><div class="reader-posting" id="postingbody">${esc(postingText(j))}</div></article></div></section>
    <aside class="reader-decide"><div class="panelhead"><span class="label">Decide</span></div><div class="decision-section"><span class="label">Gates</span>${gateLines(j)}</div><div class="decision-section"><span class="label">Why it surfaced</span><div class="dim">${esc(j.why||"No reason was stored.")}</div><div class="hint">${esc(scoreTitle(j))} Tailor re-evaluates properly.</div></div><div class="decision-section"><span class="label">Mark it</span><div class="statusbuttons">${marks}</div><textarea class="noteinput" data-note-input="${esc(j.url)}" placeholder="note to yourself — saved on blur">${esc(j.note)}</textarea><button class="primary tailor" data-tailor="${esc(j.url)}">✎&nbsp; Tailor CV + cover letter</button><div class="hint">marks it yes and queues the run · <kbd>t</kbd></div></div></aside></div>`;
  loadPosting(j);
  openView("reader",`reading ${sel+1} of ${rows.length} · active`,"/job/"+encodeURIComponent(j.url));
}

async function renderPreview(run){
  if(!run)return;PREVIEW_RUN=run.id;
  el("tailor-view").innerHTML='<div class="panel-empty">Loading compiled documents…</div>';
  openView("preview",run.company+" · "+run.role,"/run/"+encodeURIComponent(run.id)+"/preview");
  let verify;try{const response=await fetch(`/api/runs/${encodeURIComponent(run.id)}/verify?t=${T}`);verify=await response.json();if(!response.ok)throw new Error(verify.error)}catch(error){el("tailor-view").innerHTML=`<div class="panel-empty runerror">${esc(error.message||error)}</div>`;return}
  const counts={};(verify.checks||[]).forEach(check=>counts[check.state]=(counts[check.state]||0)+1);
  const checks=(verify.checks||[]).map(check=>`<div class="verify-item ${esc(check.state)}"><span class="verify-mark ${esc(check.state)}">${check.state==="pass"?"✓":"!"}</span><div><div>${esc(check.label)}</div><div class="verify-detail">${esc(check.detail)}</div></div></div>`).join("");
  const query=`?t=${encodeURIComponent(T)}&v=${Date.now()}`;
  el("tailor-view").innerHTML=`<div class="preview-shell"><aside class="verify-rail"><div class="panelhead"><span class="label">Verification</span><span class="spacer"></span><span class="dim state-summary">${counts.pass||0} pass · ${(counts.flag||0)+(counts.fail||0)+(counts.unverified||0)} flagged</span></div><div class="verify-list">${checks}</div><div class="decision-section" style="margin-top:auto"><span class="label">Keywords</span><div class="dim">Covered: ${esc((verify.keywords?.covered||[]).join(", ")||"none")}</div><div class="dim">Absent: ${esc((verify.keywords?.absent||[]).join(", ")||"none")}</div></div></aside>
    <section class="preview-main" id="preview-main"><div class="preview-tabs"><button class="chip on" data-preview-filter="both">Both documents</button><button class="chip" data-preview-filter="cv">CV only</button><button class="chip" data-preview-filter="cover">Letter only</button><span class="spacer"></span><span class="dim">${esc(run.artefacts?.cv_pdf||"cv.pdf")} · ${esc(run.artefacts?.cover_pdf||"cover.pdf")}</span></div><div class="pdf-stage"><iframe title="Compiled CV" class="pdf-frame cv" src="/api/pdf/${encodeURIComponent(run.id)}/cv${query}"></iframe><iframe title="Compiled cover letter" class="pdf-frame cover" src="/api/pdf/${encodeURIComponent(run.id)}/cover${query}"></iframe></div><div class="preview-foot"><span class="dim">Rendered from the compiled PDFs, not from source files. Every check is reproducible.</span><span class="spacer"></span><a class="secondary" href="/api/pdf/${encodeURIComponent(run.id)}/cv${query}" download>Download CV</a><a class="secondary" href="/api/pdf/${encodeURIComponent(run.id)}/cover${query}" download>Download letter</a><button class="secondary" data-recompile="${esc(run.id)}">Recompile</button></div></section></div>`;
}

async function renderRevise(run){
  if(!run)return;REVISE_RUN=run.id;const versions=RUNS.filter(r=>r.slug===run.slug&&r.phase==="done");
  let prefs={preferences:[]};try{prefs=await(await fetch("/api/prefs?t="+T)).json()}catch(_){}
  const versionRows=versions.map((v,i)=>`<div class="version-row ${v.id===run.id?"current":""}" data-version-preview="${esc(v.id)}"><span>v${versions.length-i}</span><strong>${esc((v.ended_at||v.started_at||"").slice(0,16).replace("T"," "))}</strong><span>${esc(v.kind||"apply")}</span>${v.id===run.id?"<em>current</em>":`<span class="spacer"></span><button class="linkish" data-restore-version="${esc(v.id)}">Restore</button>`}</div>`).join("");
  const prefRows=(prefs.preferences||[]).map(p=>`<li>${esc(p)}</li>`).join("")||"<li>No managed standing preferences.</li>";
  el("tailor-view").innerHTML=`<div class="revise-shell"><aside class="versions"><div class="panelhead"><span class="label">Versions</span><span class="spacer"></span><span class="dim">${versions.length}</span></div><div class="version-list">${versionRows}</div><div class="decision-section"><span class="label">Standing preferences · read only</span><ul class="pref-list">${prefRows}</ul><div class="hint">Remove a preference by editing the managed block in the candidate profile.</div></div></aside><section class="revision-current"><div class="panelhead"><span class="label">Current documents</span><span class="spacer"></span><button class="secondary" data-preview="${esc(run.id)}">Open compiled PDFs</button></div><div class="revision-summary"><h1>${esc(run.company)}</h1><h2>${esc(run.role)}</h2><div class="writing"><div>${esc(run.targets?.cv)}</div><div>${esc(run.targets?.cover)}</div></div><div class="whybox">Every successful revision becomes another immutable source + PDF snapshot. Restore replaces these live files and recompiles them; it never creates a second live document set.</div></div></section><aside class="composer"><div class="panelhead"><span class="label">Revise</span></div><form id="revise-form"><div class="decision-section"><span class="label">Scope</span><label><input type="radio" name="scope" value="both" checked> CV + cover</label><label><input type="radio" name="scope" value="cv"> CV only</label><label><input type="radio" name="scope" value="cover"> Cover only</label></div><div class="decision-section"><label class="label" for="revision-note">What should change?</label><textarea id="revision-note" required placeholder="Make the evidence for… more explicit"></textarea><label class="label" for="revision-remember">Standing preference (optional)</label><textarea id="revision-remember" placeholder="Remember this for future applications"></textarea><div class="hint">Only text in this field is written into the managed preference block.</div></div><div class="composer-actions"><button class="primary" type="submit" data-reentry-kind="revise">Revise</button><button class="secondary" type="submit" data-reentry-kind="redraft">Redraft</button><button class="secondary" type="submit" data-reentry-kind="apply">Full re-run</button></div></form></aside></div>`;
  openView("revise",run.company+" · "+run.role,"/run/"+encodeURIComponent(run.id)+"/revise");
}

async function renderCompanies(reload=true){
  if(reload||!COMPANIES){try{COMPANIES=await(await fetch("/api/companies?t="+T)).json();const suggested=await(await fetch("/api/companies/suggest?t="+T)).json();SUGGESTIONS=suggested.suggestions||suggested.results||suggested||[]}catch(error){toast("could not load companies",{warn:true});return}}
  const all=COMPANIES.companies||[];
  const bucket=row=>row.status==="paused"?"paused":row.status==="ambiguous"?"review":row.route!=="ats"?"other":row.status==="verified"?"ready":"unresolved";
  const buckets={ready:all.filter(r=>bucket(r)==="ready").length,review:all.filter(r=>bucket(r)==="review").length,unresolved:all.filter(r=>bucket(r)==="unresolved").length,other:all.filter(r=>bucket(r)==="other").length,paused:all.filter(r=>bucket(r)==="paused").length};
  const rows=all.filter(row=>COMPANY_FILTER==="all"||bucket(row)===COMPANY_FILTER);
  if(!COMPANY_SELECTED||!all.some(row=>row.name===COMPANY_SELECTED))COMPANY_SELECTED=rows[0]?.name;
  const selected=all.find(row=>row.name===COMPANY_SELECTED);
  const filters=[["all","All"],["ready","Ready"],["review","Needs review"],["unresolved","Not connected"],["other","Other sources"],["paused","Paused"]];
  const chips=filters.map(([key,label])=>`<button class="chip ${COMPANY_FILTER===key?"on":""}" data-company-filter="${key}">${label}<span class="n">${key==="all"?all.length:buckets[key]}</span></button>`).join("");
  const routeLabel=row=>row.route==="ats"?(row.status==="verified"?"Official ATS":"ATS not connected"):row.route==="linkedin"?"LinkedIn":"Manual";
  const statusLabel=row=>({ready:"Ready",review:"Needs your review",unresolved:"Not connected",other:"Uses another source",paused:"Paused"})[bucket(row)];
  const table=rows.map(row=>`<tr class="${row===selected?"sel":""}" data-company-row="${esc(row.name)}"><td><strong>${esc(row.name)}</strong><div class="dim">${esc(row.domain||"No website")}</div></td><td>${esc(routeLabel(row))}</td><td><span class="phase ${esc(row.status)}">${esc(statusLabel(row))}</span></td><td>${esc((row.last_success_at||"Never").slice(0,10))}</td><td>${esc(row.stats?.eligible_jobs||0)} matches</td></tr>`).join("");
  const boardUrl=item=>{const token=item?.token;if(!token)return "";return item.board_url||({greenhouse:`https://job-boards.greenhouse.io/${encodeURIComponent(token)}`,ashby:`https://jobs.ashbyhq.com/${encodeURIComponent(token)}`,personio:`https://${encodeURIComponent(token)}.jobs.personio.de`,lever:`https://jobs.lever.co/${encodeURIComponent(token)}`,smartrecruiters:`https://careers.smartrecruiters.com/${encodeURIComponent(token)}`}[item?.vendor]||"")};
  const candidates=(selected?.candidates||[]).map(candidate=>{const url=boardUrl(candidate);return `<div class="candidate"><strong>Possible ${esc(candidate.vendor)} board</strong><div class="dim">${esc(candidate.note||"The system found this board but cannot prove ownership.")}</div><div class="company-actions">${url?`<a class="secondary" target="_blank" rel="noopener" href="${esc(url)}">Open candidate ↗</a>`:""}<button class="primary" data-company-confirm="${esc(candidate.vendor)}|${esc(candidate.token)}">Yes, this is their board</button></div></div>`}).join("");
  const evidenceKind=selected?.identity?.evidence_kind||selected?.identity?.method;
  const evidenceText={company_site_link:"Confirmed from a link on the company’s own website.",vendor_identifier:"Confirmed because the ATS company identifier matches.",human_confirmed:"Confirmed manually by you."}[evidenceKind]||selected?.identity?.evidence||"Verification evidence is recorded.";
  const companyStatus=!selected?"":bucket(selected)==="ready"?`<div class="company-status-card"><strong>Ready for automatic monitoring</strong><p>${esc(evidenceText)}</p>${boardUrl(selected)?`<a class="primary" target="_blank" rel="noopener" href="${esc(boardUrl(selected))}">Open verified board ↗</a>`:""}<div class="dim">Verified ${esc(selected.identity?.verified_at||selected.identity?.checked||"")}</div></div>`:bucket(selected)==="review"?`<div class="company-status-card"><strong>Please check ${selected.candidates?.length||0} possible board${selected.candidates?.length===1?"":"s"}</strong><p>Open each candidate. Confirm only when the company and jobs match.</p>${candidates}<button class="secondary" data-company-neither>None match — pause company</button></div>`:bucket(selected)==="unresolved"?`<div class="company-status-card"><strong>No confirmed ATS board found</strong><p>The automatic check could not prove a board belongs to this company. You do not need to fill in technical fields.</p><div class="company-actions">${selected.domain?`<a class="secondary" target="_blank" rel="noopener" href="https://${esc(selected.domain)}">Open company website ↗</a>`:""}<button class="primary" data-company-resolve="${esc(selected.name)}">Check again</button></div></div>`:bucket(selected)==="other"?`<div class="company-status-card"><strong>Monitored through ${esc(routeLabel(selected))}</strong><p>This company does not need an ATS connection for its current monitoring route.</p></div>`:`<div class="company-status-card"><strong>Monitoring paused</strong><p>This company will not be checked automatically.</p></div>`;
  const detail=value=>esc(Array.isArray(value)?value.join(", ")||"—":value||"—");
  const readOnly=selected?`<details class="company-details"><summary>Technical details</summary><dl><dt>Priority</dt><dd>Tier ${detail(selected.tier)}</dd><dt>Source</dt><dd>${detail(selected.route)}</dd><dt>Aliases</dt><dd>${detail(selected.aliases)}</dd><dt>Countries</dt><dd>${detail(selected.countries)}</dd><dt>Cities</dt><dd>${detail(selected.cities)}</dd><dt>Tags</dt><dd>${detail(selected.flags)}</dd><dt>Notes</dt><dd>${detail(selected.note)}</dd></dl></details>`:"";
  const suggestions=(Array.isArray(SUGGESTIONS)?SUGGESTIONS:[]).slice(0,8).map(item=>`<div class="suggestion"><strong>${esc(item.company||item.name)}</strong><span>${esc(item.count||"")} matching board rows</span><button class="linkish" data-suggest-add="${esc(item.company||item.name)}">Add</button><button class="linkish" data-suggest-never="${esc(item.company||item.name)}">Never</button></div>`).join("");
  el("tailor-view").innerHTML=`<div class="companies-shell"><section class="companies-main"><div class="companies-head"><div><span class="label">Target companies</span><div class="company-summary">${buckets.ready} ready · ${buckets.review} need review · ${buckets.unresolved} not connected</div></div><span class="spacer"></span><button class="primary" id="resolve-all" ${buckets.unresolved?"":"disabled"}>Automatically check all ${buckets.unresolved||""}</button></div><form id="company-add" class="company-add"><input id="company-name" required placeholder="Company name"><input id="company-website" required placeholder="Company website"><button class="secondary">Add company</button><span class="hint">The system will try to find and verify the official job board automatically.</span></form><div class="chips">${chips}</div><div class="tablewrap"><table class="company-table"><thead><tr><th>Company</th><th>Monitoring</th><th>Connection</th><th>Last successful check</th><th>Relevant jobs found</th></tr></thead><tbody>${table||'<tr><td colspan="5" class="panel-empty">No companies in this view.</td></tr>'}</tbody></table></div></section><aside class="company-rail"><div class="panelhead"><span class="label">${selected?esc(selected.name):"Company status"}</span></div>${companyStatus}${readOnly}<div class="panelhead"><span class="label">Suggested companies</span></div><div class="suggestions">${suggestions||'<div class="panel-empty">No new suggestions.</div>'}</div></aside></div>`;
  openView("companies","","/companies?f="+encodeURIComponent(COMPANY_FILTER)+(COMPANY_SELECTED?"&c="+encodeURIComponent(COMPANY_SELECTED):""));
}

function openView(kind,title=null,route=null){
  const app=el("app");app.classList.remove("expanded-board","expanded-applications","expanded-job","tailor-mode","reader-mode","preview-mode","revise-mode","companies-mode");
  const parked=["tailor","reader","preview","revise","companies"].includes(kind);
  app.classList.add("expanded",parked?"tailor-mode":"expanded-"+kind);
  if(parked)app.classList.add(kind+"-mode");
  el("tailor-view").hidden=!parked;
  el("restore").hidden=false;el("brand").textContent=kind==="tailor"?(RUNS.find(r=>r.id===ACTIVE_RUN)?.company||"Tailoring"):"JobFlow";el("local").textContent=title===null?kind:title;
  setRoute(route||"/"+kind);
}
function restoreWorkspace(){
  const app=el("app");app.classList.remove("expanded","expanded-board","expanded-applications","expanded-job","tailor-mode","reader-mode","preview-mode","revise-mode","companies-mode");el("tailor-view").hidden=true;el("restore").hidden=true;el("brand").textContent="JobFlow";el("local").textContent="";
  setRoute("/");
}

// Routing. The fragment owns *which view is on screen*; the layout blob below
// owns only *how wide the panels are*. They used to be one thing, and that cost
// three bugs: a reload could restore board/applications/job but silently dropped
// companies, preview and revise; the tailor view came back on whatever run
// happened to be first rather than the one you were reading; and because
// localStorage is per-browser rather than per-tab, two open tabs overwrote each
// other's idea of where they were. A fragment is never sent to the server, so
// server.py stays the plain static handler it is - no SPA fallback, no second
// pass through the token check.
//
//   #/                  workspace        #/companies?f=&c=   companies
//   #/board             expanded board   #/run/<id>          tailor
//   #/applications      expanded apps    #/run/<id>/preview  compiled PDFs
//   #/job/<url>         reader           #/run/<id>/revise   revise
//
// Paging or filtering inside one view rewrites its entry; moving to another view
// pushes one. Otherwise j/k in the reader would bury the board under 80 entries.
const IN_PLACE=["job","companies"];
let APPLYING_ROUTE=false,SELF_WRITE=false;
const routeHead=value=>String(value||"").replace(/^#/,"").replace(/^\//,"").split(/[/?]/)[0];
function setRoute(route,replace=false){
  if(APPLYING_ROUTE&&!replace)return;
  const next="#"+route,head=routeHead(route);
  if(location.hash===next||(!location.hash&&route==="/"))return;
  SELF_WRITE=true;
  if(replace||(head&&head===routeHead(location.hash)&&IN_PLACE.includes(head))){history.replaceState(null,"",next);SELF_WRITE=false}
  else location.hash=next;
}
function parseRoute(){
  const raw=location.hash.replace(/^#/,"")||"/",[path,query]=raw.split("?");
  const decode=part=>{try{return decodeURIComponent(part)}catch(_){return part}};
  return {parts:path.split("/").filter(Boolean).map(decode),params:new URLSearchParams(query||"")};
}
// Returns false for a route that names something that is not there any more -
// a finished run that was pruned, a posting that dropped off the board.
function dispatchRoute(){
  const {parts,params}=parseRoute(),[head,first,second]=parts;
  if(!head){restoreWorkspace();return true}
  if(head==="board"||head==="applications"){openView(head);return true}
  if(head==="job"){
    if(!JOBS.some(job=>job.url===first))return false;
    // A deep link outranks whichever chip you happened to leave the board on.
    if(!shown().some(row=>row.url===first)){filter="all";q="";el("q").value="";render()}
    const index=shown().findIndex(row=>row.url===first);
    if(index<0)return false;
    sel=index;renderReader();return true;
  }
  if(head==="companies"){COMPANY_FILTER=params.get("f")||"all";COMPANY_SELECTED=params.get("c")||null;renderCompanies();return true}
  if(head==="run"){
    const run=RUNS.find(item=>item.id===first);if(!run)return false;
    if(second==="preview")renderPreview(run);else if(second==="revise")renderRevise(run);else renderTailor(run);
    return true;
  }
  return false;
}
function applyRoute(){
  APPLYING_ROUTE=true;
  let ok=false;try{ok=dispatchRoute()}finally{APPLYING_ROUTE=false}
  if(!ok){setRoute("/",true);restoreWorkspace()}
}
addEventListener("hashchange",()=>{if(SELF_WRITE){SELF_WRITE=false;return}applyRoute()});

// Layout state: geometry only. Which view is open lives in the URL (above),
// because that answers the back button and is per-tab. User collapses and
// automatic narrow-window collapses stay distinct.
const LAYOUT_KEY="jobflow.layout.v1";
const DEFAULT_LAYOUT={version:1,left:264,right:452,collect:300,applications:208,leftCollapsed:false,rightCollapsed:false,autoLeft:false,autoRight:false,shortcutsHidden:false};
function loadLayout(){try{const value=JSON.parse(localStorage.getItem(LAYOUT_KEY));if(value?.version===1){const merged={...DEFAULT_LAYOUT,...value};delete merged.expanded;return merged}localStorage.removeItem(LAYOUT_KEY)}catch(_){try{localStorage.removeItem(LAYOUT_KEY)}catch(__){}}return {...DEFAULT_LAYOUT}}
let layout=loadLayout();
function saveLayout(){try{localStorage.setItem(LAYOUT_KEY,JSON.stringify(layout))}catch(_){}}
function applyLayout(){
  const app=el("app");app.style.setProperty("--left",layout.left+"px");app.style.setProperty("--right",layout.right+"px");app.style.setProperty("--collect",layout.collect+"px");app.style.setProperty("--applications",layout.applications+"px");
  app.classList.toggle("left-collapsed",layout.leftCollapsed||layout.autoLeft);app.classList.toggle("right-collapsed",layout.rightCollapsed||layout.autoRight);
  el("left-rail").classList.toggle("collapsed",layout.leftCollapsed||layout.autoLeft);el("right-rail").classList.toggle("collapsed",layout.rightCollapsed||layout.autoRight);
  document.querySelector('[data-collapse="left"]').setAttribute("aria-expanded",String(!(layout.leftCollapsed||layout.autoLeft)));document.querySelector('[data-collapse="right"]').setAttribute("aria-expanded",String(!(layout.rightCollapsed||layout.autoRight)));
  el("keybar").hidden=!!layout.shortcutsHidden;el("shortcut-toggle").setAttribute("aria-expanded",String(!layout.shortcutsHidden));el("shortcut-toggle").textContent=layout.shortcutsHidden?"Show shortcuts":"Hide shortcuts";
  updateSeparatorAria();
}
function toggleShortcuts(force){layout.shortcutsHidden=typeof force==="boolean"?force:!layout.shortcutsHidden;applyLayout();saveLayout()}
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
  renderStrip();if(!el("drawer").hidden)renderActivity();if(el("app").classList.contains("tailor-mode")&&!el("app").classList.contains("reader-mode")&&!el("app").classList.contains("preview-mode")&&!el("app").classList.contains("revise-mode")&&ACTIVE_RUN)renderTailor(RUNS.find(r=>r.id===ACTIVE_RUN));
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
  if(event.target.closest("#text-modal-submit"))return void closeTextModal(true);
  if(event.target.closest("#text-modal-cancel,#text-modal-close"))return void closeTextModal(false);
  if(event.target===el("text-modal"))return void closeTextModal(false);
  if(event.target.closest("#shortcut-toggle"))return void toggleShortcuts();
  if(event.target.closest("#theme-toggle"))return void cycleTheme();
  if(event.target.closest("#companies-open"))return void renderCompanies();
  const resolveAll=event.target.closest("#resolve-all");if(resolveAll){resolveAll.disabled=true;resolveAll.textContent="Checking companies…";(async()=>{try{const response=await fetch("/api/companies/resolve-all?t="+T,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({mtime:COMPANIES.mtime})});const data=await response.json();if(!response.ok)throw new Error(data.error||"automatic check failed");COMPANIES=data;const counts=data.result?.meta?.status_counts||{};toast(`Check complete · ${counts.verified||0} ready · ${counts.ambiguous||0} need review`,{ms:5000});renderCompanies(false)}catch(error){toast(error.message,{warn:true,ms:5000});renderCompanies(false)}})();return}
  if(event.target.closest("#toastundo"))return void undo();
  if(event.target.closest("#striptoggle"))return void toggleDrawer();
  if(event.target.closest("#copylog"))return void copyLog();
  if(event.target.closest("#restore,[data-restore]"))return void restoreWorkspace();
  const collapse=event.target.closest("[data-collapse]");if(collapse)return void toggleCollapse(collapse.dataset.collapse);
  const expand=event.target.closest("[data-expand]");if(expand){const kind=expand.dataset.expand;if(kind==="job")renderReader();else if(kind==="board"||kind==="applications")openView(kind);return}
  const chip=event.target.closest("[data-filter]");if(chip){filter=chip.dataset.filter;sel=0;render();return}
  const act=event.target.closest("[data-act-filter]");if(act){actFilter=act.dataset.actFilter;renderActivity();return}
  const status=event.target.closest("[data-status]");if(status)return void setStatus(status.dataset.status);
  const note=event.target.closest("[data-note]");if(note)return void editNote(JOBS.find(j=>j.url===note.dataset.note));
  const tailor=event.target.closest("[data-tailor]");if(tailor)return void startTailor(tailor.dataset.tailor);
  const readerRow=event.target.closest("[data-reader-row]");if(readerRow){sel=+readerRow.dataset.readerRow;renderReader();return}
  const companyFilter=event.target.closest("[data-company-filter]");if(companyFilter){COMPANY_FILTER=companyFilter.dataset.companyFilter;renderCompanies(false);return}
  const companyRow=event.target.closest("[data-company-row]");if(companyRow){COMPANY_SELECTED=companyRow.dataset.companyRow;renderCompanies(false);return}
  const companyResolve=event.target.closest("[data-company-resolve]");if(companyResolve){companyResolve.disabled=true;const slug=companyResolve.dataset.companyResolve.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"");postCompany("/api/companies/"+slug+"/resolve",{mtime:COMPANIES.mtime}).then(ok=>ok&&renderCompanies());return}
  const companyConfirm=event.target.closest("[data-company-confirm]");if(companyConfirm){const [vendor,token]=companyConfirm.dataset.companyConfirm.split("|"),slug=COMPANY_SELECTED.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"");postCompany("/api/companies/"+slug+"/identity",{mtime:COMPANIES.mtime,decision:"confirm",candidate:{vendor,token}}).then(ok=>ok&&renderCompanies());return}
  if(event.target.closest("[data-company-neither]")){const slug=COMPANY_SELECTED.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"");postCompany("/api/companies/"+slug+"/identity",{mtime:COMPANIES.mtime,decision:"neither"}).then(ok=>ok&&renderCompanies());return}
  const suggestAdd=event.target.closest("[data-suggest-add]");if(suggestAdd){el("company-name").value=suggestAdd.dataset.suggestAdd;el("company-website").focus();return}
  const suggestNever=event.target.closest("[data-suggest-never]");if(suggestNever){postCompany("/api/companies",{mtime:COMPANIES.mtime,decision:"never",name:suggestNever.dataset.suggestNever}).then(ok=>ok&&renderCompanies());return}
  const preview=event.target.closest("[data-preview]");if(preview){const run=RUNS.find(r=>r.id===preview.dataset.preview);if(run)renderPreview(run);return}
  const revise=event.target.closest("[data-revise]");if(revise){const run=RUNS.find(r=>r.id===revise.dataset.revise);if(run)renderRevise(run);return}
  const restoreVersion=event.target.closest("[data-restore-version]");if(restoreVersion){restoreVersion.disabled=true;postRun("/api/runs/"+restoreVersion.dataset.restoreVersion+"/restore").then(ok=>{const run=RUNS.find(r=>r.id===REVISE_RUN);if(ok&&run)renderRevise(run)});return}
  const versionPreview=event.target.closest("[data-version-preview]");if(versionPreview){const run=RUNS.find(r=>r.id===versionPreview.dataset.versionPreview);if(run)renderPreview(run);return}
  const previewFilter=event.target.closest("[data-preview-filter]");if(previewFilter){const main=el("preview-main");main.classList.toggle("cv-only",previewFilter.dataset.previewFilter==="cv");main.classList.toggle("cover-only",previewFilter.dataset.previewFilter==="cover");document.querySelectorAll("[data-preview-filter]").forEach(node=>node.classList.toggle("on",node===previewFilter));return}
  const recompile=event.target.closest("[data-recompile]");if(recompile){recompile.disabled=true;postRun("/api/runs/"+recompile.dataset.recompile+"/compile").then(ok=>{const run=RUNS.find(r=>r.id===recompile.dataset.recompile);if(ok&&run)renderPreview(run)});return}
  const runNode=event.target.closest("[data-run]");if(runNode){const run=RUNS.find(r=>r.id===runNode.dataset.run);if(run)renderTailor(run);return}
  const pill=event.target.closest("#runpill");if(pill){const run=RUNS.find(r=>r.id===pill.dataset.run);if(run)renderTailor(run);return}
  const approve=event.target.closest(".approve");if(approve){approve.disabled=true;postRun("/api/runs/"+approve.dataset.runId+"/approve",{phase:approve.dataset.phase});return}
  const cancel=event.target.closest(".cancelrun");if(cancel){postRun("/api/runs/"+cancel.dataset.runId+"/cancel");return}
  const row=event.target.closest("tr[data-row]");if(row&&!event.target.closest("select")){sel=+row.dataset.row;render()}
});
async function postCompany(path,body,method="POST"){const response=await fetch(path+"?t="+T,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});let data={};try{data=await response.json()}catch(_){}if(!response.ok){toast(data.error||"registry request failed",{warn:true,ms:5000});if(response.status===409)renderCompanies();return false}COMPANIES=data.companies?data:(data.result&&data.mtime?data:COMPANIES);return true}
document.addEventListener("submit",event=>{
  if(event.target.id==="company-add"){event.preventDefault();postCompany("/api/companies",{mtime:COMPANIES.mtime,name:el("company-name").value,website:el("company-website").value,tier:3}).then(ok=>ok&&renderCompanies());return}
  if(event.target.id!=="revise-form")return;event.preventDefault();const button=event.submitter,kind=button?.dataset.reentryKind,run=RUNS.find(r=>r.id===REVISE_RUN);if(!run||!kind)return;
  const scope=new FormData(event.target).get("scope")||"both",note=el("revision-note").value.trim(),remember=el("revision-remember").value.trim();
  if(kind!=="apply"&&!note){toast("say what should change",{warn:true});return}
  button.disabled=true;const body={job_url:run.job_url,kind,note,scope,remember};if(kind!=="apply")body.parent=run.id;
  postRun("/api/runs",body).then(ok=>{if(ok){restoreWorkspace();toast(kind+" queued",{ms:2500})}else button.disabled=false});
});
document.addEventListener("change",event=>{
  if(event.target.matches("select[data-url]"))update(event.target.dataset.url,{status:event.target.value});
  if(event.target.matches("[data-note-input]"))update(event.target.dataset.noteInput,{note:event.target.value}).then(ok=>ok&&toast("note saved"));
});
document.addEventListener("keydown",event=>{
  if(MODAL_RESOLVE){if(event.key==="Escape"){event.preventDefault();closeTextModal(false)}else if((event.metaKey||event.ctrlKey)&&event.key==="Enter"){event.preventDefault();closeTextModal(true)}return}
  const typing=/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
  if(event.key==="/"&&!typing){event.preventDefault();el("q").focus();return}
  if(typing){if(event.key==="Escape")event.target.blur();return}
  if(event.key==="Escape"&&!el("drawer").hidden){toggleDrawer();return}
  if(event.key==="Escape"&&el("app").classList.contains("expanded")){restoreWorkspace();return}
  if(event.key==="["){event.preventDefault();toggleCollapse("left");return}
  if(event.key==="]"){event.preventDefault();toggleCollapse("right");return}
  if(event.key==="\\"){event.preventDefault();resetLayout();return}
  const rows=shown(),move={j:1,ArrowDown:1,k:-1,ArrowUp:-1};
  if(event.key in move){event.preventDefault();sel=Math.min(rows.length-1,Math.max(0,sel+move[event.key]));if(el("app").classList.contains("reader-mode"))renderReader();else render();return}
  if(event.key==="Enter"){const j=selectedJob();if(j)open(j.open_url||j.url,"_blank","noopener");return}
  if(event.key==="e"){event.preventDefault();editNote();return}if(event.key==="t"){event.preventDefault();const j=selectedJob();if(j)startTailor(j.url);return}
  if(event.key==="z"){event.preventDefault();undo();return}
  const map={s:"star",y:"yes",m:"maybe",n:"no",a:"applied",u:"new",g:"gate"};if(event.key in map){event.preventDefault();setStatus(map[event.key])}
});
el("q").addEventListener("input",event=>{q=event.target.value;sel=0;render()});

setInterval(pollActivity,3000);setInterval(pollRuns,6000);
Promise.all([reloadJobs(),pollActivity(),pollRuns()]).then(()=>{
  applyRoute();
  fetch("/api/fetch/status?t="+T).then(r=>r.json()).then(status=>{renderFetchLog(status);if(status.running){el("fetch").disabled=true;POLL=setInterval(pollFetch,2000)}});
});
