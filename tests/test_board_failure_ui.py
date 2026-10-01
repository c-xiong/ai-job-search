"""Failure details distinguish a blocked stage from the whole attempt."""

import shutil
import subprocess
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1] / "tools" / "board" / "static" / "app.js"
PROBE = r"""
const assert=require("assert/strict"),fs=require("fs"),vm=require("vm");
const source=fs.readFileSync(process.argv[1],"utf8");
const start=source.indexOf("const FAILURE_TITLE="),end=source.indexOf("function progressPanel(",start);
assert(start>=0&&end>start,"failure helpers missing");
const ctx={};vm.createContext(ctx);
vm.runInContext(source.slice(start,end)+"\nglobalThis.ui={failureActivityNote,FAILURE_NEXT};",ctx);
const note=ctx.ui.failureActivityNote;
assert.equal(note({model_started:false,cost:{total_usd:0},usage:[]}),
  " · no model work started · no model cost incurred");
assert.equal(note({model_started:false,cost:{total_usd:.59},usage:[{stage:"revise"}]}),
  " · no model work started for this stage");
assert.equal(note({model_started:false,cost:{total_usd:.59}}),
  " · no model work started for this stage");
assert.equal(note({model_started:false,cost:{total_usd:0},usage:[{stage:"review",input_tokens:100}]}),
  " · no model work started for this stage");
assert.equal(note({model_started:true,cost:{total_usd:.59},usage:[{stage:"revise"}]}),"");
assert(ctx.ui.FAILURE_NEXT.budget_cap.includes("run and daily caps"));
assert(ctx.ui.FAILURE_NEXT.budget_cap.includes("adjust them if needed"));
assert(!ctx.ui.FAILURE_NEXT.budget_cap.startsWith("Raise"));
assert(source.slice(source.indexOf("function renderTailor("),source.indexOf("function renderSend("))
  .includes("${failureActivityNote(run)}"),"rendered failure must use the activity note");
"""


class FailureUiTest(unittest.TestCase):
    def test_attempt_usage_changes_blocked_stage_copy(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        result = subprocess.run([node, "-e", PROBE, str(APP)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
