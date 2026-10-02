"""Exercise authenticated capture routing without opening network sockets."""
import io
import json
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from board import server  # noqa: E402


def request(method, path, payload=None, authed=True, length=None):
    handler = server.Handler.__new__(server.Handler)
    handler.path = path + ("?t=" + server.TOKEN if authed else "")
    encoded = json.dumps(payload if payload is not None else {}).encode()
    handler.headers = {"Content-Length": str(length if length is not None else len(encoded))}
    handler.rfile = io.BytesIO(encoded)
    result = []
    handler._send = lambda code, body, *_args, **_kwargs: result.append((code, json.loads(body)))
    getattr(handler, "do_" + method)()
    return result[0]


class InboxRoutesTest(unittest.TestCase):
    def setUp(self):
        saved = dict(server.INBOX_PROCESS)
        fetch_saved = dict(server.FETCH)
        server.FETCH["running"] = False
        server.INBOX_PROCESS.update(running=False, error=None, result=None, finished_at=None)
        self.addCleanup(server.INBOX_PROCESS.update, saved)
        self.addCleanup(server.FETCH.update, fetch_saved)

    def test_new_routes_require_token_before_reading_or_writing_state(self):
        with mock.patch.object(server.linkedin_inbox, "capture") as capture, \
                mock.patch.object(server.linkedin_inbox, "listing") as listing:
            for path in ("/api/linkedin/capture", "/api/linkedin/process",
                         "/api/companies/follow", "/api/companies/capture"):
                self.assertEqual(request("POST", path, {}, authed=False)[0], 403)
            self.assertEqual(request("GET", "/api/linkedin/inbox", authed=False)[0], 403)
        capture.assert_not_called(); listing.assert_not_called()

    def test_capture_validated_offline_payload_and_oversize_refusal(self):
        cards = {"jobs": [{"job_id": "123456"}]}
        with mock.patch.object(server.linkedin_inbox, "capture", return_value={"added": 1}) as capture:
            self.assertEqual(request("POST", "/api/linkedin/capture", cards), (200, {"added": 1}))
            capture.assert_called_once_with(cards)
            capture.reset_mock()
            self.assertEqual(request("POST", "/api/linkedin/capture", {}, length=262145)[0], 413)
            capture.assert_not_called()
        with mock.patch.object(server.linkedin_inbox, "capture", side_effect=server.linkedin_inbox.InputError("invalid ID")):
            self.assertEqual(request("POST", "/api/linkedin/capture", cards)[0], 400)
        with mock.patch.object(server.linkedin_inbox, "capture", side_effect=OSError("fixture disk error")):
            code, body = request("POST", "/api/linkedin/capture", cards)
            self.assertEqual(code, 500); self.assertIn("Could not save", body["error"])

    def test_process_is_bounded_background_work_and_poll_reports_result(self):
        with mock.patch.object(server.threading, "Thread") as thread:
            for limit in (-1, 21, "15", True):
                self.assertEqual(request("POST", "/api/linkedin/process", {"limit": limit})[0], 400)
            self.assertEqual(request("POST", "/api/linkedin/process", {"limit": 10})[0], 202)
            thread.assert_called_once_with(target=server.inbox_worker, args=(10,), daemon=True)
            thread.return_value.start.assert_called_once()
            self.assertEqual(request("POST", "/api/linkedin/process", {"limit": 10})[0], 409)
        with mock.patch.object(server.linkedin_inbox, "listing", return_value={"items": [], "counts": {"pending": 1}}):
            code, body = request("GET", "/api/linkedin/inbox")
            self.assertEqual(code, 200); self.assertTrue(body["process_status"]["running"])
        with mock.patch.object(server.linkedin_inbox, "process", return_value={"imported": 2, "retry": 1}), \
                mock.patch.object(server.fetch_jobs, "RunLock", return_value=nullcontext()), \
                mock.patch.object(server.activity, "emit"):
            server.inbox_worker(10)
        self.assertFalse(server.INBOX_PROCESS["running"])
        self.assertEqual(server.INBOX_PROCESS["result"]["imported"], 2)
        self.assertTrue(server.INBOX_PROCESS["finished_at"])

    def test_follow_company_reads_stored_job_instead_of_request_identity(self):
        job = {"company": "Demo Labs", "url": "https://www.linkedin.com/jobs/view/123456"}
        payload = {"url": job["url"], "company": "Untrusted Name", "mtime": "v1"}
        with mock.patch.object(server.state, "job_payload", return_value=job), \
                mock.patch.object(server.companies, "follow", return_value={"companies": []}) as follow:
            self.assertEqual(request("POST", "/api/companies/follow", payload)[0], 200)
            follow.assert_called_once_with(payload, job)
        with mock.patch.object(server.state, "job_payload", return_value=None):
            self.assertEqual(request("POST", "/api/companies/follow", payload)[0], 404)

    def test_processing_waits_for_shared_collector_lock(self):
        with mock.patch.object(server.fetch_jobs, "RunLock", side_effect=server.fetch_jobs.AlreadyRunning("fixture fetch in progress")), \
                mock.patch.object(server.linkedin_inbox, "process") as process, \
                mock.patch.object(server.activity, "emit"):
            server.inbox_worker(15)
        process.assert_not_called()
        self.assertFalse(server.INBOX_PROCESS["running"])
        self.assertIn("fixture fetch in progress", server.INBOX_PROCESS["error"])

    def test_active_fetch_returns_actionable_conflict_without_starting_worker(self):
        server.FETCH["running"] = True
        with mock.patch.object(server.threading, "Thread") as thread:
            code, body = request("POST", "/api/linkedin/process", {"limit": 15})
        self.assertEqual(code, 409); self.assertIn("after it finishes", body["error"])
        thread.assert_not_called()

    def test_company_preview_confirmation_failure_is_a_bad_request(self):
        with mock.patch.object(server.companies, "capture", side_effect=server.companies.CompanyError("Confirm first")):
            self.assertEqual(request("POST", "/api/companies/capture", {})[0], 400)


if __name__ == "__main__":
    unittest.main()
