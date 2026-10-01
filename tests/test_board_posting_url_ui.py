"""Posting-link editing and application association against the real browser JS."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const assert=require("assert/strict"),fs=require("fs"),vm=require("vm");
const src=fs.readFileSync(process.argv[2],"utf8");
const block=(start,end)=>{
  const a=src.indexOf(start),b=src.indexOf(end,a);
  assert(a>=0&&b>a,`missing browser block: ${start}`);return src.slice(a,b);
};
const original="https://freehire.me/jobs/engineer",legacy="https://careers.example.test/jobs/42";
const preferred="https://company.example.test/apply?jobId=42",replacement="https://company.example.test/jobs/42";
const job={url:original,open_url:legacy,default_open_url:legacy,posting_url:"",
  known_urls:[original,legacy],company:"Example",title:"Engineer",status:"yes",note:"Keep this"};
const run={id:"r1",job_url:legacy,phase:"done",scope:"cv",review:{total:1,done:1}};
const listeners={},nodes={},requests=[],responses=[],messages=[];
const classList=()=>({add(){},remove(){},toggle(){}});
const document={body:{classList:classList()},activeElement:null,
  addEventListener:(name,fn)=>listeners[name]=fn,querySelector:()=>null};
const node=id=>nodes[id]||(nodes[id]={id,value:"",hidden:false,disabled:false,isConnected:true,
  classList:classList(),focus(){document.activeElement=this},select(){this.selected=true},
  closest(selector){return selector.split(",").some(s=>s==="#"+id)?this:null}});
const controls=["posting-url-input","posting-url-close","posting-url-reset","posting-url-cancel","posting-url-save"].map(node);
node("posting-url-form").querySelectorAll=()=>controls;
node("posting-url-modal").hidden=true;
const opener=node("opener");opener.focus();
const ctx={URL,document,JOBS:[job],RUNS:[run],T:"token",ACTIVE_RUN:null,PREVIEW_RUN:null,VIEW:"board",
  POSTING_URL_JOB:null,POSTING_URL_BUSY:false,POSTING_URL_FOCUS:null,POSTING_URL_REVISION:0,
  STATUSES:[],FILTERS:{},buildFilters:()=>[],renderStatusKeys:()=>{},
  el:node,displayTitle:x=>x,requestAnimationFrame:fn=>fn(),checkAuth:()=>{},render:()=>{},pollActivity:()=>{},
  toast:message=>messages.push(message),closePopovers:()=>{},latestApplications:()=>[run],
  esc:x=>String(x||"").replaceAll("&","&amp;").replaceAll('"',"&quot;"),
  docKinds:()=>["cv"],appState:()=>"ready",DOC_TITLE:{cv:"CV"},crumbRun:()=>({}),openView:()=>{},
  mountApp:(_run,_view,html)=>ctx.sendHtml=html,
  fetch:async(url,options)=>{
    requests.push({url,...options,body:JSON.parse(options.body)});
    const response=responses.shift();if(response instanceof Error)throw response;
    assert(response,"unexpected save request");return {ok:response.ok!==false,json:async()=>response};
  }};
vm.createContext(ctx);
vm.runInContext([
  block("const jobUrls=","function postingLinkLabel"),
  block("function openPostingUrl(","function elapsed("),
  block("function appsByJob(","// The Draft cell:"),
  block("const jobOfRun=","function appHeader("),
  block("function renderSend(","// Deleting is soft"),
  block("async function reloadJobs(","function fetchSummary("),
  block('document.addEventListener("click"','el("q").addEventListener'),
  "globalThis.ui={openPostingUrl,closePostingUrl,savePostingUrl,applicationFor,jobOfRun,renderSend,reloadJobs};"
].join("\n"),ctx);
const tick=()=>new Promise(setImmediate);
const event=(target,extra={})=>({target,preventDefault(){this.prevented=true},...extra});
const reply=(posting_url,open_url)=>responses.push({posting_url,open_url});

async function editor(){
  ctx.ui.openPostingUrl(job);
  assert.equal(node("posting-url-input").value,legacy);
  assert.equal(node("posting-url-modal").hidden,false);
  assert.equal(node("posting-url-reset").hidden,true);
  assert.equal(document.activeElement,node("posting-url-input"));
  node("posting-url-input").value="  "+preferred+"  ";reply(preferred,preferred);
  const submitted=event(node("posting-url-form"));listeners.submit(submitted);await tick();
  assert.equal(submitted.prevented,true);
  assert.deepEqual(requests[0].body,{url:original,posting_url:preferred});
  assert.equal(job.open_url,preferred);assert.equal(job.posting_url,preferred);
  assert.equal(job.url,original);assert.equal(job.status,"yes");assert.equal(job.note,"Keep this");
  assert.equal(node("posting-url-modal").hidden,true);assert.equal(document.activeElement,opener);

  ctx.ui.openPostingUrl(job);assert.equal(node("posting-url-reset").hidden,false);
  node("posting-url-input").value=replacement;
  responses.push({ok:false,error:"URL rejected"});await ctx.ui.savePostingUrl();
  assert.equal(node("posting-url-modal").hidden,false);assert.equal(node("posting-url-input").value,replacement);
  assert.equal(job.open_url,preferred);assert.equal(node("posting-url-status").textContent,"URL rejected");
  assert(controls.every(control=>!control.disabled));
  responses.push(new Error("Connection lost"));await ctx.ui.savePostingUrl();
  assert.equal(node("posting-url-modal").hidden,false);assert.equal(job.open_url,preferred);
  assert.equal(node("posting-url-input").value,replacement);
  responses.push({ok:true});await ctx.ui.savePostingUrl();
  assert.equal(node("posting-url-modal").hidden,false);assert.equal(job.open_url,preferred);
  assert.equal(node("posting-url-input").value,replacement);
  assert(node("posting-url-status").textContent.includes("Restart"));

  const count=requests.length;listeners.click(event(node("posting-url-cancel")));
  assert.equal(node("posting-url-modal").hidden,true);assert.equal(requests.length,count);
  assert.equal(job.open_url,preferred);
  ctx.ui.openPostingUrl(job);listeners.keydown(event(node("posting-url-input"),{key:"Escape"}));
  assert.equal(node("posting-url-modal").hidden,true);assert.equal(requests.length,count);

  ctx.ui.openPostingUrl(job);reply("",legacy);listeners.click(event(node("posting-url-reset")));await tick();
  assert.deepEqual(requests.at(-1).body,{url:original,posting_url:""});
  assert.equal(job.open_url,legacy);assert.equal(job.posting_url,"");assert.equal(job.url,original);
  assert.equal(node("posting-url-modal").hidden,true);
}
async function applications(){
  ctx.ui.openPostingUrl(job);node("posting-url-input").value=preferred;reply(preferred,preferred);
  await ctx.ui.savePostingUrl();
  assert.equal(ctx.ui.applicationFor(job),run);assert.equal(ctx.ui.jobOfRun(run),job);
  ctx.ui.renderSend(run);
  assert(ctx.sendHtml.includes('href="https://company.example.test/apply?jobId=42"'));
  assert(ctx.sendHtml.includes('data-posting-url="'+original+'"'));
  assert(!ctx.sendHtml.includes('href="'+legacy+'"'));
  assert.equal(run.job_url,legacy);
  ctx.ui.renderSend({...run,job_url:"https://removed.example.test/job"});
  assert(ctx.sendHtml.includes('href="https://removed.example.test/job"'));
}
async function replacedRow(){
  let respond;ctx.fetch=()=>new Promise(resolve=>respond=resolve);
  ctx.ui.openPostingUrl(job);node("posting-url-input").value=preferred;
  const pending=ctx.ui.savePostingUrl();
  ctx.ui.closePostingUrl();ctx.ui.openPostingUrl({...job,url:replacement});
  assert.equal(node("posting-url-modal").hidden,false);assert.equal(ctx.POSTING_URL_JOB,original);
  assert(controls.every(control=>control.disabled));
  const extra="https://company.example.test/legacy/42";
  const live={...job,known_urls:[original,extra]};ctx.JOBS=[live];
  respond({ok:true,json:async()=>({posting_url:preferred,open_url:preferred})});await pending;
  assert.equal(live.open_url,preferred);assert.equal(live.posting_url,preferred);
  assert.equal(live.url,original);assert.equal(ctx.ui.jobOfRun(run),live);
  assert([original,legacy,extra,preferred].every(url=>live.known_urls.includes(url)));
  assert.equal(node("posting-url-modal").hidden,true);assert(controls.every(control=>!control.disabled));
}
async function staleReload(){
  let release,reads=0;const stale={...job,known_urls:[...job.known_urls]};
  ctx.fetch=async url=>{
    if(url.startsWith("/api/jobs")){
      reads++;if(reads===1)return new Promise(resolve=>release=resolve);
      return {ok:true,json:async()=>({jobs:[{...job}],statuses:["yes"]})};
    }
    return {ok:true,json:async()=>({posting_url:preferred,open_url:preferred})};
  };
  const reload=ctx.ui.reloadJobs();
  ctx.ui.openPostingUrl(job);node("posting-url-input").value=preferred;await ctx.ui.savePostingUrl();
  release({ok:true,json:async()=>({jobs:[stale],statuses:["yes"]})});await reload;
  assert.equal(reads,2);assert.equal(ctx.JOBS[0].open_url,preferred);
  assert.equal(ctx.JOBS[0].posting_url,preferred);assert.equal(ctx.JOBS[0].url,original);
  assert.equal(ctx.ui.applicationFor(ctx.JOBS[0]),run);
}
({editor,applications,replacedRow,staleReload}[process.argv[3]]()).catch(error=>{console.error(error);process.exitCode=1});
"""


class PostingUrlBrowserTest(unittest.TestCase):
    def probe(self, scenario):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "posting-url.js"
            probe.write_text(HARNESS, encoding="utf-8")
            result = subprocess.run(
                [node, str(probe), str(ROOT / "tools/board/static/app.js"), scenario],
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_edit_save_fail_cancel_and_reset(self):
        self.probe("editor")

    def test_legacy_application_keeps_association_and_opens_preferred_url(self):
        self.probe("applications")

    def test_save_updates_live_row_after_reload_replaces_jobs(self):
        self.probe("replacedRow")

    def test_overlapping_reload_retries_instead_of_restoring_stale_url(self):
        self.probe("staleReload")
