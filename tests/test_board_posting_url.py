"""Owner-selected posting links preserve row identity and source deduplication."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.board import activity, add_job, server, state

jobs_md = server.jobs_md
ats_fetch = server.ats_fetch
importer = add_job.importer
FREEHIRE = "https://freehire.me/jobs/example-engineer"
ASHBY = "https://jobs.ashbyhq.com/example/0a1b2c3d-0000-4000-8000-000000000001"
APPLY = ASHBY + "/application?utm_source=freehire#application"
BODY = ("About the role\nBuild retrieval pipelines and agent tooling in Python.\n\n"
        "Requirements\n- 2+ years of Python and PyTorch\n"
        "- Strong software engineering fundamentals\n- Fluent English\n") * 3


def entry():
    return {"url": FREEHIRE, "title": "AI Engineer", "company": "Example",
            "location": "Berlin", "first_seen": "2026-09-28", "portal": "freehire",
            "user_status": "yes", "user_note": "Ask about the team", "rank_score": 83,
            "posting_path": "postings/original.txt", "posting_excerpt": "Saved description"}


class PostingUrlTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target, attribute, value in (
                (jobs_md, "SEEN", self.root / "seen_jobs.json"),
                (activity, "LOG", self.root / "activity.jsonl"),
                (add_job.postings, "ROOT", self.root),
                (ats_fetch, "REGISTRY", self.root / "companies.json")):
            patch = mock.patch.object(target, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(ats_fetch, "scoring_context", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        self.write({FREEHIRE: entry()})

    def write(self, seen):
        with jobs_md.board_lock():
            jobs_md.save_seen(seen)

    def post(self, payload, token=server.TOKEN):
        # Execute the actual token-protected HTTP handler with an in-memory
        # request. No localhost socket or the owner's running board is needed.
        handler = object.__new__(server.Handler)
        handler.path = "/api/update" + ("?t=" + token if token else "")
        body = json.dumps(payload).encode()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        replies = []
        handler._send = lambda status, data: replies.append((status, json.loads(data)))
        handler.do_POST()
        return replies[0]

    def change(self, value=APPLY, **extra):
        return self.post({"url": FREEHIRE, "posting_url": value, **extra})

    def test_changing_url_persists_exact_destination_and_preserves_job(self):
        original = entry()
        status, result = self.change()
        self.assertEqual((status, result), (200, {
            "ok": True, "open_url": APPLY, "posting_url": APPLY}))
        seen = state.load()
        self.assertEqual(list(seen), [FREEHIRE])
        saved = seen[FREEHIRE]
        self.assertEqual({field: saved[field] for field in original}, original)
        self.assertEqual(saved["user_posting_url"], APPLY)
        self.assertEqual([source["url"] for source in saved["sources"]], [FREEHIRE, APPLY])
        row = state.jobs_payload()["jobs"][0]
        self.assertEqual((row["url"], row["open_url"], row["posting_url"],
                          row["default_open_url"]), (FREEHIRE, APPLY, APPLY, FREEHIRE))
        self.assertEqual(row["known_urls"], [FREEHIRE, APPLY])

    def test_reset_restores_automatic_primary_and_keeps_edited_aliases(self):
        saved = entry()
        saved.update(primary_source="ats-search", sources=[
            {"portal": "freehire", "url": FREEHIRE},
            {"portal": "ats-search", "url": ASHBY}])
        self.write({FREEHIRE: saved})
        company = "https://careers.example.com/jobs/42?application=1"
        self.assertEqual(self.change(company)[0], 200)
        self.assertEqual(state.jobs_payload()["jobs"][0]["default_open_url"], ASHBY)
        self.assertEqual(self.change("")[1], {"ok": True, "open_url": ASHBY, "posting_url": ""})
        saved = state.load()[FREEHIRE]
        self.assertNotIn("user_posting_url", saved)
        self.assertEqual(jobs_md.entry_key({FREEHIRE: saved}, company), FREEHIRE)
        self.assertEqual(jobs_md.entry_key({FREEHIRE: saved}, ASHBY), FREEHIRE)

    def test_repeated_edit_does_not_duplicate_source_records(self):
        self.assertEqual(self.change()[0], 200)
        self.assertEqual(self.change()[0], 200)
        self.assertEqual(len(state.load()[FREEHIRE]["sources"]), 2)

    def test_invalid_values_never_save_other_changes(self):
        original = jobs_md.SEEN.read_bytes()
        invalid = [None, 42, [], {}, "ftp://example.com/jobs/1", "javascript:alert(1)",
                   "//example.com/jobs/1", "https:///jobs/1", "https://",
                   "https://example.com/a b", "https://example.com/\njob",
                   " https://example.com/job", "https://user:pass@example.com/job",
                   "https://example.com:bad/job", "https://example.com:99999/job",
                   "https://example.com:/job", "https://[invalid]/job",
                   "https://[::1]suffix/job", "https://[::1].example.com/job",
                   "https://example.com\\@other.com/job", "https://example%2ecom/job"]
        for value in invalid:
            with self.subTest(value=value):
                status, body = self.change(value, status="no", note="should not save")
                self.assertEqual(status, 400)
                self.assertIn("Posting URL", body["error"])
                self.assertEqual(jobs_md.SEEN.read_bytes(), original)

    def test_valid_absolute_web_urls_include_ports_ipv6_and_idn(self):
        for value in ("http://example.com:8080/jobs/1", "https://[2001:db8::1]/jobs/1",
                      "https://bücher.example/jobs/1", "https://example.com/jobs/1?q=a%20b"):
            with self.subTest(value=value):
                self.assertEqual(self.change(value)[0], 200)

    def test_auth_unknown_job_and_conflicting_url_are_rejected(self):
        original = jobs_md.SEEN.read_bytes()
        self.assertEqual(self.post({"url": FREEHIRE, "posting_url": APPLY}, token=None)[0], 403)
        self.assertEqual(self.post({"url": "missing", "posting_url": APPLY})[0], 404)
        self.assertEqual(jobs_md.SEEN.read_bytes(), original)
        another = copy.deepcopy(entry())
        another["url"] = ASHBY
        self.write({FREEHIRE: entry(), ASHBY: another})
        status, result = self.change()
        self.assertEqual(status, 409)
        self.assertIn("another job", result["error"])
        self.assertNotIn("user_posting_url", state.load()[FREEHIRE])

    def test_original_and_new_urls_share_identity_for_imports(self):
        self.change()
        seen = state.load()
        for link in (FREEHIRE, APPLY, ASHBY + "?utm_source=linkedin"):
            with self.subTest(link=link):
                self.assertEqual(add_job._entry_key(seen, link), FREEHIRE)
                self.assertIs(importer._entry_for_source(seen, link), seen[FREEHIRE])
        self.assertIn("ashby:example:0a1b2c3d-0000-4000-8000-000000000001",
                      ats_fetch.known_ids_from(seen))

    def test_ats_fetch_recognizes_saved_url_without_fuzzy_title_match(self):
        self.change()
        seen = state.load()
        original = copy.deepcopy(seen[FREEHIRE])
        incoming = {"url": ASHBY, "title": "Different vendor spelling", "company": "Example",
                    "location": "Berlin", "posted": "2026-10-01"}
        result = ats_fetch.merge(seen, [incoming], "2026-10-01", lambda _: None)
        self.assertEqual((result["added"], result["already_known"], len(seen)), (0, 1, 1))
        for field in ("url", "title", "user_status", "user_note", "user_posting_url", "rank_score"):
            self.assertEqual(seen[FREEHIRE][field], original[field])
        self.assertEqual(jobs_md.primary_url(seen[FREEHIRE]), APPLY)

    def test_add_job_with_saved_company_url_enriches_the_existing_row(self):
        self.change()
        found = {"title": "Different vendor spelling", "company": "Example", "location": "Berlin",
                 "description": BODY, "url": ASHBY}
        with mock.patch.object(add_job, "resolve_ats", return_value=found), \
                mock.patch.object(add_job, "resolve_page", return_value={}):
            status, result = add_job.add({"job_url": APPLY}, today="2026-10-01")
        self.assertEqual((status, result["outcome"], result["url"]), (200, "already_known", FREEHIRE))
        seen = state.load()
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[FREEHIRE]["user_posting_url"], seen[FREEHIRE]["user_status"]),
                         (APPLY, "yes"))

    def test_mark_applied_matches_a_previous_preferred_url(self):
        saved = entry()
        saved.update(primary_source="ats-search", sources=[{"portal": "ats-search", "url": ASHBY}])
        self.write({FREEHIRE: saved})
        self.change("https://careers.example.com/jobs/42")
        self.assertEqual(server.mark_board_applied(ASHBY), "updated")
        saved = state.load()[FREEHIRE]
        self.assertEqual((saved["user_status"], saved["user_posting_url"]),
                         ("applied", "https://careers.example.com/jobs/42"))

    def test_run_lookup_retains_former_source_and_current_override_aliases(self):
        saved = entry()
        saved.update(primary_source="ats-search", sources=[{"portal": "ats-search", "url": ASHBY}])
        self.write({FREEHIRE: saved})
        self.change("https://careers.example.com/jobs/42")
        supervisor = object.__new__(server.runs.Supervisor)
        for link in (FREEHIRE, ASHBY, "https://careers.example.com/jobs/42"):
            with self.subTest(link=link):
                self.assertEqual(supervisor._board_entry(link)["url"], FREEHIRE)

    def test_missing_description_fetch_uses_owner_url_without_changing_run_identity(self):
        saved = entry()
        saved.update(primary_source="ats-search", sources=[{"portal": "ats-search", "url": ASHBY}])
        self.write({FREEHIRE: saved})
        company = "https://careers.example.com/jobs/42"
        self.change(company)
        supervisor = object.__new__(server.runs.Supervisor)
        record = {"id": "test-run", "job_url": ASHBY}
        original = copy.deepcopy(record)
        manifest = {"inputs": {}}
        with mock.patch.object(server.runs.checkpoint, "run_path",
                               side_effect=lambda run_id, name: self.root / name), \
                mock.patch.object(server.runs.checkpoint, "rel", side_effect=lambda path: path.name), \
                mock.patch.object(server.runs.checkpoint, "save"), \
                mock.patch.object(server.runs, "fetch_posting_text", return_value=(BODY, None)) as fetch:
            supervisor._stage_prepare(record, manifest, {})
        fetch.assert_called_once_with(company)
        self.assertEqual(record, original)
        self.assertEqual((self.root / "posting.md").read_text(), BODY.strip() + "\n")
        self.assertEqual(manifest["inputs"]["posting"]["origin"], "a direct fetch by the supervisor")


if __name__ == "__main__":
    unittest.main()
