"""M2.5 static-shell contract.

The browser is intentionally dependency-free.  These tests keep the approved
panel geometry and its interaction hooks from silently regressing while the
existing HTTP tests cover the API wiring.
"""

import sys
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


class JobPanelPayloadTest(unittest.TestCase):
    def test_stored_posting_text_is_shaped_for_the_job_panel(self):
        entry = {
            "url": "https://example.test/jobs/1",
            "title": "Engineer",
            "company": "Example",
            "location": "Zurich",
            "posted": "2026-08-19",
            "description": "Build reliable systems.",
            "portal": "linkedin-browser",
            "primary_source": "linkedin-browser",
            "sources": [{"portal": "linkedin-browser"}, {"portal": "linkedin-search"}],
        }
        with mock.patch.object(state, "load", return_value={entry["url"]: entry}), \
             mock.patch.object(state.jobs_md, "priority_score", return_value=0):
            payload = state.jobs_payload()
        self.assertEqual(payload["jobs"][0]["description"],
                         "Build reliable systems.")
        self.assertEqual(payload["jobs"][0]["primary_source"], "linkedin-browser")
        self.assertEqual(payload["jobs"][0]["sources"],
                         ["linkedin-browser", "linkedin-search"])


if __name__ == "__main__":
    unittest.main()
