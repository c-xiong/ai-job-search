"""Exercise provider selection through the actual browser event handlers."""
import shutil
import subprocess
import unittest
from pathlib import Path

PROBE = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8');
const slice=(a,b)=>src.slice(src.indexOf(a),src.indexOf(b,src.indexOf(a)));
const listeners={},requests=[],nodes={};
const run={id:'r1',provider:'claude',scope:'both',job_url:'https://example.test/job'};
const ctx={RUNS:[run],PREVIEW_RUN:'r1',REVISE_RUN:'r1',POSTING_URL_JOB:null,
  closePopovers:()=>{},esc:String,toast:()=>{},confirm:()=>true,renderTailor:()=>{},openApp:()=>{},
  restoreWorkspace:()=>{},el:id=>nodes[id]||(nodes[id]={value:''}),
  postRun:async(path,body)=>{requests.push({path,body});return {ok:false}},
  FormData:class{constructor(form){this.data=form.data||{}}get(k){return this.data[k]}},
  document:{addEventListener:(type,fn)=>listeners[type]=fn,querySelector:()=>null}};
vm.createContext(ctx);
vm.runInContext(slice('let DEFAULT_PROVIDER=','const generatePicker=')+'\n'+
  slice('document.addEventListener("click"','el("q").addEventListener')+
  '\nglobalThis.ui={enginePicker,chosenEngine};',ctx);
assert(ctx.ui.enginePicker('r1','claude').includes('value="claude" selected'));
listeners.change({target:{dataset:{engine:'r1'},value:'codex',matches:s=>s==='[data-engine]'}});
assert.equal(ctx.ui.chosenEngine('r1',run),'codex');
assert(ctx.ui.enginePicker('r1','claude').includes('value="codex" selected'));
const event=(selector,fields={})=>{
  const target={dataset:{runId:'r1'},...fields};
  target.closest=s=>s===selector?target:null;
  return {target,preventDefault(){},submitter:{dataset:{reentryKind:'apply'}}};
};
listeners.click(event('.continuerun'));
listeners.click(event('.retryrun'));
listeners.submit(event('',{id:'regen-form',data:{edit:'cover'}}));
for(const kind of ['revise','redraft','apply']){
  nodes['revision-note']={value:'Use existing evidence'};
  const e=event('',{id:'revise-form',data:{scope:'both'}});
  e.submitter.dataset.reentryKind=kind;listeners.submit(e);
}
assert.equal(requests.length,6);
for(const request of requests)assert.equal(request.body.provider,'codex');
assert.equal(requests[0].path,'/api/runs/r1/continue');
assert.equal(requests[1].path,'/api/runs/r1/retry');
assert.equal(requests[2].body.edit,'cover');
assert.equal(requests[5].body.kind,'apply');
assert.equal(requests[5].body.parent,undefined);
assert.equal(ctx.ui.chosenEngine('legacy',{provider:'claude'}),'claude');
"""


class ProviderUiTest(unittest.TestCase):
    def test_engine_selection_reaches_all_reentry_requests(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        app = Path(__file__).resolve().parents[1] / "tools/board/static/app.js"
        result = subprocess.run([node, "-e", PROBE, str(app)], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
