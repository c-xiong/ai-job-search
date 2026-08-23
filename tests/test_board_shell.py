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
                      '+"/preview"', '+"/revise"', '"/companies?f="'):
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
        self.assertIn("Gaps, stated not smoothed", self.js)
        self.assertIn("runpill", self.html)
        for marker in ("Retry from beginning", "provider_rate_limit", "failure-card",
                       "data-run-base", "data-base-start", "latestApplications",
                       "retry_of"):
            self.assertIn(marker, self.js if marker != "failure-card" else self.css)
        # The job identity lives in the body, in sentence case - never in the
        # app bar's uppercase slot, and never twice.
        self.assertIn(".runtitle h1{", self.css)
        self.assertIn("const runTitle=run=>", self.js)
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
        # Exactly two tabs. Runs is not one of them (§0.3): it has no
        # full-width form, so a run is reached from the rail and sits under
        # Board, which stays lit while you are inside one.
        self.assertIn('<nav class="nav" aria-label="Primary">', self.html)
        for tab in ('<button class="navitem" data-nav="board">Board</button>',
                    '<button class="navitem" data-nav="companies">Companies</button>'):
            self.assertIn(tab, self.html)
        self.assertNotIn('data-nav="runs"', self.html)
        self.assertIn('VIEW==="companies"?"companies":"board"', self.js)
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
                       "preview-shell", "verify-rail", "pdf-stage"):
            self.assertIn(marker, self.css)
        for marker in ("renderReader", "renderPreview", "data-reader-row",
                       "data-preview-filter", "data-recompile",
                       "/api/pdf/", "Rendered from the compiled PDFs"):
            self.assertIn(marker, self.js)
        # The frames are built per document kind now, so the titles are in the
        # branch rather than in two literal tags.
        self.assertIn('"Compiled CV"', self.js)
        self.assertIn('"Compiled cover letter"', self.js)
        self.assertIn('class="pdf-frame ${kind}"', self.js)

    def test_a_run_can_be_asked_for_one_document_instead_of_both(self):
        """Some postings are worth a letter and not a fresh CV, and the choice
        has to survive the poll that rebuilds the panel it lives in."""
        for marker in ("data-scope-start", "data-run-scope", "scopeOptions",
                       "TAILOR_LABEL", "DRAFT_LABEL", "docKinds",
                       "Cover letter only", "const START={base:", "const base_cv=START.base,scope=START.scope"):
            self.assertIn(marker, self.js)
        # The approval card sends the scope it is showing, not a default.
        self.assertIn('scope:document.querySelector("[data-run-scope]")?.value||"both"',
                      self.js)

    def test_revise_and_companies_artboards_are_on_the_runtime_surface(self):
        self.assertIn('data-nav="companies"', self.html)
        for marker in ("revise-shell", "grid-template-columns:268px", "428px",
                       "companies-shell", "360px", "company-table"):
            self.assertIn(marker, self.css)
        # Target companies is a focused careers-page-to-ATS workflow. Internal
        # registry metadata is deliberately absent from the UI.
        for marker in ("renderRevise", "/api/prefs", "data-restore-version",
                       "--fork-session", "renderCompanies", "/api/companies",
                       "Save company", "company-careers", "countries", "health-check",
                       "Check monitoring health", "handleCompanyHealthClick", "company-batch-status",
                       "data-company-confirm", "data-company-test-fetch", "company-settings",
                       "monitoring_status", "will_be_searched"):
            if marker == "--fork-session":
                continue
            self.assertIn(marker, self.js)
        for removed in ("Technical details", "Suggested companies", "<dt>Priority</dt>",
                        "<dt>Source</dt>", "<dt>Tags</dt>", "<dt>Notes</dt>"):
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
  let filter="active",q="",sel=0,COMPANY_FILTER="all",COMPANY_SELECTED=null;
  const shown=()=>JOBS.filter(job=>filter==="all"||job.status==="new");
  const el=()=>({value:"seeded"});
  const render=()=>{};
  const restoreWorkspace=()=>{ctx.opened.push("workspace");setRoute("/")};
  const openView=(kind,title,route)=>{ctx.opened.push(kind);setRoute(route||"/"+kind)};
  const renderReader=()=>openView("reader",null,"/job/"+encodeURIComponent(shown()[sel].url));
  const renderCompanies=()=>openView("companies","","/companies?f="+encodeURIComponent(COMPANY_FILTER)+(COMPANY_SELECTED?"&c="+encodeURIComponent(COMPANY_SELECTED):""));
  const renderTailor=run=>openView("tailor",null,"/run/"+encodeURIComponent(run.id));
  const renderPreview=run=>openView("preview",null,"/run/"+encodeURIComponent(run.id)+"/preview");
  const renderRevise=run=>openView("revise",null,"/run/"+encodeURIComponent(run.id)+"/revise");
  ${src.slice(start,end)}
  return {applyRoute,renderReader,renderCompanies,renderTailor,renderPreview,
          state:()=>({filter,sel,COMPANY_FILTER,COMPANY_SELECTED}),
          set:(k,v)=>{if(k==="filter")filter=v;if(k==="sel")sel=v;if(k==="COMPANY_FILTER")COMPANY_FILTER=v;if(k==="COMPANY_SELECTED")COMPANY_SELECTED=v}};
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
at("#/companies?f=all"); R.set("COMPANY_FILTER","review"); R.set("COMPANY_SELECTED","Acme & Co"); R.renderCompanies();
t("filtering companies replaces", log.length===1&&log[0].startsWith("replace"));
const companyRoute=location.hash;
at("#/board"); R.renderTailor(ctx.RUNS[0]);
t("board -> tailor pushes", log[0]==="push #/run/run-7");
at("#/run/run-7"); R.renderPreview(ctx.RUNS[0]);
t("tailor -> preview pushes, so back returns to the pipeline", log[0]==="push #/run/run-7/preview");

// A name needing escaping round-trips through the query string.
at(companyRoute); R.applyRoute();
t("companies filter and selection come back",
  R.state().COMPANY_FILTER==="review"&&R.state().COMPANY_SELECTED==="Acme & Co"&&ctx.opened[0]==="companies");

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

// A deep link outranks whichever chip the board was left on.
at("#/job/"+encodeURIComponent("https://ex.com/b")); R.applyRoute();
t("deep link widens a filter that would hide the row",
  R.state().filter==="all"&&ctx.opened[0]==="reader");

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


HEALTH_CHECK_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("async function checkCompanyHealth("),end=src.indexOf('document.addEventListener("click"',start);
if(start<0||end<start)throw new Error("health-check functions were not found in app.js");

(async()=>{
  const checked={mtime:"new",companies:[],health_check:{tested:3,succeeded:2,failed:1}};
  const ctx={COMPANIES:{mtime:"old",companies:[]},requests:[],toasts:[],rendered:[],
    fetch:async(url,opts)=>{ctx.requests.push({url,opts});return {ok:true,json:async()=>checked}}};
  const build=new Function("ctx",`
    let COMPANIES=ctx.COMPANIES,T="token",COMPANY_HEALTH_RUNNING=false,COMPANY_HEALTH_RESULT=null;
    const fetch=ctx.fetch,toast=(...args)=>ctx.toasts.push(args),renderCompanies=value=>ctx.rendered.push(value);
    ${src.slice(start,end)}
    return {handleCompanyHealthClick,state:()=>COMPANIES,result:()=>COMPANY_HEALTH_RESULT};
  `);
  const R=build(ctx),button={disabled:false,textContent:""};
  const event={target:{closest:selector=>selector==="#company-health-check"?button:null}};
  let bad=0;
  const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL  "+name)}};
  const pending=R.handleCompanyHealthClick(event);
  t("click disables immediately",button.disabled===true);
  t("click shows progress",button.textContent.includes("Testing"));
  await pending;
  t("one click makes one request",ctx.requests.length===1);
  t("the health endpoint is used",ctx.requests[0].url==="/api/companies/health-check?t=token");
  t("the request is POST",ctx.requests[0].opts.method==="POST");
  t("the current registry version is sent",JSON.parse(ctx.requests[0].opts.body).mtime==="old");
  t("the checked registry replaces local state",R.state()===checked);
  t("the running and completed states rerender",ctx.rendered.length===2&&ctx.rendered.every(value=>value===false));
  t("the final result stays visible",R.result().state==="error"&&R.result().message.includes("2 succeeded"));
  t("a disabled button cannot start a second batch",R.handleCompanyHealthClick(event)===null&&ctx.requests.length===1);

  const failed={COMPANIES:{mtime:"old",companies:[]},requests:[],toasts:[],rendered:[],
    fetch:async(url,opts)=>{failed.requests.push({url,opts});return {ok:false,json:async()=>({error:"resolver timed out"})}}};
  const F=build(failed),failedButton={disabled:false,textContent:""};
  await F.handleCompanyHealthClick({target:{closest:()=>failedButton}});
  t("an error remains visible",F.result().state==="error"&&F.result().message==="resolver timed out");
  t("an error also rerenders the status",failed.rendered.length===2);
  process.exit(bad?1:0);
})().catch(error=>{console.error(error);process.exit(1)});
"""


class HealthCheckClickTest(unittest.TestCase):
    """The visible health button must invoke exactly one real batch request."""

    def test_click_posts_the_registry_version_and_applies_the_result(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; resolve-all click is unchecked")
        app = ROOT / "tools" / "board" / "static" / "app.js"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "health-check.js"
            probe.write_text(HEALTH_CHECK_HARNESS, encoding="utf-8")
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
const row={dataset:{companyRow:"Company Near The Bottom"},closest:selector=>selector===".tablewrap"?table:null};
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


ATS_ONLY_HARNESS = r"""
const fs=require("fs");
const src=fs.readFileSync(process.argv[2],"utf8");
const start=src.indexOf("async function renderCompanies("),end=src.indexOf("function openView(",start);
if(start<0||end<start)throw new Error("renderCompanies was not found in app.js");

(async()=>{
  const ctx={nodes:{},tablewrap:{scrollTop:0},companies:{mtime:"1",companies:[
    {name:"ATS Co",route:"ats",status:"unresolved",countries:["CH"]},
    {name:"Legacy Ready",route:"ats",status:"verified",vendor:"ashby",token:"legacy",countries:["CH","DE"]},
    {name:"LinkedIn Co",route:"linkedin",status:"verified"},
    {name:"Manual Co",route:"manual",status:"paused"},
    {name:"Legacy ATS Co",status:"unresolved",countries:["DE"]}
  ]}};
  const build=new Function("ctx",`
    const el=id=>ctx.nodes[id]||(ctx.nodes[id]={innerHTML:"",querySelector:()=>ctx.tablewrap});
    const esc=value=>String(value??"");
    const openView=()=>{},toast=()=>{};
    let COMPANIES=ctx.companies,COMPANY_FILTER="all",COMPANY_SELECTED=null,T="token",COMPANY_HEALTH_RUNNING=false,COMPANY_HEALTH_RESULT=null,COMPANY_TEST_RESULTS={},COMPANY_SOURCE_RESULTS={};
    const fetch=async()=>{throw new Error("render should use the loaded registry")};
    ${src.slice(start,end)}
    return {renderCompanies};
  `);
  await build(ctx).renderCompanies(false,612);
  const html=ctx.nodes["tailor-view"].innerHTML;
  let bad=0;
  const t=(name,cond)=>{if(!cond){bad++;console.log("FAIL  "+name)}};
  t("ATS company is visible",html.includes("ATS Co"));
  t("legacy rows default to ATS",html.includes("Legacy ATS Co"));
  t("LinkedIn company is hidden",!html.includes("LinkedIn Co"));
  t("manual company is hidden",!html.includes("Manual Co"));
  t("the All count excludes other sources",html.includes("All<span class=\"n\">3</span>"));
  t("legacy API fields do not become all No",html.includes("1 of 3 companies will be searched"));
  t("a mixed frontend/backend version asks for restart",html.includes("Backend restart required"));
  t("a company rerender restores the table position",ctx.tablewrap.scrollTop===612);
  process.exit(bad?1:0);
})().catch(error=>{console.error(error);process.exit(1)});
"""


class AtsOnlyCompaniesViewTest(unittest.TestCase):
    """Target Companies is an ATS monitor, not a LinkedIn/manual registry view."""

    def test_linkedin_and_manual_companies_are_not_rendered_or_counted(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; ATS-only rendering is unchecked")
        app = ROOT / "tools" / "board" / "static" / "app.js"
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "ats-only.js"
            probe.write_text(ATS_ONLY_HARNESS, encoding="utf-8")
            result = subprocess.run([node, str(probe), str(app)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).strip())


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
  ctx.tabs=[makeTab("board"),makeTab("companies")];
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

// Exactly one tab is lit, and a run keeps you under Board.
R.openView("companies",null,"/companies");
t("Companies lights its own tab",String(R.lit())==="companies");
R.openView("preview",[R.run(RUN),{label:"Preview"}],"/run/r-1/preview");
t("a run stays under Board",String(R.lit())==="board");
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


if __name__ == "__main__":
    unittest.main()
