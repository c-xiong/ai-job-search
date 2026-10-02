"""Offline end-to-end source failure, backfill, inbox allocation and dry-run checks."""

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import fetch_jobs


class CollectionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root, self.jm = root, fetch_jobs.jobs_md
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for obj, attr, value in ((self.jm, "SEEN", root / "seen_jobs.json"),
            (fetch_jobs, "CONFIG", root / "config.json"), (fetch_jobs, "LOG", root / "scrape.log"),
            (fetch_jobs, "LOCK", root / ".fetch.lock"), (fetch_jobs, "STATUS", root / "fetch_status.json"),
            (fetch_jobs.ats_fetch, "REGISTRY", root / "companies.json")):
            self.stack.enter_context(patch.object(obj, attr, value))
        self.stack.enter_context(patch.object(fetch_jobs.ats_fetch, "scoring_context", return_value=None))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.linkedin_url = "https://www.linkedin.com/jobs/view/9000001"
        self.external_url = "https://careers.example.test/apply?jobId=example-123"

    def config(self, value):
        fetch_jobs.CONFIG.write_text(json.dumps(value), encoding="utf-8")

    def existing(self):
        self.jm.save_seen({self.external_url: {
            "url": self.external_url, "title": "Data Engineer", "company": "Example Employer",
            "location": "Berlin", "posted": "2026-10-01", "first_seen": "2026-10-01",
            "first_seen_at": "2026-10-01T10:00:00", "portal": "company-careers",
            "user_status": "yes", "user_note": "Discuss role", "rank_score": 81,
            "user_posting_url": self.external_url, "sources": [{"portal": "linkedin-browser", "url": self.linkedin_url, "id": "9000001"}]}})

    @staticmethod
    def detail(text="Build data pipelines using Python."):
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"description": text, "request_meta": {"http_attempts": 1, "retries": 0}}))

    def test_known_missing_body_is_backfilled_without_a_new_search_sighting_and_preserves_decisions(self):
        self.existing()
        self.config({"linkedin": [], "linkedin_max_detail_fetches": 1})
        with patch.object(fetch_jobs.collectors.subprocess, "run", return_value=self.detail()) as detail:
            summaries, _ = fetch_jobs.fetch(["linkedin"])
        seen = fetch_jobs.load_seen()
        self.assertEqual(len(seen), 1)
        row = seen[self.external_url]
        self.assertEqual(row["user_status"], "yes")
        self.assertEqual(row["user_note"], "Discuss role")
        self.assertEqual(row["rank_score"], 81)
        self.assertEqual(row["url"], self.external_url)
        self.assertEqual(row["first_seen_at"], "2026-10-01T10:00:00")
        self.assertTrue(row["posting_path"])
        self.assertEqual(summaries[0]["enriched"], 1)
        self.assertEqual(detail.call_count, 1)
        # Successful enrichment removes this row from the next detail backlog.
        with patch.object(fetch_jobs.collectors.subprocess, "run", side_effect=AssertionError("unexpected detail request")):
            fetch_jobs.fetch(["linkedin"])

    def test_failed_detail_remains_eligible_on_later_runs(self):
        self.existing()
        self.config({"linkedin": [], "linkedin_max_detail_fetches": 1})
        failure = types.SimpleNamespace(returncode=1, stdout="", stderr='{"error":"fixture unavailable"}')
        with patch.object(fetch_jobs.collectors.subprocess, "run", return_value=failure):
            summaries, _ = fetch_jobs.fetch(["linkedin"])
        self.assertEqual(summaries[0]["failed_descriptions"], 1)
        self.assertTrue(summaries[0]["degraded"])
        self.assertNotIn("posting_path", fetch_jobs.load_seen()[self.external_url])
        with patch.object(fetch_jobs.collectors.subprocess, "run", return_value=self.detail()):
            fetch_jobs.fetch(["linkedin"])
        self.assertTrue(fetch_jobs.load_seen()[self.external_url]["posting_path"])

    def test_newly_hydrated_hard_requirement_gates_only_untouched_unreviewed_rows(self):
        self.existing()
        seen = fetch_jobs.load_seen()
        row = seen[self.external_url]
        row.update(user_status="new", user_note="")
        row.pop("rank_score")
        self.jm.save_seen(seen)
        self.config({"linkedin": []})
        with patch.object(fetch_jobs.collectors.subprocess, "run", return_value=self.detail("German is required. Build Python tools.")):
            summaries, _ = fetch_jobs.fetch(["linkedin"])
        row = fetch_jobs.load_seen()[self.external_url]
        self.assertEqual(row["user_status"], "gate")
        self.assertIn("German", row["note"])
        self.assertEqual(summaries[0]["gated"], 1)

    def test_ranked_or_human_annotated_rows_keep_decisions_when_body_is_hydrated(self):
        for protection in ({"rank_score": 81}, {"user_note": "Manual assessment is pending"}):
            self.existing()
            seen = fetch_jobs.load_seen()
            row = seen[self.external_url]
            row.update(user_status="new", user_note="")
            row.pop("rank_score")
            row.update(protection)
            self.jm.save_seen(seen)
            self.config({"linkedin": []})
            with patch.object(fetch_jobs.collectors.subprocess, "run", return_value=self.detail("German is required. Build Python tools.")):
                fetch_jobs.fetch(["linkedin"])
            row = fetch_jobs.load_seen()[self.external_url]
            self.assertNotEqual(row["user_status"], "gate")
            for field, value in protection.items():
                self.assertEqual(row[field], value)

    def test_malformed_optional_configuration_fails_per_source(self):
        self.config({"linkedin_intake": [], "linkedin_inbox": "invalid"})
        summaries, _ = fetch_jobs.fetch(["linkedin"], dry_run=True)
        failed = [s for s in summaries if s.get("degraded")]
        self.assertEqual({s["source"] for s in failed}, {"linkedin-intake", "linkedin-inbox"})

    def test_dry_run_leaves_board_checkpoints_logs_status_and_postings_untouched(self):
        self.existing()
        self.config({"linkedin": []})
        state = {"query_cursor": 3, "backfill_cursor": 0}
        fetch_jobs.collection_state_path().write_text(json.dumps(state))
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with patch.object(fetch_jobs.collectors.subprocess, "run", return_value=self.detail()):
            fetch_jobs.fetch(["linkedin"], dry_run=True)
        after = {path.name: path.read_bytes() for path in self.root.iterdir() if path.name not in (".fetch.lock", ".board.lock")}
        self.assertEqual(before, after)
        self.assertFalse((self.root / "postings").exists())

    def test_failed_source_preserves_other_source_and_reports_failure(self):
        self.config({"linkedin": [{"q": "Engineer", "l": "Remote"}], "freehire": [{"q": "Data"}]})
        def cli(args, _log):
            if args[0] == fetch_jobs.collectors.LINKEDIN:
                return None
            return {"results": [{"id": "example-data", "title": "Data Engineer", "company": "Example Employer",
                                 "description": "Build Python data pipelines."}]}
        with patch.object(fetch_jobs.collectors, "bun", side_effect=cli):
            summaries, _ = fetch_jobs.fetch(["freehire", "linkedin"])
        self.assertEqual(len(fetch_jobs.load_seen()), 1)
        self.assertTrue(next(s for s in summaries if s["source"] == "linkedin-search")["degraded"])
        self.assertEqual(next(s for s in summaries if s["source"] == "freehire-search")["added"], 1)

    def test_inbox_and_collector_share_one_detail_budget(self):
        self.config({"linkedin": [], "linkedin_max_detail_fetches": 3,
                     "linkedin_inbox": {"auto_process": True, "process_limit": 5}})
        self.existing()
        calls = []
        def process(limit, dry_run):
            calls.append(limit)
            return {"processed": limit, "imported": limit, "detail_calls": limit}
        with patch.dict(sys.modules, {"linkedin_inbox": types.SimpleNamespace(process=process)}), \
             patch.object(fetch_jobs.collectors.subprocess, "run", side_effect=AssertionError("budget exhausted")):
            summaries, _ = fetch_jobs.fetch(["linkedin"])
        self.assertEqual(calls, [3])
        self.assertEqual(next(s for s in summaries if s["source"] == "linkedin-search")["detail_calls"], 3)


class CollectionCliReportingTest(unittest.TestCase):
    def run_main(self, summaries):
        output = io.StringIO()
        with patch.object(sys, "argv", ["fetch_jobs.py", "--sources", "linkedin"]), \
             patch.object(fetch_jobs, "fetch", return_value=(summaries, [])) as fetch, \
             contextlib.redirect_stdout(output):
            code = fetch_jobs.main()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(code, 0)
        return output.getvalue()

    def test_degraded_inbox_reports_retry_and_manual_counts_without_failed_key(self):
        output = self.run_main([{"source": "linkedin-inbox", "added": 1,
                                 "degraded": True, "retry": 2, "needs_manual": 1}])
        self.assertIn("linkedin-inbox", output)
        self.assertIn("DEGRADED: 2 retries pending, 1 need manual review", output)

    def test_degraded_intake_exception_keeps_successful_source_counts_visible(self):
        output = self.run_main([{"source": "linkedin-intake", "degraded": True,
                                 "error": "fixture intake unavailable"},
                                {"source": "arbeitnow", "found": 4, "added": 2, "gated": 1}])
        self.assertIn("DEGRADED: fixture intake unavailable", output)
        self.assertIn("arbeitnow", output)
        self.assertRegex(output, r"arbeitnow\s+found 4\s+added 2\s+gated 1")

    def test_numeric_failure_counts_and_missing_details_have_safe_fallbacks(self):
        output = self.run_main([{"source": "linkedin-intake", "degraded": True, "failed": 3},
                                {"source": "linkedin-search", "degraded": True}])
        self.assertIn("DEGRADED: 3 failed", output)
        self.assertIn("DEGRADED: source incomplete; check fetch log", output)


if __name__ == "__main__":
    unittest.main()
