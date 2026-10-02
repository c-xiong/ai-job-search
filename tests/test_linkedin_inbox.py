"""Anonymous local inbox scenarios; guest HTTP and personal state are never used."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import linkedin_inbox as inbox

BODY = ("JOBFLOW-ANONYMOUS-FIXTURE\nAbout the role\nBuild example Python services.\n"
        "Requirements\nExperience writing software and fluent English are required.\n") * 5


def card(job_id="4000000001", **fields):
    return dict(job_id=job_id, title="Software Engineer", company="Invented Example Works",
                location="Berlin, Germany", posted="2026-01-01", **fields)


class LinkedInInboxTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch(inbox, "INBOX", self.root / "linkedin_inbox.json")
        self.patch(inbox.jobs_md, "SEEN", self.root / "seen_jobs.json")
        self.patch(inbox.importer.ats_fetch, "load_registry", lambda: {"companies": []})
        self.patch(inbox.importer.ats_fetch, "scoring_context", lambda *args: None)
        self.calls = []

        def detail(job_id):
            self.calls.append(job_id)
            return {"title": "Software Engineer", "company": "Invented Example Works",
                    "location": "Berlin, Germany", "description": BODY}

        self.patch(inbox, "resolve_detail", detail)

    def patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seen(self):
        return inbox.importer._load_seen()

    def test_capture_accepts_sparse_cards_and_preserves_metadata(self):
        first = inbox.capture([{"job_id": "4000000001"}], origin="saved-jobs")
        second = inbox.capture([card(badges=["Promoted", "Easy Apply"],
                                    posted_date="2026-01-01T12:00:00Z")], origin="bookmarklet")
        self.assertEqual((first["added"], second["added"], second["updated"]), (1, 0, 1))
        item = inbox.listing()["items"][0]
        self.assertEqual(item["location"], "Berlin, Germany")
        self.assertEqual(item["posted"], "2026-01-01")
        self.assertEqual(item["posted_date"], "2026-01-01T12:00:00Z")
        self.assertEqual(item["badges"], ["Promoted", "Easy Apply"])
        self.assertEqual(item["origins"], ["saved-jobs", "bookmarklet"])
        self.assertFalse(inbox.jobs_md.SEEN.exists())

    def test_capture_idempotence_and_entire_batch_validation(self):
        result = inbox.capture([card(), card()])
        self.assertEqual((result["added"], result["duplicates"]), (1, 1))
        before = inbox.INBOX.read_bytes()
        for payload in ([card("4000000002"), {"job_id": "bad"}],
                        [{"linkedin_url": "https://example.com/jobs/4000000001"}],
                        [{"job_id": "4000000001", "url": "https://linkedin.com/jobs/view/4000000002"}],
                        [{"job_id": "4000000001", "title": "x" * 501}],
                        [card()] * (inbox.MAX_CARDS + 1)):
            with self.subTest(payload_type=type(payload)), self.assertRaises(inbox.InputError):
                inbox.capture(payload)
        self.assertEqual(inbox.INBOX.read_bytes(), before)

    def test_payload_size_is_bounded(self):
        with self.assertRaises(inbox.InputError):
            inbox.capture([{"job_id": "4000000001", "description": "x" * inbox.MAX_BYTES}])
        self.assertFalse(inbox.INBOX.exists())

    def test_corrupt_state_is_reported_and_never_overwritten(self):
        inbox.INBOX.write_text("not valid JSON", encoding="utf-8")
        for function in (inbox.listing, lambda: inbox.capture([card()]), inbox.process):
            with self.assertRaises(inbox.InputError):
                function()
        self.assertEqual(inbox.INBOX.read_text(encoding="utf-8"), "not valid JSON")

    def test_invalid_stored_cards_are_reported_without_overwrite(self):
        for item in ([], None, {"state": "pending"}, {"job_id": "4000000001", "state": "unknown"}):
            raw = json.dumps({"items": {"4000000001": item}})
            inbox.INBOX.write_text(raw, encoding="utf-8")
            with self.subTest(item=item):
                for function in (inbox.listing, lambda: inbox.capture([card()]), inbox.process):
                    with self.assertRaises(inbox.InputError):
                        function()
                self.assertEqual(inbox.INBOX.read_text(encoding="utf-8"), raw)

    def test_capture_dry_run_writes_nothing(self):
        result = inbox.capture([card()], dry_run=True)
        self.assertEqual(result["added"], 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sparse_capture_hydrates_and_persists(self):
        inbox.capture([{"job_id": "4000000001"}])
        result = inbox.process()
        self.assertEqual((result["imported"], result["detail_calls"]), (1, 1))
        item = inbox.listing()["items"][0]
        self.assertEqual(item["state"], "imported")
        self.assertEqual(item["company"], "Invented Example Works")
        self.assertTrue(inbox.postings.load(next(iter(self.seen().values()))))

    def test_known_id_skips_network_but_missing_body_is_hydrated(self):
        inbox.jobs_md.save_seen({"https://www.linkedin.com/jobs/view/4000000001": {
            "title": "Software Engineer", "company": "Invented Example Works",
            "url": "https://www.linkedin.com/jobs/view/4000000001", "user_status": "yes",
            "user_note": "Anonymous test note", "rank_score": 83}})
        inbox.capture([card()])
        first = inbox.process()
        self.assertEqual(first["detail_calls"], 1)
        entry = next(iter(self.seen().values()))
        self.assertEqual((entry["user_status"], entry["rank_score"]), ("yes", 83))
        data = inbox._load()
        data["items"]["4000000001"]["state"] = "pending"
        inbox._save(data)
        second = inbox.process(limit=0)
        self.assertEqual((second["known"], second["detail_calls"], second["added"]), (1, 0, 0))

    def test_likely_duplicate_is_not_dropped(self):
        inbox.jobs_md.save_seen({"https://careers.example.com/jobs/one": {
            "title": "Software Engineer", "company": "Invented Example Works"}})
        inbox.capture([card()])
        item = inbox.listing()["items"][0]
        self.assertEqual(item["dedup_status"], "likely_known")
        self.assertIn("possible_duplicate", item)
        self.assertEqual(inbox.process()["detail_calls"], 1)

    def test_detail_budget_leaves_remainder_pending(self):
        inbox.capture([card("4000000001"), card("4000000002")])
        result = inbox.process(limit=1)
        self.assertEqual((result["detail_calls"], result["counts"]["pending"]), (1, 1))
        for limit in (-1, 21, True, "5"):
            with self.assertRaises(inbox.InputError):
                inbox.process(limit=limit)

    def test_transient_failure_retries_without_creating_board_row(self):
        inbox.capture([card()])
        with patch.object(inbox, "resolve_detail", side_effect=inbox.add_job.LinkedInDetailError("temporary")):
            result = inbox.process()
        self.assertEqual((result["retry"], result["imported"]), (1, 0))
        item = inbox.listing()["items"][0]
        self.assertTrue(item["next_retry_at"])
        self.assertEqual(inbox.process()["detail_calls"], 0)
        self.assertFalse(inbox.jobs_md.SEEN.exists())

    def test_rate_limit_stops_without_claiming_other_cards(self):
        inbox.capture([card("4000000001"), card("4000000002")])
        error = inbox.add_job.LinkedInDetailError("rate limited", rate_limited=True)
        with patch.object(inbox, "resolve_detail", side_effect=error):
            result = inbox.process()
        self.assertTrue(result["rate_limited"])
        self.assertEqual((result["detail_calls"], result["counts"]["pending"]), (1, 1))

    def test_missing_body_requires_manual_review(self):
        inbox.capture([card()])
        with patch.object(inbox, "resolve_detail", return_value={"title": "Software Engineer"}):
            result = inbox.process()
        self.assertEqual(result["needs_manual"], 1)
        self.assertFalse(inbox.jobs_md.SEEN.exists())

    def test_interrupted_processing_and_persistence_failure_recover(self):
        inbox.capture([card()])
        data = inbox._load()
        data["items"]["4000000001"].update(state="processing", attempts=1)
        inbox._save(data)
        with patch.object(inbox.importer, "persist", side_effect=OSError("fixture failure")):
            result = inbox.process()
        self.assertEqual(result["retry"], 1)
        self.assertFalse(inbox.jobs_md.SEEN.exists())
        data = inbox._load()
        data["items"]["4000000001"]["next_retry_at"] = None
        inbox._save(data)
        self.assertEqual(inbox.process()["imported"], 1)
        self.assertEqual(len(self.seen()), 1)

    def test_replay_after_board_commit_preserves_status_and_is_idempotent(self):
        inbox.capture([card()])
        inbox.process()
        seen = self.seen()
        next(iter(seen.values()))["user_status"] = "applied"
        inbox.jobs_md.save_seen(seen)
        data = inbox._load()
        data["items"]["4000000001"]["state"] = "processing"
        inbox._save(data)
        result = inbox.process()
        self.assertEqual((result["known"], result["detail_calls"]), (1, 0))
        self.assertEqual(next(iter(self.seen().values()))["user_status"], "applied")

    def test_crash_after_board_commit_before_inbox_completion_replays_safely(self):
        inbox.capture([card()])
        save = inbox._save
        calls = []
        def interrupted_save(data):
            calls.append(1)
            if len(calls) == 3:
                raise OSError("anonymous interrupted inbox completion")
            save(data)
        with patch.object(inbox, "_save", side_effect=interrupted_save):
            with self.assertRaises(OSError):
                inbox.process()
        self.assertEqual(inbox.listing()["items"][0]["state"], "processing")
        self.assertEqual(len(self.seen()), 1)
        replay = inbox.process()
        self.assertEqual((replay["known"], replay["added"], replay["detail_calls"]), (1, 0, 0))

    def test_capture_and_user_updates_during_network_are_preserved(self):
        inbox.capture([card()])
        def concurrent_detail(job_id):
            inbox.capture([card(badges=["Easy Apply"])], origin="email")
            inbox.jobs_md.save_seen({"https://careers.example.com/jobs/another": {
                "title": "Another Role", "company": "Invented Other Works", "user_status": "no"}})
            return {"title": "Software Engineer", "company": "Invented Example Works", "description": BODY}
        with patch.object(inbox, "resolve_detail", side_effect=concurrent_detail):
            inbox.process()
        item = inbox.listing()["items"][0]
        self.assertEqual(item["badges"], ["Easy Apply"])
        self.assertIn("email", item["origins"])
        self.assertEqual(self.seen()["https://careers.example.com/jobs/another"]["user_status"], "no")

    def test_external_apply_url_is_preferred_with_linkedin_provenance(self):
        inbox.capture([card()])
        external = "https://careers.example.com/apply?jobId=42&utm_source=linkedin"
        with patch.object(inbox, "resolve_detail", return_value={
                "title": "Software Engineer", "company": "Invented Example Works",
                "description": BODY, "apply_url": external}):
            inbox.process()
        key = "https://careers.example.com/apply?jobId=42"
        entry = self.seen()[key]
        self.assertIn("https://www.linkedin.com/jobs/view/4000000001",
                      [source["url"] for source in entry["sources"]])

    def test_title_language_and_seniority_are_not_hard_gates(self):
        inbox.capture([{"job_id": "4000000001", "title": "Senior Software Engineer",
                        "company": "Invented Example Works"}])
        self.assertEqual(inbox.process()["imported"], 1)

    def test_hard_german_requirement_records_exclusion(self):
        inbox.capture([card()])
        with patch.object(inbox, "resolve_detail", return_value={
                "title": "Software Engineer", "company": "Invented Example Works",
                "description": BODY + "\nGerman is required."}):
            result = inbox.process()
        self.assertEqual(result["excluded"], 1)
        self.assertEqual(next(iter(self.seen().values()))["user_status"], "gate")

    def test_process_dry_run_has_no_state_or_sidecar_changes(self):
        inbox.capture([card()])
        before = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        result = inbox.process(dry_run=True)
        self.assertEqual(result["imported"], 1)
        after = {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / "postings").exists())

    def test_only_one_processor_can_run(self):
        with inbox._locked(process=True):
            with self.assertRaises(inbox.AlreadyProcessing):
                inbox.process()


if __name__ == "__main__":
    unittest.main()
