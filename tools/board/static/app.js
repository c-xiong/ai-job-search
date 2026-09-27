const T=document.querySelector('meta[name="board-token"]').content;
const el=id=>document.getElementById(id);
const esc=value=>String(value==null?"":value).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ACTIVE=["star","yes","new","maybe"];
const RUNNING=["evaluating","queued","preparing","drafting","reviewing","revising","compiling","inspecting","publishing"];
// Three owner-facing stages; the checkpoint keeps the detailed internal ones.
const PHASE_STEP={evaluating:1,awaiting_approval:1,queued:1,preparing:1,drafting:2,reviewing:2,revising:2,compiling:3,inspecting:3,publishing:3,done:3};
const STAGE_COUNT=3;
let JOBS=[],STATUSES=[],FILTERS=[],filter="active",q="",sel=0;
let RUNS=[],QUEUE=[],LEDGER=null,BUDGET=null,RUNPOLL=null,ACTIVE_RUN=null;
let PREVIEW_RUN=null;
let REVISE_RUN=null;
// Which view is on screen. The `-mode` classes cannot answer that: openView
// marks *every* parked view "tailor-mode" because they share one layout, so a
// live refresh that asked the class list repainted the tailor run over whatever
// was actually open - the companies view lasted until the next 3-second poll.
let VIEW=null;
let COMPANIES=null,COMPANY_SELECTED=null;
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
// Not every posting deserves both documents: a speculative application may want
// the letter alone, and a portal that only takes a CV has nowhere to put one.
// The fit evaluation is the first honest moment to make that choice, so it lives
// only on the approval card instead of being asked once before and once after.
const SCOPES=[["both","CV + cover letter"],["cv","CV only"],["cover","Cover letter only"]];
const COUNTRIES=[["default","Country: master default"],["ch","Country: CH"],["de","Country: DE"]];
// Chosen before the run starts: there is no evaluation to wait for any more.
// The CV is one of the two master variants, chosen here; the default follows
// the title the same way the supervisor's `auto` would.
const AI_TITLE=/\b(machine learning|ml engineer|ml scientist|data scientist|ai|artificial intelligence|llm|nlp|generative ai)\b/i;
const variantOptions=url=>{const title=(JOBS.find(j=>j.url===url)||{}).title||"",pick=AI_TITLE.test(title)?"ai":"sde";
  return [["sde","CV: SDE"],["ai","CV: AI"]].map(([value,label])=>`<option value="${value}" ${pick===value?"selected":""}>${label}</option>`).join("")};
const generatePicker=url=>`<div class="generate-picker"><select data-gen-scope aria-label="Documents">${scopeOptions("both")}</select><select data-gen-base aria-label="CV variant">${variantOptions(url)}</select><select data-gen-country aria-label="Work-authorisation line">${COUNTRIES.map(([v,l])=>`<option value="${v}">${l}</option>`).join("")}</select></div><button class="primary tailor" data-tailor="${esc(url)}">Generate</button>`;
const DRAFT_LABEL={both:"Draft CV + cover letter",cv:"Draft CV",cover:"Draft cover letter"};
const DRAFT_STEP={both:["Draft CV + cover letter","Tailors both documents and audits every factual claim."],
  cv:["Draft CV","Tailors the CV and audits every factual claim."],
  cover:["Draft cover letter","Tailors the letter and audits every factual claim."]};
const DOC_TITLE={cv:"CV",cover:"cover letter"};
const docKinds=scope=>(scope||"both")==="both"?["cv","cover"]:[scope];
const scopeOptions=(selected="both")=>SCOPES.map(([value,label])=>`<option value="${value}" ${selected===value?"selected":""}>${label}</option>`).join("");
const SOURCE_LABELS={"linkedin-search":"LinkedIn search","linkedin-browser":"LinkedIn browser","ats-search":"Your companies","company-careers":"Company careers","freehire-search":"freehire"};
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
      ${generatePicker(j.url)}
      <div class="hint">posting → write and check → build and verify · no scoring, no approval pause</div></div>`;
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
    const step=PHASE_STEP[r.phase]||1,pct=Math.round(step/STAGE_COUNT*100),running=RUNNING.includes(r.phase),bad=["failed","orphaned"].includes(r.phase);
    // Live rows answer "how far along"; finished ones answer "when, and which
    // attempt" - the two columns the Applications table used to carry.
    const meta=[r.phase.replaceAll("_"," ")];
    if(running||r.phase==="awaiting_approval")meta.push("stage "+step+"/"+STAGE_COUNT);
    else if(r.started_at)meta.push(r.started_at.slice(5,16).replace("T"," "));
    if(elapsed(r))meta.push(elapsed(r));
    if((r.attempt||1)>1)meta.push("attempt "+r.attempt);
    // Nothing navigates except a link you aimed at: the card itself is inert.
    const actions=[`<button class="linkish runopen" data-run="${esc(r.id)}">${running?"Watch run":"Open run"} <span class="runarrow">↗</span></button>`];
    if(r.phase==="done")actions.push(`<button class="linkish" data-preview="${esc(r.id)}">Preview</button>`,`<button class="linkish" data-revise="${esc(r.id)}">Revise</button>`);
    else if(["failed","cancelled","awaiting_approval"].includes(r.phase))actions.push(`<button class="linkish continuerun" data-run-id="${esc(r.id)}">Continue</button>`);
    return `<div class="runitem ${running?"live":""} ${ACTIVE_RUN===r.id?"selected":""}">
      <div class="runwho"><strong>${esc(r.company)}</strong><span class="runrole">${esc(r.role)}</span></div>
      <div class="runmeta ${bad?"runerror":""}">${meta.map(bit=>`<span>${esc(bit)}</span>`).join("<span>·</span>")}</div>
      ${running?`<div class="runprogress"><span style="width:${pct}%"></span></div>`:""}
      <div class="runactions">${actions.join("")}</div></div>`;
  }).join(""):'<div class="panel-empty">No runs yet.</div>';
  // The pill and the strip count only work in progress. A run parked for
  // approval or orphaned by a restart is not "tailoring" anything; it waits
  // in the Runs list for Continue or Kill.
  const running=live.filter(r=>RUNNING.includes(r.phase)),active=running[0];el("striprunning").textContent=running.length?running.length+" running":"";
  if(active){el("runpill").hidden=false;el("runpill").innerHTML=`<span>Tailoring <strong>${esc(active.company)}</strong> · stage ${PHASE_STEP[active.phase]||1} of ${STAGE_COUNT}</span><span class="elapsed">${elapsed(active)}</span>`;el("runpill").dataset.run=active.id}
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
  const pick=name=>document.querySelector(`[data-gen-${name}]`)?.value;
  const scope=pick("scope")||"both",base_cv=pick("base")||"auto",cv_country=pick("country")||"default";
  const note=await openTextModal({eyebrow:"Generate "+(DRAFT_LABEL[scope]||"").replace(/^Draft /,""),title:j.company+" — "+j.title,label:"One-off instruction (optional)",placeholder:"Emphasize a project, explain a transition, or leave this empty…",hint:"This instruction applies only to this run unless you later add it as a standing preference.",submit:"Generate"});
  if(note===null)return;
  const started=await postRun("/api/runs",{job_url:url,kind:"apply",note,scope,base_cv,cv_country});
  // `run_id` on a refusal means this posting already has a run: `postRun` has
  // said so, and the honest next move is to show it rather than leave you to
  // find it in the rail.
  const run=started.run_id&&RUNS.find(r=>r.id===started.run_id);
  if(run){renderTailor(run);return}
  // The run list is refreshed inside postRun and the supervisor records the run
  // before it answers, so this is the blipped-poll case rather than a real one.
  // Say something anyway: a press that neither moves nor speaks reads as broken.
  if(started.ok)toast("generation queued — open it from Runs",{ms:3000});
}

// A run's identity belongs in the document it is about, not squeezed into the
// app bar's uppercase breadcrumb slot where it read as a system label.
const runTitle=(run,action="")=>`<header class="runtitle"><div><h1>${esc(run.company)}</h1><p>${esc(run.role)}</p></div>${action}</header>`;
// You apply on the posting's own site; the board opens it and records that you
// did. "Mark applied" moves Notion Stage Interested -> Applied and the tracker
// row drafted -> applied; any later status is shown instead of the button.
function applyActions(run){
  if(run.phase!=="done")return "";
  const open=run.job_url?`<a class="secondary" href="${esc(run.job_url)}" target="_blank" rel="noopener">Open posting ↗</a>`:"";
  const status=run.tracker_status||"",when=(run.tracker_date||"").slice(5).replace("-","/");
  const mark=!status||status==="drafted"?`<button class="secondary" data-applied="${esc(run.id)}">Mark applied</button>`
    :`<span class="applied-badge">${status==="applied"?"Applied ✓":esc(status.replaceAll("_"," "))}${when?" "+esc(when):""}</span>`;
  return open+mark;
}
const STEPS=[
  ["Prepare materials","The complete posting is saved once; the CV master is snapshotted read-only."],
  ["Write","The CV is your chosen master variant; Claude drafts the letter from your cover base."],
  ["Build","Compiles the PDFs and measures pages, text layer and keywords. You check the PDFs in Preview."]
];
const DOC_STATE_MARK={"verified":"✓","PDF built":"◐","content checked":"◐","draft saved":"○","missing":"—"};
const FAILURE_TITLE={quota_exhausted:"Claude usage limit reached",rate_limited:"Claude is rate limiting",budget_cap:"Local run budget reached",
  timeout:"A step ran out of time",hard_conflict:"The posting conflicts with your deal-breakers",missing_input:"An input is missing",
  compile_error:"LaTeX did not compile",verification_failed:"A check did not pass",content_unresolved:"Review findings are unresolved",
  interrupted:"Interrupted",guard_unverified:"Write guard not proven",provider_rate_limit:"Claude session limit reached"};
const FAILURE_NEXT={quota_exhausted:"A new session does not reset the allowance. Continue when your usage resets - the saved work is kept.",
  rate_limited:"Wait a moment, then Continue. Nothing is retried automatically.",
  budget_cap:"Raise the cap in job_scraper/board_config.json, then Continue.",
  hard_conflict:"Continue anyway only if you still want these documents.",
  missing_input:"Fix the named input, then Continue.",
  interrupted:"Continue resumes from the last checkpoint."};
function progressPanel(run){
  const p=run.progress;if(!p)return "";if(p.error)return `<div class="dim">Checkpoint unreadable: ${esc(p.error)}</div>`;
  const docs=Object.entries(p.docs||{}).map(([kind,d])=>`<div><span class="verify-mark ${d.state==="verified"?"pass":""}">${DOC_STATE_MARK[d.state]||"·"}</span> ${esc(DOC_TITLE[kind])}: ${esc(d.state)}${d.pdf?` · <button class="linkish" data-preview="${esc(run.id)}">view PDF</button>`:""}</div>`).join("");
  const checks=Object.entries(p.checks||{}).map(([id,c])=>`<span class="badge" title="${esc(c.detail||"")}">${esc(id.replace("_"," "))}: ${esc(c.state)}</span>`).join(" ");
  const pending=(p.pending||[]).length?`<div class="dim">Remaining: ${esc(p.pending.join(" → "))}</div>`:"";
  const issues=(p.issues||[]).map(x=>`<div class="dim">${esc(x)}</div>`).join("");
  const conflicts=(p.conflicts||[]).map(x=>`<div><strong>Conflict:</strong> ${esc(x)}</div>`).join("");
  return `<div class="progress-card">${docs}${checks?`<div class="checkrow">${checks}</div>`:""}${pending}${conflicts}${issues}</div>`;
}
// The Runs tab reopens the run you last looked at, else the newest one.
function openRuns(){
  const run=RUNS.find(r=>r.id===ACTIVE_RUN)||latestApplications()[0];
  if(run)renderTailor(run);else toast("No runs yet — start one with Generate on a job.",{ms:3000});
}
function renderTailor(run){
  if(!run){restoreWorkspace();return}
  const previousLog=el("runlog");if(previousLog&&ACTIVE_RUN)RUN_LOG_SCROLL.set(ACTIVE_RUN,previousLog.scrollTop);
  ACTIVE_RUN=run.id;const current=PHASE_STEP[run.phase]||1,fit=run.fit||{};
  // Every attempt of this application: the same application id, plus anything
  // that continues or regenerates an attempt already in the list.
  const family=new Map([[run.id,run]]);RUNS.filter(r=>(r.application_id||r.id)===(run.application_id||run.id)).forEach(r=>family.set(r.id,r));
  for(let grew=true;grew;){grew=false;RUNS.forEach(r=>{if(!family.has(r.id)&&(family.has(r.continue_of)||family.has(r.retry_of))){family.set(r.id,r);grew=true}})}
  const lineage=[...family.values()].sort((a,b)=>(a.attempt||1)-(b.attempt||1));
  const kinds=docKinds(run.scope),legacyGate=run.phase==="awaiting_approval";
  const steps=STEPS.map((s,i)=>{const n=i+1,state=run.phase==="done"||n<current?"done":n===current&&!["failed","cancelled"].includes(run.phase)?"live":n===current?"stopped":"todo";return `<div class="step ${state}"><div class="steprow"><span class="stepdot">${n}</span><div><div class="steptitle">${s[0]}</div><div class="stepdetail">${s[1]}</div></div><span class="steptime">${state==="live"?esc(run.phase.replaceAll("_"," ")):""}</span></div></div>`}).join("");
  const logs=EV.filter(e=>e.run_id===run.id&&(e.source==="claude"||e.source==="latex"||e.source==="verify")).slice(-100).map(e=>`<div><span>${esc((e.ts||"").slice(11,19))}</span>&nbsp; ${esc(e.cmd?"$ "+e.cmd:e.msg)}</div>`).join("")||"<div>No run-specific activity recorded yet.</div>";
  const code=run.failure_code||"";
  const stopped=run.phase==="failed"||run.phase==="cancelled";
  const continuedBy=RUNS.find(r=>r.continue_of===run.id||r.retry_of===run.id);
  // One switcher for every attempt of this application: a tab per attempt with
  // its outcome, and how it relates to the one before it in the tooltip.
  const numberOf=id=>(RUNS.find(r=>r.id===id)||{}).attempt||"?";
  const attemptMark=r=>r.phase==="done"?"✓":["failed","cancelled"].includes(r.phase)?"✕":RUNNING.includes(r.phase)?"●":"·";
  const attemptTitle=r=>`Attempt ${r.attempt||1} · ${(r.phase||"queued").replaceAll("_"," ")} · ${r.continue_of?"continued from #"+numberOf(r.continue_of):r.retry_of?"regenerated from scratch after #"+numberOf(r.retry_of):"first attempt"}`;
  const attempts=lineage.length>1?`<div class="attempts" role="tablist" aria-label="Attempts">${lineage.map(r=>`<button class="attempt ${esc(r.phase)}${r.id===run.id?" on":""}" role="tab" aria-selected="${r.id===run.id}" data-run="${esc(r.id)}" title="${esc(attemptTitle(r))}"><span class="attempt-mark">${attemptMark(r)}</span>${esc(r.attempt||1)}</button>`).join("")}</div>`:"";
  const latest=lineage[lineage.length-1];
  const origin=lineage.length>1&&latest&&latest.id!==run.id?`<div class="attempt-note">You are viewing an earlier attempt. <button class="linkish" data-run="${esc(latest.id)}">Go to the latest (#${esc(latest.attempt||lineage.length)})</button></div>`:"";
  const failure=stopped?`<div class="failure-card"><strong>${esc(run.phase==="cancelled"?"Cancelled":FAILURE_TITLE[code]||"Run stopped")}</strong><div>${esc(run.error||"No error detail was recorded.")}</div><div class="dim">Stopped during ${esc((run.failed_phase||"unknown").replaceAll("_"," "))}${run.model_started===false?" · no model work started":""}${Number(run.cost?.total_usd||0)===0?" · no model cost incurred":""}</div>${FAILURE_NEXT[code]&&!continuedBy?`<div class="dim">${esc(FAILURE_NEXT[code])}</div>`:""}</div>`:"";
  // Legacy runs keep their evaluation visible; staged runs never had one.
  const fitCard=run.fit?`<div class="fitcard"><div class="fithead"><span class="fitword ${fit.overall>=70?"high":fit.overall>=50?"medium":"low"}">✓</span><strong>Earlier fit evaluation — ${esc(fit.verdict||"")}${fit.overall!=null?", "+esc(fit.overall):""}</strong></div></div>`:"";
  const notes=(run.tailoring_notes||[]).map(x=>`<div>${esc(x)}</div>`).join("");
  const actions=[];
  if(RUNNING.includes(run.phase))actions.push(`<button class="secondary cancelrun" data-run-id="${esc(run.id)}">Cancel run</button>`);
  if((stopped||legacyGate)&&!continuedBy){
    actions.push(`<button class="primary continuerun" data-run-id="${esc(run.id)}">Continue</button>`);
    if(code==="hard_conflict")actions.push(`<button class="secondary continuerun" data-run-id="${esc(run.id)}" data-proceed="1">Continue anyway</button>`);
    if(run.kind==="apply"&&!legacyGate)actions.push(`<button class="secondary retryrun" data-run-id="${esc(run.id)}">Regenerate</button>`);
  }
  // Preview sits beside the title, where the eye lands once a run is done.
  const preview=run.phase==="done"?`<div class="title-actions"><button class="primary" data-preview="${esc(run.id)}">Preview PDFs</button>${applyActions(run)}</div>`:"";
  el("tailor-view").innerHTML=`<div class="tailor-shell"><section class="pipeline"><div class="panelhead"><span class="label">Pipeline</span><span class="spacer"></span><span class="dim">stage ${current} of ${STAGE_COUNT}</span></div>${steps}<div class="writing"><div class="label">Publishes to</div>${kinds.map(kind=>`<div>${esc(run.targets?.[kind]||DOC_TITLE[kind]+" target pending")}</div>`).join("")}<div class="dim">Base ${esc((run.resolved_base_cv||run.base_cv||"auto").toUpperCase())} · country ${esc(run.cv_country||"master default")}</div></div></section>
    <section class="runoutput"><div class="panelhead"><span class="label">Run output</span>${attempts}<span class="spacer"></span><label class="source"><input type="checkbox" data-run-follow ${RUN_FOLLOW?"checked":""}> follow</label><span class="phase ${esc(run.phase)}">${esc(run.phase.replaceAll("_"," "))}</span></div>
      ${runTitle(run,preview)}${origin}${fitCard}${progressPanel(run)}${notes?`<div class="whybox"><span class="label">Tailoring choices</span>${notes}</div>`:""}
      ${failure}<div class="runlog" id="runlog">${logs}</div><div class="runfooter"><span class="dim">Closing this panel does not stop the run.</span><span class="spacer"></span>${actions.join("")}</div></section></div>`;
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
    <aside class="reader-decide"><div class="panelhead"><span class="label">Decide</span></div><div class="decision-section"><span class="label">Gates</span>${gateLines(j)}</div><div class="decision-section"><span class="label">Why it surfaced</span><div class="dim">${esc(j.why||"No reason was stored.")}</div><div class="hint">${esc(scoreTitle(j))} Generation reads the full posting.</div></div><div class="decision-section"><span class="label">Mark it</span><div class="statusbuttons">${marks}</div><textarea class="noteinput" data-note-input="${esc(j.url)}" placeholder="note to yourself — saved on blur">${esc(j.note)}</textarea>${generatePicker(j.url)}<div class="hint">opens the run · <kbd>t</kbd></div></div></aside></div>`;
  loadPosting(j);
  openView("reader",[{label:j.company,sub:j.title,count:`${sel+1} of ${rows.length}`}],"/job/"+encodeURIComponent(j.url));
}

async function renderPreview(run){
  if(!run)return;PREVIEW_RUN=run.id;
  el("tailor-view").innerHTML='<div class="panel-empty">Loading compiled documents…</div>';
  openView("preview",[crumbRun(run),{label:"Preview"}],"/run/"+encodeURIComponent(run.id)+"/preview");
  let verify;try{const response=await fetch(`/api/runs/${encodeURIComponent(run.id)}/verify?t=${T}`);verify=await response.json();if(!response.ok)throw new Error(verify.error)}catch(error){el("tailor-view").innerHTML=`<div class="panel-empty runerror">${esc(error.message||error)}</div>`;return}
  const query=`?t=${encodeURIComponent(T)}&v=${Date.now()}`;
  // The scope says what was asked for; the artefacts say what exists. A run that
  // produced one document gets one frame and no filter tabs.
  const produced=docKinds(run.scope).filter(kind=>(run.artefacts||{})[kind+"_pdf"]);
  const kinds=produced.length?produced:docKinds(run.scope),single=kinds.length===1?kinds[0]:null;
  const tabs=single?`<span class="chip on">${single==="cv"?"CV only":"Letter only"}</span>`
    :`<button class="chip on" data-preview-filter="both">Both documents</button><button class="chip" data-preview-filter="cv">CV only</button><button class="chip" data-preview-filter="cover">Letter only</button>`;
  const frames=produced.map(kind=>`<iframe title="${kind==="cv"?"Compiled CV":"Compiled cover letter"}" class="pdf-frame ${kind}" src="/api/pdf/${encodeURIComponent(run.id)}/${kind}${query}"></iframe>`).join("")||`<div class="panel-empty">No compiled PDF yet.</div>`;
  const reveals=Object.keys(verify.files||{}).map(kind=>`<button class="secondary" data-reveal="${kind}" title="${esc(verify.files[kind])}">Reveal ${kind==="cv"?"CV":"letter"} in Finder</button>`).join("");
  el("tailor-view").innerHTML=`<div class="preview-shell"><aside class="screen-rail">${screeningPanel(verify)}</aside>
    <section class="preview-main${single?" "+single+"-only":""}" id="preview-main">${runTitle(run,`<div class="title-actions">${applyActions(run)}</div>`)}<div class="preview-tabs">${tabs}</div><div class="pdf-stage">${frames}</div><div class="preview-foot"><span class="spacer"></span>${reveals}<button class="secondary" data-recompile="${esc(run.id)}">Recompile</button></div></section>
    <aside class="check-rail">${checklistPanel(verify)}${regeneratePanel(run,verify)}</aside></div>`;
}
// Screening is the one thing a pinned CV cannot answer by itself: what the
// posting asks for, and where your evidence is missing, adjacent or met.
const SCREEN_TAG={gap:"missing",adjacent:"adjacent",documented:"met"};
function screeningPanel(v){
  const s=v.screening;
  if(!s)return `<div class="panelhead"><span class="label">Screening</span></div><div class="panel-empty">No requirement brief yet.</div>`;
  const reqs=s.requirements||[],count=st=>reqs.filter(r=>r.status===st).length;
  const item=r=>`<div class="screen-item ${esc(r.status)}"><div class="screen-req"><span class="screen-tag">${esc(SCREEN_TAG[r.status]||r.status)}</span><strong>${esc(r.requirement)}</strong>${r.priority&&r.priority!=="required"?` <span class="dim">${esc(r.priority)}</span>`:""}</div>${r.evidence?`<div class="screen-evidence">${esc(r.evidence)}</div>`:""}</div>`;
  const open=reqs.filter(r=>r.status!=="documented"),met=reqs.filter(r=>r.status==="documented");
  const facts=Object.values(s.facts||{}).map(esc).join(" · ");
  const conflicts=(s.conflicts||[]).map(c=>`<div class="screen-conflict">${esc(c)}</div>`).join("");
  const kw=v.keywords||{},absent=kw.measured===false?null:(kw.absent||[]);
  const terms=absent===null?`<div class="dim">Measured after the CV is built.</div>`:absent.length?`<div class="term-list">${absent.map(t=>`<span class="term">${esc(t)}</span>`).join("")}</div>`:`<div class="dim">Every posting term is on your CV.</div>`;
  return `<div class="panelhead"><span class="label">Screening</span><span class="spacer"></span><span class="dim">${count("gap")} missing · ${count("adjacent")} adjacent · ${met.length} met</span></div>
    <div class="screen-scroll">${facts?`<div class="screen-facts">${facts}</div>`:""}${conflicts}${open.map(item).join("")||`<div class="dim screen-none">No stated requirement is missing from your evidence.</div>`}
    <div class="screen-block"><span class="label">Posting terms not on your CV</span>${terms}</div>
    ${met.length?`<details class="screen-met"><summary>${met.length} requirements met</summary>${met.map(item).join("")}</details>`:""}</div>`;
}
// You check the PDFs; the board only measures what a machine can measure
// (text layer, contacts, pages, keywords) and lists it underneath.
function checklistPanel(v){
  const manual=v.manual||[],done=manual.filter(m=>m.done).length;
  const group=(doc,title)=>{const rows=manual.filter(m=>m.doc===doc);return rows.length?`<div class="check-group"><span class="label">${title}</span>${rows.map(m=>`<label class="check-row${m.stale?" stale":""}"><input type="checkbox" data-mark="${esc(m.id)}" ${m.done?"checked":""}> <span>${esc(m.label)}${m.stale?` <em class="dim">PDF changed</em>`:""}</span></label>`).join("")}</div>`:""};
  const auto=v.checks||[],flagged=auto.filter(c=>c.state!=="pass");
  const autoRow=c=>`<div class="verify-item ${esc(c.state)}"><span class="verify-mark ${esc(c.state)}">${c.state==="pass"?"✓":"!"}</span><div><div>${esc(c.label)}</div><div class="verify-detail">${esc(c.detail)}</div></div></div>`;
  const autoBlock=auto.length?`<details class="auto-checks"${flagged.length?" open":""}><summary>Automatic checks · ${auto.length-flagged.length} pass${flagged.length?` · ${flagged.length} to look at`:""}</summary>${[...flagged,...auto.filter(c=>c.state==="pass")].map(autoRow).join("")}</details>`:"";
  return `<div class="panelhead"><span class="label">Your checks</span><span class="spacer"></span><span class="dim state-summary">${done} / ${manual.length}</span></div><div class="check-scroll">${group("cv","CV")}${group("cover","Cover letter")}${group("both","Both")}${autoBlock}</div>`;
}
// Regenerate one document from a prompt. The CV variant switch needs no model:
// it re-seeds the CV from the other master variant and rebuilds.
function regeneratePanel(run,v){
  if(run.phase!=="done")return `<div class="regen"><div class="hint">Regenerate is available once this run has finished. ${["failed","cancelled"].includes(run.phase)?"Continue it from the run view first.":""}</div></div>`;
  const kinds=docKinds(run.scope),hasCv=kinds.includes("cv");
  const variant=hasCv?`<div class="regen-row"><span class="label">CV variant</span><div class="seg">${[["sde","SDE"],["ai","AI"]].map(([value,label])=>`<button type="button" class="chip${v.variant===value?" on":""}" data-variant="${value}" ${v.variant===value?"disabled":""}>${label}</button>`).join("")}</div></div>`:"";
  const docs=kinds.map((kind,i)=>`<label><input type="radio" name="edit" value="${kind}" ${i===(kinds.length>1?1:0)?"checked":""}> ${kind==="cv"?"CV":"Cover letter"}</label>`).join("");
  return `<form class="regen" id="regen-form">${variant}<div class="regen-row"><span class="label">Regenerate</span><div class="seg">${docs}</div></div><textarea id="regen-note" placeholder="${hasCv?"e.g. change the CV title to “AI Engineer | LLM Applications”":"What should change?"}"></textarea><button class="primary" type="submit">Regenerate with Claude</button></form>`;
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

// ---------------------------------------------------------------- companies
//
// COMPANIES_PLAN §4: paste a careers page, and JobFlow finds the company's job
// board and checks it on every fetch. One row per company, grouped by the one
// question that matters - does this need me, is it still being looked for, or is
// it watched - with the rest behind ⋯.
const WATCH_GROUPS=[["needs_you","Needs you"],["finding","Finding"],["watching","Watching"],["cant_watch","Can't watch yet"],["not_watched","Not watched"],["paused","Paused"]];
const FOLDED_GROUPS=["cant_watch","not_watched","paused"];
let COMPANY_OPEN_GROUPS=new Set();
const VENDOR_LABEL={greenhouse:"Greenhouse",ashby:"Ashby",personio:"Personio",lever:"Lever",smartrecruiters:"SmartRecruiters",workday:"Workday"};
function ago(value){
  const at=Date.parse(value||"");if(Number.isNaN(at))return "";
  const minutes=Math.max(0,Math.round((Date.now()-at)/60000));
  if(minutes<60)return minutes<2?"just now":minutes+" min ago";
  const hours=Math.round(minutes/60);if(hours<36)return hours+" h ago";
  return Math.round(hours/24)+" d ago";
}
const shortDay=value=>{const at=Date.parse((value||"")+"T12:00:00");return Number.isNaN(at)?"":new Date(at).toLocaleDateString("en-GB",{day:"numeric",month:"short"})};
const sameCompany=(a,b)=>String(a||"").toLowerCase().replace(/[^a-z0-9]+/g,"")===String(b||"").toLowerCase().replace(/[^a-z0-9]+/g,"");
const boardCount=row=>JOBS.filter(j=>sameCompany(j.company,row.name)||(row.aliases||[]).some(alias=>sameCompany(j.company,alias))).length;
function companyLink(row){
  const value=row.board_url||row.careers_url||(row.domain?"https://"+row.domain:"");
  try{const url=new URL(value);return ["https:","http:"].includes(url.protocol)?url.href:""}catch(_){return ""}
}
// What a row says and offers, by its watch state. Words only: the registry's
// statuses, routes and tokens stay out of sight.
function companyLine(row){
  const link=companyLink(row),busy=COMPANY_BUSY===row.name;
  const careers=link?`<a class="open" href="${esc(link)}" target="_blank" rel="noopener">${row.watch_state==="watching"?"job board":"careers"} ↗</a>`:"";
  const tried=row.resolve_attempts?`tried ${row.resolve_attempts}×`:"not tried yet";
  switch(row.watch_state){
    case "watching":{
      const count=boardCount(row);
      const check=busy?"checking…":row.fetch_status==="failed"?`<span class="warn">! last check failed · retries next fetch</span>`
        :row.last_success_at?`<span class="ok-dot"></span>checked ${esc(ago(row.last_success_at))}`:"first check on the next fetch";
      return {what:`<span class="vendor">${esc(VENDOR_LABEL[row.vendor]||row.vendor||"")}</span><button class="linkish company-count" data-company-board="${esc(row.name)}" title="Show this company's jobs on the Board">${count} on Board →</button>`,
        state:check,actions:careers};
    }
    case "finding":
      return {what:`<span class="dim">Looking for its job board</span>`,
        state:busy?"looking…":`${tried} · ${row.next_resolve_at?"next try "+esc(shortDay(row.next_resolve_at)):"next fetch"}`,actions:careers};
    case "needs_you":{
      if(row.status==="ambiguous"){
        // A candidate whose board answered "not found" is dead: shown, never offered.
        const options=(row.candidates||[]).map(c=>/not_found|no longer/.test(c.note||"")
          ?`<span class="candidate dead" title="This job board no longer exists">${esc(VENDOR_LABEL[c.vendor]||c.vendor)} · ${esc(c.token)} — no longer active</span>`
          :`<span class="candidate"><a class="open" href="${esc(companyLink(c)||"#")}" target="_blank" rel="noopener">${esc(VENDOR_LABEL[c.vendor]||c.vendor)} · ${esc(c.token)} ↗</a><button class="secondary" data-company-confirm="${esc(row.name)}|${esc(c.vendor)}|${esc(c.token)}" title="Yes - this board belongs to ${esc(row.name)}; watch it">This one</button></span>`).join("");
        return {what:`<span>Is this their job board?</span>${options}`,state:"",
          actions:`<button class="secondary" data-company-relook="${esc(row.name)}" title="Read the careers page again">${busy?"Looking…":"Look again"}</button><button class="secondary" data-company-neither="${esc(row.name)}" title="None of these belongs to ${esc(row.name)} - you will paste its board link instead">None of these</button>`};
      }
      const rejected=/you said none/.test(row.resolve_detail||"");
      return {what:`<span title="${esc(row.resolve_detail||"")}">${rejected?"None of the suggested boards is theirs":"We couldn't find its job board"}</span>`,
        state:row.resolve_attempts?`tried ${esc(row.resolve_attempts)}×`:"",
        actions:`<button class="primary" data-company-row="${esc(row.name)}" data-company-focus="link" title="Open the company's jobs page in your browser, copy its address, and paste it here">Paste board link</button><button class="secondary" data-company-check="${esc(row.name)}">${busy?"Looking…":"Try again"}</button>`};
    }
    case "cant_watch":
      return {what:`<span class="dim" title="${esc(row.resolve_detail||row.monitoring_reason||"")}">${esc(row.resolve_detail&&/uses (\S+)/.test(row.resolve_detail)?"Uses "+row.resolve_detail.match(/uses (\S+)/)[1]+" — no reader for it yet":"Its job board can't be read automatically")}</span>`,state:"",actions:careers};
    case "not_watched":
      return {what:`<span class="dim">Kept for reference</span>`,state:"",
        actions:`${careers}<button class="secondary" data-company-watch="${esc(row.name)}">Find its board</button>`};
    default:
      return {what:`<span class="dim">Paused</span>`,state:"",
        actions:`<button class="secondary" data-company-action-row="${esc(row.name)}|resume">Resume</button>`};
  }
}
function companyEditor(row,disabled){
  const markets=[["","Your usual locations"],["CH","Switzerland"],["DE","Germany"],["CH,DE","Switzerland + Germany"]];
  const market=row.countries?.length?row.countries.join(","):"";
  const canCheck=["watching","finding","needs_you"].includes(row.watch_state)&&row.status!=="ambiguous";
  return `<form id="company-settings" class="company-editor"><label>Name<input id="company-name-value" required value="${esc(row.name)}"></label><label class="wide">Careers page or job-board link<input id="company-careers-value" inputmode="url" value="${esc(row.careers_url||(row.domain?"https://"+row.domain:""))}" placeholder="https://company.com/careers"></label><label>Locations<select id="company-market-value">${markets.map(([key,label])=>`<option value="${key}" ${market===key?"selected":""}>${label}</option>`).join("")}</select></label>
    <div class="company-editor-actions"><button class="primary" ${disabled}>Save</button>${canCheck?`<button type="button" class="secondary" ${disabled} data-company-check="${esc(row.name)}">Check now</button>`:""}<button type="button" class="secondary" ${disabled} data-company-action="${row.status==="paused"?"resume":"pause"}">${row.status==="paused"?"Resume":"Pause"}</button><span class="spacer"></span><button type="button" class="linkish company-remove" ${disabled} data-company-action="remove">Remove</button></div></form>`;
}
async function renderCompanies(reload=true,scrollTop=null){
  if(reload||!COMPANIES){try{const response=await fetch("/api/companies?t="+T);if(!response.ok)throw new Error();COMPANIES=await response.json()}catch(error){toast("Could not load companies. Please try again.",{warn:true});return}}
  const schemaReady=COMPANIES.schema_version===2&&COMPANIES.company_controls===true;
  const all=COMPANIES.companies||[],query=COMPANY_QUERY.toLowerCase();
  const rows=all.filter(row=>[row.name,row.domain,row.careers_url].join(" ").toLowerCase().includes(query));
  const disabled=COMPANY_BUSY||!schemaReady?"disabled":"";
  const perFetch=COMPANIES.defaults?.resolve_per_fetch||5;
  const groups=WATCH_GROUPS.map(([key,label])=>{
    const members=rows.filter(row=>(row.watch_state||"finding")===key);if(!members.length)return "";
    const folded=FOLDED_GROUPS.includes(key)&&!COMPANY_OPEN_GROUPS.has(key)&&!query&&!members.some(row=>row.name===COMPANY_SELECTED);
    const extra=key==="finding"?`<span class="company-group-note">the next fetch looks at up to ${perFetch} of these</span><button class="linkish" ${disabled} data-company-look-now>${COMPANY_BUSY==="__all__"?"Looking…":"Look now"}</button>`:"";
    const head=`<div class="company-group-head ${key}">${FOLDED_GROUPS.includes(key)?`<button class="linkish" data-company-group="${key}">${label} <span class="n">${members.length}</span> ${folded?"▸":"▾"}</button>`:`<span>${label} <span class="n">${members.length}</span></span>`}${extra}</div>`;
    const body=folded?"":members.map(row=>{
      const line=companyLine(row),open=row.name===COMPANY_SELECTED;
      return `<div class="company-row ${key}${open?" open":""}"><strong class="company-name">${esc(row.name)}</strong><div class="company-what">${line.what}</div><div class="company-state">${line.state}</div><div class="company-actions">${line.actions}<button class="linkish company-more" data-company-row="${esc(row.name)}" aria-expanded="${open}" aria-label="${open?"Close":"Edit"} ${esc(row.name)}" title="Edit, check, pause or remove">⋯</button></div></div>${open?companyEditor(row,disabled):""}`;
    }).join("");
    return `<section class="company-group">${head}${body}</section>`;
  }).join("");
  el("tailor-view").innerHTML=`<div class="companies-shell"><section class="companies-main"><div class="companies-head"><div><h1>Companies</h1><p>Paste a company's careers page — JobFlow finds its job board and checks it on every fetch.</p></div><label class="search"><span>⌕</span><input id="company-search" type="search" aria-label="Search companies" placeholder="search companies" value="${esc(COMPANY_QUERY)}"></label></div>${!schemaReady?'<div class="company-notice" role="status">Restart JobFlow to enable company editing.</div>':""}${COMPANY_ERROR?`<div class="company-notice" role="alert">${esc(COMPANY_ERROR)}</div>`:""}<form id="company-add" class="company-add"><input id="company-careers" inputmode="url" required aria-label="Careers page or job-board link" value="${esc(COMPANY_ADD_DRAFT.url)}" placeholder="https://… a careers page or job-board link"><button class="primary" ${disabled}>${COMPANY_BUSY==="__add__"?"Looking…":"Watch"}</button></form><div class="companies-scroll">${groups||`<div class="panel-empty">${all.length?"No company matches.":"Paste your first company's careers page above."}</div>`}</div></section></div>`;
  if(scrollTop!==null)el("tailor-view").querySelector(".companies-scroll").scrollTop=scrollTop;
  openView("companies",null,"/companies"+(COMPANY_SELECTED?"?c="+encodeURIComponent(COMPANY_SELECTED):""));
}
// "N on Board →": the Board, searched to that company.
function showCompanyOnBoard(name){
  filter="all";q=name;el("q").value=name;sel=0;restoreWorkspace();render();
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
  const place=VIEW==="companies"?"companies":["tailor","preview","revise"].includes(VIEW)?"runs":"board";
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
  if(head==="companies"){COMPANY_SELECTED=params.get("c")||null;renderCompanies();return true}
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
  const labels={"ats-search":"Your companies","linkedin-search":"LinkedIn","freehire-search":"freehire"};
  const failed=sources.filter(s=>s.degraded||s.error||s.failed?.length||s.retry_later).map(s=>labels[s.source]||s.source);
  // Boards the fetch found for companies you saved (COMPANIES_PLAN §3.1).
  const boards=sources.flatMap(s=>s.boards_found||[]);
  const result=(added?`${added} new job${added===1?"":"s"} added`:"No new jobs found")+(boards.length?` · found job boards for ${boards.slice(0,3).join(", ")}${boards.length>3?` and ${boards.length-3} more`:""}`:"");
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
  if(row?.will_be_searched)return postCompany(companyPath(name)+"/test-fetch",{mtime:COMPANIES.mtime});
  return null;
}
// What a check found, in one line: the board and how many of its jobs fit you.
function reportCheck(name,result){
  const row=COMPANIES.companies.find(r=>r.name===name);if(!row)return;
  const test=result?.test_fetch;
  if(row.watch_state==="watching")toast(test?`${name}: ${VENDOR_LABEL[row.vendor]||row.vendor} board · ${test.eligible_jobs} matching job${test.eligible_jobs===1?"":"s"} — Fetch new jobs adds them to the Board`:`${name}: watching its ${VENDOR_LABEL[row.vendor]||row.vendor} board`,{ms:6000});
  else if(row.watch_state==="needs_you")toast(`${name}: ${row.status==="ambiguous"?"pick which job board is theirs":"no job board found — paste its board link"}`,{ms:6000,warn:true});
  else if(row.watch_state==="finding")toast(`${name}: no job board found yet — the next fetch tries again`,{ms:6000});
  else if(row.watch_state==="cant_watch")toast(`${name}: its job board can't be read automatically yet`,{ms:6000,warn:true});
}
function selectCompanyRow(row){
  const scrollTop=row.closest(".companies-scroll")?.scrollTop||0;
  COMPANY_SELECTED=COMPANY_SELECTED===row.dataset.companyRow&&!row.dataset.companyFocus?null:row.dataset.companyRow;
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
  const nav=event.target.closest("[data-nav]");if(nav)return void(nav.dataset.nav==="companies"?renderCompanies():nav.dataset.nav==="runs"?openRuns():restoreWorkspace());
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
  const companyRow=event.target.closest("[data-company-row]");if(companyRow){selectCompanyRow(companyRow);if(companyRow.dataset.companyFocus)el("company-careers-value")?.select();return}
  const companyBoard=event.target.closest("[data-company-board]");if(companyBoard)return void showCompanyOnBoard(companyBoard.dataset.companyBoard);
  const companyGroup=event.target.closest("[data-company-group]");if(companyGroup){const key=companyGroup.dataset.companyGroup;COMPANY_OPEN_GROUPS.has(key)?COMPANY_OPEN_GROUPS.delete(key):COMPANY_OPEN_GROUPS.add(key);renderCompanies(false,el("tailor-view").querySelector(".companies-scroll")?.scrollTop||0);return}
  if(event.target.closest("[data-company-look-now]")){void companyWork("__all__",async()=>{const data=await postCompany("/api/companies/resolve-all",{mtime:COMPANIES.mtime});const counts=data.result?.meta?.status_counts||{};toast(`Looked for ${data.result?.meta?.resolved||0} job boards: ${counts.verified||0} found${counts.ambiguous?`, ${counts.ambiguous} need you`:""}`,{ms:6000})});return}
  const companyCheck=event.target.closest("[data-company-check]");if(companyCheck){const name=companyCheck.dataset.companyCheck;void companyWork(name,async()=>reportCheck(name,await checkCompany(name)));return}
  const companyWatch=event.target.closest("[data-company-watch]");if(companyWatch){const name=companyWatch.dataset.companyWatch;void companyWork(name,async()=>{await postCompany(companyPath(name)+"/manage",{mtime:COMPANIES.mtime,action:"watch"});reportCheck(name,await checkCompany(name))});return}
  const companyNeither=event.target.closest("[data-company-neither]");if(companyNeither){const name=companyNeither.dataset.companyNeither;void companyWork(name,async()=>{await postCompany(companyPath(name)+"/identity",{mtime:COMPANIES.mtime,decision:"neither"});toast(`${name}: paste its job board link to watch it`,{ms:5000})});return}
  const companyRelook=event.target.closest("[data-company-relook]");if(companyRelook){const name=companyRelook.dataset.companyRelook;void companyWork(name,async()=>{await postCompany(companyPath(name)+"/resolve",{mtime:COMPANIES.mtime});reportCheck(name,await checkCompany(name))});return}
  const companyRowAction=event.target.closest("[data-company-action-row]");if(companyRowAction){const [name,action]=companyRowAction.dataset.companyActionRow.split("|");void companyWork(name,()=>postCompany(companyPath(name)+"/manage",{mtime:COMPANIES.mtime,action}));return}
  const companyAction=event.target.closest("[data-company-action]");if(companyAction){const name=COMPANY_SELECTED,action=companyAction.dataset.companyAction;if(action==="remove"&&!confirm("Remove "+name+" from your company list? Saved jobs will stay on the Board."))return;void companyWork(name,async()=>{await postCompany(companyPath(name)+"/manage",{mtime:COMPANIES.mtime,action});if(action==="remove")COMPANY_SELECTED=null});return}
  const companyConfirm=event.target.closest("[data-company-confirm]");if(companyConfirm){const [name,vendor,token]=companyConfirm.dataset.companyConfirm.split("|");void companyWork(name,async()=>{await postCompany(companyPath(name)+"/identity",{mtime:COMPANIES.mtime,decision:"confirm",candidate:{vendor,token}});reportCheck(name,await checkCompany(name))});return}
  const preview=event.target.closest("[data-preview]");if(preview){const run=RUNS.find(r=>r.id===preview.dataset.preview);if(run)renderPreview(run);return}
  const revise=event.target.closest("[data-revise]");if(revise){const run=RUNS.find(r=>r.id===revise.dataset.revise);if(run)renderRevise(run);return}
  const restoreVersion=event.target.closest("[data-restore-version]");if(restoreVersion){restoreVersion.disabled=true;postRun("/api/runs/"+restoreVersion.dataset.restoreVersion+"/restore").then(({ok})=>{const run=RUNS.find(r=>r.id===REVISE_RUN);if(ok&&run)renderRevise(run)});return}
  const versionPreview=event.target.closest("[data-version-preview]");if(versionPreview){const run=RUNS.find(r=>r.id===versionPreview.dataset.versionPreview);if(run)renderPreview(run);return}
  const previewFilter=event.target.closest("[data-preview-filter]");if(previewFilter){const main=el("preview-main");main.classList.toggle("cv-only",previewFilter.dataset.previewFilter==="cv");main.classList.toggle("cover-only",previewFilter.dataset.previewFilter==="cover");document.querySelectorAll("[data-preview-filter]").forEach(node=>node.classList.toggle("on",node===previewFilter));return}
  const applied=event.target.closest("[data-applied]");if(applied){const run=RUNS.find(r=>r.id===applied.dataset.applied);if(!run)return;
    applied.disabled=true;postRun("/api/runs/"+run.id+"/applied",{}).then(({ok,notion})=>{
      if(ok)toast(notion?`Marked applied · Notion Stage: ${notion}`:"Marked applied in the tracker (Notion sync is off)",{ms:3500});
      const fresh=RUNS.find(r=>r.id===run.id)||run;if(VIEW==="preview")renderPreview(fresh);else renderTailor(fresh)});return}
  const reveal=event.target.closest("[data-reveal]");if(reveal&&PREVIEW_RUN){postRun("/api/runs/"+PREVIEW_RUN+"/reveal",{kind:reveal.dataset.reveal});return}
  const variant=event.target.closest("[data-variant]");if(variant&&PREVIEW_RUN){const run=RUNS.find(r=>r.id===PREVIEW_RUN);if(!run)return;
    if(!confirm(`Switch the CV to the ${variant.dataset.variant.toUpperCase()} master variant? Earlier edits to this CV are not carried over; the current version stays under Versions.`))return;
    variant.disabled=true;postRun("/api/runs",{job_url:run.job_url,kind:"revise",parent:run.id,scope:run.scope||"both",base_cv:variant.dataset.variant,note:""}).then(({ok,run_id})=>{const next=ok&&RUNS.find(r=>r.id===run_id);if(next)renderTailor(next);else variant.disabled=false});return}
  const recompile=event.target.closest("[data-recompile]");if(recompile){recompile.disabled=true;postRun("/api/runs/"+recompile.dataset.recompile+"/compile").then(({ok})=>{const run=RUNS.find(r=>r.id===recompile.dataset.recompile);if(ok&&run)renderPreview(run)});return}
  const runNode=event.target.closest("[data-run]");if(runNode){const run=RUNS.find(r=>r.id===runNode.dataset.run);if(run)renderTailor(run);return}
  const pill=event.target.closest("#runpill");if(pill){const run=RUNS.find(r=>r.id===pill.dataset.run);if(run)renderTailor(run);return}
  const cont=event.target.closest(".continuerun");if(cont){cont.disabled=true;const source=cont.dataset.runId;postRun("/api/runs/"+source+"/continue",{proceed:cont.dataset.proceed==="1"}).then(({ok,run_id})=>{if(!ok){cont.disabled=false;return}toast("continuing from the saved checkpoint",{ms:2500});const next=RUNS.find(r=>r.id===run_id);if(next)renderTailor(next)});return}
  const cancel=event.target.closest(".cancelrun");if(cancel){postRun("/api/runs/"+cancel.dataset.runId+"/cancel");return}
  const retry=event.target.closest(".retryrun");if(retry){retry.disabled=true;const failed=RUNS.find(r=>r.id===retry.dataset.runId);postRun("/api/runs/"+retry.dataset.runId+"/retry").then(({ok})=>{if(!ok){retry.disabled=false;return}toast("regenerating from the saved posting; earlier versions are kept",{ms:2500});const next=RUNS.find(r=>r.retry_of===failed?.id);if(next)renderTailor(next)});return}
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
    // One field: the name comes from the link and can be changed under ⋯.
    event.preventDefault();const careers_url=el("company-careers").value,known=new Set(COMPANIES.companies.map(row=>row.name));
    COMPANY_ADD_DRAFT={name:"",url:careers_url};
    void companyWork("__add__",async()=>{await postCompany("/api/companies",{mtime:COMPANIES.mtime,name:"",careers_url});COMPANY_ADD_DRAFT={name:"",url:""};const added=COMPANIES.companies.find(row=>!known.has(row.name));if(added){COMPANY_QUERY="";COMPANY_BUSY=added.name;if(VIEW==="companies")renderCompanies(false);reportCheck(added.name,added.will_be_searched?await checkCompany(added.name):null)}});return;
  }
  if(event.target.id==="company-settings"){
    event.preventDefault();const oldName=COMPANY_SELECTED,name=el("company-name-value").value.trim(),careers_url=el("company-careers-value").value.trim(),market=el("company-market-value").value;
    const old=COMPANIES.companies.find(row=>row.name===oldName),sourceChanged=careers_url&&careers_url!==(old?.careers_url||(old?.domain?"https://"+old.domain:""));
    const changes={name,countries:market?market.split(","):[]};if(sourceChanged)changes.careers_url=careers_url;
    void companyWork(oldName,async()=>{await postCompany(companyPath(oldName),{mtime:COMPANIES.mtime,inspect:true,changes},"PATCH");COMPANY_SELECTED=name;COMPANY_BUSY=name;if(sourceChanged&&COMPANIES.companies.find(row=>row.name===name)?.will_be_searched)await checkCompany(name);toast("Company saved")});return;
  }
  if(event.target.id==="regen-form"){
    event.preventDefault();const run=RUNS.find(r=>r.id===PREVIEW_RUN),note=el("regen-note").value.trim(),edit=new FormData(event.target).get("edit");
    if(!run||!edit)return;if(!note){toast("say what should change",{warn:true});return}
    const button=event.submitter;if(button)button.disabled=true;
    postRun("/api/runs",{job_url:run.job_url,kind:"revise",parent:run.id,scope:run.scope||"both",edit,note}).then(({ok,run_id})=>{const next=ok&&RUNS.find(r=>r.id===run_id);if(next)renderTailor(next);else if(button)button.disabled=false});return;
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
  if(event.target.matches("[data-mark]")&&PREVIEW_RUN){const box=event.target;box.disabled=true;
    fetch(`/api/runs/${encodeURIComponent(PREVIEW_RUN)}/marks?t=${T}`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:box.dataset.mark,done:box.checked})})
      .then(async response=>{checkAuth(response);const data=await response.json().catch(()=>({}));if(!response.ok){box.checked=!box.checked;toast(data.error||"could not save the mark",{warn:true})}
        else{const manual=data.manual||[],summary=document.querySelector(".check-rail .state-summary");if(summary)summary.textContent=`${manual.filter(m=>m.done).length} / ${manual.length}`}})
      .finally(()=>{box.disabled=false});return}
  if(event.target.matches("[data-run-follow]")){RUN_FOLLOW=event.target.checked;
    const runlog=el("runlog");if(RUN_FOLLOW&&runlog)runlog.scrollTop=runlog.scrollHeight}

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
