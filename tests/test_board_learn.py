"""Rules that learn (tools/board/learn.py): misses, note proposals, preview, accept."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import job_filters  # noqa: E402
from board import learn  # noqa: E402

BODY = ("We build AI products. Requirements: Python, LLMs. "
        "Applicants must hold a valid Swiss passport to be considered for this role.")


class LearnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [mock.patch.object(learn, "STORE", tmp / "rule_suggestions.json"),
                        mock.patch.object(learn, "LESSONS", tmp / "review_lessons.md"),
                        mock.patch.object(learn, "_body", lambda entry: entry.get("_body", "")),
                        mock.patch.object(learn.activity, "emit", lambda *a, **k: None)]
        for p in self.patches:
            p.start()
        self.seen = {
            "u1": {"title": "AI Engineer", "company": "Acme", "user_status": "yes", "_body": BODY,
                   "user_note": 'Needs a Swiss passport: "must hold a valid Swiss passport"'},
            "u2": {"title": "ML Engineer", "company": "Beta", "user_status": "backlog", "fit": "high",
                   "_body": BODY},
            "u3": {"title": "Werkstudent KI", "company": "Gamma", "user_status": "new", "_body": "x"},
        }

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_report_miss_gates_and_suggests_a_verbatim_body_rule(self):
        result, error = learn.report_miss(self.seen, "u1", {
            "reason": "citizenship", "quote": "must hold a valid Swiss passport"})
        self.assertIsNone(error)
        self.assertEqual(self.seen["u1"]["user_status"], "gate")
        self.assertEqual(result["suggestion"]["kind"], "body")
        hits = learn.preview(result["suggestion"], self.seen)
        self.assertEqual([h["url"] for h in hits], ["u2"])

    def test_report_miss_keeps_text_not_in_the_posting_as_an_explanation(self):
        result, error = learn.report_miss(self.seen, "u1", {"reason": "other",
                                                            "quote": "this role is based in Seoul"})
        self.assertIsNone(error)
        self.assertTrue(result["explained"])
        self.assertIsNone(result["suggestion"])
        report = self.seen["u1"]["miss_reports"][-1]
        self.assertEqual((report["quote"], report["explanation"]), ("", "this role is based in Seoul"))
        self.assertEqual(self.seen["u1"]["user_status"], "gate")
        rows = learn._note_rows(self.seen, learn._load())
        self.assertIn("owner explains: this role is based in Seoul", learn.build_prompt(rows, {}))
        _, error = learn.report_miss(self.seen, "u1", {"reason": "nope"})
        self.assertIsNotNone(error)

    def test_model_proposals_are_verified_against_cited_postings(self):
        data = learn._load()
        rows = [(url, entry, entry.get("user_note", ""), [], "d") for url, entry in self.seen.items()]
        added = learn.accept_proposals(data, rows, {"suggestions": [
            {"kind": "body", "value": "must hold a valid Swiss passport", "label": "Citizenship",
             "why": "note on job 1", "job_ids": [1]},
            {"kind": "body", "value": "EU citizenship is strictly required", "label": "Invented",
             "why": "", "job_ids": [1]},
            {"kind": "title", "value": "Werkstudent", "label": "Working student", "why": "",
             "job_ids": [3]},
            {"kind": "title", "value": "Senior", "label": "Not in title", "why": "", "job_ids": [1]},
            {"kind": "lesson", "value": "Treat a passport requirement as citizenship.",
             "label": "Passport", "why": "", "job_ids": [1]},
            {"kind": "body", "value": "must hold a valid Swiss passport", "label": "No cite",
             "why": "", "job_ids": [99]},
        ]})
        self.assertEqual(added, 3)
        kinds = sorted(s["kind"] for s in data["suggestions"])
        self.assertEqual(kinds, ["body", "lesson", "title"])

    def test_rejected_suggestions_are_not_proposed_again(self):
        data = learn._load()
        first = learn._add(data, "title", "Werkstudent", "WS", "", [], "notes")
        first["status"] = "rejected"
        self.assertIsNone(learn._add(data, "title", "werkstudent", "WS", "", [], "notes"))

    def test_note_rows_skip_what_was_already_learned(self):
        data = learn._load()
        rows = learn._note_rows(self.seen, data)
        self.assertEqual([r[0] for r in rows], ["u1"])
        data["learned"]["u1"] = rows[0][4]
        self.assertEqual(learn._note_rows(self.seen, data), [])

    def test_custom_rules_feed_the_filters(self):
        opts = job_filters.settings({"filters": {
            "custom_title": [{"word": "Werkstudent", "label": "WS"}],
            "custom_body": [{"phrase": "must hold a valid Swiss passport", "label": "Passport"}]}})
        self.assertEqual(job_filters.title_hit("Werkstudent KI", opts)[0], "custom")
        self.assertEqual(job_filters.body_hit(BODY, opts)[1], "Passport")

    def test_accept_writes_the_rule_and_gates_open_rows(self):
        data = learn._load()
        suggestion = learn._add(data, "body", "must hold a valid Swiss passport", "Passport",
                                "", [], "miss")
        learn._save(data)
        config = Path(self.tmp.name) / "scrape_config.json"
        config.write_text(json.dumps({"jobage_days": 7}))
        saved = {}

        class FakeState:
            @staticmethod
            def load():
                return self.seen

            @staticmethod
            def save(seen, why=""):
                saved["why"] = why

        import fetch_jobs
        with mock.patch.object(learn, "state", FakeState), \
                mock.patch.object(learn.search_prefs, "CONFIG", config), \
                mock.patch.object(fetch_jobs, "load_config", lambda: json.loads(config.read_text())), \
                mock.patch.object(learn.jobs_md, "board_lock", mock.MagicMock()):
            code, body = learn.decide(suggestion["id"], True)
        self.assertEqual(code, 200)
        self.assertEqual(body["gated"], 1)
        self.assertEqual(self.seen["u2"]["user_status"], "gate")
        rules = json.loads(config.read_text())["filters"]["custom_body"]
        self.assertEqual(rules[0]["phrase"], "must hold a valid Swiss passport")
        self.assertEqual(json.loads(config.read_text())["jobage_days"], 7)

    def test_auto_waits_for_notes_to_go_quiet_and_spaces_passes(self):
        from datetime import datetime, timedelta
        started = []

        class FakeState:
            @staticmethod
            def load():
                return self.seen

        t0 = datetime(2026, 10, 10, 12, 0)
        learn.AUTO.update(sig=None, since=None, last_start=None, backoff_until=None)
        with mock.patch.object(learn, "state", FakeState), \
                mock.patch.object(learn, "auto_enabled", lambda: True), \
                mock.patch.object(learn, "start_learning",
                                  lambda payload: (started.append(payload), (202, {}))[1]):
            self.assertFalse(learn.auto_tick(t0))                          # first sight
            self.assertFalse(learn.auto_tick(t0 + timedelta(minutes=5)))   # still settling
            self.seen["u2"]["user_note"] = "another note"                  # a change restarts the wait
            self.assertFalse(learn.auto_tick(t0 + timedelta(minutes=9)))
            self.assertFalse(learn.auto_tick(t0 + timedelta(minutes=15)))
            self.assertTrue(learn.auto_tick(t0 + timedelta(minutes=20)))
            self.assertEqual(started, [{"auto": True}])
            learn.AUTO["last_start"] = t0 + timedelta(minutes=20)
            self.assertFalse(learn.auto_tick(t0 + timedelta(minutes=35)))  # min gap
            self.assertTrue(learn.auto_tick(t0 + timedelta(minutes=55)))

    def test_auto_can_be_switched_off(self):
        with mock.patch.object(learn.run_registry, "config", lambda: {"auto_learn": False}):
            self.assertFalse(learn.auto_enabled())


if __name__ == "__main__":
    unittest.main()
