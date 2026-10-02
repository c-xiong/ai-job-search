"""Anonymous, offline tests for optional LinkedIn intake."""

from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import linkedin_intake as intake


def snapshot(rows, next_start=None):
    links = [] if next_start is None else [{"rel": "next", "href": "/rest/memberSnapshotData?q=criteria&domain=SAVED_JOBS&start=%d" % next_start}]
    return {"elements": [{"snapshotDomain": "SAVED_JOBS", "snapshotData": rows}], "paging": {"links": links}}


def saved(ident="4000000001"):
    return {"Job URL": "https://www.linkedin.com/jobs/view/" + ident,
            "Job Title": "Example Engineer", "Company Name": "Example Robotics",
            "Date Saved": "2026-01-02"}


class IntakeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (("STATE", self.root / "state.json"), ("LOCK", self.root / "intake.lock")):
            patcher = patch.object(intake, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.capture = patch.object(intake, "_capture", return_value={"added": 1}).start()
        self.addCleanup(patch.stopall)

    def email(self, ident="4000000001"):
        message = EmailMessage()
        message["From"] = "jobs@example.invalid"
        message["To"] = "reader@example.invalid"
        message.set_content("A job: https://www.linkedin.com/jobs/view/%s?tracking=x" % ident)
        message.add_alternative('<a href="https://www.linkedin.com/comm/jobs/view/%s?tracking=x"><b>Example Engineer</b></a>' % ident, subtype="html")
        return message.as_bytes()

    def test_email_merges_plain_html_and_keeps_descriptive_title(self):
        cards = intake.parse_email(self.email())
        self.assertEqual(cards, [{"job_id": "4000000001", "linkedin_url": "https://www.linkedin.com/jobs/view/4000000001", "title": "Example Engineer"}])

    def test_email_ignores_attachments_and_script_link_text(self):
        message = EmailMessage()
        message.set_content("No jobs here")
        message.add_alternative('<script><a href="https://www.linkedin.com/jobs/view/4000000002">Fake title</a></script><a href="https://www.linkedin.com/jobs/view/4000000001">View job</a>', subtype="html")
        message.add_attachment(b"https://www.linkedin.com/jobs/view/4000000003", maintype="text", subtype="plain", filename="private.txt")
        cards = intake.parse_email(message.as_bytes())
        self.assertNotIn("4000000002", {card["job_id"] for card in cards})
        self.assertNotIn("4000000003", {card["job_id"] for card in cards})
        self.assertTrue(all("title" not in card for card in cards))

    def test_url_reader_rejects_unrelated_hosts_and_credentials(self):
        for url in ("https://linkedin.com.attacker.invalid/jobs/view/4000000001", "file:///jobs/view/4000000001", "https://name:password@linkedin.com/jobs/view/4000000001"):
            self.assertIsNone(intake.job_id(url))
        self.assertEqual(intake.job_id("https://www.linkedin.com/jobs/view/example-engineer-4000000001/"), "4000000001")
        self.assertEqual(intake.job_id("https://www.linkedin.com/jobs/search/?currentJobId=4000000002"), "4000000002")

    def test_email_size_limit(self):
        with self.assertRaises(intake.IntakeError):
            intake.parse_email(b"x" * (intake.MAX_MESSAGE_BYTES + 1))

    def test_attached_email_subtree_is_not_imported(self):
        outer = EmailMessage()
        outer.set_content("No jobs in the visible message")
        outer.add_attachment(BytesParser(policy=policy.default).parsebytes(self.email()), filename="forwarded.eml")
        self.assertEqual(intake.parse_email(outer.as_bytes()), [])

    def test_email_checkpoints_only_after_capture_and_deduplicates_files(self):
        raw = self.email()
        (self.root / "a.eml").write_bytes(raw)
        (self.root / "b.eml").write_bytes(raw)
        spec = {"directory": str(self.root)}
        first = intake.import_emails(spec)
        second = intake.import_emails(spec)
        self.assertEqual((first["messages"], first["cards"], second["cards"]), (1, 1, 0))
        self.assertEqual(self.capture.call_count, 1)

    def test_email_capture_failure_is_retried(self):
        (self.root / "a.eml").write_bytes(self.email())
        self.capture.side_effect = ValueError("invalid inbox")
        self.assertEqual(intake.import_emails({"directory": str(self.root)})["failed"], 1)
        self.assertFalse(intake.STATE.exists())
        self.capture.side_effect = None
        self.assertEqual(intake.import_emails({"directory": str(self.root)})["cards"], 1)

    def test_email_dry_run_does_not_advance_checkpoint(self):
        (self.root / "a.eml").write_bytes(self.email())
        intake.import_emails({"directory": str(self.root)}, dry_run=True)
        self.assertFalse(intake.STATE.exists())
        self.assertTrue(self.capture.call_args.args[1])

    def test_email_budget_and_symlink_refusal(self):
        (self.root / "a.eml").write_bytes(self.email())
        (self.root / "b.eml").write_bytes(self.email("4000000002"))
        (self.root / "c.eml").symlink_to(self.root / "b.eml")
        self.assertEqual(intake.import_emails({"directory": str(self.root), "max_messages": 1})["messages"], 1)
        self.assertEqual(intake.import_emails({"directory": str(self.root), "max_messages": 10})["messages"], 1)

    def test_saved_snapshot_preserves_identity_without_inventing_posted_date(self):
        cards, entries, unparsed = intake.parse_snapshot(snapshot([saved()]))
        self.assertEqual(entries, 1)
        self.assertEqual(unparsed, 0)
        self.assertEqual(cards[0]["company"], "Example Robotics")
        self.assertNotIn("posted", cards[0])

    def test_unparsed_saved_job_page_does_not_advance(self):
        with patch.dict(os.environ, {"LINKEDIN_PORTABILITY_TOKEN": "example-test-token"}), patch.object(intake, "request_snapshot", return_value=(200, snapshot([{"Job Title": "Unknown schema"}], 1))):
            result = intake.sync_saved_jobs({})
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["unparsed"], 1)
            self.assertEqual(json.loads(intake.STATE.read_text())["saved_jobs_next_start"], 0)

    def test_snapshot_rejects_unrelated_domain_and_invalid_shape(self):
        for data in ({}, {"elements": [{"snapshotDomain": "PROFILE", "snapshotData": []}]}, snapshot(["bad"]), {"elements": [{"snapshotDomain": "SAVED_JOBS", "snapshotData": {}}]}):
            with self.subTest(data=data), self.assertRaises(intake.IntakeError):
                intake.parse_snapshot(data)

    def test_saved_sync_is_unconfigured_without_token(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(intake, "request_snapshot") as request:
            self.assertEqual(intake.sync_saved_jobs({})["status"], "unconfigured")
            request.assert_not_called()

    def test_saved_sync_uses_bounded_pagination_and_resumes_next_run(self):
        with patch.dict(os.environ, {"LINKEDIN_PORTABILITY_TOKEN": "example-test-token"}), patch.object(intake, "request_snapshot", side_effect=[(200, snapshot([saved()], 1)), (200, snapshot([saved("4000000002")], 2)), (200, snapshot([]))]) as request:
            first = intake.sync_saved_jobs({"max_pages": 2})
            self.assertTrue(first["deferred"])
            self.assertEqual(json.loads(intake.STATE.read_text())["saved_jobs_next_start"], 2)
            second = intake.sync_saved_jobs({"max_pages": 2})
            self.assertTrue(second["complete"])
            self.assertEqual(request.call_args.args[1], 2)
            self.assertEqual(json.loads(intake.STATE.read_text())["saved_jobs_next_start"], 0)

    def test_saved_api_no_data_terminal_page_completes_scan(self):
        with patch.dict(os.environ, {"LINKEDIN_PORTABILITY_TOKEN": "example-test-token"}), patch.object(intake, "request_snapshot", side_effect=[(200, snapshot([saved()], 1)), (400, {"message": "No data found for this memberId"})]):
            result = intake.sync_saved_jobs({})
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["complete"])

    def test_rate_limit_stops_and_keeps_successful_page_cursor(self):
        with patch.dict(os.environ, {"LINKEDIN_PORTABILITY_TOKEN": "example-test-token"}), patch.object(intake, "request_snapshot", side_effect=[(200, snapshot([saved()], 1)), (429, None)]) as request:
            result = intake.sync_saved_jobs({})
            self.assertEqual(result["status"], "partial")
            self.assertEqual(request.call_count, 2)
            self.assertEqual(json.loads(intake.STATE.read_text())["saved_jobs_next_start"], 1)

    def test_saved_dry_run_does_not_write_state(self):
        with patch.dict(os.environ, {"LINKEDIN_PORTABILITY_TOKEN": "example-test-token"}), patch.object(intake, "request_snapshot", return_value=(200, snapshot([]))):
            intake.sync_saved_jobs({}, dry_run=True)
            self.assertFalse(intake.STATE.exists())

    def test_saved_sync_rejects_external_pagination_without_requesting_it(self):
        data = snapshot([saved()])
        data["paging"]["links"] = [{"rel": "next", "href": "https://attacker.invalid/?start=1"}]
        with patch.dict(os.environ, {"LINKEDIN_PORTABILITY_TOKEN": "example-test-token"}), patch.object(intake, "request_snapshot", return_value=(200, data)) as request:
            with self.assertRaises(intake.IntakeError):
                intake.sync_saved_jobs({})
            self.assertEqual(request.call_count, 1)

    def test_config_failure_does_not_log_secret(self):
        logs = []
        with patch.object(intake, "sync_saved_jobs", side_effect=RuntimeError("sensitive test value")):
            result = intake.ingest_from_config({"linkedin_intake": {"saved_jobs": {"enabled": True}}}, logs.append)
        self.assertEqual(result["saved_jobs"]["status"], "failed")
        self.assertNotIn("sensitive test value", json.dumps(result) + str(logs))

    def test_transport_does_not_follow_redirect_or_leak_token(self):
        class Response:
            status = 302
            def read1(self, _size):
                return b""
            def close(self):
                pass
        class Connection:
            sock = None
            def request(self, method, target, headers):
                self.headers = headers
            def getresponse(self):
                return Response()
            def close(self):
                pass
        connection = Connection()
        with patch.object(intake.fetch_url, "check_url", return_value=(intake.urlsplit(intake.API + "?x=1"), ["93.184.216.34"])), patch.object(intake.fetch_url, "open_connection", return_value=connection), patch.object(intake.fetch_url, "_retime"):
            code, data = intake.request_snapshot("example-test-token", 0, 10)
        self.assertEqual((code, data), (302, None))
        self.assertEqual(connection.headers["Linkedin-Version"], "202312")


if __name__ == "__main__":
    unittest.main()
