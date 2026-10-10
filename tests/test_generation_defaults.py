"""Rule-based generation choices agree between the picker and supervisor."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.board_harness import runs


class GenerationDefaultsTest(unittest.TestCase):
    def test_role_rules_match_in_browser_and_supervisor(self):
        examples = [
            ("AI Engineer", "ai"), ("AI/ML Engineer", "ai"),
            ("ML Research Engineer", "ai"), ("MLOps Engineer", "ai"),
            ("Machine-Learning Engineer", "ai"), ("Deep Learning Researcher", "ai"),
            ("Data Scientist", "ai"), ("Data Science Intern", "ai"),
            ("Computer Vision Engineer", "ai"), ("NLP Engineer", "ai"),
            ("GenAI Engineer", "ai"), ("LLM Engineer", "ai"),
            ("Software Engineer", "sde"), ("Backend Developer", "sde"),
            ("Data Engineer", "sde"), ("Retail Platform Engineer", "sde"), ("", "sde"),
        ]
        for title, expected in examples:
            with self.subTest(title=title):
                self.assertEqual(runs.Supervisor._recommend_base_cv(title), expected)
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        probe = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const src=fs.readFileSync(process.argv[1],'utf8');
const JOBS=[{url:'https://example.test/job',title:'',attrs:{country:'CH'}}];
const ctx={JOBS,esc:String,scopeOptions:()=>''};vm.createContext(ctx);
vm.runInContext(src.slice(src.indexOf('const SCOPES='),src.indexOf('const DRAFT_LABEL='))+
  '\nglobalThis.ui={variantOptions,countryOptions,generatePicker,chosenEngine};',ctx);
for(const [title,variant] of JSON.parse(process.argv[2])){
  JOBS[0].title=title;
  assert(ctx.ui.variantOptions(JOBS[0].url).includes(`value="${variant}" selected`),title);
}
for(const [country,expected] of [['CH','ch'],['DE','de'],['AT','ch'],['unknown','ch']]){
  JOBS[0].attrs.country=country;
  assert(ctx.ui.countryOptions(JOBS[0].url).includes(`value="${expected}" selected`),country);
}
assert.equal(ctx.ui.chosenEngine('generate'),'codex');
assert(ctx.ui.generatePicker(JOBS[0].url).includes('value="codex" selected'));
'''
        app = Path(__file__).resolve().parents[1] / "tools/board/static/app.js"
        result = subprocess.run([node, "-e", probe, str(app), json.dumps(examples)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
