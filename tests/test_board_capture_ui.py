"""Execute the actual capture parser and preview; malicious handoffs never write."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tools" / "board" / "static"
PROBE = r'''
const assert=require("assert/strict"),fs=require("fs"),vm=require("vm");
const nodes={},listeners={},requests=[];
const node=id=>nodes[id]||(nodes[id]={value:"",textContent:"",hidden:false,disabled:false,checked:false,open:true,children:[],replaceChildren(){this.children=[]},append(child){this.children.push(child)},addEventListener(event,fn){listeners[id+":"+event]=fn}});
const payload={jobs:[{job_id:"123456",title:'<img src=x onerror=alert(1)>',company:"Demo",location:"Berlin",posted:"1 day ago",badges:["Easy Apply"]}]};
const context={URL,TextEncoder,console,JSON,document:{getElementById:node,createElement:()=>({textContent:"",children:[],append(child){this.children.push(child)}})},
  location:{hash:"#"+encodeURIComponent(JSON.stringify(payload)),pathname:"/static/capture.html"},history:{replaceState(...args){context.cleared=args}},
  localStorage:{getItem:()=>"fake-preview-token"},fetch:async(url,options)=>{requests.push({url,options});return {ok:true,json:async()=>({added:1,duplicates:0,mtime:"v1"})}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],"utf8"),context);
const api=context.JobFlowCapture;
assert.equal(api.parse(JSON.stringify(payload)).jobs[0].location,"Berlin");
assert.equal(api.parse(JSON.stringify({jobs:[payload.jobs[0],payload.jobs[0]]})).jobs.length,1);
for(const bad of [{jobs:[{job_id:"abc"}]},{jobs:[{url:"https://evil.test/jobs/view/123456"}]},{jobs:[{job_id:"123456",url:"https://www.linkedin.com/jobs/view/999"}]},{jobs:[{job_id:"1",badges:[{}]}]}])assert.throws(()=>api.parse(JSON.stringify(bad)));
assert.throws(()=>api.parse(' '.repeat(262145)));assert.throws(()=>api.publicUrl("javascript:alert(1)"));assert.throws(()=>api.publicUrl("https://user:pass@demo.test"));
const bookmarklet=api.linkedinBookmarklet("http://127.0.0.1:8765");assert(bookmarklet.startsWith("javascript:"));assert(!bookmarklet.includes("fake-preview-token"));
let handoff;
const card={getClientRects:()=>[{}],querySelector:selector=>selector.includes("title")?{innerText:"Demo Engineer"}:null,querySelectorAll:()=>[]};
const link={href:"https://www.linkedin.com/jobs/view/123456/?trackingId=fake",innerText:"Demo Engineer",closest:()=>card};
vm.runInNewContext(bookmarklet.slice(11),{location:{hostname:"www.linkedin.com"},document:{querySelectorAll:()=>[link,link]},Map,Set,TextEncoder,JSON,window:{open:url=>handoff=url},alert:message=>{throw new Error(message)}});
assert.equal(api.parse(decodeURIComponent(handoff.split("#")[1])).jobs[0].job_id,"123456");
let companyHandoff;
vm.runInNewContext(api.companyBookmarklet("http://127.0.0.1:8765").slice(11),{URL,Set,JSON,location:{href:"https://demo.test/careers"},document:{title:"Demo Labs",querySelector:()=>null,querySelectorAll:selector=>{assert(selector.includes("iframe[src]"));return [{src:"https://jobs.ashbyhq.com/demo"}]}},window:{open:url=>companyHandoff=url}});
assert.equal(api.parse(decodeURIComponent(companyHandoff.split("#")[1])).company.candidates[0],"https://jobs.ashbyhq.com/demo");
vm.runInContext(fs.readFileSync(process.argv[2],"utf8"),context);
assert.equal(requests.length,0);assert.equal(context.cleared[2],"/static/capture.html");
assert.equal(node("jobs").children[0].children[0].textContent,payload.jobs[0].title);
assert.equal(node("save").disabled,false);
const tick=()=>new Promise(setImmediate);
(async()=>{
  listeners["save:click"]();await tick();assert.equal(requests.length,1);
  assert(requests[0].url.startsWith("/api/linkedin/capture?t="));assert(node("board").href.endsWith("#/inbox"));
  const company={kind:"company",company:{name:"Demo Labs",careers_url:"https://demo.test/careers",candidates:["https://jobs.ashbyhq.com/demo"]}};
  node("payload").value=JSON.stringify(company);listeners["preview:click"]();assert.equal(requests.length,1);
  listeners["save:click"]();await tick();assert.equal(requests.length,1);assert(node("status").textContent.includes("Confirm"));
  node("confirmed").checked=true;listeners["save:click"]();await tick();assert.equal(requests.length,3);
  const body=JSON.parse(requests[2].options.body);assert.equal(body.confirmed,true);assert.equal(body.mtime,"v1");
  node("payload").value=JSON.stringify({jobs:[{url:"https://evil.test/jobs/view/123456"}]});listeners["preview:click"]();assert(node("save").disabled);assert.equal(requests.length,3);
})().catch(error=>{console.error(error);process.exitCode=1});
'''

INBOX_PROBE = r'''
const assert=require("assert/strict"),fs=require("fs"),vm=require("vm");
const src=fs.readFileSync(process.argv[1],"utf8"),nodes={},requests=[],messages=[];
const node=id=>nodes[id]||(nodes[id]={innerHTML:"",value:"15",reportValidity:()=>true});
const fixture={items:[{job_id:"123456",title:"<script>alert(1)</script>",company:"Demo",state:"retry",location:"Berlin",reason:"Try again",last_captured_at:"2026-10-02T10:00:00Z",next_retry_at:"2026-10-03T10:00:00Z",possible_duplicate:["x"]}],counts:{retry:1,pending:2},last_capture_at:"2026-10-02T10:00:00Z",process_status:{running:false}};
let companies={mtime:"v1",companies:[]},prompt="",promptCalls=0;
const ctx={URL,TextEncoder,console,INBOX:null,INBOX_POLL:null,INBOX_REVISION:0,VIEW:null,T:"t",FOLLOW_BUSY:false,COMPANIES:null,
  location:{origin:"http://127.0.0.1:8765"},document:{querySelector:()=>({disabled:false})},
  el:node,esc:x=>String(x||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;"),
  postedText:x=>x.posted,openView:view=>ctx.VIEW=view,renderJob:()=>{},renderCompanies:async()=>{},reloadJobs:async()=>{},pollActivity:()=>{},
  toast:msg=>messages.push(msg),checkAuth:()=>true,clearTimeout:()=>{},setTimeout:()=>1,
  openTextModal:async()=>{promptCalls++;return prompt},
  postCompany:async(path,body)=>{requests.push({path,body});return {followed:{name:"Demo Labs",existing:false}}},
  fetch:async(path,options)=>{requests.push({path,body:options?JSON.parse(options.body):null});return {ok:true,json:async()=>path.startsWith("/api/companies")?companies:path.startsWith("/api/linkedin/process")?{started:true}:fixture}}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[2],"utf8"),ctx);
vm.runInContext(src.slice(src.indexOf("async function followJobCompany("),src.indexOf('// "N on Board')),ctx);
vm.runInContext(src.slice(src.indexOf("function fetchSummary("),src.indexOf("function renderFetchLog(")),ctx);
(async()=>{
  assert(ctx.fetchSummary({finished_at:"2026-10-02",sources:[{source:"linkedin-search",enriched:2,cached:true}]}).message.includes("2 descriptions completed"));
  await ctx.renderInbox();const html=node("tailor-view").innerHTML;
  assert(html.includes("Capture LinkedIn list"));assert(html.includes("Paste/import JSON"));assert(html.includes("Process inbox"));
  assert(html.includes("&lt;script>alert(1)&lt;/script>"));assert(!html.includes("<script>"));
  assert(html.includes("Possible duplicate — kept for review"));assert(html.includes("Last capture:"));assert(html.includes("Retry after"));
  node("inbox-limit").value="10";await ctx.processInbox();assert(requests.some(r=>r.path.startsWith("/api/linkedin/process")&&r.body.limit===10));
  const job={url:"https://www.linkedin.com/jobs/view/123456",company:"Demo Labs",default_open_url:"https://jobs.ashbyhq.com/demo/role"};
  const count=requests.length;await ctx.followJobCompany(job);assert.equal(promptCalls,0);
  const write=requests.slice(count).find(r=>r.path==="/api/companies/follow");assert.equal(write.body.url,job.url);assert.equal(write.body.mtime,"v1");assert.equal(write.body.confirmed,undefined);
  const easy={...job,default_open_url:job.url};prompt=null;const before=requests.filter(r=>r.path==="/api/companies/follow").length;
  await ctx.followJobCompany(easy);assert.equal(promptCalls,1);assert.equal(requests.filter(r=>r.path==="/api/companies/follow").length,before);
  companies={mtime:"v2",companies:[{name:"Demo Labs"}]};await ctx.followJobCompany(easy);assert.equal(promptCalls,1);
})().catch(error=>{console.error(error);process.exitCode=1});
'''


class CaptureUITest(unittest.TestCase):
    def test_preview_validates_text_only_handoff_and_requires_click_before_requests(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        result = subprocess.run([node, "-e", PROBE, str(STATIC / "capture.js"),
                                 str(STATIC / "capture-preview.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inbox_states_and_company_follow_use_auth_bounded_processing_and_safe_text(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        result = subprocess.run([node, "-e", INBOX_PROBE, str(STATIC / "app.js"),
                                 str(STATIC / "capture.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
