"""Application navigation and folding against the browser implementation."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ApplicationSidebarTest(unittest.TestCase):
    def test_selection_collapses_sidebar_and_applied_group_can_stay_folded(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        script = r'''
const assert=require('assert/strict'),fs=require('fs'),vm=require('vm');
const src=fs.readFileSync(process.argv[1],'utf8');
const block=(start,end)=>src.slice(src.indexOf(start),src.indexOf(end,src.indexOf(start)));
const run={id:'example',company:'Example',role:'Engineer'};
const ctx={RUNS:[run],ACTIVE_RUN:run.id,APPS_Q:'',APPS_SHOW_APPLIED:true,
 APPS_REVIEW_LIST:true,layout:{appsCollapsed:false},esc:String,
 appsOrdered:()=>[['applied','Applied',[run]]],saved:0,opened:0,
 appRowMeta:()=>'',el:()=>null};
ctx.saveLayout=()=>ctx.saved++;
ctx.openApp=value=>{assert.equal(value,run);ctx.opened++;assert.equal(ctx.layout.appsCollapsed,true);assert.equal(ctx.APPS_REVIEW_LIST,false)};
vm.createContext(ctx);
vm.runInContext(block('function appsGroupsHtml(){','function attemptTabs('),ctx);
ctx.event={target:{closest:()=>({dataset:{app:run.id}})}};
vm.runInContext('(function(){'+block('  const appLink=event.target.closest("[data-app]");','  const chip=event.target.closest("[data-filter]");')+'})()',ctx);
assert.equal(ctx.opened,1);assert.equal(ctx.saved,1);
assert.equal(vm.runInContext('appsListCollapsed("review")',ctx),true);
assert.equal(vm.runInContext('appsListCollapsed("send")',ctx),true);
assert.equal(vm.runInContext('appsListCollapsed("log")',ctx),true);
assert(ctx.appsGroupsHtml().includes('data-app="example"'));
const toggle=block('  if(event.target.closest("[data-apps-applied]"))','  const delApp=');
vm.runInContext('(function(){'+toggle+'})()',ctx);
assert.equal(ctx.APPS_SHOW_APPLIED,false);
assert(ctx.appsGroupsHtml().includes('aria-expanded="false"'));
assert(!ctx.appsGroupsHtml().includes('data-app="example"'),'selected application must not force group open');
ctx.renderAppsList();assert(!ctx.appsGroupsHtml().includes('data-app="example"'));
vm.runInContext('(function(){'+toggle+'})()',ctx);
assert(ctx.appsGroupsHtml().includes('aria-expanded="true"'));
assert(ctx.appsGroupsHtml().includes('data-app="example"'));
'''
        result = subprocess.run([node, '-e', script, str(ROOT / 'tools/board/static/app.js')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
