"""Exercise archive and restore actions against the real browser functions."""
import shutil
import subprocess
import unittest
from pathlib import Path


class ArchiveBrowserTest(unittest.TestCase):
    def test_archive_restore_and_cancel(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = r'''
const assert=require('assert/strict'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const block=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const run={id:'example',company:'Example',role:'Engineer',phase:'done',review:{total:8,flagged:2,measured:true}};
const ctx={RUNS:[run],RUNNING:[],QUEUE:[],APPS_SHOW_ARCHIVED:false,
  modal:'Not a match',requests:[],opened:[],messages:[],reloads:0,render:()=>{}};
ctx.openTextModal=async options=>{assert(options.hint.includes('mark the job as No'));return ctx.modal};
ctx.postRun=async(path,body)=>{ctx.requests.push({path,body});
  if(path.endsWith('/unarchive'))delete run.archived_at;else run.archived_at='2026-10-10';
  return {ok:true,board:path.endsWith('/unarchive')?'restored':'updated'};};
ctx.reloadJobs=async()=>ctx.reloads++;
ctx.openApp=r=>ctx.opened.push(r.id);
ctx.toast=text=>ctx.messages.push(text);
vm.createContext(ctx);
vm.runInContext(block('function appState(','const APP_GROUPS='),ctx);
vm.runInContext(block('async function archiveApp(','async function deleteApp('),ctx);
(async()=>{
  assert.equal(ctx.appState(run),'review');
  await ctx.archiveApp(run.id);
  assert.equal(ctx.appState(run),'archived');
  assert.equal(ctx.requests[0].path,'/api/runs/example/archive');
  assert.equal(ctx.requests[0].body.reason,'Not a match');
  assert.equal(ctx.reloads,1);assert(ctx.APPS_SHOW_ARCHIVED);
  await ctx.archiveApp(run.id,true);
  assert.equal(ctx.appState(run),'review');
  assert.equal(ctx.requests[1].path,'/api/runs/example/unarchive');
  assert(!ctx.APPS_SHOW_ARCHIVED);
  ctx.modal=null;await ctx.archiveApp(run.id);
  assert.equal(ctx.requests.length,2,'cancel must not write');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([node, "-e", script, str(Path(__file__).resolve().parents[1] / "tools/board/static/app.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
