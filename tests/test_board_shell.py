"""M2.5 static-shell contract.

The browser is intentionally dependency-free.  These tests keep the approved
panel geometry and its interaction hooks from silently regressing while the
existing HTTP tests cover the API wiring.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from board import state  # noqa: E402


class ShellMarkupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        static = ROOT / "tools" / "board" / "static"
        cls.html = (static / "index.html").read_text(encoding="utf-8")
        cls.css = (static / "app.css").read_text(encoding="utf-8")
        cls.js = (static / "app.js").read_text(encoding="utf-8")

    def test_workspace_has_the_four_approved_panels(self):
        """Applications merged into Runs: one ledger, in the rail, not two."""
        for marker in ('id="collect-panel"', 'id="runs"', 'id="board-panel"',
                       'id="job-panel"'):
            self.assertIn(marker, self.html)
        for gone in ('id="applications-panel"', 'id="applicationlist"',
                     'id="applicationcount"', 'apptable'):
            self.assertNotIn(gone, self.html)

    def test_all_three_painted_gaps_are_accessible_splitters(self):
        for marker in ('id="left-split"', 'id="right-split"',
                       'id="left-row-split"'):
            self.assertIn(marker, self.html)
        self.assertNotIn('id="centre-row-split"', self.html)
        self.assertEqual(self.html.count('role="separator"'), 3)
        self.assertEqual(self.html.count('tabindex="0"'), 3)
        self.assertIn("aria-valuenow", self.js)
        self.assertIn("setPointerCapture", self.js)

    def test_the_runs_ledger_navigates_only_by_link_and_has_no_expanded_form(self):
        """A row is a card, not a hit target: only its links open a run, and a
        run's link goes straight to its Tailor view - there is no ledger page
        in between."""
        self.assertNotIn("data-expand=\"runs\"", self.html)
        self.assertNotIn("expanded-runs", self.css)
        self.assertNotIn("expanded-runs", self.js)
        self.assertNotIn("cursor:pointer", self.css.split(".runitem{")[1].split("}")[0])
        runlist = self.js.split("function renderRuns(){")[1].split("async function pollRuns")[0]
        # The link has to look like one: accent chip plus an arrow glyph.
        self.assertIn(".runopen{", self.css)
        for marker in ('data-run="${esc(r.id)}"', "Watch run", "Open run",
                       'class="runarrow">↗', 'class="linkish runopen"',
                       'data-preview="${esc(r.id)}"', 'data-revise="${esc(r.id)}"',
                       '"attempt "+r.attempt', "drafted"):
            self.assertIn(marker, runlist)
        self.assertNotIn('<div class="runitem ${running?"live":""} ${ACTIVE_RUN===r.id?"selected":""}" data-run=', runlist)
        self.assertNotIn("function renderApplications", self.js)

    def test_theme_is_a_named_three_state_button_outside_the_layout_blob(self):
        self.assertIn('id="theme-toggle"', self.html)
        self.assertIn('id="theme-name"', self.html)
        self.assertIn(".theme-toggle{", self.css)
        self.assertIn("cycleTheme", self.js)
        for mode in ('mode:"system"', 'mode:"light"', 'mode:"dark"'):
            self.assertIn(mode, self.js)
        # Its own key: resetting the layout must not flip the user's theme.
        self.assertIn("jobflow.theme.v1", self.js)
        self.assertNotIn("theme", self.js.split("DEFAULT_LAYOUT=")[1].split("}")[0])

    def test_layout_is_versioned_persistent_and_keeps_auto_collapse_separate(self):
        self.assertIn('jobflow.layout.v1', self.js)
        self.assertIn("version:1", self.js)
        self.assertIn("leftCollapsed:false", self.js)
        self.assertIn("rightCollapsed:false", self.js)
        self.assertIn("autoLeft:false", self.js)
        self.assertIn("autoRight:false", self.js)
        self.assertIn("shortcutsHidden:false", self.js)
        self.assertIn("toggleShortcuts", self.js)
        self.assertIn('event.key==="["', self.js)
        self.assertIn('event.key==="]"', self.js)
        self.assertIn('event.key==="\\\\"', self.js)
        # Geometry only.  Which view is open moved into the URL, because the
        # blob is per-browser and could not answer a reload or the back button.
        self.assertNotIn("expanded", self.js.split("DEFAULT_LAYOUT=")[1].split("}")[0])

    def test_every_parked_view_is_addressable_and_the_url_drives_boot(self):
        """A view that does not write a route is a view a reload cannot restore."""
        for marker in ("function setRoute", "function parseRoute",
                       "function dispatchRoute", "function applyRoute",
                       '"hashchange"', "history.replaceState"):
            self.assertIn(marker, self.js)
        # Each parked artboard writes its own address...
        for route in ('"/run/"+encodeURIComponent(run.id)',
                      '"/job/"+encodeURIComponent(j.url)',
                      '+"/preview"', '+"/revise"', '"/companies"+'):
            self.assertIn(route, self.js)
        # ...and every one of them is reachable coming back the other way.
        for head in ('head==="board"', 'head==="job"',
                     'head==="companies"', 'head==="run"',
                     'second==="preview"', 'second==="revise"'):
            self.assertIn(head, self.js)
        # Boot is one function of the URL, not a ladder over saved state.
        self.assertIn("applyRoute();", self.js.split("Promise.all(")[1])
        # Paging inside a view rewrites its entry instead of stacking another.
        self.assertIn('IN_PLACE=["job","companies"]', self.js)

    def test_workspace_and_tailor_artboards_have_runtime_hooks(self):
        self.assertIn('data-expand="board"', self.html)
        self.assertIn("expanded-board", self.css)
        self.assertIn("tailor-shell", self.css)
        self.assertIn("Tailoring choices", self.js)
        self.assertIn("runpill", self.html)
        for marker in ("Regenerate", "continuerun", "quota_exhausted", "failure-card",
                       "data-gen-base", "latestApplications", "progressPanel",
                       "retry_of"):
            self.assertIn(marker, self.js if marker != "failure-card" else self.css)
        # The job identity lives in the body, in sentence case - never in the
        # app bar's uppercase slot, and never twice.
        self.assertIn(".runtitle h1{", self.css)
        self.assertIn("const runTitle=(run,action=\"\")=>", self.js)
        for view in ('openView("tailor",[crumbRun(run)]',
                     'openView("preview",[crumbRun(run),{label:"Preview"}]',
                     'openView("revise",[crumbRun(run),{label:"Revise"}]'):
            self.assertIn(view, self.js)
        self.assertNotIn("Back to board", self.js)
        self.assertIn("overflow-wrap:anywhere", self.css)
        self.assertIn(".writing>div:not(.label)", self.css)

    def test_the_app_bar_separates_navigation_from_state(self):
        """Left of the hairline you move; right of it you read the machine.

        The bar used to mix all three: a dead `JobFlow` wordmark, a back arrow
        that only existed inside a parked view, a slot printing the raw view
        kind, and `Target companies` filed next to the theme control as though a
        destination and a setting were the same kind of thing.
        """
        # Identity is the way home.
        self.assertIn('<button class="brand" data-nav="board"', self.html)
        self.assertIn(".brand{", self.css)
        self.assertIn("font-size:15px;font-weight:700", self.css)
        # Three tabs (owner sign-off 2026-09-27, DESIGN.md §14): a run you are
        # inside lights Runs, not Board.
        self.assertIn('<nav class="nav" aria-label="Primary">', self.html)
        for tab in ('<button class="navitem" data-nav="board">Board</button>',
                    '<button class="navitem" data-nav="runs">Runs</button>',
                    '<button class="navitem" data-nav="companies">Companies</button>'):
            self.assertIn(tab, self.html)
        self.assertIn('["tailor","preview","revise"].includes(VIEW)?"runs":"board"', self.js)
        # Underlined, not filled - a top-level tab must not read as a chip.
        self.assertIn(".navitem.on{color:var(--text);font-weight:600;box-shadow:inset 0 -2px 0 var(--selbar)}", self.css)
        self.assertIn('node.setAttribute("aria-current","page")', self.js)
        # The modal back arrow and the raw-view-kind slot are both gone; one
        # nav handler now serves the wordmark, the tabs and the Board crumb.
        for gone in ('id="restore"', 'class="back"', 'id="local"', 'id="companies-open"'):
            self.assertNotIn(gone, self.html)
        self.assertIn('const nav=event.target.closest("[data-nav]");', self.js)

    def test_the_crumb_reports_location_and_only_links_upwards(self):
        """Every segment but the last is a link, and none of them is a company."""
        self.assertIn('<div class="crumb" id="crumb" hidden></div>', self.html)
        self.assertIn("function renderCrumb(", self.js)
        self.assertIn("const last=index===parts.length-1", self.js)
        self.assertIn('`<span class="crumbseg here"', self.js)
        # The trail starts below the lit tab: a leading "Board" segment would
        # be the same word twice in a row, 40px apart.
        self.assertNotIn("CRUMB_BOARD", self.js)
        # The run segment goes to the run's own Tailor view, reusing the
        # existing data-run handler - there is no company page behind it.
        self.assertIn('const crumbRun=run=>({label:run.company,sub:run.role,run:run.id});', self.js)
        self.assertIn('seg.run?`data-run="${esc(seg.run)}"`', self.js)
        # Position in the reader queue is a count, not another layer.
        self.assertIn('count:`${sel+1} of ${rows.length}`', self.js)
        self.assertIn('`<span class="count">${esc(seg.count)}</span>`', self.js)
        # A long role truncates instead of pushing the run pill off the bar.
        self.assertIn("text-overflow:ellipsis", self.css.split(".crumbsub{")[1].split("}")[0])
        # The right-hand hairline appears only when there is state to fence off.
        self.assertIn('el("right-sep").hidden=!active;', self.js)

    def test_job_reader_and_compiled_pdf_preview_are_real_views(self):
        for marker in ("reader-shell", "reader-queue", "reader-decide",
                       "preview-shell", "screen-rail", "check-rail", "pdf-stage"):
            self.assertIn(marker, self.css)
        for marker in ("renderReader", "renderPreview", "data-reader-row",
                       "data-preview-filter", "data-recompile",
                       "/api/pdf/", "data-reveal", "data-mark", "regen-form"):
            self.assertIn(marker, self.js)
        # The frames are built per document kind now, so the titles are in the
        # branch rather than in two literal tags.
        self.assertIn('"Compiled CV"', self.js)
        self.assertIn('"Compiled cover letter"', self.js)
        self.assertIn('class="pdf-frame ${kind}"', self.js)

    def test_a_run_can_be_asked_for_one_document_instead_of_both(self):
        """Some postings are worth a letter and not a fresh CV. The choice is
        made once, before Generate: there is no fit evaluation to wait for."""
        for marker in ("data-gen-scope", "scopeOptions", "DRAFT_LABEL", "docKinds",
                       "Cover letter only", "data-gen-country"):
            self.assertIn(marker, self.js)
        for removed in ("data-scope-start", "TAILOR_LABEL", "const START={base:",
                        "Choose after fit evaluation.", ">Evaluate fit</button>",
                        'closest(".approve")', "Start fit evaluation"):
            self.assertNotIn(removed, self.js)
        self.assertIn('>Generate</button>', self.js)
        self.assertIn('{job_url:url,kind:"apply",note,scope,base_cv,cv_country}', self.js)

    def test_run_output_can_follow_the_latest_line(self):
        self.assertIn('let RUN_FOLLOW=true;', self.js)
        self.assertIn('data-run-follow ${RUN_FOLLOW?"checked":""}', self.js)
        self.assertIn('if(RUN_FOLLOW)runlog.scrollTop=runlog.scrollHeight;', self.js)
        self.assertIn('RUN_LOG_SCROLL.set(ACTIVE_RUN,previousLog.scrollTop)', self.js)

    def test_revise_and_companies_artboards_are_on_the_runtime_surface(self):
        self.assertIn('data-nav="companies"', self.html)
        for marker in ("revise-shell", "grid-template-columns:268px", "428px",
                       "companies-shell", ".company-row{", ".company-editor{"):
            self.assertIn(marker, self.css)
        self.assertNotIn("company-table", self.css)
        # COMPANIES_PLAN §4: one field to add, groups by what the owner has to do.
        for marker in ("renderRevise", "/api/prefs", "data-restore-version",
                       "renderCompanies", "/api/companies", ">${COMPANY_BUSY===\"__add__\"?\"Looking…\":\"Watch\"}<",
                       "company-careers", "data-company-confirm", "company-settings",
                       "data-company-check", "data-company-look-now", "data-company-board",
                       '["needs_you","Needs you"]', '["finding","Finding"]', '["watching","Watching"]'):
            self.assertIn(marker, self.js)
        for removed in ("Check monitoring health", "Save & inspect", "Adapter missing",
                        "<th>monitoring_status</th>", "will_be_searched</span>",
                        'id="company-name"', "Last updated", "data-company-filter"):
            self.assertNotIn(removed, self.js)

    def test_source_column_custom_text_modal_and_no_price_chrome(self):
        self.assertIn('<th class="sourcecol">Source</th>', self.html)
        self.assertIn('id="text-modal"', self.html)
        self.assertIn('id="text-modal-input"', self.html)
        self.assertIn('id="shortcut-toggle"', self.html)
        self.assertIn('Show shortcuts', self.js)
        self.assertIn('Hide shortcuts', self.js)
        self.assertIn("LinkedIn browser", self.js)
        self.assertIn("openTextModal", self.js)
        self.assertIn("background:var(--panel,#fff)", self.css)
        self.assertIn("background:rgba(10,11,14,.72)", self.css)
        self.assertLess(self.html.index('id="text-modal"'), self.html.index('id="toast"'))
        self.assertNotIn("prompt(", self.js)
        self.assertNotIn("money(", self.js)
        self.assertNotIn("<th>Cost</th>", self.html)

    def test_the_board_filters_and_sorts_on_more_than_status(self):
        """The status chips answer one question; these are the other three."""
        for marker in ('id="filterbar"', 'id="sourcechips"', 'id="f-fit"',
                       'id="f-found"', 'id="f-sort"', 'id="f-reset"'):
            self.assertIn(marker, self.html)
        # Arrival is a column of its own, after Posted: the employer's date and
        # the date it reached the board are different facts, and only the second
        # one can tell you what the last fetch brought in.
        self.assertIn('<th class="foundcol">Found</th>', self.html)
        self.assertLess(self.html.index('class="postedcol"'),
                        self.html.index('class="foundcol"'))
        # The fetch summary offers one click through to the rows it just added.
        self.assertIn('id="show-new"', self.html)
        # A standing preference, kept out of the layout blob and out of the URL.
        self.assertIn('jobflow.board.v1', self.js)
        self.assertNotIn('jobflow.board.v1', self.js.split("const LAYOUT_KEY")[1])
        for rule in (".filterbar{", ".source-chip{", ".newdot{", ".foundcol{"):
            self.assertIn(rule, self.css)

    def test_approved_tokens_and_hard_centre_minimum_are_present(self):
        for token in ("--bg:", "--panel:", "--line:", "--text:", "--dim:",
                      "--sel:", "--selbar:", "--high:", "--medium:", "--low:",
                      "--chip:", "--warn:", "--banner:"):
            self.assertIn(token, self.css)
        self.assertIn("minmax(480px,1fr)", self.css)


class PostingAndScoreMarkupTest(unittest.TestCase):
    """The Job panel reads a stored posting, and says which score it sorted on."""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "tools" / "board" / "static" / "app.js").read_text(encoding="utf-8")

    def test_the_body_is_fetched_lazily_from_the_single_row_route(self):
        for marker in ("POSTING_CACHE", "loadPosting", "/api/job?t=", "postingbody"):
            self.assertIn(marker, self.js)

    def test_a_late_response_cannot_land_in_the_wrong_row(self):
        """Arrow-key walking the list must not paint row N-1's posting into row N."""
        self.assertIn("selectedJob()?.url===j.url", self.js)

    def test_the_score_badge_names_its_own_provenance(self):
        for marker in ("SCORE_LABEL", "scoreTitle", "score_source"):
            self.assertIn(marker, self.js)
        self.assertNotIn('badge">prefit ${esc(j.score)}', self.js,
                         "every score was being labelled a prefit, including /rank's")

    def test_title_only_rows_say_so(self):
        self.assertIn('fit_evidence==="title-only"', self.js)

    def test_the_language_gate_is_read_not_guessed_in_the_browser(self):
        """A loose inline regex over an excerpt is not a language gate.

        collectors.GERMAN_RE distinguishes German stated as a job condition from
        German mentioned in passing, and it runs over the whole posting. The
        browser sees only an excerpt, so re-deriving the verdict there could only
        ever be wrong - and it reported "no blocking requirement detected".
        """
        self.assertIn("gateLines", self.js)
        code = "\n".join(line for line in self.js.splitlines()
                         if not line.lstrip().startswith("//"))
        self.assertNotIn("german required|deutsch", code)


class JobPanelPayloadTest(unittest.TestCase):
    """The list payload carries an excerpt; the single-row route carries the body.

    Bodies live in sidecars because `/api/jobs` ships every row on every reload.
    Putting a few kilobytes of posting on each row would be megabytes on the
    wire and megabytes rewritten on every status click, which is the whole
    reason the store exists.
    """

    URL = "https://example.test/jobs/1"
    BODY = ("Build reliable systems. Requirements: Python, FastAPI and a habit "
            "of writing the test before the fix. We offer unhurried review.")

    def entry(self, **over):
        base = {
            "url": self.URL,
            "title": "Engineer",
            "company": "Example",
            "location": "Zurich",
            "posted": "2026-08-19",
            "portal": "linkedin-browser",
            "primary_source": "linkedin-browser",
            "sources": [{"portal": "linkedin-browser"}, {"portal": "linkedin-search"}],
        }
        base.update(state.postings.describe(self.URL, self.BODY))
        base.update(over)
        return base

    def test_the_list_payload_carries_the_excerpt_not_the_body(self):
        entry = self.entry()
        with mock.patch.object(state, "load", return_value={self.URL: entry}), \
             mock.patch.object(state.jobs_md, "priority_score", return_value=0):
            payload = state.jobs_payload()
        row = payload["jobs"][0]
        self.assertTrue(row["description"])
        self.assertLessEqual(len(row["description"]), state.postings.EXCERPT_CHARS + 1)
        self.assertTrue(row["has_posting"])
        self.assertEqual(row["posting_chars"], len(self.BODY))
        self.assertEqual(row["primary_source"], "linkedin-browser")
        self.assertEqual(row["sources"], ["linkedin-browser", "linkedin-search"])

    def test_the_single_row_route_serves_the_full_body(self):
        entry = self.entry()
        with tempfile.TemporaryDirectory() as tmp:
            saved = state.postings.jobs_md.SEEN
            state.postings.jobs_md.SEEN = Path(tmp) / "seen_jobs.json"
            try:
                state.postings.commit(self.URL, self.BODY)
                with mock.patch.object(state, "load", return_value={self.URL: entry}), \
                     mock.patch.object(state.jobs_md, "priority_score", return_value=0):
                    row = state.job_payload(self.URL)
            finally:
                state.postings.jobs_md.SEEN = saved
        self.assertEqual(row["description"], self.BODY)

    def test_a_row_with_no_stored_body_says_so_rather_than_failing(self):
        entry = self.entry()
        for field in list(state.postings.ENTRY_FIELDS):
            entry.pop(field, None)
        with mock.patch.object(state, "load", return_value={self.URL: entry}), \
             mock.patch.object(state.jobs_md, "priority_score", return_value=0):
            row = state.jobs_payload()["jobs"][0]
        self.assertEqual(row["description"], "")
        self.assertFalse(row["has_posting"])

    def test_an_unknown_url_is_not_an_error(self):
        with mock.patch.object(state, "load", return_value={}):
            self.assertIsNone(state.job_payload("https://example.test/nope"))

    def test_the_payload_names_which_score_the_row_is_sorted_on(self):
        """Otherwise the page calls a rank score a 'prefit' and misleads you."""
        cases = [({"rank_score": 80}, "rank"),
                 ({"fit_priority_score": 70}, "fit"),
                 ({"prefit_score": 60}, "prefit"),
                 ({}, "band")]
        for extra, expected in cases:
            entry = self.entry(**extra)
            with mock.patch.object(state, "load", return_value={self.URL: entry}), \
                 mock.patch.object(state.jobs_md, "priority_score", return_value=0):
                row = state.jobs_payload()["jobs"][0]
            self.assertEqual(row["score_source"], expected, extra)

    def test_the_reasons_shown_are_the_ones_behind_the_sorted_number(self):
        entry = self.entry(fit_reasons=["title matches \"ai engineer\""],
                           prefit_reasons=["older prior"])
        with mock.patch.object(state, "load", return_value={self.URL: entry}), \
             mock.patch.object(state.jobs_md, "priority_score", return_value=0):
            row = state.jobs_payload()["jobs"][0]
        self.assertIn("ai engineer", row["why"])
        self.assertNotIn("PREFIT", row["why"])

    def test_a_screen_note_and_the_score_reasons_both_survive(self):
        """They answer different questions, so neither may hide the other."""
        entry = self.entry(note="AUTO-SCREEN: German stated as a job condition",
                           fit_reasons=["German stated as a job condition",
                                        'title matches "ai engineer"'])
        with mock.patch.object(state, "load", return_value={self.URL: entry}), \
             mock.patch.object(state.jobs_md, "priority_score", return_value=0):
            row = state.jobs_payload()["jobs"][0]
        self.assertIn("AUTO-SCREEN", row["why"])
        self.assertIn("ai engineer", row["why"])

    def test_a_bland_note_no_longer_hides_the_score_reasons(self):
        entry = self.entry(note="AUTO-SCREEN: not screened - the run's detail budget was spent",
                           fit_reasons=['title matches "ai engineer"'])
        with mock.patch.object(state, "load", return_value={self.URL: entry}), \
             mock.patch.object(state.jobs_md, "priority_score", return_value=0):
            row = state.jobs_payload()["jobs"][0]
        self.assertIn("ai engineer", row["why"])

    def test_prefit_reasons_only_appear_when_there_is_no_computed_score(self):
        entry = self.entry(prefit_reasons=["older prior"])
        with mock.patch.object(state, "load", return_value={self.URL: entry}), \
             mock.patch.object(state.jobs_md, "priority_score", return_value=0):
            row = state.jobs_payload()["jobs"][0]
        self.assertIn("PREFIT", row["why"])


# The grep tests above pin that every view writes *a* route.  They cannot tell a
# push from a replace, nor catch a router that re-enters itself.  This harness
# slices the real routing block out of app.js and runs it against stand-ins for
# the handful of browser objects it touches, so the algebra is checked rather
# than the spelling.  Node is optional: without it these skip.
ROUTE_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("const IN_PLACE="),marker="applyRoute()});";
const end=src.indexOf(marker)+marker.length;
if(start<0||end<marker.length)throw new Error("routing block not found in app.js");

const build=new Function("ctx","location","history","addEventListener",`
  let JOBS=ctx.JOBS,RUNS=ctx.RUNS;
  let filter="active",q="",sel=0,COMPANY_SELECTED=null,facetsReset=0;
  const resetFacets=()=>{facetsReset++};
  const shown=()=>JOBS.filter(job=>filter==="all"||job.status==="new");
  const el=()=>({value:"seeded"});
  const render=()=>{};
  const restoreWorkspace=()=>{ctx.opened.push("workspace");setRoute("/")};
  const openView=(kind,title,route)=>{ctx.opened.push(kind);setRoute(route||"/"+kind)};
  const renderReader=()=>openView("reader",null,"/job/"+encodeURIComponent(shown()[sel].url));
  const renderCompanies=()=>openView("companies","","/companies"+(COMPANY_SELECTED?"?c="+encodeURIComponent(COMPANY_SELECTED):""));
  const renderTailor=run=>openView("tailor",null,"/run/"+encodeURIComponent(run.id));
  const renderPreview=run=>openView("preview",null,"/run/"+encodeURIComponent(run.id)+"/preview");
  const renderRevise=run=>openView("revise",null,"/run/"+encodeURIComponent(run.id)+"/revise");
  ${src.slice(start,end)}
  return {applyRoute,renderReader,renderCompanies,renderTailor,renderPreview,
          state:()=>({filter,sel,COMPANY_SELECTED,facetsReset}),
          set:(k,v)=>{if(k==="filter")filter=v;if(k==="sel")sel=v;if(k==="COMPANY_SELECTED")COMPANY_SELECTED=v}};
`);

let stack,log,fire=null;
const location={get hash(){return stack[stack.length-1]},set hash(v){stack.push(v);log.push("push "+v);fire&&fire()}};
const history={replaceState:(_s,_t,v)=>{stack[stack.length-1]=v;log.push("replace "+v)}};
const addEventListener=(name,fn)=>{if(name==="hashchange")fire=fn};
const ctx={JOBS:[{url:"https://ex.com/a?id=1&x=2",status:"new"},{url:"https://ex.com/b",status:"no"}],
           RUNS:[{id:"run-7"}],opened:[]};
let R;
const at=hash=>{stack=[hash];log=[];ctx.opened=[];R=build(ctx,location,history,addEventListener)};
let bad=0;
const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL  "+name)}};

// A posting URL carrying its own query string survives encode -> hash -> decode.
at(""); R.renderReader();
const jobRoute="#/job/"+encodeURIComponent(ctx.JOBS[0].url);
t("reader writes an encoded job route", location.hash===jobRoute);
at(jobRoute); R.applyRoute();
t("that route comes back to the reader", ctx.opened[0]==="reader"&&R.state().sel===0);

// Paging within a view rewrites its entry; changing view stacks a new one.
at(jobRoute); R.set("filter","all"); R.set("sel",1); R.renderReader();
t("j/k in the reader replaces, never stacks", log.length===1&&log[0].startsWith("replace")&&stack.length===1);
at("#/companies"); R.set("COMPANY_SELECTED","Acme & Co"); R.renderCompanies();
t("opening a company's editor replaces", log.length===1&&log[0].startsWith("replace"));
const companyRoute=location.hash;
at("#/board"); R.renderTailor(ctx.RUNS[0]);
t("board -> tailor pushes", log[0]==="push #/run/run-7");
at("#/run/run-7"); R.renderPreview(ctx.RUNS[0]);
t("tailor -> preview pushes, so back returns to the pipeline", log[0]==="push #/run/run-7/preview");

// A name needing escaping round-trips through the query string.
at(companyRoute); R.applyRoute();
t("the open company comes back",
  R.state().COMPANY_SELECTED==="Acme & Co"&&ctx.opened[0]==="companies");

// Rendering *from* a route must not write back, or the router feeds itself.
at("#/run/run-7"); R.applyRoute();
t("applying a route writes nothing back", log.length===0&&ctx.opened[0]==="tailor");
at("#/board"); R.renderTailor(ctx.RUNS[0]);
t("our own write does not re-enter the router", ctx.opened.filter(v=>v==="tailor").length===1);
at("#/board"); location.hash="#/companies";
t("a hash typed by hand does re-render", ctx.opened.includes("companies"));

// A route naming something that is gone lands on the workspace, and does not
// leave the broken address sitting in the history.
[["a pruned run","#/run/deleted"],
 ["a posting that dropped off the board","#/job/"+encodeURIComponent("https://gone.example/x")],
 ["an unknown view","#/nope/nope"]].forEach(([name,hash])=>{
  at(hash); R.applyRoute();
  t(name+" falls back to the workspace",
    ctx.opened.includes("workspace")&&location.hash==="#/"&&stack.length===1);
});

// A deep link outranks whichever chip the board was left on - and the source,
// fit and found facets too, which can hide a row just as thoroughly.
at("#/job/"+encodeURIComponent("https://ex.com/b")); R.applyRoute();
t("deep link widens a filter that would hide the row",
  R.state().filter==="all"&&ctx.opened[0]==="reader");
t("deep link clears the facets as well", R.state().facetsReset===1);

process.exit(bad?1:0);
"""


class RouteBehaviourTest(unittest.TestCase):
    """The URL is the single source of truth for which view is on screen."""

    def test_the_router_round_trips_stacks_and_recovers(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; the routing algebra is unchecked")
        app = ROOT / "tools" / "board" / "static" / "app.js"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.js"
            probe.write_text(ROUTE_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(app)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).strip())


RUN_OUTPUT_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("const STEPS=["),end=src.indexOf("function renderReader(",start);
if(start<0||end<start)throw new Error("renderTailor was not found in app.js");

const ctx={nodes:{},html:""};
const tailor={};
Object.defineProperty(tailor,"innerHTML",{set(value){
  ctx.html=value;
  ctx.nodes.runlog={scrollTop:0,scrollHeight:900};
},get(){return ctx.html}});
ctx.nodes["tailor-view"]=tailor;

const build=new Function("ctx",`
  const el=id=>ctx.nodes[id]||(ctx.nodes[id]={innerHTML:"",scrollTop:0,scrollHeight:0});
  const esc=value=>String(value==null?"":value);
  const PHASE_STEP={evaluating:1,awaiting_approval:1,queued:1,preparing:1,drafting:2,reviewing:2,revising:2,compiling:3,inspecting:3,publishing:3,done:3};
  const STAGE_COUNT=3;
  const RUNNING=["evaluating","queued","preparing","drafting","reviewing","revising","compiling","inspecting","publishing"];
  const DOC_TITLE={cv:"CV",cover:"cover letter"};
  const docKinds=scope=>(scope||"both")==="both"?["cv","cover"]:[scope];
  const runTitle=()=>"<header></header>",crumbRun=()=>({}),openView=()=>{},restoreWorkspace=()=>{};
  let RUNS=ctx.runs||[],EV=[],ACTIVE_RUN=null,RUN_FOLLOW=true;
  const RUN_LOG_SCROLL=new Map();
  ${src.slice(start,end)}
  return {renderTailor,setFollow:value=>RUN_FOLLOW=value,active:()=>ACTIVE_RUN,setRuns:v=>RUNS=v};
`);
const R=build(ctx);
const run={id:"run-1",application_id:"run-1",kind:"apply",pipeline:2,phase:"drafting",scope:"cover",
  company:"Acme",role:"Engineer",targets:{cv:"cv/secret.tex",cover:"cover/secret.tex"}};
let bad=0;
const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL  "+name)}};

R.renderTailor(run);
t("the chosen scope shows its target",ctx.html.includes("cover/secret.tex"));
t("an unselected target stays hidden",!ctx.html.includes("cv/secret.tex"));
t("three owner-facing stages",ctx.html.includes("stage 2 of 3"));
t("no fit evaluation card on a staged run",!ctx.html.includes("fit evaluation"));
t("a running run offers Cancel, not Continue",ctx.html.includes("cancelrun")&&!ctx.html.includes("continuerun"));
t("follow is checked by default",ctx.html.includes("data-run-follow checked"));
t("the newest line is visible",ctx.nodes.runlog.scrollTop===ctx.nodes.runlog.scrollHeight);

ctx.nodes.runlog.scrollTop=123;
R.setFollow(false);
R.renderTailor(run);
t("turning follow off preserves the reading position",ctx.nodes.runlog.scrollTop===123);
t("the unchecked state survives a repaint",ctx.html.includes("data-run-follow >"));

R.setFollow(true);
const failed={...run,phase:"failed",failure_code:"quota_exhausted",error:"session limit",failed_phase:"reviewing",
  progress:{docs:{cover:{state:"draft saved",pdf:null}},checks:{},pending:["review","build","mechanical","publish"],issues:[],conflicts:[]}};
R.renderTailor(failed);
t("a stopped run offers Continue and Regenerate",ctx.html.includes("continuerun")&&ctx.html.includes("retryrun"));
t("the card says what was saved and what remains",ctx.html.includes("draft saved")&&ctx.html.includes("Remaining: review"));
t("an exhausted allowance is not called transient",ctx.html.includes("does not reset the allowance"));
R.renderTailor({...failed,failure_code:"hard_conflict"});
t("a hard conflict offers an explicit override",ctx.html.includes("Continue anyway")&&ctx.html.includes('data-proceed="1"'));
R.setRuns([{id:"run-2",continue_of:"run-1",attempt:2}]);
R.renderTailor(failed);
t("an attempt already continued links forward instead of offering Continue again",
  !ctx.html.includes("continuerun")&&ctx.html.includes('data-run="run-2"'));
t("every attempt is one tab in a single switcher",
  (ctx.html.match(/class="attempt /g)||[]).length===2&&ctx.html.includes('aria-selected="true"'));
t("an earlier attempt points to the latest one",ctx.html.includes("Go to the latest (#2)"));
R.renderTailor({...failed,id:"run-2",continue_of:"run-1",attempt:2});
t("the latest attempt shows no redirect note",!ctx.html.includes("Go to the latest"));
R.setRuns([]);
R.renderTailor({...run,pipeline:undefined,phase:"awaiting_approval",fit:{overall:78,verdict:"good"}});
t("a legacy gate is continued, not re-evaluated",ctx.html.includes("continuerun")&&!ctx.html.includes("retryrun"));
t("its earlier evaluation stays readable",ctx.html.includes("Earlier fit evaluation"));
process.exit(bad?1:0);
"""


class RunOutputBehaviourTest(unittest.TestCase):
    def test_follow_and_staged_run_rendering(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; run output behaviour is unchecked")
        app = ROOT / "tools" / "board" / "static" / "app.js"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "run-output.js"
            probe.write_text(RUN_OUTPUT_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(app)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).strip())


COMPANY_ROW_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("function selectCompanyRow("),end=src.indexOf('document.addEventListener("click"',start);
if(start<0||end<start)throw new Error("selectCompanyRow was not found in app.js");
const ctx={calls:[]};
const build=new Function("ctx",`
  let COMPANY_SELECTED=null;
  const renderCompanies=(...args)=>ctx.calls.push(args);
  ${src.slice(start,end)}
  return {selectCompanyRow,selected:()=>COMPANY_SELECTED};
`);
const R=build(ctx),table={scrollTop:847};
const row={dataset:{companyRow:"Company Near The Bottom"},closest:selector=>selector===".companies-scroll"?table:null};
R.selectCompanyRow(row);
let bad=0;
const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL  "+name)}};
t("the clicked company remains selected",R.selected()==="Company Near The Bottom");
t("selection renders without reloading",ctx.calls.length===1&&ctx.calls[0][0]===false);
t("the old scroll position is carried into the render",ctx.calls[0][1]===847);
process.exit(bad?1:0);
"""


class CompanyRowClickTest(unittest.TestCase):
    """Selecting a lower row must not throw the table back to its first row."""

    def test_click_preserves_scroll_and_the_exact_selected_company(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; company row clicks are unchecked")
        app = ROOT / "tools" / "board" / "static" / "app.js"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "company-row.js"
            probe.write_text(COMPANY_ROW_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(app)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).strip())


COMPANIES_HARNESS = r"""
const fs=require("fs"),src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("const WATCH_GROUPS="),end=src.indexOf("// The app bar",start);
if(start<0||end<start)throw new Error("the companies block was not found in app.js");
(async()=>{
  const recent=new Date(Date.now()-2*3600e3).toISOString();
  const ctx={nodes:{},scroll:{scrollTop:0},companies:{schema_version:2,company_controls:true,mtime:"1",companies:[
    {name:"Ready Co",route:"ats",status:"verified",vendor:"greenhouse",watch_state:"watching",will_be_searched:true,fetch_status:"never",board_url:"https://jobs.example.com"},
    {name:"Failed Co",status:"verified",vendor:"ashby",watch_state:"watching",will_be_searched:true,fetch_status:"failed",last_success_at:recent},
    {name:"Fine Co",status:"verified",vendor:"workday",watch_state:"watching",will_be_searched:true,fetch_status:"success",last_success_at:recent},
    {name:"Seeking Co",status:"unresolved",watch_state:"finding",resolve_attempts:2,next_resolve_at:"2026-10-01",domain:"seeking.test"},
    {name:"Pick Co",status:"ambiguous",watch_state:"needs_you",candidates:[{vendor:"personio",token:"pick",board_url:"https://pick.jobs.personio.de"},{vendor:"dead",token:"gone",note:"not_found"}]},
    {name:"Lost Co",status:"unresolved",watch_state:"needs_you",resolve_attempts:4,next_resolve_at:null},
    {name:"LinkedIn Co",route:"linkedin",status:"unresolved",watch_state:"not_watched"},
    {name:"Manual Co",route:"manual",status:"paused",watch_state:"paused"}
  ]}};
  const R=new Function("ctx",`
    const el=id=>ctx.nodes[id]||(ctx.nodes[id]={innerHTML:"",value:"",querySelector:()=>ctx.scroll});
    const esc=value=>String(value??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
    const openView=(...a)=>ctx.opened=a,toast=()=>{},restoreWorkspace=()=>ctx.home=true,render=()=>{};
    let JOBS=[{company:"Fine Co"},{company:"fine co"},{company:"Other"}],filter="active",q="",sel=4;
    let COMPANIES=ctx.companies,COMPANY_SELECTED=null,T="token",COMPANY_BUSY=null,COMPANY_ERROR="",COMPANY_QUERY="",COMPANY_ADD_DRAFT={name:"",url:""};
    ${src.slice(start,end)}
    return {renderCompanies,companyLink,showCompanyOnBoard,select:name=>COMPANY_SELECTED=name,board:()=>({filter,q,sel})};
  `)(ctx);
  await R.renderCompanies(false,612);
  let html=ctx.nodes["tailor-view"].innerHTML,bad=0;
  const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL "+name)}};
  const order=["Needs you","Finding","Watching"].map(label=>html.indexOf(label));
  t("the groups run from what needs you to what is handled",order.every((at,i)=>at>0&&(i===0||at>order[i-1])));
  t("reference and paused companies are folded but counted",html.includes("Not watched <span")&&!html.includes("LinkedIn Co")&&!html.includes("Manual Co"));
  t("a watched company links to its jobs on the Board",html.includes("2 on Board →"));
  t("never checked is not called checked",html.includes("first check on the next fetch"));
  t("a failed check says what happens next",html.includes("last check failed · retries next fetch"));
  t("a healthy check says when",html.includes("checked 2 h ago"));
  t("a company still being looked for says when the next try is",html.includes("tried 2× · next try 1 Oct"));
  t("an ambiguous board is one click to confirm",html.includes('data-company-confirm="Pick Co|personio|pick"'));
  t("an ambiguous row can be looked at again or answered with none of these",html.includes('data-company-relook="Pick Co"')&&html.includes(">None of these<"));
  t("a dead candidate is shown but never offered",html.includes("dead · gone — no longer active")&&!html.includes('data-company-confirm="Pick Co|greenhouse|gone"'));
  t("a company the retries gave up on asks for its link",html.includes("We couldn't find its job board")&&html.includes("tried 4×")&&html.includes("Paste board link"));
  t("adding needs one field",html.includes('id="company-careers"')&&!html.includes('id="company-name"'));
  t("no editor is forced open",!html.includes('id="company-settings"'));
  t("scroll survives",ctx.scroll.scrollTop===612);
  t("unsafe website links are not clickable",R.companyLink({careers_url:"javascript:alert(1)"})==="");
  t("technical terminology is absent",!/monitoring_status|will_be_searched|adapter|unresolved|verified/.test(html));
  R.select("Ready Co");await R.renderCompanies(false);
  t("⋯ opens editing on demand",ctx.nodes["tailor-view"].innerHTML.includes('id="company-settings"'));
  R.showCompanyOnBoard("Fine Co");
  t("N on Board searches the Board for the company",ctx.home&&R.board().q==="Fine Co"&&R.board().filter==="all"&&R.board().sel===0);
  process.exit(bad?1:0);
})().catch(error=>{console.error(error);process.exit(1)});
"""


class CompaniesViewTest(unittest.TestCase):
    def test_plain_statuses_preserve_success_failure_and_unknown_distinctions(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "companies.js"
            probe.write_text(COMPANIES_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(ROOT / "tools/board/static/app.js")],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).strip())


COMPANY_WORK_HARNESS = r"""
const fs=require("fs"),src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("const companyPath="),end=src.indexOf("function selectCompanyRow",start);
(async()=>{
  const ctx={requests:[],renders:0,fail:false};
  const R=new Function("ctx",`
    let COMPANIES={mtime:"v1",companies:[{name:"New Co",status:"unresolved",route:"ats"}]},COMPANY_BUSY=null,COMPANY_ERROR="",VIEW="companies";
    const renderCompanies=()=>ctx.renders++,toast=()=>{};
    const postCompany=async(path,body)=>{ctx.requests.push({path,body});await Promise.resolve();if(ctx.fail)throw new Error("Offline");if(path.endsWith("/resolve")){COMPANIES.mtime="v2";COMPANIES.companies[0].will_be_searched=true}};
    ${src.slice(start,end)}
    return {companyWork,checkCompany,row:()=>COMPANIES.companies[0],state:()=>({busy:COMPANY_BUSY,error:COMPANY_ERROR}),leave:()=>VIEW="reader"};
  `)(ctx);
  let bad=0;const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL "+name)}};
  const first=R.companyWork("New Co",()=>R.checkCompany("New Co"));
  await R.companyWork("New Co",()=>R.checkCompany("New Co"));
  await first;
  t("double clicks do not start duplicate work",ctx.requests.length===2);
  t("unconnected source is resolved before fetching",ctx.requests[0].path.endsWith("/resolve")&&ctx.requests[1].path.endsWith("/test-fetch"));
  t("fetch uses the version returned after resolution",ctx.requests[1].body.mtime==="v2");
  R.row().status="paused";await R.checkCompany("New Co");
  t("paused companies never fetch",ctx.requests.length===2);
  R.row().status="verified";ctx.fail=true;const renders=ctx.renders;
  const failed=R.companyWork("New Co",()=>R.checkCompany("New Co"));R.leave();await failed;
  t("errors release busy state and remain visible",R.state().busy===null&&R.state().error==="Offline");
  t("completion cannot hijack another page",ctx.renders===renders+1);
  process.exit(bad?1:0);
})().catch(error=>{console.error(error);process.exit(1)});
"""


class CompanyWorkTest(unittest.TestCase):
    def test_check_flow_prevents_duplicates_and_preserves_navigation(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "company-work.js"
            probe.write_text(COMPANY_WORK_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(ROOT / "tools/board/static/app.js")],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).strip())


# A poll may repaint the tailor view and nothing else.  openView marks *every*
# parked view "tailor-mode" because they share one layout, so a guard that read
# the class list matched the companies view as well: opening Target companies
# lasted until the next three-second poll, which painted the last watched run
# over it - or, when that run had been pruned from the snapshot, dropped the
# user back on the workspace.  This harness runs the real openView against the
# real guard.
VIEW_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("const crumbRun="),end=src.indexOf("// Routing.");
if(start<0||end<start)throw new Error("the view block was not found in app.js");
const guards=src.match(/if\([^;{}]*\)renderTailor\(RUNS\.find\(r=>r\.id===ACTIVE_RUN\)\);/g)||[];
if(guards.length!==2)throw new Error("expected two live-refresh guards, found "+guards.length);
if(guards[0]!==guards[1])throw new Error("the live-refresh guards disagree:\n"+guards.join("\n"));

const build=new Function("ctx",`
  const nodes={};
  const el=id=>nodes[id]||(nodes[id]={id,hidden:false,textContent:"",dataset:{},
    classList:{seen:new Set(),
      add(...c){c.forEach(x=>this.seen.add(x))},
      remove(...c){c.forEach(x=>this.seen.delete(x))},
      contains(c){return this.seen.has(c)}}});
  const esc=value=>String(value==null?"":value).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const makeTab=nav=>{const node={dataset:{nav},state:{}};
    node.classList={toggle:(cls,on)=>{node.state[cls]=on}};
    node.setAttribute=(key,value)=>{node.state[key]=value};
    node.removeAttribute=key=>{delete node.state[key]};
    return node};
  ctx.tabs=[makeTab("board"),makeTab("runs"),makeTab("companies")];
  const document={querySelectorAll:()=>ctx.tabs};
  const RUNS=ctx.RUNS;
  let ACTIVE_RUN=null,VIEW=null;
  const setRoute=route=>ctx.routes.push(route);
  const renderTailor=run=>{ctx.painted.push(run?run.id:"workspace")};
  ${src.slice(start,end)}
  return {openView,restoreWorkspace,watch:id=>{ACTIVE_RUN=id},
          crumb:()=>el("crumb").innerHTML,lit:()=>ctx.tabs.filter(t=>t.state.on).map(t=>t.dataset.nav),
          run:crumbRun,
          poll:()=>{${guards[0]}}};
`);

const ctx={RUNS:[{id:"r-1",company:"Example",role:"Engineer"}],routes:[],painted:[]};
const R=build(ctx);
let bad=0;
const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL  "+name)}};

const RUN=ctx.RUNS[0];

R.watch("r-1");  // the user watched a run earlier in the session
for(const kind of ["companies","reader","preview","revise"]){
  ctx.painted=[];R.openView(kind,null,"/"+kind);R.poll();
  t("the "+kind+" view survives a poll",ctx.painted.length===0);
}
ctx.painted=[];R.openView("tailor",[R.run(RUN)],"/run/r-1");R.poll();
t("the tailor view still refreshes itself",ctx.painted[0]==="r-1");
ctx.painted=[];R.restoreWorkspace();R.poll();
t("the workspace is left alone",ctx.painted.length===0);

// Exactly one tab is lit, and a run lights Runs.
R.openView("companies",null,"/companies");
t("Companies lights its own tab",String(R.lit())==="companies");
R.openView("preview",[R.run(RUN),{label:"Preview"}],"/run/r-1/preview");
t("a run lights Runs",String(R.lit())==="runs");
R.restoreWorkspace();
t("home lights Board",String(R.lit())==="board");

// A crumb segment links only while something sits below it.
R.openView("tailor",[R.run(RUN)],"/run/r-1");
t("the run is plain text when it is where you are",!R.crumb().includes('data-run='));
t("the trail does not repeat the lit tab",!R.crumb().includes("Board"));
R.openView("preview",[R.run(RUN),{label:"Preview"}],"/run/r-1/preview");
t("the run becomes a link once Preview sits below it",R.crumb().includes('data-run="r-1"'));
t("Preview is where you are",R.crumb().includes('class="crumbseg here" title="Preview"'));
R.restoreWorkspace();
t("the workspace carries no crumb",R.crumb()==="");

process.exit(bad?1:0);
"""


class LiveRefreshTest(unittest.TestCase):
    """Only the view that shows a run may be repainted by a run poll."""

    def test_a_poll_repaints_the_tailor_view_and_nothing_else(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; the refresh guard is unchecked")
        app = ROOT / "tools" / "board" / "static" / "app.js"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "probe.js"
            probe.write_text(VIEW_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(app)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).strip())


FACET_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf('const BOARD_KEY="jobflow.board.v1";');
const marker="const selectedJob=()=>shown()[sel]||null;";
const end=src.indexOf(marker)+marker.length;
if(start<0||end<marker.length)throw new Error("the facet block was not found in app.js");

const store=new Map();
const localStorage={getItem:k=>store.has(k)?store.get(k):null,
                    setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)};
const build=new Function("ctx","localStorage",`
  const ACTIVE=["star","yes","new","maybe"];
  let JOBS=ctx.JOBS,filter=ctx.filter,q=ctx.q,sel=0;
  ${src.slice(start,end)}
  return {shown,match,isNewArrival,batchIsExact,facets:()=>facets,resetFacets,facetsAreDefault,
          setLast:v=>{LAST_FETCH=v},dayFloor,
          set:(patch)=>Object.assign(facets,patch),reload:()=>{facets=loadFacets()}};
`);

// Four rows from three sources, arriving in three batches. `old` predates
// arrival stamping and carries a bare date, which is what the real board holds
// for everything collected before this feature.
const JOBS=[
  {url:"a",title:"ML Engineer",company:"Zeta AG",status:"new",fit:"high",score:80,
   primary_source:"ats-search",posted:"2026-09-10",first_seen:"2026-09-18",first_seen_at:"2026-09-18T12:00:00"},
  {url:"b",title:"Backend Engineer",company:"Acme AG",status:"new",fit:"",score:40,
   primary_source:"freehire-search",posted:"2026-09-12",first_seen:"2026-09-18",first_seen_at:"2026-09-18T12:00:00"},
  {url:"c",title:"Data Engineer",company:"Mid AG",status:"new",fit:"medium",score:60,
   primary_source:"linkedin-search",posted:"2026-09-01",first_seen:"2026-09-17",first_seen_at:"2026-09-17T09:00:00"},
  {url:"d",title:"Analyst",company:"Old AG",status:"new",fit:"low",score:20,
   primary_source:"freehire-search",posted:"2026-08-02",first_seen:"2026-08-20"},
];
let bad=0;
const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL  "+name)}};
const urls=R=>R.shown().map(j=>j.url).join("");
const fresh=()=>build({JOBS,filter:"all",q:""},localStorage);

store.clear();
let R=fresh();
t("nothing is filtered or reordered by default",urls(R)==="abcd"&&R.facetsAreDefault());

// The whole point of the source chips: one low-signal board, gone.
R.set({hidden:["freehire-search"]});
t("hiding a source drops exactly its rows",urls(R)==="ac");
t("hiding is not the default state",!R.facetsAreDefault());
R.resetFacets();
t("reset brings them back",urls(R)==="abcd"&&R.facetsAreDefault());

R.set({fit:"unranked"});
t("unranked means no band at all, not a low one",urls(R)==="b");
R.set({fit:"high"});
t("a band filter is exact",urls(R)==="a");
R.resetFacets();

// "Latest fetch" is measured against the run, not against the newest row.
R.setLast("2026-09-18T12:00:00");
R.set({found:"latest"});
t("the latest fetch is the rows that run inserted",urls(R)==="ab");
t("and they are the ones marked new",R.isNewArrival(JOBS[0])&&!R.isNewArrival(JOBS[2]));
R.setLast("2026-09-19T08:00:00");
t("a run that found nothing marks nothing",urls(R)===""&&!R.isNewArrival(JOBS[0]));
// With no stamp recorded - every row collected before stamping existed - the
// board falls back to its own newest arrival instead of showing an empty table
// under a header that says 98 rows were added.
R.setLast(null);
t("with no stamp, the newest arrival stands in",
  urls(R)==="ab"&&R.isNewArrival(JOBS[0])&&!R.isNewArrival(JOBS[2]));
t("and the control says which of the two it is",!R.batchIsExact());
R.setLast("2026-09-18T12:00:00");
t("a real stamp takes over again",R.batchIsExact());
R.resetFacets();

// Sorting, including the row whose arrival is a bare date.
R.set({sort:"found"});
t("newest found first, mixing stamps and older dates",urls(R)==="abcd");
R.set({sort:"found-asc"});
t("oldest found first",urls(R)==="dcab");
R.set({sort:"posted"});
t("newest posted is a different order from newest found",urls(R)==="bacd");
// LinkedIn posts "6 days ago" as often as a date; a row whose date cannot be
// read is unknown, and unknown sinks instead of string-sorting among the dates.
const relative=build({JOBS:[{url:"x",posted:"6 days ago",status:"new",fit:"",score:0,
  primary_source:"linkedin-search",first_seen:"2026-09-18",first_seen_at:"2026-09-18T12:00:00"},
  {url:"y",posted:"2026-09-11",status:"new",fit:"",score:0,
   primary_source:"linkedin-search",first_seen:"",first_seen_at:""},
  ...JOBS],filter:"all",q:""},localStorage);
relative.set({sort:"posted"});
t("an unreadable posted date sinks to the bottom",
  relative.shown().map(j=>j.url).join("")==="byacdx");
// Unknown sinks whichever way the sort runs: reversing the comparison would
// have floated the row with no arrival recorded to the top of "oldest first".
relative.set({sort:"found-asc"});
t("a row with no arrival recorded sinks in ascending order too",
  relative.shown().map(j=>j.url).join("").endsWith("y"));
relative.set({sort:"found"});
t("and in descending order",relative.shown().map(j=>j.url).join("").endsWith("y"));
R.set({sort:"company"});
t("company sorts by name, not by the payload order",urls(R)==="bcda");
R.set({sort:"score"});
t("best score first",urls(R)==="acbd");
R.set({sort:"priority"});
t("priority is the payload order, untouched",urls(R)==="abcd");

// Ties fall back to the order the server sent, not to an arbitrary one.
R.set({sort:"found"});
t("rows sharing a stamp keep their server order",urls(R).slice(0,2)==="ab");

// The facets are a standing preference; the status chip is not.
store.clear();
R=fresh();R.set({hidden:["freehire-search"],sort:"found",fit:"high",found:"7"});
build({JOBS,filter:"all",q:""},localStorage);  // nothing saved yet
t("a facet is only persisted when it is saved",store.size===0);
store.set("jobflow.board.v1",JSON.stringify({version:1,hidden:["freehire-search"],
  fit:"any",found:"any",sort:"found"}));
R=fresh();
t("a saved facet set comes back",urls(R)==="ac"&&R.facets().sort==="found");
store.set("jobflow.board.v1",JSON.stringify({version:1,hidden:"not-an-array",
  fit:"purple",found:"forever",sort:"__proto__"}));
R=fresh();
t("a corrupt blob falls back to the defaults rather than breaking the board",
  R.facetsAreDefault()&&urls(R)==="abcd");
store.set("jobflow.board.v1","{oh no");
R=fresh();
t("unparseable storage is dropped, not thrown",R.facetsAreDefault()&&store.size===0);

// A day window is local midnight, which is not what toISOString() would give
// anywhere east of UTC.
const now=new Date(),pad=n=>String(n).padStart(2,"0");
t("today's window starts at local midnight today",
  R.dayFloor(1)===`${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}`);

// The status chip and the search box still apply, and compose with the facets.
R=build({JOBS,filter:"all",q:"engineer"},localStorage);
t("the search box still narrows",urls(R)==="abc");
R.set({hidden:["freehire-search"]});
t("search and facets compose",urls(R)==="ac");

const empty=build({JOBS:[],filter:"all",q:""},localStorage);
empty.setLast(null);
t("an empty board claims no batch at all",empty.shown().length===0);

process.exit(bad?1:0);
"""


class FacetBehaviourTest(unittest.TestCase):
    """Filtering and sorting the board: the rules, not the markup."""

    def test_facets_filter_sort_and_survive_storage(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; the filter algebra is unchecked")
        app = ROOT / "tools" / "board" / "static" / "app.js"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "facets.js"
            probe.write_text(FACET_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(app)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).strip())


START_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("async function postRun(");
const marker="async function startTailor(url){";
const end=src.indexOf("\n}",src.indexOf(marker))+2;
if(start<0||end<2)throw new Error("the run-start block was not found in app.js");

const build=new Function("ctx",`
  const T="tok";
  const JOBS=ctx.JOBS;
  let RUNS=ctx.RUNS;
  const fetch=(url,init)=>ctx.fetch(url,init);
  const checkAuth=()=>true;
  const toast=(msg)=>ctx.toasts.push(msg);
  const pollRuns=async()=>{RUNS=ctx.serverRuns};
  const pollActivity=()=>{};
  const openTextModal=async()=>ctx.note;
  const renderTailor=run=>ctx.opened.push(run.id);
  const document={querySelector:selector=>ctx.picks[selector]?{value:ctx.picks[selector]}:null};
  const DRAFT_LABEL={both:"Draft CV + cover letter",cv:"Draft CV",cover:"Draft cover letter"};
  ${src.slice(start,end)}
  return {startTailor,postRun};
`);

const RUN={id:"r-20260918-120000-acme-abc123",company:"Acme",role:"ML Engineer"};
const JOBS=[{url:"https://ex.com/a",company:"Acme",title:"ML Engineer"}];
const make=(status,body,serverRuns)=>{
  const ctx={JOBS,RUNS:[],serverRuns:serverRuns||[],toasts:[],opened:[],note:"",posted:[],picks:{},
    fetch:async(url,init)=>{ctx.posted.push([url,JSON.parse(init.body)]);
      return {ok:status<400,status,json:async()=>body}}};
  return [build(ctx),ctx];
};
let bad=0;
const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL  "+name)}};

(async()=>{
  // The press takes you to the run it started - no toast landing on the button
  // in the corner it was pressed in.
  let [R,ctx]=make(202,{run_id:RUN.id,phase:"queued"},[RUN]);
  await R.startTailor("https://ex.com/a");
  t("starting an evaluation opens its run",ctx.opened.join("")===RUN.id);
  t("and says nothing on top of the button",ctx.toasts.length===0);
  t("the one-off instruction still reaches the request",ctx.posted[0][1].kind==="apply");
  t("with no picker on screen the defaults are sent",
    ctx.posted[0][1].scope==="both"&&ctx.posted[0][1].base_cv==="auto"&&ctx.posted[0][1].cv_country==="default");

  // The documents, base and country are chosen before Generate - there is no
  // evaluation to wait for - and all three reach the request.
  [R,ctx]=make(202,{run_id:RUN.id,phase:"queued"},[RUN]);
  ctx.picks={"[data-gen-scope]":"cover","[data-gen-base]":"ai","[data-gen-country]":"de"};
  await R.startTailor("https://ex.com/a");
  t("the chosen scope, base and country are posted",
    ctx.posted[0][1].scope==="cover"&&ctx.posted[0][1].base_cv==="ai"&&ctx.posted[0][1].cv_country==="de");

  // A posting that already has a run in flight: the server hands back which one,
  // so show it instead of describing it.
  [R,ctx]=make(409,{error:"a run for this posting is already drafting",run_id:RUN.id},[RUN]);
  await R.startTailor("https://ex.com/a");
  t("a duplicate opens the run that already exists",ctx.opened.join("")===RUN.id);
  t("and the refusal is still reported",ctx.toasts.some(m=>String(m).includes("already drafting")));

  // A refusal with no run behind it must not navigate, and must not go quiet.
  [R,ctx]=make(429,{error:"the queue is full (8 waiting)"},[]);
  await R.startTailor("https://ex.com/a");
  t("a refusal with no run stays put",ctx.opened.length===0);
  t("and is reported",ctx.toasts.some(m=>String(m).includes("queue is full")));

  // Started, but the run list did not come back with it.
  [R,ctx]=make(202,{run_id:RUN.id,phase:"queued"},[]);
  await R.startTailor("https://ex.com/a");
  t("a started run that cannot be found still acknowledges the press",
    ctx.opened.length===0&&ctx.toasts.some(m=>String(m).includes("queued")));

  // Cancelling the instruction modal starts nothing at all.
  [R,ctx]=make(202,{run_id:RUN.id},[RUN]);ctx.note=null;
  await R.startTailor("https://ex.com/a");
  t("cancelling the modal posts nothing",ctx.posted.length===0&&ctx.opened.length===0);

  process.exit(bad?1:0);
})();
"""


class StartEvaluationTest(unittest.TestCase):
    """Pressing Generate opens the run, rather than toasting over itself."""

    def test_the_press_navigates_to_the_run_it_started(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; the start path is unchecked")
        app = ROOT / "tools" / "board" / "static" / "app.js"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "start.js"
            probe.write_text(START_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(app)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).strip())


if __name__ == "__main__":
    unittest.main()
