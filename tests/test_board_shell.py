"""M2.5 static-shell contract.

The browser is intentionally dependency-free.  These tests keep the approved
panel geometry and its interaction hooks from silently regressing while the
existing HTTP tests cover the API wiring.
"""

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

    def test_workspace_has_the_five_approved_panels(self):
        for marker in (
                'id="collect-panel"', 'id="runs"', 'id="board-panel"',
                'id="applications-panel"', 'id="job-panel"'):
            self.assertIn(marker, self.html)

    def test_all_four_painted_gaps_are_accessible_splitters(self):
        for marker in (
                'id="left-split"', 'id="right-split"',
                'id="left-row-split"', 'id="centre-row-split"'):
            self.assertIn(marker, self.html)
        self.assertEqual(self.html.count('role="separator"'), 4)
        self.assertEqual(self.html.count('tabindex="0"'), 4)
        self.assertIn("aria-valuenow", self.js)
        self.assertIn("setPointerCapture", self.js)

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

    def test_workspace_and_tailor_artboards_have_runtime_hooks(self):
        self.assertIn('data-expand="board"', self.html)
        self.assertIn("expanded-board", self.css)
        self.assertIn("tailor-shell", self.css)
        self.assertIn("Gaps, stated not smoothed", self.js)
        self.assertIn("runpill", self.html)

    def test_job_reader_and_compiled_pdf_preview_are_real_views(self):
        for marker in ("reader-shell", "reader-queue", "reader-decide",
                       "preview-shell", "verify-rail", "pdf-stage"):
            self.assertIn(marker, self.css)
        for marker in ("renderReader", "renderPreview", "data-reader-row",
                       "data-preview-filter", "data-recompile",
                       "/api/pdf/", "Rendered from the compiled PDFs"):
            self.assertIn(marker, self.js)
        self.assertIn('iframe title="Compiled CV"', self.js)
        self.assertIn('iframe title="Compiled cover letter"', self.js)

    def test_revise_and_companies_artboards_are_on_the_runtime_surface(self):
        self.assertIn('id="companies-open"', self.html)
        for marker in ("revise-shell", "grid-template-columns:268px", "428px",
                       "companies-shell", "396px", "company-table"):
            self.assertIn(marker, self.css)
        # The companies view speaks plain language now: the add button is
        # "Add company" rather than "Add & resolve", and the request-budget
        # hint was replaced by a sentence about what the system does. Resolving
        # everything at once is a first-class button instead of a per-row
        # action, so `resolve-all` is what pins it.
        for marker in ("renderRevise", "/api/prefs", "data-restore-version",
                       "--fork-session", "renderCompanies", "/api/companies",
                       "Add company", "resolve-all", "Automatically check all",
                       "data-company-confirm"):
            if marker == "--fork-session":
                continue
            self.assertIn(marker, self.js)

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


if __name__ == "__main__":
    unittest.main()
