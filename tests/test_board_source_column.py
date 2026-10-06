"""The Source column identifies acquisition even after a destination URL edit."""

import shutil
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "tools" / "board" / "static" / "app.js"
PROBE = r"""
const assert=require("assert");
const source=require("fs").readFileSync(process.argv[1],"utf8");
const labels=source.slice(source.indexOf("const SOURCE_LABELS="),source.indexOf("function renderChips(){"));
const table=source.slice(source.indexOf("function render(){"),source.indexOf('// "Found" is when'));
const jobs=[
  {portal:"freehire-search",primary_source:"ats-search",sources:["freehire-search","manual"]},
  {portal:"linkedin-search",primary_source:"ats-search",sources:["linkedin-search","ats-search"]},
  {portal:"linkedin-browser",primary_source:"company-careers",sources:["linkedin-browser","company-careers"]},
].map((job,index)=>({...job,url:"original-"+index,posting_url:"https://careers.example.com/"+index,
                    open_url:"https://careers.example.com/"+index,title:"Engineer",company:"Example",
                    status:"new",fit:"high",location:"Zurich",posted:"2026-10-02"}));
const render=new Function("JOBS",`
  let sel=0;
  const elements={},el=id=>elements[id]||(elements[id]={});
  const renderChips=()=>{},renderSourceChips=()=>{},renderJob=()=>{},evalMarker=()=>"",fitTitle=()=>"";
  const shown=()=>JOBS,appsByJob=()=>new Map(),isNewArrival=()=>false;
  const esc=value=>String(value||""),displayTitle=value=>value;
  const draftCell=()=>"",applicationFor=()=>null,postedLabel=()=>"10-02";
  const foundTitle=()=>"Found today",foundAt=()=>"2026-10-02",statusChoices=()=>["new"];
  const facetsAreDefault=()=>true,document={querySelector:()=>null};
  ${labels}
  ${table}
  render();return {html:elements.tb.innerHTML,freehire:sourceLabel("freehire")};
`)(jobs);
const cells=[...render.html.matchAll(/<td class="co sourcecell" title="([^"]*)">([^<]*)<\/td>/g)];
assert.deepEqual(cells.map(cell=>cell[2]),["freehire","LinkedIn search","LinkedIn browser"]);
assert(cells[0][1].startsWith("freehire"));
assert(cells[1][1].includes("Your companies"));
assert.equal(render.freehire,"freehire");
"""


class SourceColumnTest(unittest.TestCase):
    def test_rows_show_distinct_acquisition_sources_with_changed_company_urls(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        result = subprocess.run([node, "-e", PROBE, str(APP)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
