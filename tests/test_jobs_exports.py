"""Markdown and CSV are explicit, one-way exports from canonical JSON state."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools import jobs_md
from tools.board import state as board_state


def entry():
    return {
        "title": "ML Engineer",
        "company": "Example AG",
        "location": "Zurich",
        "url": "https://www.linkedin.com/jobs/view/4451224579",
        "first_seen": "2026-08-20",
        "posted": "2026-08-19",
        "fit": "high",
        "user_status": "new",
        "user_note": "",
        "note": "",
        "portal": "linkedin-browser",
    }


class ExplicitExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = (jobs_md.SEEN, jobs_md.MD, jobs_md.CSV_ACTIVE, jobs_md.CSV_EXCLUDED)
        jobs_md.SEEN = root / "seen_jobs.json"
        jobs_md.MD = root / "jobs.md"
        jobs_md.CSV_ACTIVE = root / "jobs_active.csv"
        jobs_md.CSV_EXCLUDED = root / "jobs_excluded.csv"
        jobs_md.save_seen({entry()["url"]: entry()})

        def restore():
            jobs_md.SEEN, jobs_md.MD, jobs_md.CSV_ACTIVE, jobs_md.CSV_EXCLUDED = self.paths

        self.addCleanup(restore)

    def run_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return jobs_md.main(["jobs_md.py"] + list(args))

    def test_no_argument_does_not_generate_any_export(self):
        self.assertEqual(self.run_cli(), 2)
        self.assertFalse(jobs_md.MD.exists())
        self.assertFalse(jobs_md.CSV_ACTIVE.exists())
        self.assertFalse(jobs_md.CSV_EXCLUDED.exists())

    def test_markdown_export_is_independent(self):
        self.assertEqual(self.run_cli("export-md"), 0)
        self.assertIn("Read-only snapshot", jobs_md.MD.read_text(encoding="utf-8"))
        self.assertFalse(jobs_md.CSV_ACTIVE.exists())
        self.assertFalse(jobs_md.CSV_EXCLUDED.exists())

    def test_csv_export_is_independent(self):
        self.assertEqual(self.run_cli("export-csv"), 0)
        self.assertFalse(jobs_md.MD.exists())
        self.assertTrue(jobs_md.CSV_ACTIVE.exists())
        self.assertTrue(jobs_md.CSV_EXCLUDED.exists())


class CanonicalBoardStateTest(unittest.TestCase):
    def test_board_save_writes_only_seen_jobs_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = board_state.jobs_md
            saved = (module.SEEN, module.MD, module.CSV_ACTIVE, module.CSV_EXCLUDED,
                     board_state.activity.LOG)
            module.SEEN = root / "seen_jobs.json"
            module.MD = root / "jobs.md"
            module.CSV_ACTIVE = root / "jobs_active.csv"
            module.CSV_EXCLUDED = root / "jobs_excluded.csv"
            board_state.activity.LOG = root / "activity.jsonl"
            try:
                with module.board_lock():
                    board_state.save({entry()["url"]: entry()}, why="test")
                self.assertTrue(module.SEEN.exists())
                self.assertFalse(module.MD.exists())
                self.assertFalse(module.CSV_ACTIVE.exists())
                self.assertFalse(module.CSV_EXCLUDED.exists())
            finally:
                (module.SEEN, module.MD, module.CSV_ACTIVE, module.CSV_EXCLUDED,
                 board_state.activity.LOG) = saved


if __name__ == "__main__":
    unittest.main()
