"""Anonymous guest-detail adapter fixtures; no requests are sent."""

import json
import subprocess
import unittest
from unittest.mock import patch

from tools.board import add_job


class LinkedInDetailTest(unittest.TestCase):
    def response(self, data=None, stderr="", exit_code=0):
        return subprocess.CompletedProcess([], exit_code, json.dumps(data), stderr)

    def test_legacy_resolver_preserves_external_url_and_meaningful_query(self):
        with patch.object(add_job.collectors, "bun", return_value={
                "title": "Software Engineer", "company": "Invented Example Works",
                "description": "JOBFLOW-ANONYMOUS-FIXTURE",
                "applyUrl": "https://careers.example.com/apply?jobId=42&utm_source=linkedin"}):
            row = add_job.resolve_linkedin("4000000001")
        self.assertEqual(row["apply_url"], "https://careers.example.com/apply?jobId=42")

    def test_strict_resolver_reads_job_and_preserves_destination(self):
        reply = self.response({"title": "Software Engineer", "company": "Invented Example Works",
                               "description": "JOBFLOW-ANONYMOUS-FIXTURE",
                               "applyUrl": "https://jobs.ashbyhq.com/example/42?source=linkedin"})
        with patch.object(add_job.subprocess, "run", return_value=reply) as run:
            row = add_job.resolve_linkedin_detail("4000000001")
        self.assertEqual(row["apply_url"], "https://jobs.ashbyhq.com/example/42?source=linkedin")
        self.assertEqual(run.call_args.kwargs["timeout"], 45)

    def test_structured_errors_distinguish_manual_retry_and_rate_limit(self):
        for code, retryable, rate_limited in (("NOT_FOUND", False, False),
                                             ("DETAIL_FAILED", True, False),
                                             ("RATE_LIMITED", True, True)):
            with self.subTest(code=code), patch.object(add_job.subprocess, "run",
                    return_value=self.response(stderr=json.dumps({"error": "fixture", "code": code}),
                                               exit_code=1)):
                with self.assertRaises(add_job.LinkedInDetailError) as raised:
                    add_job.resolve_linkedin_detail("4000000001")
                self.assertEqual((raised.exception.retryable, raised.exception.rate_limited),
                                 (retryable, rate_limited))

    def test_empty_malformed_and_timed_out_response_remain_retryable(self):
        for response in (self.response({}), self.response([]),
                         subprocess.CompletedProcess([], 0, "not json", "")):
            with patch.object(add_job.subprocess, "run", return_value=response):
                with self.assertRaises(add_job.LinkedInDetailError) as raised:
                    add_job.resolve_linkedin_detail("4000000001")
                self.assertTrue(raised.exception.retryable)
        with patch.object(add_job.subprocess, "run", side_effect=subprocess.TimeoutExpired("bun", 45)):
            with self.assertRaises(add_job.LinkedInDetailError):
                add_job.resolve_linkedin_detail("4000000001")

    def test_invalid_id_is_refused_without_running_cli(self):
        with patch.object(add_job.subprocess, "run") as run:
            with self.assertRaises(add_job.LinkedInDetailError):
                add_job.resolve_linkedin_detail("bad-id")
            run.assert_not_called()

    def test_external_destination_refuses_wrappers_private_hosts_and_credentials(self):
        invalid = ("javascript:alert(1)", "https://www.linkedin.com/jobs/view/4000000001",
                   "https://localhost/jobs/42", "https://127.0.0.1/jobs/42",
                   "https://192.168.1.10/jobs/42", "https://jobs.example.local/42",
                   "https://name:password@careers.example.com/jobs/42")
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(add_job.external_apply_url(value), "")
        self.assertEqual(add_job.external_apply_url(
            "https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fcareers.example.com%2Fapply%3FjobId%3D42"),
            "https://careers.example.com/apply?jobId=42")


if __name__ == "__main__":
    unittest.main()
