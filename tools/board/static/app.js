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
// Which view is on screen. The `-mode` classes cannot answer that: openView
// marks *every* parked view "tailor-mode" because they share one layout, so a
// live refresh that asked the class list repainted the tailor run over whatever
// was actually open - the companies view lasted until the next 3-second poll.
let VIEW=null;
let COMPANIES=null,COMPANY_FILTER="all",COMPANY_SELECTED=null;
let COMPANY_BUSY=null,COMPANY_ERROR="",COMPANY_QUERY="",COMPANY_ADD_DRAFT={name:"",url:""};
let EV=[],EPOCH=null,SEQ=0,actFilter="all",COUNTS={};
let RUN_FOLLOW=true;
const RUN_LOG_SCROLL=new Map();
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

// A dead token is not an empty board. Every API answer carries a body, and a
// 403's body - {"error":"forbidden"} - is valid JSON, so the poll sites used to
// parse it, find no `runs`/`events` key, and render the fallback: a board with
// nothing on it, and no error anywhere on screen. The token now outlives a
// restart (server.py load_token), so this should be rare - but when a link
// really is stale the page says so instead of showing you a past that is not
// there.
let STALE=false;
function checkAuth(response){
  const stale=response.status===403;
  if(stale!==STALE){
    STALE=stale;const banner=el("stale");banner.hidden=!stale;
    banner.textContent=stale?"This tab's link is not valid for the board that is running \u2014 nothing below is live. Open the URL printed in your terminal.":"";
  }
  return !stale;
}

function toast(msg,opts={}){
  const box=el("toast"),btn=el("toastundo");el("toastmsg").textContent=msg;
  clearTimeout(toastTimer);clearInterval(undoTimer);box.classList.toggle("warn",!!opts.warn);box.classList.add("on");
  if(opts.undo){let left=opts.seconds||6;btn.hidden=false;el("toastsec").textContent="("+left+")";
    undoTimer=setInterval(()=>{left--;el("toastsec").textContent="("+left+")";if(left<=0)clearInterval(undoTimer)},1000);
    toastTimer=setTimeout(()=>{box.classList.remove("on");btn.hidden=true},left*1000);
  }else{btn.hidden=true;toastTimer=setTimeout(()=>box.classList.remove("on"),opts.ms||1400)}
}

const buildFilters=()=>[["active","active"],...STATUSES.map(s=>[s,s]),["all","all"]];

// ---------------------------------------------------------------- facets
//
// The status chip answers "where is this in my pipeline". These answer the
// three other questions the list could not be asked before: where did it come
// from, how well does it score, and *when did it reach me*. They are separate
// predicates rather than one growing `match()` so the source chips can count
// what turning one back on would bring in - a count computed with every facet
// applied except the one being counted.
//
// They live in localStorage, not in the URL. §0.5 gives the fragment one job,
// naming the view you are on; "never show me freehire" is a standing
// preference and belongs with the other ones. The status chip deliberately
// does not persist: it is a triage move you make and unmake within a sitting,
// and coming back tomorrow to a board silently stuck on `no` would be a bug
// with a plausible-looking cause.
const BOARD_KEY="jobflow.board.v1";
const FACET_DEFAULTS={version:1,hidden:[],fit:"any",found:"any",sort:"priority"};
const SORT_KEYS=["priority","found","found-asc","posted","score","company","role"];
const FIT_VALUES=["any","high","medium","low","unranked"];
const FOUND_VALUES=["any","latest","1","7","30"];
function loadFacets(){
  try{
    const value=JSON.parse(localStorage.getItem(BOARD_KEY));
    if(value?.version===1)return {
      version:1,
      hidden:Array.isArray(value.hidden)?value.hidden.filter(v=>typeof v==="string"):[],
      fit:FIT_VALUES.includes(value.fit)?value.fit:"any",
      found:FOUND_VALUES.includes(value.found)?value.found:"any",
      sort:SORT_KEYS.includes(value.sort)?value.sort:"priority",
    };
    localStorage.removeItem(BOARD_KEY);
  }catch(_){try{localStorage.removeItem(BOARD_KEY)}catch(__){}}
  return {...FACET_DEFAULTS,hidden:[]};
}
let facets=loadFacets();
function saveFacets(){try{localStorage.setItem(BOARD_KEY,JSON.stringify(facets))}catch(_){}}
const facetsAreDefault=()=>!facets.hidden.length&&facets.fit==="any"&&facets.found==="any"&&facets.sort==="priority";
function resetFacets(){facets={...FACET_DEFAULTS,hidden:[]};saveFacets()}

// The run stamp of the most recent fetch, read from /api/fetch/status. It is
// what "latest fetch" means, and it comes from the fetch rather than from the
// rows on purpose: a run that found nothing new must mark nothing, where
// "whatever arrived most recently" would have gone on pointing at the run
// before it and quietly called week-old rows new.
let LAST_FETCH=null;
const foundAt=j=>j.first_seen_at||j.first_seen||"";

// …but only a fetch that ran since arrival stamping existed leaves a stamp
// behind, and a board full of rows from before it would otherwise answer "what
// did the last fetch bring in" with an empty table - which is what it did, on a
// board holding 98 rows collected that morning.
//
// So when no stamp is recorded, fall back to the newest arrival the board
// actually holds. It is the weaker claim the paragraph above rejects - it
// cannot tell "this run added nothing" from "this run added these" - so it is
// only ever the fallback, and `batchIsExact()` is what the control reads to
// name itself honestly rather than the board quietly meaning something else.
function latestBatch(){
  if(LAST_FETCH)return LAST_FETCH;
  let newest="";
  for(const job of JOBS){const at=foundAt(job);if(at>newest)newest=at}
  return newest;
}
const batchIsExact=()=>!!LAST_FETCH;
function isNewArrival(j){
  const batch=latestBatch();
  return !!batch&&!!foundAt(j)&&foundAt(j)>=batch;
}
const sourceKey=j=>j.primary_source||j.portal||"";
// Local midnight `days-1` days back, formatted by hand: toISOString() would
// convert local midnight to the previous day everywhere east of UTC.
function dayFloor(days){
  const d=new Date();d.setHours(0,0,0,0);d.setDate(d.getDate()-(days-1));
  const pad=n=>String(n).padStart(2,"0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
}
const inStatus=j=>filter==="all"||filter==="active"&&ACTIVE.includes(j.status)||j.status===filter;
function inQuery(j){
  if(!q)return true;
  const hay=[j.title,j.company,j.location,j.why,j.note].join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).every(word=>hay.includes(word));
}
const inSource=j=>!facets.hidden.includes(sourceKey(j));
const inFit=j=>facets.fit==="any"||(facets.fit==="unranked"?!j.fit:j.fit===facets.fit);
function inFound(j){
  if(facets.found==="any")return true;
  if(facets.found==="latest")return isNewArrival(j);
  // A date is a prefix of the timestamp that replaced it, so one string
  // comparison covers both the stamped rows and the older date-only ones.
  return foundAt(j)>=dayFloor(+facets.found);
}
function match(j){return inStatus(j)&&inQuery(j)&&inSource(j)&&inFit(j)&&inFound(j)}

// Sorting is a client-side reorder of the payload, never a second opinion
// about it: `priority` is the absence of a sorter, so the default order stays
// the one `state._sorted()` and the markdown export agree on. Every other key
// sorts a copy, and because Array#sort is stable, rows that tie fall back to
// that same server order rather than to an arbitrary one.
const byName=(a,b)=>String(a||"").localeCompare(String(b||""),undefined,{sensitivity:"base"});
// Not every `posted` is a date. LinkedIn hands back "6 days ago" and "Reposted
// 2 weeks ago" for a fair share of rows, which string-sort among the dates as
// nonsense - so an unparseable one is *unknown*, and unknown sinks rather than
// pretending to a position. `first_seen` is always a real date, which is half
// of why arrival is the more honest column to sort on.
const isoDate=value=>/^\d{4}-\d{2}-\d{2}/.test(String(value||""))?String(value):"";
const undated=(x,y)=>x===y?0:!x?1:!y?-1:null;   // unknown sinks, either way round
const newestFirst=(a,b)=>{
  const x=isoDate(a),y=isoDate(b),sunk=undated(x,y);
  return sunk===null?(x<y?1:-1):sunk;
};
const oldestFirst=(a,b)=>{
  const x=isoDate(a),y=isoDate(b),sunk=undated(x,y);
  return sunk===null?(x<y?-1:1):sunk;
};
const SORTERS={
  found:(a,b)=>newestFirst(foundAt(a),foundAt(b)),
  "found-asc":(a,b)=>oldestFirst(foundAt(a),foundAt(b)),
  posted:(a,b)=>newestFirst(a.posted,b.posted),
  score:(a,b)=>(b.score||0)-(a.score||0),
  company:(a,b)=>byName(a.company,b.company),
  role:(a,b)=>byName(a.title,b.title),
};
function shown(){
  const rows=JOBS.filter(match),sorter=SORTERS[facets.sort];
  return sorter?rows.sort(sorter):rows;
}
const selectedJob=()=>shown()[sel]||null;
const baseOptions=(selected="auto",recommended="")=>[
  ["auto",`Auto${recommended?" (recommended: "+recommended.toUpperCase()+")":""}`],
  ["sde","SDE"],["ai","AI / ML Engineer"]
].map(([value,label])=>`<option value="${value}" ${selected===value?"selected":""}>${label}</option>`).join("");
// Not every posting deserves both documents: a speculative application may want
// the letter alone, and a portal that only takes a CV has nowhere to put one.
// The fit evaluation is the first honest moment to make that choice, so it lives
// only on the approval card instead of being asked once before and once after.
const SCOPES=[["both","CV + cover letter"],["cv","CV only"],["cover","Cover letter only"]];
const DRAFT_LABEL={both:"Draft CV + cover letter",cv:"Draft CV",cover:"Draft cover letter"};
const DRAFT_STEP={both:["Draft CV + cover letter","Tailors both documents and audits every factual claim."],
  cv:["Draft CV","Tailors the CV and audits every factual claim."],
  cover:["Draft cover letter","Tailors the letter and audits every factual claim."]};
const DOC_TITLE={cv:"CV",cover:"cover letter"};
const docKinds=scope=>(scope||"both")==="both"?["cv","cover"]:[scope];
const scopeOptions=(selected="both")=>SCOPES.map(([value,label])=>`<option value="${value}" ${selected===value?"selected":""}>${label}</option>`).join("");
const SOURCE_LABELS={"linkedin-search":"LinkedIn search","linkedin-browser":"LinkedIn browser","ats-search":"Company ATS","company-careers":"Company careers","freehire-search":"freehire"};
const sourceLabel=value=>SOURCE_LABELS[value]||String(value||"Other website").replace(/-search$/,"").replaceAll("-"," ");
const sourceTitle=job=>[...new Set([job.primary_source,...(job.sources||[])].filter(Boolean))].map(sourceLabel).join(" · ");

function renderChips(){
  const counts={all:JOBS.length,active:JOBS.filter(j=>ACTIVE.includes(j.status)).length};
  STATUSES.forEach(s=>counts[s]=JOBS.filter(j=>j.status===s).length);
  el("chips").innerHTML=FILTERS.map(([key,label])=>`<button class="chip ${key===filter?"on":""}" data-filter="${esc(key)}">${esc(label)}<span class="n">${counts[key]||0}</span></button>`).join("");
}

// One chip per source the board actually holds - not per source the fetcher
// can run, because a portal you stopped fetching from still owns rows you can
// see. Each count is what that chip would add back, so it is taken with every
// facet applied *except* this one: a chip reading 0 because the fit filter
// excludes its rows is telling you the truth about clicking it.
function renderSourceChips(){
  const pool=JOBS.filter(j=>inStatus(j)&&inQuery(j)&&inFit(j)&&inFound(j));
  const counts=new Map();
  for(const j of JOBS)if(!counts.has(sourceKey(j)))counts.set(sourceKey(j),0);
  for(const j of pool)counts.set(sourceKey(j),(counts.get(sourceKey(j))||0)+1);
  const chips=[...counts.keys()].sort((a,b)=>byName(sourceLabel(a),sourceLabel(b)));
  el("sourcechips").innerHTML=chips.map(key=>{
    const on=!facets.hidden.includes(key);
    return `<button class="chip source-chip ${on?"on":""}" data-source="${esc(key)}" aria-pressed="${on}"
      title="${esc(sourceLabel(key))} — click to ${on?"hide":"show"}, alt-click to show only this source">${esc(sourceLabel(key))}<span class="n">${counts.get(key)}</span></button>`;
  }).join("");
  el("f-fit").value=facets.fit;el("f-found").value=facets.found;el("f-sort").value=facets.sort;
  // The option names what it can actually deliver. Without a run stamp the best
  // the board can do is the newest arrival date it holds, which on a board
  // fetched twice in one day is a day, not a run.
  const exact=batchIsExact(),option=el("f-found").options[1];
  option.textContent=exact?"latest fetch":"newest batch";
  option.title=exact?"The rows the last fetch inserted."
    :"No fetch has recorded its own timestamp yet, so this is everything that arrived on the most recent day the board saw new rows. The next fetch will be exact.";
  el("f-reset").hidden=facetsAreDefault();
}
function render(){
  renderChips();renderSourceChips();
  const rows=shown();if(sel>=rows.length)sel=Math.max(0,rows.length-1);
  el("tb").innerHTML=rows.map((j,i)=>`<tr class="${i===sel?"sel":""}${isNewArrival(j)?" fresh":""}" data-row="${i}">
    <td><span class="fitword ${esc(j.fit)}">${esc(j.fit||"—")}</span></td>
    <td class="role" title="${esc(j.title)}">${esc(j.title)}</td><td class="co">${esc(j.company)}</td><td class="co sourcecell" title="${esc(sourceTitle(j))}">${esc(sourceLabel(j.primary_source||j.portal))}</td>
    <td class="co">${esc(j.location)}</td><td class="co" title="${esc(j.posted)}">${esc(postedLabel(j))}</td>
    <td class="co foundcell" title="${esc(foundTitle(j))}">${isNewArrival(j)?'<span class="newdot" aria-label="new in the latest fetch"></span>':""}${esc(foundAt(j).slice(5,10))}</td>
    <td><select data-url="${esc(j.url)}">${STATUSES.map(s=>`<option value="${esc(s)}" ${s===j.status?"selected":""}>${esc(s)}</option>`).join("")}</select></td>
    <td class="wide-only why" title="${esc(j.why)}">${esc(j.why)}${j.dupes?.length?'<span class="dupe"> · possible dupe</span>':""}</td>
    <td class="wide-only note" data-note="${esc(j.url)}">${esc(j.note)}</td></tr>`).join("");
  const fresh=JOBS.filter(isNewArrival).length;
  el("empty").hidden=rows.length>0;
  el("empty").textContent=rows.length||facetsAreDefault()?"Nothing here."
    :"No job matches these filters. Reset them to see the rest of the board.";
  el("count").textContent=`${rows.length} shown · ${JOBS.length} total${fresh?` · ${fresh} new`:""}`;
  renderJob();document.querySelector("tr.sel")?.scrollIntoView({block:"nearest"});
}
// "Found" is when the row reached the board, which is not when it was posted:
// a job posted in June can arrive today because a company was added to the
// registry today. The cell prints a bare MM-DD like the Posted column beside
// it; the full stamp, and what it means, are in the tooltip.
// MM-DD for a date. For the rows where LinkedIn gives an interval instead, the
// same interval abbreviated - "Reposted 2 weeks ago" becomes "2 w" - so a 58px
// column shows the whole fact rather than a truncated "Repo…". Nothing is
// converted into a date here: the collector did not know one, and inventing it
// from the day the page happens to be open would be a guess printed as data.
const AGE_UNITS={hour:"h",day:"d",week:"w",month:"mo",year:"y"};
function postedLabel(j){
  const raw=String(j.posted||"");
  if(isoDate(raw))return raw.slice(5,10);
  const age=raw.match(/(\d+)\s*(hour|day|week|month|year)/i);
  return age?age[1]+" "+AGE_UNITS[age[2].toLowerCase()]:raw;
}
function foundTitle(j){
  const at=foundAt(j);
  if(!at)return "Not recorded - this row predates arrival stamping.";
  return (isNewArrival(j)?"Arrived in the latest fetch, ":"Arrived ")
    +at.replace("T"," ")+(j.posted&&j.posted!==j.first_seen?" · posted "+j.posted:"");
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
      <button class="primary tailor" data-tailor="${esc(j.url)}">Evaluate fit</button>
      <div class="hint">opens the run · you choose documents after the fit evaluation</div></div>`;
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
  if(seconds<90)return seconds+" s";
  const minutes=Math.round(seconds/60);if(minutes<90)return minutes+" min";
  // A run parked overnight waiting for approval used to read "4485 min".
  const hours=Math.round(minutes/60);return hours<36?hours+" h":Math.round(hours/24)+" d";
}
function activeRuns(){return RUNS.filter(r=>RUNNING.includes(r.phase)||r.phase==="awaiting_approval"||r.phase==="orphaned")}
function latestApplications(){
  const latest=new Map();RUNS.forEach(r=>{const key=r.application_id||r.id;if(!latest.has(key))latest.set(key,r)});return [...latest.values()]
}
function renderRuns(){
  const applications=latestApplications(),live=applications.filter(r=>activeRuns().includes(r));
  const rows=live.concat(applications.filter(r=>!live.includes(r))),drafted=applications.filter(r=>r.phase==="done").length;
  const counts=[];if(QUEUE.length)counts.push(QUEUE.length+" queued");if(live.length)counts.push(live.length+" active");
  if(!counts.length&&drafted)counts.push(drafted+" drafted");
  el("runqueue").textContent=counts.join(" · ");
  el("runlist").innerHTML=rows.length?rows.map(r=>{
    const step=PHASE_STEP[r.phase]||1,pct=Math.round(step/6*100),running=RUNNING.includes(r.phase),bad=["failed","orphaned"].includes(r.phase);
    // Live rows answer "how far along"; finished ones answer "when, and which
    // attempt" - the two columns the Applications table used to carry.
    const meta=[r.phase.replaceAll("_"," ")];
    if(running||r.phase==="awaiting_approval")meta.push("step "+step+"/6");
    else if(r.started_at)meta.push(r.started_at.slice(5,16).replace("T"," "));
    if(elapsed(r))meta.push(elapsed(r));
    if((r.attempt||1)>1)meta.push("attempt "+r.attempt);
    // Nothing navigates except a link you aimed at: the card itself is inert.
    const actions=[`<button class="linkish runopen" data-run="${esc(r.id)}">${running?"Watch run":"Open run"} <span class="runarrow">↗</span></button>`];
    if(r.phase==="done")actions.push(`<button class="linkish" data-preview="${esc(r.id)}">Preview</button>`,`<button class="linkish" data-revise="${esc(r.id)}">Revise</button>`);
    return `<div class="runitem ${running?"live":""} ${ACTIVE_RUN===r.id?"selected":""}">
      <div class="runwho"><strong>${esc(r.company)}</strong><span class="runrole">${esc(r.role)}</span></div>
      <div class="runmeta ${bad?"runerror":""}">${meta.map(bit=>`<span>${esc(bit)}</span>`).join("<span>·</span>")}</div>
      ${running?`<div class="runprogress"><span style="width:${pct}%"></span></div>`:""}
      <div class="runactions">${actions.join("")}</div></div>`;
  }).join(""):'<div class="panel-empty">No runs yet.</div>';
  const active=live.find(r=>RUNNING.includes(r.phase))||live[0];el("striprunning").textContent=live.length?live.length+" running":"";
  if(active){el("runpill").hidden=false;el("runpill").innerHTML=`<span>Tailoring <strong>${esc(active.company)}</strong> · step ${PHASE_STEP[active.phase]||1} of 6</span><span class="elapsed">${elapsed(active)}</span>`;el("runpill").dataset.run=active.id}
  else el("runpill").hidden=true;
  el("right-sep").hidden=!active;
  if(VIEW==="tailor"&&ACTIVE_RUN)renderTailor(RUNS.find(r=>r.id===ACTIVE_RUN));
}
async function pollRuns(){
  let data;try{const response=await fetch("/api/runs?t="+T);if(!checkAuth(response)||!response.ok)return;data=await response.json()}catch(_){return}
  const before=JSON.stringify(RUNS.map(r=>[r.id,r.phase]));RUNS=data.runs||[];QUEUE=data.queue||[];LEDGER=data.ledger;BUDGET=data.budget_usd;renderRuns();
  if(JSON.stringify(RUNS.map(r=>[r.id,r.phase]))!==before)render();
  const busy=activeRuns().length>0;if(busy&&!RUNPOLL)RUNPOLL=setInterval(pollRuns,2000);if(!busy&&RUNPOLL){clearInterval(RUNPOLL);RUNPOLL=null}
}
// Returns the response body with `ok` on it. The body is worth keeping: a
// started run answers with its `run_id`, and so does the 409 that says one is
// already in flight for this posting - which is the id you want to be taken to
// rather than told about.
async function postRun(path,body){
  const response=await fetch(path+"?t="+T,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
  checkAuth(response);
  let data={};try{data=await response.json()}catch(_){}
  if(!response.ok)toast(data.error||("request failed ("+response.status+")"),{warn:true,ms:6000});
  await pollRuns();pollActivity();return {...data,ok:response.ok};
}
// Starting an evaluation opens the run it started.
//
// It used to stay on the board and say so in a toast, which was the wrong
// answer twice: the toast is fixed to the bottom-right corner, which is exactly
// where the right rail keeps this button, so the confirmation landed on top of
// the control that had just been pressed - and what it said ("evaluating - you
// will choose documents before drafting") described a screen you were not being
// shown. The run's own view carries the same information as six named steps,
// and `Esc` comes back.
async function startTailor(url){
  const j=JOBS.find(x=>x.url===url);if(!j)return;
  const note=await openTextModal({eyebrow:"Evaluate application",title:j.company+" — "+j.title,label:"One-off instruction (optional)",placeholder:"Emphasize a project, explain a transition, or leave this empty…",hint:"This instruction applies only to this run unless you later add it as a standing preference.",submit:"Start fit evaluation"});
  if(note===null)return;
  const started=await postRun("/api/runs",{job_url:url,kind:"apply",note});
  // `run_id` on a refusal means this posting already has a run: `postRun` has
  // said so, and the honest next move is to show it rather than leave you to
  // find it in the rail.
  const run=started.run_id&&RUNS.find(r=>r.id===started.run_id);
  if(run){renderTailor(run);return}
  // The run list is refreshed inside postRun and the supervisor records the run
  // before it answers, so this is the blipped-poll case rather than a real one.
  // Say something anyway: a press that neither moves nor speaks reads as broken.
  if(started.ok)toast("evaluation queued — open it from Runs",{ms:3000});
}

// A run's identity belongs in the document it is about, not squeezed into the
// app bar's uppercase breadcrumb slot where it read as a system label.
const runTitle=run=>`<header class="runtitle"><h1>${esc(run.company)}</h1><p>${esc(run.role)}</p></header>`;
const STEPS=[
  ["Evaluate fit","Posting fetched and scored against your profile. Pauses for your go-ahead."],
  ["Draft CV + cover letter","Tailors both documents and audits every factual claim."],
  ["Reviewer critique","A second pass attacks grounding first, then style."],
  ["Revise","Structured edits are applied; invented facts are skipped."],
  ["Compile PDFs","Runs the registered document toolchains and page checks."],
  ["Verify","ATS text extraction and the final verification checklist."]
];
function renderTailor(run){
  if(!run){restoreWorkspace();return}
  const previousLog=el("runlog");if(previousLog&&ACTIVE_RUN)RUN_LOG_SCROLL.set(ACTIVE_RUN,previousLog.scrollTop);
  ACTIVE_RUN=run.id;const current=PHASE_STEP[run.phase]||1,fit=run.fit||{};
  const lineage=RUNS.filter(r=>(r.application_id||r.id)===(run.application_id||run.id)).sort((a,b)=>(a.attempt||1)-(b.attempt||1));
  const kinds=docKinds(run.scope);
  // `approved_at` is authoritative for current records. The phase fallback
  // keeps completed records from older registries readable after an upgrade.
  const documentsChosen=run.kind!=="apply"||!!run.approved_at||
    ["drafting","reviewing","compiling","inspecting","done"].includes(run.phase);
  const pendingDraft=["Choose & draft documents","After fit review, choose CV, cover letter, or both."];
  const steps=STEPS.map((s,i)=>{const n=i+1,state=n<current||run.phase==="done"?"done":n===current?"live":"todo",step=i===1?(documentsChosen?DRAFT_STEP[run.scope||"both"]:pendingDraft):s;return `<div class="step ${state}"><div class="steprow"><span class="stepdot">${n}</span><div><div class="steptitle">${step[0]}</div><div class="stepdetail">${step[1]}</div></div><span class="steptime">${state==="live"?esc(run.phase.replaceAll("_"," ")):""}</span></div></div>`}).join("");
  const matches=(fit.matches||[]).map(x=>`<div>${esc(x)}</div>`).join("")||"<div>No structured match list yet.</div>";
  const gaps=(fit.gaps||[]).map(x=>`<div>${esc(x)}</div>`).join("")||"<div>No structured gap list yet.</div>";
  const logs=EV.filter(e=>e.run_id===run.id&&(e.source==="claude"||e.source==="latex"||e.source==="verify")).slice(-100).map(e=>`<div><span>${esc((e.ts||"").slice(11,19))}</span>&nbsp; ${esc(e.cmd?"$ "+e.cmd:e.msg)}</div>`).join("")||"<div>No run-specific activity recorded yet.</div>";
  const failureTitle=run.failure_code==="provider_rate_limit"?"Claude session limit reached":run.failure_code==="budget_cap"?"Local run budget exhausted":"Run failed";
  const failure=run.phase==="failed"?`<div class="failure-card"><strong>${failureTitle}</strong><div>${esc(run.error||"No error detail was recorded.")}</div><div class="dim">Failed during ${esc((run.failed_phase||"unknown").replaceAll("_"," "))}${run.model_started===false?" · no model work started":""}${Number(run.cost?.total_usd||0)===0?" · no model cost incurred":""}</div></div>`:"";
  el("tailor-view").innerHTML=`<div class="tailor-shell"><section class="pipeline"><div class="panelhead"><span class="label">Pipeline</span><span class="spacer"></span><span class="dim">step ${current} of 6</span></div>${steps}<div class="writing"><div class="label">${documentsChosen?"Writing to":"Documents"}</div>${documentsChosen?kinds.map(kind=>`<div>${esc(run.targets?.[kind]||DOC_TITLE[kind]+" target pending")}</div>`).join(""):"<div>Choose after fit evaluation.</div>"}</div></section>
    <section class="runoutput"><div class="panelhead"><span class="label">Run output${lineage.length>1?` · attempt ${esc(run.attempt||1)} of ${lineage.length}`:""}</span><span class="spacer"></span><label class="source"><input type="checkbox" data-run-follow ${RUN_FOLLOW?"checked":""}> follow</label><span class="phase ${esc(run.phase)}">${esc(run.phase.replaceAll("_"," "))}</span></div>
      ${runTitle(run)}<div class="fitcard"><div class="fithead"><span class="fitword ${fit.overall>=70?"high":fit.overall>=50?"medium":"low"}">✓</span><strong>Fit evaluation — ${esc(fit.verdict||"pending")}${fit.overall!=null?", "+esc(fit.overall):""}</strong><span class="spacer"></span><span class="dim">${run.phase==="awaiting_approval"?"waiting for your approval":""}</span></div>
      <div class="fitgrid"><div><span class="label">Matches</span>${matches}</div><div><span class="label">Gaps, stated not smoothed</span>${gaps}</div></div></div>
      ${failure}<div class="runlog" id="runlog">${logs}</div><div class="runfooter"><span class="dim">Closing this panel does not stop the run.</span><span class="spacer"></span>
      ${run.phase==="awaiting_approval"?`<label class="base-picker compact"><span>Documents</span><select data-run-scope>${scopeOptions(run.scope||"both")}</select></label><label class="base-picker compact"><span>CV base</span><select data-run-base>${baseOptions(run.base_cv||"auto",run.recommended_base_cv||"")}</select></label><button class="primary approve" data-run-id="${esc(run.id)}" data-phase="${esc(run.phase)}">${esc(DRAFT_LABEL[run.scope||"both"])}</button>`:""}${RUNNING.includes(run.phase)?`<button class="secondary cancelrun" data-run-id="${esc(run.id)}">Cancel run</button>`:""}${run.phase==="failed"?`<button class="primary retryrun" data-run-id="${esc(run.id)}">Retry from beginning</button>`:""}${run.phase==="done"?`<button class="primary" data-preview="${esc(run.id)}">Preview PDFs</button>`:""}</div></section></div>`;
  const runlog=el("runlog");if(RUN_FOLLOW)runlog.scrollTop=runlog.scrollHeight;
  else if(RUN_LOG_SCROLL.has(run.id))runlog.scrollTop=RUN_LOG_SCROLL.get(run.id);
  openView("tailor",[crumbRun(run)],"/run/"+encodeURIComponent(run.id));
}

function renderReader(){
  const rows=shown(),j=selectedJob();if(!j)return;
  const queue=rows.slice(0,80).map((row,index)=>`<div class="queue-row ${index===sel?"selected":""}" data-reader-row="${index}"><div class="queue-top"><span class="fitdot ${esc(row.fit)}"></span><span class="queue-company">${esc(row.company)}</span><span class="queue-mark">${esc(row.status==="new"?"":row.status)}</span></div><div class="queue-title">${esc(row.title)}</div></div>`).join("");
  const marks=["star","yes","maybe","gate","no","applied"].map(s=>`<button class="${j.status===s?"on":""}" data-status="${s}">${s}</button>`).join("");
  el("tailor-view").innerHTML=`<div class="reader-shell"><section class="reader-queue"><div class="panelhead"><span class="label">Queue</span><span class="spacer"></span><span class="dim">${rows.length} active</span></div><div class="queue-list">${queue}</div></section>
    <section class="reader-main"><div class="reader-scroll"><article class="reader-copy"><div class="reader-badges"><span class="fitword ${esc(j.fit)}">${esc(j.fit||"unranked")}</span><span class="badge">${esc(j.portal||"source unknown")}</span>${j.score?`<span class="badge" title="${esc(scoreTitle(j))}">${esc(SCORE_LABEL[j.score_source]||"score")} ${esc(Math.round(j.score))}</span>`:""}${j.fit_evidence==="title-only"?'<span class="badge">title only</span>':""}<span class="spacer"></span><a class="open" href="${esc(j.open_url||j.url)}" target="_blank" rel="noopener">open posting ↗</a></div><h1>${esc(j.title)}</h1><div class="reader-meta"><strong>${esc(j.company)}</strong><span>·</span><span>${esc(j.location)}</span><span>·</span><span>posted ${esc(j.posted).slice(5)}</span></div><div class="reader-posting" id="postingbody">${esc(postingText(j))}</div></article></div></section>
    <aside class="reader-decide"><div class="panelhead"><span class="label">Decide</span></div><div class="decision-section"><span class="label">Gates</span>${gateLines(j)}</div><div class="decision-section"><span class="label">Why it surfaced</span><div class="dim">${esc(j.why||"No reason was stored.")}</div><div class="hint">${esc(scoreTitle(j))} Evaluation reads the full posting.</div></div><div class="decision-section"><span class="label">Mark it</span><div class="statusbuttons">${marks}</div><textarea class="noteinput" data-note-input="${esc(j.url)}" placeholder="note to yourself — saved on blur">${esc(j.note)}</textarea><button class="primary tailor" data-tailor="${esc(j.url)}">Evaluate fit</button><div class="hint">opens the run · you choose documents after the fit evaluation · <kbd>t</kbd></div></div></aside></div>`;
  loadPosting(j);
  openView("reader",[{label:j.company,sub:j.title,count:`${sel+1} of ${rows.length}`}],"/job/"+encodeURIComponent(j.url));
}

async function renderPreview(run){
  if(!run)return;PREVIEW_RUN=run.id;
  el("tailor-view").innerHTML='<div class="panel-empty">Loading compiled documents…</div>';
  openView("preview",[crumbRun(run),{label:"Preview"}],"/run/"+encodeURIComponent(run.id)+"/preview");
  let verify;try{const response=await fetch(`/api/runs/${encodeURIComponent(run.id)}/verify?t=${T}`);verify=await response.json();if(!response.ok)throw new Error(verify.error)}catch(error){el("tailor-view").innerHTML=`<div class="panel-empty runerror">${esc(error.message||error)}</div>`;return}
  const counts={};(verify.checks||[]).forEach(check=>counts[check.state]=(counts[check.state]||0)+1);
  const checks=(verify.checks||[]).map(check=>`<div class="verify-item ${esc(check.state)}"><span class="verify-mark ${esc(check.state)}">${check.state==="pass"?"✓":"!"}</span><div><div>${esc(check.label)}</div><div class="verify-detail">${esc(check.detail)}</div></div></div>`).join("");
  const query=`?t=${encodeURIComponent(T)}&v=${Date.now()}`;
  // The scope says what was asked for; the artefacts say what exists. A run that
  // produced one document gets one frame, one download and no filter tabs.
  const produced=docKinds(run.scope).filter(kind=>(run.artefacts||{})[kind+"_pdf"]);
  const kinds=produced.length?produced:docKinds(run.scope),single=kinds.length===1?kinds[0]:null;
  const tabs=single?`<span class="chip on">${single==="cv"?"CV only":"Letter only"}</span>`
    :`<button class="chip on" data-preview-filter="both">Both documents</button><button class="chip" data-preview-filter="cv">CV only</button><button class="chip" data-preview-filter="cover">Letter only</button>`;
  const frames=kinds.map(kind=>`<iframe title="${kind==="cv"?"Compiled CV":"Compiled cover letter"}" class="pdf-frame ${kind}" src="/api/pdf/${encodeURIComponent(run.id)}/${kind}${query}"></iframe>`).join("");
  const files=kinds.map(kind=>esc(run.artefacts?.[kind+"_pdf"]||kind+".pdf")).join(" · ");
  const downloads=kinds.map(kind=>`<a class="secondary" href="/api/pdf/${encodeURIComponent(run.id)}/${kind}${query}" download>Download ${kind==="cv"?"CV":"letter"}</a>`).join("");
  el("tailor-view").innerHTML=`<div class="preview-shell"><aside class="verify-rail"><div class="panelhead"><span class="label">Verification</span><span class="spacer"></span><span class="dim state-summary">${counts.pass||0} pass · ${(counts.flag||0)+(counts.fail||0)+(counts.unverified||0)} flagged</span></div><div class="verify-list">${checks}</div><div class="decision-section" style="margin-top:auto"><span class="label">Keywords</span><div class="dim">Covered: ${esc((verify.keywords?.covered||[]).join(", ")||"none")}</div><div class="dim">Absent: ${esc((verify.keywords?.absent||[]).join(", ")||"none")}</div></div></aside>
    <section class="preview-main${single?" "+single+"-only":""}" id="preview-main">${runTitle(run)}<div class="preview-tabs">${tabs}<span class="spacer"></span><span class="dim">${files}</span></div><div class="pdf-stage">${frames}</div><div class="preview-foot"><span class="dim">Rendered from the compiled PDFs, not from source files. Every check is reproducible.</span><span class="spacer"></span>${downloads}<button class="secondary" data-recompile="${esc(run.id)}">Recompile</button></div></section></div>`;
}

async function renderRevise(run){
  if(!run)return;REVISE_RUN=run.id;const versions=RUNS.filter(r=>r.slug===run.slug&&r.phase==="done");
  const kinds=docKinds(run.scope);
  const scopeChoices=(kinds.length>1?[["both","CV + cover"],["cv","CV only"],["cover","Cover only"]]
    :[[kinds[0],kinds[0]==="cv"?"CV only":"Cover only"]])
    .map(([value,label],index)=>`<label><input type="radio" name="scope" value="${value}" ${index===0?"checked":""}> ${label}</label>`).join("");
  let prefs={preferences:[]};try{prefs=await(await fetch("/api/prefs?t="+T)).json()}catch(_){}
  const versionRows=versions.map((v,i)=>`<div class="version-row ${v.id===run.id?"current":""}" data-version-preview="${esc(v.id)}"><span>v${versions.length-i}</span><strong>${esc((v.ended_at||v.started_at||"").slice(0,16).replace("T"," "))}</strong><span>${esc(v.kind||"apply")}</span>${v.id===run.id?"<em>current</em>":`<span class="spacer"></span><button class="linkish" data-restore-version="${esc(v.id)}">Restore</button>`}</div>`).join("");
  const prefRows=(prefs.preferences||[]).map(p=>`<li>${esc(p)}</li>`).join("")||"<li>No managed standing preferences.</li>";
  el("tailor-view").innerHTML=`<div class="revise-shell"><aside class="versions"><div class="panelhead"><span class="label">Versions</span><span class="spacer"></span><span class="dim">${versions.length}</span></div><div class="version-list">${versionRows}</div><div class="decision-section"><span class="label">Standing preferences · read only</span><ul class="pref-list">${prefRows}</ul><div class="hint">Remove a preference by editing the managed block in the candidate profile.</div></div></aside><section class="revision-current"><div class="panelhead"><span class="label">Current documents</span><span class="spacer"></span><button class="secondary" data-preview="${esc(run.id)}">Open compiled PDFs</button></div><div class="revision-summary"><h1>${esc(run.company)}</h1><h2>${esc(run.role)}</h2><div class="writing">${kinds.map(kind=>`<div>${esc(run.targets?.[kind])}</div>`).join("")}</div><div class="whybox">Every successful revision becomes another immutable source + PDF snapshot. Restore replaces these live files and recompiles them; it never creates a second live document set.</div></div></section><aside class="composer"><div class="panelhead"><span class="label">Revise</span></div><form id="revise-form"><div class="decision-section"><span class="label">Scope</span>${scopeChoices}</div><div class="decision-section"><label class="label" for="revision-note">What should change?</label><textarea id="revision-note" required placeholder="Make the evidence for… more explicit"></textarea><label class="label" for="revision-remember">Standing preference (optional)</label><textarea id="revision-remember" placeholder="Remember this for future applications"></textarea><div class="hint">Only text in this field is written into the managed preference block.</div></div><div class="composer-actions"><button class="primary" type="submit" data-reentry-kind="revise">Revise</button><button class="secondary" type="submit" data-reentry-kind="redraft">Redraft</button><button class="secondary" type="submit" data-reentry-kind="apply">Full re-run</button></div></form></aside></div>`;
  openView("revise",[crumbRun(run),{label:"Revise"}],"/run/"+encodeURIComponent(run.id)+"/revise");
}

function companySummary(row){
  if(row.status==="paused")return {label:"Paused",tone:"quiet",detail:"This company is saved, but job checks are paused."};
  if(COMPANY_BUSY===row.name)return {label:"Checking…",tone:"working",detail:"Looking for a jobs page and checking available jobs."};
  if(row.monitoring_status==="needs_confirmation"||row.status==="ambiguous")return {label:"Confirm company",tone:"attention",detail:"We found a possible jobs page. Please confirm it belongs to this company."};
  if(row.will_be_searched){
    if(row.fetch_status==="failed")return {label:"Update failed",tone:"attention",detail:"The latest check failed. Any previous results are still shown. You can try again."};
    if(row.last_success_at)return {label:"Updated",tone:"good",detail:"The last successful check is shown in the table. Check again for the latest jobs."};
    return {label:"Not checked",tone:"quiet",detail:"The jobs page is connected. Check jobs to get the first results."};
  }
  const details={
    adapter_missing:"Automatic checks are not available for this jobs website yet. You can still open it directly.",
    policy_disabled:"Automatic access to this jobs website is disabled. You can still open it directly.",
    no_public_board:"No public jobs page has been confirmed for this company.",
    other_route:"This company is saved, but its website is not connected for automatic job checks. You can add a careers link below.",
    careers_url_needed:"A jobs page has not been connected yet. Check jobs to look for one, or add a link below.",
    source_not_detected:"We could not connect a supported jobs page from this link. You can try another link or open the website directly."
  };
  return {label:"Not connected",tone:"quiet",detail:details[row.monitoring_status]||"A jobs page has not been connected yet. Add a website or careers link below."};
}
function companyLink(row){
  const value=row.board_url||row.careers_url||(row.domain?"https://"+row.domain:"");
  try{const url=new URL(value);return ["https:","http:"].includes(url.protocol)?url.href:""}catch(_){return ""}
}
async function renderCompanies(reload=true,scrollTop=null){
  if(reload||!COMPANIES){try{const response=await fetch("/api/companies?t="+T);if(!response.ok)throw new Error();COMPANIES=await response.json()}catch(error){toast("Could not load companies. Please try again.",{warn:true});return}}
  const schemaReady=COMPANIES.schema_version===2&&COMPANIES.company_controls===true;
  const all=COMPANIES.companies||[];
  if(!["all","following","paused"].includes(COMPANY_FILTER))COMPANY_FILTER="all";
  const following=all.filter(row=>row.status!=="paused").length;
  const rows=all.filter(row=>(COMPANY_FILTER==="all"||(row.status==="paused"?"paused":"following")===COMPANY_FILTER)&&[row.name,row.domain,row.careers_url].join(" ").toLowerCase().includes(COMPANY_QUERY.toLowerCase()));
  const selected=rows.find(row=>row.name===COMPANY_SELECTED);
  const shortTime=value=>{if(!value)return "Never";const date=new Date(value);return Number.isNaN(date.getTime())?String(value).slice(0,16):date.toLocaleString("en-GB",{day:"numeric",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"})};
  const marketValue=row=>row?.countries?.length?row.countries.join(","):"";
  const marketOptions=value=>[["","Use search defaults"],["CH","Switzerland"],["DE","Germany"],["CH,DE","Switzerland + Germany"]].map(([key,label])=>`<option value="${key}" ${value===key?"selected":""}>${label}</option>`).join("");
  const disabled=COMPANY_BUSY||!schemaReady?"disabled":"";
  const details=row=>{
    const summary=companySummary(row),link=companyLink(row);
    const canCheck=row.status!=="paused"&&((row.route||"ats")==="ats")&&(!["unsupported_vendor","no_public_board","ambiguous"].includes(row.status))&&row.monitoring_status!=="policy_disabled";
    const candidates=row.status==="ambiguous"?(row.candidates||[]).map(candidate=>`<div class="company-candidate"><a class="open" href="${esc(companyLink(candidate)||"#")}" target="_blank" rel="noopener">Open possible jobs page ↗</a><button class="secondary" ${disabled} data-company-confirm="${esc(candidate.vendor)}|${esc(candidate.token)}">This is the right company</button></div>`).join(""):"";
    return `<tr class="company-detail-row"><td colspan="5"><div class="company-detail"><div class="company-detail-summary"><strong>${esc(row.name)}</strong><p>${esc(summary.detail)}</p>${candidates}<div class="company-actions">${canCheck?`<button class="primary" ${disabled} data-company-check="${esc(row.name)}">${COMPANY_BUSY===row.name?"Checking…":"Check jobs"}</button>`:""}${link?`<a class="secondary" href="${esc(link)}" target="_blank" rel="noopener">Open website ↗</a>`:""}<button class="secondary" ${disabled} data-company-action="${row.status==="paused"?"resume":"pause"}">${row.status==="paused"?"Resume following":"Pause following"}</button><button class="linkish company-remove" ${disabled} data-company-action="remove">Remove company</button></div></div><form id="company-settings" class="company-settings"><label>Company name<input id="company-name-value" required value="${esc(row.name)}"></label><label>Website or careers link<input id="company-careers-value" inputmode="url" value="${esc(row.careers_url||(row.domain?"https://"+row.domain:""))}" placeholder="https://company.com/careers"></label><label>Job locations<select id="company-market-value">${marketOptions(marketValue(row))}</select></label><button class="secondary" ${disabled}>Save changes</button></form></div></td></tr>`;
  };
  const table=rows.map(row=>{
    const summary=companySummary(row),link=companyLink(row),count=row.last_success_at?row.stats?.last_eligible_jobs:null;
    return `<tr class="${row===selected?"sel":""}"><td><strong>${esc(row.name)}</strong></td><td class="company-updated">${esc(shortTime(row.last_success_at))}</td><td>${link?`<a class="open" href="${esc(link)}" target="_blank" rel="noopener">${row.will_be_searched||row.last_success_at?"View jobs":"Open website"} ↗</a>`:'<span class="dim">No link yet</span>'}${count!=null?`<div class="company-match-count">${esc(count)} matching at last update</div>`:""}</td><td><span class="company-state ${summary.tone}" title="${esc(summary.detail)}">${summary.label}</span></td><td class="company-manage-cell"><button class="secondary" data-company-row="${esc(row.name)}" aria-expanded="${row===selected}" aria-label="${row===selected?"Close":"Manage"} ${esc(row.name)}">${row===selected?"Close":"Manage"}</button></td></tr>${row===selected?details(row):""}`;
  }).join("");
  const chips=[["all","All",all.length],["following","Following",following],["paused","Paused",all.length-following]].map(([key,label,count])=>`<button class="chip ${COMPANY_FILTER===key?"on":""}" data-company-filter="${key}">${label}<span class="n">${count}</span></button>`).join("");
  el("tailor-view").innerHTML=`<div class="companies-shell"><section class="companies-main"><div class="companies-head"><h1>Target companies</h1><p>Keep your company list here. Add a link to find and check its jobs page.</p></div>${!schemaReady?'<div class="company-notice" role="status">Restart JobFlow to enable company editing.</div>':""}${COMPANY_ERROR?`<div class="company-notice" role="alert">${esc(COMPANY_ERROR)}</div>`:""}<form id="company-add" class="company-add"><label class="company-url-label">Website or careers link<input id="company-careers" inputmode="url" required value="${esc(COMPANY_ADD_DRAFT.url)}" placeholder="Paste a company or careers URL"></label><label>Company name <span class="dim">(optional)</span><input id="company-name" value="${esc(COMPANY_ADD_DRAFT.name)}" placeholder="Use the name from the link"></label><button class="primary" ${disabled}>${COMPANY_BUSY==="__add__"?"Adding…":"Add company"}</button><span class="hint">We’ll find a supported jobs page and check it when available. You can edit the name and job locations later.</span></form><div class="company-toolbar"><div class="chips">${chips}</div><input id="company-search" type="search" aria-label="Search companies" placeholder="Search companies" value="${esc(COMPANY_QUERY)}"></div><div class="tablewrap"><table class="company-table"><thead><tr><th>Company</th><th>Last updated</th><th>Jobs</th><th>Status</th><th><span class="sr-only">Manage company</span></th></tr></thead><tbody>${table||`<tr><td colspan="5" class="panel-empty">${all.length?"No companies match this view.":"Add your first company using the link field above."}</td></tr>`}</tbody></table></div><div class="companies-foot">Last updated means the last successful check. Matching jobs use your location and language preferences. Use Check jobs to refresh a company.</div></section></div>`;
  if(scrollTop!==null)el("tailor-view").querySelector(".tablewrap").scrollTop=scrollTop;
  openView("companies",null,"/companies?f="+encodeURIComponent(COMPANY_FILTER)+(COMPANY_SELECTED?"&c="+encodeURIComponent(COMPANY_SELECTED):""));
}

// The app bar carries two classes of thing and keeps them apart: identity and
// navigation on the left, machine state and settings on the right. Only Board
// and Companies are tabs, because only those two are places you stay in. A run
// is reached from the Board's Runs rail and lives under Board - §0.3 keeps Runs
// out of the nav deliberately, because its full-width form was a page you only
// ever passed through on the way to one run.
// The trail starts *below* the lit tab and never repeats it: with the tabs
// 40px to its left, a leading "Board" segment was the same word twice in a row.
const crumbRun=run=>({label:run.company,sub:run.role,run:run.id});
function renderChrome(crumb){
  const place=VIEW==="companies"?"companies":"board";
  document.querySelectorAll(".navitem[data-nav]").forEach(node=>{
    const on=node.dataset.nav===place;node.classList.toggle("on",on);
    if(on)node.setAttribute("aria-current","page");else node.removeAttribute("aria-current");
  });
  renderCrumb(crumb);
}
// Location, not title. The crumb answers "which layer am I on"; what the
// document is called stays in .runtitle at 21px in the body. A segment is a
// link only while something sits below it - the last one is where you are, and
// is plain text. Both link kinds reuse handlers the bar already has, data-nav
// for Board and data-run for the run's own Tailor view, so the bar never grows
// a second copy of the routing table.
function renderCrumb(segments){
  const parts=segments||[],node=el("crumb");
  node.hidden=!parts.length;
  node.innerHTML=parts.map((seg,index)=>{
    const last=index===parts.length-1,attr=seg.run?`data-run="${esc(seg.run)}"`:seg.nav?`data-nav="${esc(seg.nav)}"`:"";
    const body=`${esc(seg.label)}${seg.sub?`<span class="crumbsub">${esc(seg.sub)}</span>`:""}`;
    const full=esc(seg.sub?seg.label+" \u00b7 "+seg.sub:seg.label);
    const piece=last||!attr?`<span class="crumbseg here" title="${full}">${body}</span>`:`<button class="crumbseg" ${attr} title="${full}">${body}</button>`;
    return (index?'<span class="crumbsep">\u203a</span>':"")+piece+(seg.count?`<span class="count">${esc(seg.count)}</span>`:"");
  }).join("");
}
function openView(kind,crumb=null,route=null){
  VIEW=kind;const app=el("app");app.classList.remove("expanded-board","expanded-job","tailor-mode","reader-mode","preview-mode","revise-mode","companies-mode");
  const parked=["tailor","reader","preview","revise","companies"].includes(kind);
  app.classList.add("expanded",parked?"tailor-mode":"expanded-"+kind);
  if(parked)app.classList.add(kind+"-mode");
  el("tailor-view").hidden=!parked;
  renderChrome(crumb);
  setRoute(route||"/"+kind);
}
function restoreWorkspace(){
  VIEW=null;const app=el("app");app.classList.remove("expanded","expanded-board","expanded-job","tailor-mode","reader-mode","preview-mode","revise-mode","companies-mode");el("tailor-view").hidden=true;renderChrome(null);
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
//   #/job/<url>         reader           #/run/<id>/preview  compiled PDFs
//                                        #/run/<id>/revise   revise
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
  if(head==="board"){openView(head);return true}
  if(head==="job"){
    if(!JOBS.some(job=>job.url===first))return false;
    // A deep link outranks whichever chip you happened to leave the board on.
    if(!shown().some(row=>row.url===first)){filter="all";q="";el("q").value="";resetFacets();render()}
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
const DEFAULT_LAYOUT={version:1,left:264,right:452,leftCollapsed:false,rightCollapsed:false,autoLeft:false,autoRight:false,shortcutsHidden:false};
function loadLayout(){try{const value=JSON.parse(localStorage.getItem(LAYOUT_KEY));if(value?.version===1){const merged={...DEFAULT_LAYOUT,...value};delete merged.expanded;delete merged.applications;delete merged.collect;return merged}localStorage.removeItem(LAYOUT_KEY)}catch(_){try{localStorage.removeItem(LAYOUT_KEY)}catch(__){}}return {...DEFAULT_LAYOUT}}
let layout=loadLayout();
function saveLayout(){try{localStorage.setItem(LAYOUT_KEY,JSON.stringify(layout))}catch(_){}}
function applyLayout(){
  const app=el("app");app.style.setProperty("--left",layout.left+"px");app.style.setProperty("--right",layout.right+"px");
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
  const specs={"left-split":[layout.left,200,420],"right-split":[layout.right,320,640]};
  Object.entries(specs).forEach(([id,[now,min,max]])=>{const node=el(id);node.setAttribute("aria-valuenow",Math.round(now));node.setAttribute("aria-valuemin",min);node.setAttribute("aria-valuemax",Math.round(max))});
}
const splitSpecs={
  "left-split":{key:"left",axis:"x",sign:1,min:200,max:420,def:264},
  "right-split":{key:"right",axis:"x",sign:-1,min:320,max:640,def:452}
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

// Fetching: one action, truthful status, optional settings.
const FETCH_SETTINGS_KEY="jobflow.fetch.v1";
const FETCH_DEFAULTS={sources:["ats","freehire","linkedin"],mc:8,mn:40,md:15};
let FETCH_STARTING=false,FETCH_POLLING=false;
const chosenSources=()=>["ats","freehire","linkedin"].filter(s=>el("s-"+s).checked);
function fetchSettings(){return {sources:chosenSources(),mc:+el("mc").value,mn:+el("mn").value,md:+el("md").value}}
function applyFetchSettings(value){
  ["ats","freehire","linkedin"].forEach(s=>el("s-"+s).checked=(value.sources||FETCH_DEFAULTS.sources).includes(s));
  for(const [id,min,max] of [["mc",1,20],["mn",1,200],["md",0,20]]){const n=value[id];el(id).value=Number.isInteger(n)?Math.max(min,Math.min(max,n)):FETCH_DEFAULTS[id]}
}
try{applyFetchSettings(JSON.parse(localStorage.getItem(FETCH_SETTINGS_KEY))||FETCH_DEFAULTS)}catch(_){applyFetchSettings(FETCH_DEFAULTS)}
el("fetch-options").addEventListener("change",()=>{applyFetchSettings(fetchSettings());try{localStorage.setItem(FETCH_SETTINGS_KEY,JSON.stringify(fetchSettings()))}catch(_){}});
el("fetch-reset").addEventListener("click",()=>{applyFetchSettings(FETCH_DEFAULTS);try{localStorage.removeItem(FETCH_SETTINGS_KEY)}catch(_){}});
async function reloadJobs(){const response=await fetch("/api/jobs?t="+T);checkAuth(response);if(!response.ok)throw new Error("Could not reload the job list.");const data=await response.json();JOBS=data.jobs;STATUSES=data.statuses;FILTERS=buildFilters();render()}
function fetchSummary(status){
  if(status.running)return {message:"Checking for new jobs…",running:true};
  const sources=status.sources||[],added=sources.reduce((n,s)=>n+(s.added||0),0);
  const labels={"ats-search":"Target companies","linkedin-search":"LinkedIn","freehire-search":"freehire"};
  const failed=sources.filter(s=>s.degraded||s.error||s.failed?.length||s.retry_later).map(s=>labels[s.source]||s.source);
  const result=added?`${added} new job${added===1?"":"s"} added`:"No new jobs found";
  if(status.error)return {message:added?`${result} · Fetch stopped early. See Activity.`:"Could not finish fetching. Try again or see Activity.",error:true};
  if(failed.length)return {message:`${result} · ${failed.join(", ")} could not be fully checked. See Activity.`,error:true};
  if(!status.finished_at)return {message:"Ready to fetch"};
  const remaining=sources.reduce((n,s)=>n+(s.remaining_due||0)+(s.deferred_descriptions||0),0);
  if(remaining)return {message:`${result} · More to check — fetch again.`};
  if(sources.length&&sources.every(s=>s.source==="ats-search"?s.remaining_due===0:s.cached===true))return {message:added?`${result} · Up to date for connected sources.`:"Up to date for connected sources. Recent searches are reused for an hour."};
  return {message:result};
}
function renderFetchLog(status){
  // The run stamp is what "latest fetch" filters on and what the arrival dots
  // are measured against, so a status poll that moves it repaints the list.
  // Only a real status carries the field. This function is also called with
  // `{}` and `{error:true}` to repaint the button synchronously, and treating
  // those as "no fetch has ever run" would blink every arrival dot off and on.
  if("started_at" in status&&(status.started_at||null)!==LAST_FETCH){
    LAST_FETCH=status.started_at||null;
    if(JOBS.length)render();
  }
  const added=(status.sources||[]).reduce((n,s)=>n+(s.added||0),0);
  el("show-new").hidden=!(added&&!status.running&&status.finished_at);
  const view=fetchSummary(status);el("collectlog").textContent=view.message;
  el("collectlog").parentElement.classList.toggle("error",!!view.error);
  el("fetch").disabled=!!view.running||FETCH_STARTING;el("fetch").textContent=view.running?"Fetching…":FETCH_STARTING?"Starting…":"Fetch new jobs";
  el("fetch-options").disabled=!!view.running||FETCH_STARTING;
  el("collectlast").textContent=status.finished_at?"Last checked: "+new Date(status.finished_at).toLocaleString("en-GB",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"}):"";
  const banner=el("degraded");banner.hidden=true;
}
function watchFetch(){if(!POLL)POLL=setInterval(()=>void pollFetch(),2000)}
async function pollFetch(){
  if(FETCH_POLLING)return;
  FETCH_POLLING=true;
  try{
    const response=await fetch("/api/fetch/status?t="+T);if(!checkAuth(response))return;if(!response.ok)throw new Error();
    const status=await response.json();renderFetchLog(status);
    if(status.running){watchFetch();return}
    const completed=!!POLL;clearInterval(POLL);POLL=null;
    if(completed){try{await reloadJobs()}catch(error){el("collectlog").textContent="Fetch finished, but the list could not reload. Refresh this page."}void pollActivity()}
  }catch(_){
    el("collectlog").textContent="Cannot check fetch status. Retrying…";
    el("fetch").disabled=true;el("fetch-options").disabled=true;watchFetch();
  }finally{FETCH_POLLING=false}
}
el("fetch").addEventListener("click",async()=>{
  if(FETCH_STARTING||el("fetch").disabled)return;
  const sources=chosenSources();if(!sources.length){el("fetch-settings").open=true;toast("Choose at least one job source.",{warn:true});return}
  for(const id of ["mc","mn","md"])if(!el(id).reportValidity()){el("fetch-settings").open=true;return}
  FETCH_STARTING=true;renderFetchLog({});el("fetch-settings").open=false;el("collectlog").textContent="Starting fetch…";
  try{
    const response=await fetch("/api/fetch?t="+T,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({sources,max_companies:+el("mc").value,max_new_jobs:+el("mn").value,linkedin_detail_fetches:+el("md").value})});
    const data=await response.json();
    if(!response.ok&&response.status!==409)throw new Error(data.error||"Could not start fetching.");
    FETCH_STARTING=false;watchFetch();await pollFetch();
  }catch(error){FETCH_STARTING=false;renderFetchLog({error:true});toast(error.message||"Could not start fetching.",{warn:true,ms:4000});watchFetch()}
});

// Activity strip and drawer
async function pollActivity(){
  const url="/api/activity?t="+T+"&since="+SEQ+(EPOCH?"&epoch="+EPOCH:"");let data;try{const response=await fetch(url);if(!checkAuth(response)||!response.ok)return;data=await response.json()}catch(_){return}
  if(data.reset||EPOCH===null)EV=[];EPOCH=data.epoch;SEQ=data.seq;COUNTS=data.counts||{};if(data.events?.length)EV=EV.concat(data.events).slice(-500);
  renderStrip();if(!el("drawer").hidden)renderActivity();if(VIEW==="tailor"&&ACTIVE_RUN)renderTailor(RUNS.find(r=>r.id===ACTIVE_RUN));
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

const companyPath=name=>"/api/companies/"+name.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"");
async function companyWork(name,work){
  if(COMPANY_BUSY)return;
  COMPANY_BUSY=name;COMPANY_ERROR="";renderCompanies(false);
  try{await work()}catch(error){COMPANY_ERROR=error.message||"Could not finish. Please try again.";toast(COMPANY_ERROR,{warn:true,ms:5000})}
  finally{COMPANY_BUSY=null;if(VIEW==="companies")renderCompanies(false)}
}
async function checkCompany(name){
  let row=COMPANIES.companies.find(row=>row.name===name);
  if(!row||row.status==="paused")return;
  if(!row.will_be_searched){
    if((row.route||"ats")!=="ats"||row.status==="ambiguous")return;
    await postCompany(companyPath(name)+"/resolve",{mtime:COMPANIES.mtime});
    row=COMPANIES.companies.find(row=>row.name===name);
  }
  if(row?.will_be_searched)await postCompany(companyPath(name)+"/test-fetch",{mtime:COMPANIES.mtime});
}
function selectCompanyRow(row){
  const scrollTop=row.closest(".tablewrap")?.scrollTop||0;
  COMPANY_SELECTED=COMPANY_SELECTED===row.dataset.companyRow?null:row.dataset.companyRow;
  renderCompanies(false,scrollTop);
}

document.addEventListener("click",event=>{
  if(event.target.closest("#text-modal-submit"))return void closeTextModal(true);
  if(event.target.closest("#text-modal-cancel,#text-modal-close"))return void closeTextModal(false);
  if(event.target===el("text-modal"))return void closeTextModal(false);
  if(event.target.closest("#shortcut-toggle"))return void toggleShortcuts();
  if(event.target.closest("#theme-toggle"))return void cycleTheme();
  if(event.target.closest("#toastundo"))return void undo();
  if(event.target.closest("#striptoggle"))return void toggleDrawer();
  if(event.target.closest("#copylog"))return void copyLog();
  const nav=event.target.closest("[data-nav]");if(nav)return void(nav.dataset.nav==="companies"?renderCompanies():restoreWorkspace());
  const collapse=event.target.closest("[data-collapse]");if(collapse)return void toggleCollapse(collapse.dataset.collapse);
  const expand=event.target.closest("[data-expand]");if(expand){const kind=expand.dataset.expand;if(kind==="job")renderReader();else if(kind==="board")openView(kind);return}
  const chip=event.target.closest("[data-filter]");if(chip){filter=chip.dataset.filter;sel=0;render();return}
  const source=event.target.closest("[data-source]");
  if(source){
    const key=source.dataset.source,others=[...new Set(JOBS.map(sourceKey))].filter(k=>k!==key);
    // Alt-click solos: with five sources, "show me only the ATS rows" is one
    // click that way and four the other.
    facets.hidden=event.altKey?(facets.hidden.length===others.length?[]:others)
      :facets.hidden.includes(key)?facets.hidden.filter(k=>k!==key):[...facets.hidden,key];
    saveFacets();sel=0;render();return;
  }
  const act=event.target.closest("[data-act-filter]");if(act){actFilter=act.dataset.actFilter;renderActivity();return}
  const status=event.target.closest("[data-status]");if(status)return void setStatus(status.dataset.status);
  const note=event.target.closest("[data-note]");if(note)return void editNote(JOBS.find(j=>j.url===note.dataset.note));
  const tailor=event.target.closest("[data-tailor]");if(tailor)return void startTailor(tailor.dataset.tailor);
  const readerRow=event.target.closest("[data-reader-row]");if(readerRow){sel=+readerRow.dataset.readerRow;renderReader();return}
  const companyFilter=event.target.closest("[data-company-filter]");if(companyFilter){COMPANY_FILTER=companyFilter.dataset.companyFilter;renderCompanies(false);return}
  const companyRow=event.target.closest("[data-company-row]");if(companyRow)return void selectCompanyRow(companyRow);
  const companyCheck=event.target.closest("[data-company-check]");if(companyCheck){const name=companyCheck.dataset.companyCheck;void companyWork(name,()=>checkCompany(name));return}
  const companyAction=event.target.closest("[data-company-action]");if(companyAction){const name=COMPANY_SELECTED,action=companyAction.dataset.companyAction;if(action==="remove"&&!confirm("Remove "+name+" from your company list? Saved jobs will stay on the Board."))return;void companyWork(name,async()=>{await postCompany(companyPath(name)+"/manage",{mtime:COMPANIES.mtime,action});if(action==="remove")COMPANY_SELECTED=null});return}
  const companyConfirm=event.target.closest("[data-company-confirm]");if(companyConfirm){const name=COMPANY_SELECTED,[vendor,token]=companyConfirm.dataset.companyConfirm.split("|");void companyWork(name,async()=>{await postCompany(companyPath(name)+"/identity",{mtime:COMPANIES.mtime,decision:"confirm",candidate:{vendor,token}});await checkCompany(name)});return}
  const preview=event.target.closest("[data-preview]");if(preview){const run=RUNS.find(r=>r.id===preview.dataset.preview);if(run)renderPreview(run);return}
  const revise=event.target.closest("[data-revise]");if(revise){const run=RUNS.find(r=>r.id===revise.dataset.revise);if(run)renderRevise(run);return}
  const restoreVersion=event.target.closest("[data-restore-version]");if(restoreVersion){restoreVersion.disabled=true;postRun("/api/runs/"+restoreVersion.dataset.restoreVersion+"/restore").then(({ok})=>{const run=RUNS.find(r=>r.id===REVISE_RUN);if(ok&&run)renderRevise(run)});return}
  const versionPreview=event.target.closest("[data-version-preview]");if(versionPreview){const run=RUNS.find(r=>r.id===versionPreview.dataset.versionPreview);if(run)renderPreview(run);return}
  const previewFilter=event.target.closest("[data-preview-filter]");if(previewFilter){const main=el("preview-main");main.classList.toggle("cv-only",previewFilter.dataset.previewFilter==="cv");main.classList.toggle("cover-only",previewFilter.dataset.previewFilter==="cover");document.querySelectorAll("[data-preview-filter]").forEach(node=>node.classList.toggle("on",node===previewFilter));return}
  const recompile=event.target.closest("[data-recompile]");if(recompile){recompile.disabled=true;postRun("/api/runs/"+recompile.dataset.recompile+"/compile").then(({ok})=>{const run=RUNS.find(r=>r.id===recompile.dataset.recompile);if(ok&&run)renderPreview(run)});return}
  const runNode=event.target.closest("[data-run]");if(runNode){const run=RUNS.find(r=>r.id===runNode.dataset.run);if(run)renderTailor(run);return}
  const pill=event.target.closest("#runpill");if(pill){const run=RUNS.find(r=>r.id===pill.dataset.run);if(run)renderTailor(run);return}
  const approve=event.target.closest(".approve");if(approve){approve.disabled=true;postRun("/api/runs/"+approve.dataset.runId+"/approve",{phase:approve.dataset.phase,base_cv:document.querySelector("[data-run-base]")?.value||"auto",scope:document.querySelector("[data-run-scope]")?.value||"both"});return}
  const cancel=event.target.closest(".cancelrun");if(cancel){postRun("/api/runs/"+cancel.dataset.runId+"/cancel");return}
  const retry=event.target.closest(".retryrun");if(retry){retry.disabled=true;const failed=RUNS.find(r=>r.id===retry.dataset.runId);postRun("/api/runs/"+retry.dataset.runId+"/retry").then(({ok})=>{if(!ok){retry.disabled=false;return}toast("fresh attempt queued",{ms:2500});const next=RUNS.find(r=>r.retry_of===failed?.id);if(next)renderTailor(next)});return}
  const row=event.target.closest("tr[data-row]");if(row&&!event.target.closest("select")){sel=+row.dataset.row;render()}
});
async function postCompany(path,body,method="POST"){
  const response=await fetch(path+"?t="+T,{method,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  checkAuth(response);
  let data={};try{data=await response.json()}catch(_){}
  if(!response.ok){
    if(response.status===409){const latest=await fetch("/api/companies?t="+T);if(latest.ok)COMPANIES=await latest.json()}
    throw new Error(response.status===409&&data.error?.includes("registry changed")?"This list changed in another window. Please try again.":path.endsWith("/test-fetch")?"Could not check jobs. Your company is saved; please try again.":data.error||"Could not save changes. Please try again.");
  }
  if(data.companies)COMPANIES=data;
  return data;
}
document.addEventListener("input",event=>{
  if(event.target.id!=="company-search")return;
  const position=event.target.selectionStart;COMPANY_QUERY=event.target.value;
  renderCompanies(false);const search=el("company-search");search.focus();try{search.setSelectionRange(position,position)}catch(_){}
});
document.addEventListener("submit",event=>{
  if(event.target.id==="company-add"){
    event.preventDefault();const name=el("company-name").value,careers_url=el("company-careers").value,known=new Set(COMPANIES.companies.map(row=>row.name));
    COMPANY_ADD_DRAFT={name,url:careers_url};
    void companyWork("__add__",async()=>{await postCompany("/api/companies",{mtime:COMPANIES.mtime,name,careers_url});COMPANY_ADD_DRAFT={name:"",url:""};const added=COMPANIES.companies.find(row=>!known.has(row.name));if(added){COMPANY_FILTER="all";COMPANY_QUERY="";COMPANY_SELECTED=added.name;COMPANY_BUSY=added.name;if(VIEW==="companies")renderCompanies(false);if(added.will_be_searched)await checkCompany(added.name)}});return;
  }
  if(event.target.id==="company-settings"){
    event.preventDefault();const oldName=COMPANY_SELECTED,name=el("company-name-value").value.trim(),careers_url=el("company-careers-value").value.trim(),market=el("company-market-value").value;
    const old=COMPANIES.companies.find(row=>row.name===oldName),sourceChanged=careers_url&&careers_url!==(old?.careers_url||(old?.domain?"https://"+old.domain:""));
    const changes={name,countries:market?market.split(","):[]};if(sourceChanged)changes.careers_url=careers_url;
    void companyWork(oldName,async()=>{await postCompany(companyPath(oldName),{mtime:COMPANIES.mtime,inspect:true,changes},"PATCH");COMPANY_SELECTED=name;COMPANY_BUSY=name;if(sourceChanged&&COMPANIES.companies.find(row=>row.name===name)?.will_be_searched)await checkCompany(name);toast("Company saved")});return;
  }
  if(event.target.id!=="revise-form")return;event.preventDefault();const button=event.submitter,kind=button?.dataset.reentryKind,run=RUNS.find(r=>r.id===REVISE_RUN);if(!run||!kind)return;
  const scope=new FormData(event.target).get("scope")||"both",note=el("revision-note").value.trim(),remember=el("revision-remember").value.trim();
  if(kind!=="apply"&&!note){toast("say what should change",{warn:true});return}
  button.disabled=true;const body={job_url:run.job_url,kind,note,scope,remember};if(kind!=="apply")body.parent=run.id;
  postRun("/api/runs",body).then(({ok})=>{if(ok){restoreWorkspace();toast(kind+" queued",{ms:2500})}else button.disabled=false});
});
document.addEventListener("change",event=>{
  if(event.target.id==="f-fit"||event.target.id==="f-found"||event.target.id==="f-sort"){
    facets[event.target.id.slice(2)]=event.target.value;saveFacets();sel=0;render();return;
  }
  if(event.target.matches("[data-run-follow]")){RUN_FOLLOW=event.target.checked;
    const runlog=el("runlog");if(RUN_FOLLOW&&runlog)runlog.scrollTop=runlog.scrollHeight}
  if(event.target.matches("[data-run-scope]")){const button=document.querySelector(".approve");if(button)button.textContent=DRAFT_LABEL[event.target.value]||DRAFT_LABEL.both}
  if(event.target.matches("select[data-url]"))update(event.target.dataset.url,{status:event.target.value});
  if(event.target.matches("[data-note-input]"))update(event.target.dataset.noteInput,{note:event.target.value}).then(ok=>ok&&toast("note saved"));
});
document.addEventListener("keydown",event=>{
  if(MODAL_RESOLVE){if(event.key==="Escape"){event.preventDefault();closeTextModal(false)}else if((event.metaKey||event.ctrlKey)&&event.key==="Enter"){event.preventDefault();closeTextModal(true)}return}
  const typing=/^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
  if(event.key==="/"&&!typing){event.preventDefault();el(VIEW==="companies"?"company-search":"q").focus();return}
  if(typing){if(event.key==="Escape")event.target.blur();return}
  if(event.key==="Escape"&&!el("drawer").hidden){toggleDrawer();return}
  if(event.key==="Escape"&&el("app").classList.contains("expanded")){restoreWorkspace();return}
  if(event.key==="["){event.preventDefault();toggleCollapse("left");return}
  if(event.key==="]"){event.preventDefault();toggleCollapse("right");return}
  if(event.key==="\\"){event.preventDefault();resetLayout();return}
  if(VIEW==="companies")return;
  const rows=shown(),move={j:1,ArrowDown:1,k:-1,ArrowUp:-1};
  if(event.key in move){event.preventDefault();sel=Math.min(rows.length-1,Math.max(0,sel+move[event.key]));if(el("app").classList.contains("reader-mode"))renderReader();else render();return}
  if(event.key==="Enter"){const j=selectedJob();if(j)open(j.open_url||j.url,"_blank","noopener");return}
  if(event.key==="e"){event.preventDefault();editNote();return}if(event.key==="t"){event.preventDefault();const j=selectedJob();if(j)startTailor(j.url);return}
  if(event.key==="z"){event.preventDefault();undo();return}
  const map={s:"star",y:"yes",m:"maybe",n:"no",a:"applied",u:"new",g:"gate"};if(event.key in map){event.preventDefault();setStatus(map[event.key])}
});
el("q").addEventListener("input",event=>{q=event.target.value;sel=0;render()});
el("f-reset").addEventListener("click",()=>{resetFacets();sel=0;render()});
// The fetch summary says "8 new jobs added"; this is the one click that shows
// which eight, instead of leaving you to find them in four hundred rows.
el("show-new").addEventListener("click",()=>{
  facets.found="latest";facets.sort="found";saveFacets();sel=0;render();
  document.querySelector(".tablewrap")?.scrollTo({top:0});
});

renderChrome(null);
setInterval(pollActivity,3000);setInterval(pollRuns,6000);
Promise.all([reloadJobs(),pollActivity(),pollRuns()]).then(()=>{
  applyRoute();
  void pollFetch();
});
