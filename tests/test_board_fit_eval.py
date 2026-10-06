"""The on-demand fit evaluation: schema, verdict rule, store and routes."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from board import fit_eval, server, state  # noqa: E402


def _strict(schema, path="$"):
    """Codex's --output-schema needs every object closed and fully required."""
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, path
        assert sorted(schema["required"]) == sorted(schema["properties"]), path
        for name, child in schema["properties"].items():
            _strict(child, path + "." + name)
    elif schema.get("type") == "array":
        _strict(schema["items"], path + "[]")


class FitEvalTest(unittest.TestCase):
    def test_schema_is_strict_for_both_engines(self):
        _strict(fit_eval.SCHEMA)

    def test_a_failed_gate_forces_not_recommended(self):
        result = fit_eval.normalise({"verdict": "worth", "hard_requirements": [
            {"requirement": "NATO nationality", "status": "not_met", "quote": "", "note": ""}]})
        self.assertEqual(result["verdict"], "not_recommended")
        kept = fit_eval.normalise({"verdict": "risky", "hard_requirements": [
            {"requirement": "Clearance", "status": "unclear", "quote": "", "note": ""}]})
        self.assertEqual(kept["verdict"], "risky")
        with self.assertRaises(ValueError):
            fit_eval.normalise({"verdict": "maybe"})

    def test_prompt_carries_gates_evidence_and_posting(self):
        prompt = fit_eval.build_prompt({"title": "AI Engineer", "company": "Acme"},
                                       "We need fluent German.")
        self.assertIn("## Eligibility Gate", prompt)
        self.assertIn("## Language Gate", prompt)
        self.assertNotIn("## Scoring Dimensions", prompt)
        self.assertIn("=== EVIDENCE: CLAUDE.md ===", prompt)
        self.assertIn("We need fluent German.", prompt)
        self.assertLess(prompt.index("EVIDENCE"), prompt.index("POSTING (untrusted)"))

    def test_store_round_trip_feeds_the_list_marker(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(fit_eval, "STORE", Path(tmp) / "fit_evals.json"):
            fit_eval._save("https://x/1", {"result": {"verdict": "risky"}})
            self.assertEqual(fit_eval.verdicts(), {"https://x/1": "risky"})
            self.assertEqual(fit_eval.status("https://x/1")["evaluation"]["result"]["verdict"],
                             "risky")
            with mock.patch.object(state, "load", return_value={
                    "https://x/1": {"title": "A"}, "https://x/2": {"title": "B"}}):
                rows = {r["url"]: r["evaluation"] for r in state.jobs_payload()["jobs"]}
            self.assertEqual(rows, {"https://x/1": "risky", "https://x/2": ""})

    def test_start_refuses_unknown_and_postingless_jobs(self):
        with mock.patch.object(state, "load", return_value={}):
            self.assertEqual(fit_eval.start({"url": "https://x/none"})[0], 404)
        with mock.patch.object(state, "load", return_value={"https://x/1": {"title": "A"}}), \
                mock.patch.object(fit_eval.postings, "load", return_value="short"):
            code, body = fit_eval.start({"url": "https://x/1"})
        self.assertEqual(code, 409)
        self.assertIn("No posting text", body["error"])

    def test_codex_pass_disables_web_search(self):
        execution = {"binary": "codex", "model": None}
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            line = json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": json.dumps({"verdict": "strong"})}})
            return mock.Mock(stdout=line + "\n", stderr="", returncode=0)

        with mock.patch.object(fit_eval.providers, "preflight"), \
                mock.patch.object(fit_eval.subprocess, "run", side_effect=fake_run):
            result, cost = fit_eval._run_codex(execution, "prompt")
        self.assertEqual(result, {"verdict": "strong"})
        self.assertIn('web_search="disabled"', seen["argv"])
        self.assertNotIn('web_search="live"', seen["argv"])

    def _board(self, seen):
        """Patch the board state onto an in-memory dict."""
        store = {"seen": seen}
        return [mock.patch.object(state, "load", side_effect=lambda: store["seen"]),
                mock.patch.object(state, "save", side_effect=lambda s, why="": store.update(seen=s)),
                mock.patch.object(fit_eval.jobs_md, "board_lock", return_value=mock.MagicMock())]

    def _apply(self, entry, result):
        seen = {"u": dict(entry)}
        patches = self._board(seen)
        for p in patches:
            p.start()
        try:
            fit_eval.apply_to_board("u", result)
        finally:
            for p in patches:
                p.stop()
        return seen["u"]

    def test_verdicts_move_the_band_and_keep_keyword_order(self):
        strong = self._apply({"fit": "high", "fit_score": 80, "fit_source": "deterministic"},
                             {"verdict": "strong"})
        self.assertEqual((strong["fit"], strong["fit_source"], strong["rank_score"]),
                         ("high", "ranked", 98.0))
        risky = self._apply({"fit": "high", "fit_score": 100}, {"verdict": "risky"})
        self.assertEqual((risky["fit"], risky["rank_score"]), ("medium", 74.0))
        self.assertLessEqual(fit_eval.jobs_md.priority_score(risky), 74)

    def test_failed_gate_files_unreviewed_rows_only(self):
        gate = {"verdict": "not_recommended", "hard_requirements": [
            {"requirement": "NATO nationality", "status": "not_met", "quote": "", "note": ""}]}
        row = self._apply({"fit": "high", "fit_score": 90, "user_status": "backlog"}, gate)
        self.assertEqual((row["fit"], row["user_status"]), ("low", "gate"))
        self.assertIn("NATO nationality", row["note"])
        mine = self._apply({"fit": "high", "fit_score": 90, "user_status": "yes"}, gate)
        self.assertEqual(mine["user_status"], "yes")
        cleared = self._apply(row, {"verdict": "worth"})
        self.assertEqual((cleared["user_status"], cleared["note"], cleared["fit"]),
                         ("backlog", "", "high"))
        manual = self._apply({"user_status": "gate", "note": "German required"}, {"verdict": "worth"})
        self.assertEqual(manual["user_status"], "gate")

    def test_auto_queue_takes_unjudged_keyword_high_rows(self):
        seen = {
            "a": {"fit": "high", "fit_source": "deterministic", "posting_chars": 900,
                  "fit_priority_score": 85, "user_status": "backlog"},
            "b": {"fit": "high", "fit_source": "deterministic", "posting_chars": 900,
                  "fit_priority_score": 95, "user_status": "new"},
            "ranked": {"fit": "high", "fit_source": "ranked", "posting_chars": 900},
            "medium": {"fit": "medium", "fit_source": "deterministic", "posting_chars": 900},
            "no-text": {"fit": "high", "fit_source": "deterministic", "posting_chars": 0},
            "applied": {"fit": "high", "fit_source": "deterministic", "posting_chars": 900,
                        "user_status": "applied"},
            "done": {"fit": "high", "fit_source": "deterministic", "posting_chars": 900},
        }
        with mock.patch.object(fit_eval, "load_all", return_value={"done": {"prompt_version": fit_eval.PROMPT_VERSION}}):
            self.assertEqual(fit_eval.auto_candidates(seen), ["b", "a"])

    def test_usage_limit_pauses_the_queue_instead_of_failing_rows(self):
        self.addCleanup(fit_eval.retry_failed)
        with mock.patch.object(fit_eval, "_run_claude",
                               side_effect=RuntimeError("You've hit your session limit · resets 3am")):
            fit_eval._worker("https://x/limit", {}, "posting", "claude", {})
        self.assertNotIn("https://x/limit", fit_eval.ERRORS)
        self.assertIsNotNone(fit_eval.paused())
        with mock.patch.object(fit_eval, "auto_enabled", return_value=True), \
                mock.patch.object(fit_eval, "start") as start:
            self.assertEqual(fit_eval.auto_tick(), 0)
        start.assert_not_called()
        fit_eval.ERRORS["https://x/other"] = "boom"
        self.assertEqual(fit_eval.retry_failed(), 1)
        self.assertIsNone(fit_eval.paused())
        self.assertEqual(fit_eval.ERRORS, {})

    def test_older_prompt_results_are_requeued_first(self):
        seen = {"old": {"fit": "high", "fit_source": "ranked", "user_status": "backlog"},
                "applied": {"fit": "high", "fit_source": "ranked", "user_status": "applied"},
                "fresh": {"fit": "high", "fit_source": "deterministic", "posting_chars": 900,
                          "user_status": "new"}}
        store = {"old": {"result": {}}, "applied": {"result": {}},
                 "current": {"result": {}, "prompt_version": fit_eval.PROMPT_VERSION}}
        with mock.patch.object(fit_eval, "load_all", return_value=store):
            self.assertEqual(fit_eval.auto_candidates(seen), ["old", "fresh"])

    def test_scheduled_start_holds_the_queue_until_its_time(self):
        future = {"auto_evaluate": True, "auto_evaluate_after": "2999-01-01T00:00:00"}
        with mock.patch.object(fit_eval.run_registry, "config", return_value=future), \
                mock.patch.object(fit_eval, "start") as start:
            self.assertIsNotNone(fit_eval.scheduled_after())
            self.assertEqual(fit_eval.auto_tick(), 0)
        start.assert_not_called()
        past = {"auto_evaluate": True, "auto_evaluate_after": "2000-01-01T00:00:00"}
        with mock.patch.object(fit_eval.run_registry, "config", return_value=past):
            self.assertIsNone(fit_eval.scheduled_after())

    def test_auto_tick_is_off_unless_configured(self):
        with mock.patch.object(fit_eval, "auto_enabled", return_value=False), \
                mock.patch.object(fit_eval, "start") as start:
            self.assertEqual(fit_eval.auto_tick(), 0)
        start.assert_not_called()

    def test_prompt_protects_visa_and_seniority(self):
        self.assertIn("an `unclear` row never lowers the verdict", fit_eval.INSTRUCTIONS)
        self.assertIn("Work permits matter only for Switzerland", fit_eval.INSTRUCTIONS)
        self.assertIn("NEVER\nrestate the candidate's", fit_eval.INSTRUCTIONS)
        self.assertIn("A seniority gap alone", fit_eval.INSTRUCTIONS)

    def test_routes_are_token_gated(self):
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn('"/api/evaluate",', source)
        self.assertIn("fit_eval.start(payload)", source)


if __name__ == "__main__":
    unittest.main()
