"""The run supervisor, driven end to end against a stand-in for the CLI.

`tests/fake_claude.py` reproduces the parts of `claude -p` this module depends
on - the `stream-json` envelope, and the `PreToolUse` hook call before a write.
Everything else here is the real code path: the real wrapper, the real lock, the
real guard, the real registry.

The failure cases matter more than the happy one, because they are the ones that
cost money when they go wrong: a guard that is not installed, a `fit.json` that
does not match its schema, two clicks on Approve, a board that restarted while a
model was running.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from board import activity, run_guard, run_registry, runs  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_claude.py"


# Generous on purpose. These tests spawn real subprocesses, and several run
# concurrently under `unittest discover`; a deadline tuned to an idle machine
# turns a slow scheduler into a phantom failure. Nothing here waits for the
# timeout in the happy case, so a large value costs nothing.
def wait_for(predicate, timeout=60, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class SupervisorCase(unittest.TestCase):
    """Every path the supervisor touches is redirected into a temp directory."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / "job_scraper").mkdir()
        (home / "cv").mkdir()
        (home / "cover_letters").mkdir()

        fake = home / "fake-claude"
        shutil.copy(FAKE, fake)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.fake = fake

        config = {"claude_bin": str(fake), "canary_timeout_s": 8,
                  "timeout_s": {"pass_a": 25, "pass_b": 25, "pass_c": 25},
                  "inspection_enabled": False,
                  "daily_budget_usd": 50.0}
        (home / "job_scraper" / "board_config.json").write_text(
            json.dumps(config), encoding="utf-8")

        self.patched = {}
        for name, value in (("ROOT", home),
                            ("REGISTRY", home / "job_scraper" / "runs.json"),
                            ("RUN_STATE", home / "job_scraper" / "run_state"),
                            ("RUN_DIRS", home / "documents" / "runs"),
                            ("MODEL_LOCK", home / "job_scraper" / ".model.lock"),
                            ("PIPELINE_LOCK", home / "job_scraper" / ".pipeline.lock"),
                            ("RUNS_LOCK", home / "job_scraper" / ".runs.lock"),
                            ("CONFIG", home / "job_scraper" / "board_config.json")):
            self.patched[name] = getattr(run_registry, name)
            setattr(run_registry, name, value)
        self.addCleanup(self._restore)

        for key, value in (("FAKE_GUARD", str(ROOT / "tools" / "board" / "guard_write.py")),
                           ("FAKE_MODE", "auto")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)

        self.home = home
        def fake_compile(record):
            directory = run_registry.run_dir(record["id"])
            directory.mkdir(parents=True, exist_ok=True)
            pdfs = {"cv": directory / "cv.pdf", "cover": directory / "cover.pdf"}
            for path in pdfs.values():
                path.write_bytes(b"%PDF-1.4\n1 0 obj <</Type /Page>> endobj\n%%EOF")
            verify = {"schema": "jobflow.verify/1", "run_id": record["id"],
                      "checks": [{"id": "visual_layout", "label": "Visual layout",
                                  "state": "unverified", "detail": "test fixture",
                                  "evidence": {}}],
                      "keywords": {"covered": [], "absent": [], "source": "posting"}}
            (directory / "verify.json").write_text(json.dumps(verify), encoding="utf-8")
            artefacts = {"cv_source": record["targets"]["cv"],
                         "cover_source": record["targets"]["cover"],
                         "cv_pdf": str(pdfs["cv"].relative_to(home)),
                         "cover_pdf": str(pdfs["cover"].relative_to(home))}
            return pdfs, verify, artefacts

        self._docs_patches = [
            mock.patch.object(runs.docs, "compile_record", side_effect=fake_compile),
            mock.patch.object(runs.docs, "archive_posting",
                              return_value="documents/applications/test/job_posting.md"),
            mock.patch.object(runs.docs, "merge_tracker", return_value="appended"),
        ]
        for patcher in self._docs_patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.supervisor = runs.Supervisor()
        self.supervisor.ensure_worker()
        # Without this the worker outlives its test, keeps draining its queue,
        # and writes into the *next* test's redirected paths.
        self.addCleanup(self.supervisor.stop)
        activity.reset_for_tests()

    def _restore(self):
        for name, value in self.patched.items():
            setattr(run_registry, name, value)

    # -- helpers ------------------------------------------------------------

    def start(self, url="https://example.com/jobs/1", **extra):
        payload = {"job_url": url, "company": "Acme", "role": "ML Engineer"}
        payload.update(extra)
        return self.supervisor.start(payload)

    def phase_of(self, run_id):
        record = run_registry.get(run_id)
        return record["phase"] if record else None

    def spawn_stub(self, seconds=30, marker=None):
        """A long-lived process standing in for another board or another run.

        `marker` goes into its argv, the way a real run carries its session uuid
        - which is half of what `process_matches()` checks."""
        import subprocess
        argv = [sys.executable, "-c", "import time; time.sleep(%d)" % seconds]
        if marker:
            argv.append(marker)
        proc = subprocess.Popen(argv, start_new_session=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        if marker:
            # `ps` shows only the executable path for a moment after `exec`, before
            # the argument area is readable. A record is meant to stand for a
            # process that has been running, so wait until it looks like one -
            # otherwise the identity check is racing process startup, not testing
            # identity.
            self.assertIsNotNone(
                wait_for(lambda: marker in (run_registry._ps("args=", proc.pid) or "")),
                "the stub's argv never became visible to ps")
        return proc

    def through_gate(self, **extra):
        """Start a run and leave it sitting at the approval gate."""
        code, body = self.start(**extra)
        self.assertEqual(code, 202, body)
        run_id = body["run_id"]
        self.assertEqual(self.wait_phase(run_id, "awaiting_approval", "failed"),
                         "awaiting_approval", run_registry.get(run_id).get("error"))
        return run_id

    def wait_phase(self, run_id, *phases, **kw):
        return wait_for(lambda: self.phase_of(run_id) if self.phase_of(run_id) in phases else None,
                        timeout=kw.get("timeout", 60))


class HappyPathTest(SupervisorCase):
    def test_pass_a_stops_at_the_approval_gate_and_pass_b_drafts(self):
        code, body = self.start()
        self.assertEqual(code, 202, body)
        run_id = body["run_id"]

        self.assertEqual(self.wait_phase(run_id, "awaiting_approval", "failed"),
                         "awaiting_approval", run_registry.get(run_id).get("error"))
        record = run_registry.get(run_id)
        self.assertEqual(record["fit"]["schema"], "jobflow.fit/1")
        self.assertEqual(record["fit"]["overall"], 78)
        # Pass A must not have produced drafts - that is the whole point of the gate.
        self.assertFalse((self.home / record["targets"]["cv"]).exists())

        code, _ = self.supervisor.approve(run_id, "awaiting_approval")
        self.assertEqual(code, 200)

        self.assertEqual(self.wait_phase(run_id, "done", "failed"), "done",
                         run_registry.get(run_id).get("error"))
        record = run_registry.get(run_id)
        self.assertTrue((self.home / record["targets"]["cv"]).exists())
        self.assertTrue((self.home / record["targets"]["cover"]).exists())
        self.assertEqual(sorted(record["artefacts"]),
                         ["cover_pdf", "cover_source", "cv_pdf", "cv_source", "posting"])

    def test_the_reported_cost_is_what_the_ledger_is_debited_with(self):
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        self.supervisor.approve(run_id, "awaiting_approval")
        self.wait_phase(run_id, "done", "failed")
        # 0.12 from pass A plus 0.87 from pass B, from the result envelopes - not
        # from the caps, which were $0.40 and $2.00.
        self.assertAlmostEqual(run_registry.get(run_id)["cost"]["total_usd"], 0.99, places=4)
        self.assertAlmostEqual(run_registry.spent_today(), 0.99, places=4)

    def test_the_transcript_is_written_to_the_run_directory(self):
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        stream = run_registry.run_dir(run_id) / "stream.jsonl"
        self.assertTrue(stream.exists())
        kinds = [json.loads(l)["type"] for l in stream.read_text(encoding="utf-8").splitlines()]
        self.assertIn("system", kinds)
        self.assertIn("result", kinds)

    def test_revise_forks_the_parent_session_and_publishes_a_version(self):
        parent = self.through_gate()
        self.supervisor.approve(parent, "awaiting_approval")
        self.assertEqual(self.wait_phase(parent, "done", "failed"), "done")
        parent_record = run_registry.get(parent)
        code, body = self.start(kind="revise", parent=parent, scope="cv",
                                note="tighten the opening evidence")
        self.assertEqual(code, 202, body)
        child = body["run_id"]
        self.assertEqual(self.wait_phase(child, "done", "failed"), "done",
                         run_registry.get(child).get("error"))
        child_record = run_registry.get(child)
        self.assertEqual(child_record["parent"], parent)
        self.assertNotEqual(child_record["session_id"], parent_record["session_id"])
        self.assertEqual(child_record["resume_session_id"], parent_record["session_id"])


class DocumentsPipelineTest(SupervisorCase):
    def setUp(self):
        super().setUp()
        path = self.home / "job_scraper" / "board_config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["inspection_enabled"] = True
        path.write_text(json.dumps(config), encoding="utf-8")

    def _complete(self):
        run_id = self.through_gate()
        self.assertEqual(self.supervisor.approve(run_id, "awaiting_approval")[0], 200)
        self.assertEqual(self.wait_phase(run_id, "done", "failed"), "done",
                         run_registry.get(run_id).get("error"))
        return run_id

    def test_pass_c_proves_it_read_both_pdfs(self):
        run_id = self._complete()
        verify = json.loads((run_registry.run_dir(run_id) / "verify.json").read_text())
        visual = next(check for check in verify["checks"]
                      if check["id"] == "visual_layout")
        self.assertEqual(visual["state"], "pass")
        self.assertEqual(len(visual["evidence"]["read"]), 2)
        record = run_registry.get(run_id)
        self.assertIn("cv_pdf", record["artefacts"])
        self.assertIn("posting", record["artefacts"])

    def test_missing_read_evidence_can_never_turn_green(self):
        os.environ["FAKE_INSPECT"] = "noread"
        self.addCleanup(os.environ.pop, "FAKE_INSPECT", None)
        run_id = self._complete()
        verify = json.loads((run_registry.run_dir(run_id) / "verify.json").read_text())
        visual = next(check for check in verify["checks"]
                      if check["id"] == "visual_layout")
        self.assertEqual(visual["state"], "unverified")

    def test_blocked_inspection_is_published_as_failure(self):
        os.environ["FAKE_INSPECT"] = "blocked"
        self.addCleanup(os.environ.pop, "FAKE_INSPECT", None)
        run_id = self._complete()
        verify = json.loads((run_registry.run_dir(run_id) / "verify.json").read_text())
        visual = next(check for check in verify["checks"]
                      if check["id"] == "visual_layout")
        self.assertEqual(visual["state"], "fail")

    def test_compile_failure_stops_before_inspection_and_publication(self):
        run_id = self.through_gate()
        with mock.patch.object(runs.docs, "compile_record",
                               side_effect=runs.docs.DocumentError("compile exploded")):
            self.supervisor.approve(run_id, "awaiting_approval")
            self.assertEqual(self.wait_phase(run_id, "failed", "done"), "failed")
        self.assertIn("compile exploded", run_registry.get(run_id)["error"])


class GuardEnforcementTest(SupervisorCase):
    def test_a_run_whose_guard_never_fires_is_killed(self):
        """The canary. A hook that is not installed and a run that simply never
        wrote anything are the same silence, so the run has to prove the guard.

        `noguard` is a CLI with **no hook at all**: the probe write succeeds, and
        so would every other write. The run must be killed anyway, and its output
        must not be published - the whole point is that unguarded output is not
        trusted even when it looks correct."""
        os.environ["FAKE_MODE"] = "noguard"
        code, body = self.start()
        run_id = body["run_id"]
        self.assertEqual(self.wait_phase(run_id, "failed", "done", "awaiting_approval",
                                         timeout=60), "failed")
        self.assertIn("canary", run_registry.get(run_id)["error"])
        # It wrote a valid fit.json - unguarded - and it was still not published.
        self.assertTrue((run_registry.run_dir(run_id) / "fit.json").exists())
        self.assertIsNone(run_registry.get(run_id).get("fit"))
        self.assertTrue((run_registry.run_dir(run_id) / ".guard-probe-missing").exists()
                        or any(run_registry.run_dir(run_id).glob(".guard-probe-*")),
                        "the unguarded probe write should have succeeded")

    def test_pass_a_cannot_write_the_target_documents(self):
        """Pass A's allowlist is the run directory only, so a run that ignores
        the instruction and starts drafting is refused rather than paid for."""
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        allowlist = json.loads((run_registry.state_dir(run_id) / "allowlist.json")
                               .read_text(encoding="utf-8"))
        self.assertEqual(allowlist["files"], [])
        self.assertEqual(allowlist["dirs"], [str(run_registry.run_dir(run_id).resolve())])

    def test_pass_b_allowlists_exactly_two_files(self):
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        self.supervisor.approve(run_id, "awaiting_approval")
        self.wait_phase(run_id, "done", "failed")
        allowlist = json.loads((run_registry.state_dir(run_id) / "allowlist.json")
                               .read_text(encoding="utf-8"))
        record = run_registry.get(run_id)
        self.assertEqual(allowlist["files"],
                         sorted([str((self.home / record["targets"]["cv"]).resolve()),
                                 str((self.home / record["targets"]["cover"]).resolve())]))

    def test_the_hook_log_records_the_refused_probe(self):
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        logs = sorted(run_registry.state_dir(run_id).glob("hook-*.jsonl"))
        self.assertEqual(len(logs), 1, "one hook log per pass")
        entries = [json.loads(l) for l in logs[0].read_text(encoding="utf-8").splitlines()]
        probes = [e for e in entries if e["probe"]]
        self.assertTrue(probes)
        self.assertEqual(probes[0]["decision"], "deny")

    def test_each_pass_proves_the_guard_with_its_own_nonce(self):
        """Pass B must not inherit pass A's proof. The window between them is a
        human looking at a card, which is exactly when a settings file could be
        edited - so each pass gets a fresh probe path and a fresh log."""
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        self.supervisor.approve(run_id, "awaiting_approval")
        self.assertEqual(self.wait_phase(run_id, "done", "failed"), "done",
                         run_registry.get(run_id).get("error"))
        logs = sorted(run_registry.state_dir(run_id).glob("hook-*.jsonl"))
        self.assertEqual(len(logs), 2, "one hook log per pass, not one shared")
        nonces = set()
        for log in logs:
            for line in log.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry["probe"]:
                    nonces.add(entry["target"].rsplit("-", 1)[-1])
        self.assertEqual(len(nonces), 2, "the two passes used the same probe path")

    def test_preflight_runs_again_before_pass_b(self):
        """Break the settings file while the run sits at the approval gate. Pass
        B must refuse rather than spend under a guard nobody re-checked."""
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")

        original = run_guard.SETTINGS.read_text(encoding="utf-8")
        broken = Path(self.tmp.name) / "broken-settings.json"
        broken.write_text("{ not json", encoding="utf-8")
        run_guard.SETTINGS = broken
        self.addCleanup(setattr, run_guard, "SETTINGS", run_guard.HERE / "run-settings.json")

        self.supervisor.approve(run_id, "awaiting_approval")
        self.assertEqual(self.wait_phase(run_id, "failed", "done"), "failed")
        self.assertIn("no guard", run_registry.get(run_id)["error"])
        self.assertFalse((self.home / run_registry.get(run_id)["targets"]["cv"]).exists())
        self.assertEqual(original[:1], "{")

    def test_a_symlinked_target_is_refused_before_the_run_starts(self):
        """`cv/main_<slug>.tex` as a link to CLAUDE.md would make an allowlisted
        CV write overwrite the profile: both realpath to the same file."""
        secret = self.home / "CLAUDE.md"
        secret.write_text("the profile", encoding="utf-8")
        link = self.home / "cv" / "main_acme_ml_engineer.tex"
        link.symlink_to(secret)
        with self.assertRaises(run_guard.PreflightError):
            run_guard.write_allowlist("r-x", targets=[link], nonce="abcd1234")
        self.assertEqual(secret.read_text(encoding="utf-8"), "the profile")


class FitContractTest(SupervisorCase):
    def test_a_fit_file_that_misses_the_schema_fails_the_run(self):
        os.environ["FAKE_MODE"] = "badfit"
        code, body = self.start()
        run_id = body["run_id"]
        self.assertEqual(self.wait_phase(run_id, "failed", "awaiting_approval"), "failed")
        self.assertIn("jobflow.fit/1", run_registry.get(run_id)["error"])

    def test_a_missing_fit_file_fails_the_run(self):
        os.environ["FAKE_MODE"] = "nofit"
        code, body = self.start()
        run_id = body["run_id"]
        self.assertEqual(self.wait_phase(run_id, "failed", "awaiting_approval"), "failed")
        self.assertIn("no fit.json", run_registry.get(run_id)["error"])

    def test_the_validator_rejects_a_score_out_of_range(self):
        payload = {
            "schema": "jobflow.fit/1", "company": "A", "role": "B", "location": "C",
            "deadline": None, "language_gate": "PASS", "language_note": "",
            "location_gate": "PASS",
            "scores": {"technical": 120, "experience": 1, "behavioural": 1, "career": 1},
            "overall": 50, "verdict": "good", "matches": [], "gaps": [],
            "sector": None, "role_type": None, "contact_person": None, "channel": None,
            "posting_chars": 1}
        self.assertIn("scores.technical must be a number 0-100", run_guard.validate_fit(payload))

    def test_a_null_and_an_absent_field_are_not_the_same_thing(self):
        base = {
            "schema": "jobflow.fit/1", "company": "A", "role": "B", "location": "C",
            "deadline": None, "language_gate": "PASS", "language_note": "",
            "location_gate": "PASS",
            "scores": {"technical": 1, "experience": 1, "behavioural": 1, "career": 1},
            "overall": 50, "verdict": "good", "matches": [], "gaps": [],
            "sector": None, "role_type": None, "contact_person": None, "channel": None,
            "posting_chars": 1}
        self.assertEqual(run_guard.validate_fit(base), [])
        without = dict(base)
        del without["sector"]
        self.assertIn("sector is required (use null when the posting does not state it)",
                      run_guard.validate_fit(without))


class ApprovalTest(SupervisorCase):
    def test_approval_persists_the_selected_cv_base(self):
        run_id = self.through_gate()
        record = run_registry.get(run_id)
        self.assertEqual(record["recommended_base_cv"], "ml")
        code, _ = self.supervisor.approve(run_id, "awaiting_approval", "ai")
        self.assertEqual(code, 200)
        record = run_registry.get(run_id)
        self.assertEqual(record["base_cv"], "ai")
        self.assertEqual(record["resolved_base_cv"], "ai")

    def test_approving_twice_costs_one_pass_b(self):
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        first = self.supervisor.approve(run_id, "awaiting_approval")
        second = self.supervisor.approve(run_id, "awaiting_approval")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 409)
        self.wait_phase(run_id, "done", "failed")
        # One pass A and one pass B, not two of either.
        self.assertAlmostEqual(run_registry.get(run_id)["cost"]["total_usd"], 0.99, places=4)

    def test_a_stale_phase_from_the_page_is_refused(self):
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        code, payload = self.supervisor.approve(run_id, "evaluating")
        self.assertEqual(code, 409)
        self.assertEqual(payload["phase"], "awaiting_approval")

    def test_an_unanswered_approval_does_not_block_a_later_run(self):
        """Approval re-queues instead of holding `.pipeline.lock` across a human
        decision. An evaluation nobody has looked at must not stop other work."""
        code, first = self.start(url="https://example.com/jobs/1")
        self.assertEqual(self.wait_phase(first["run_id"], "awaiting_approval", "failed"),
                         "awaiting_approval")

        code, second = self.start(url="https://example.com/jobs/2")
        self.assertEqual(code, 202, second)
        self.assertEqual(self.wait_phase(second["run_id"], "awaiting_approval", "failed"),
                         "awaiting_approval", run_registry.get(second["run_id"]).get("error"))
        # and the first is still sitting at its gate, unharmed
        self.assertEqual(self.phase_of(first["run_id"]), "awaiting_approval")

    def test_an_approval_that_arrives_immediately_is_not_lost(self):
        """No waiter to miss: approve() appends to the queue the worker reads."""
        for _ in range(3):
            code, body = self.start(url="https://example.com/jobs/%d" % time.time_ns())
            run_id = body["run_id"]
            self.assertEqual(self.wait_phase(run_id, "awaiting_approval", "failed"),
                             "awaiting_approval")
            self.assertEqual(self.supervisor.approve(run_id, "awaiting_approval")[0], 200)
            self.assertEqual(self.wait_phase(run_id, "done", "failed"), "done",
                             run_registry.get(run_id).get("error"))

    def test_cancelling_at_the_gate_stops_the_run(self):
        code, body = self.start()
        run_id = body["run_id"]
        self.wait_phase(run_id, "awaiting_approval", "failed")
        self.supervisor.cancel(run_id)
        self.assertEqual(self.wait_phase(run_id, "cancelled", "done"), "cancelled")
        self.assertFalse((self.home / run_registry.get(run_id)["targets"]["cv"]).exists())


class AdmissionTest(SupervisorCase):
    def test_unknown_cv_base_is_refused(self):
        code, body = self.start(base_cv="quantum")
        self.assertEqual(code, 400)
        self.assertIn("base_cv", body["error"])

    def test_a_non_http_url_is_refused(self):
        code, body = self.start(url="file:///etc/passwd")
        self.assertEqual(code, 400)

    def test_a_second_run_for_the_same_posting_is_refused(self):
        code, body = self.start()
        self.wait_phase(body["run_id"], "awaiting_approval", "failed")
        code, second = self.start()
        self.assertEqual(code, 409)
        self.assertEqual(second["run_id"], body["run_id"])

    def test_a_url_that_is_not_on_the_board_needs_a_company_and_role(self):
        code, body = self.supervisor.start({"job_url": "https://example.com/x"})
        self.assertEqual(code, 400)
        self.assertIn("company/role", body["error"])

    def test_the_daily_cap_refuses_a_run_it_cannot_afford(self):
        run_registry.debit(49.9)
        code, body = self.start()
        self.assertEqual(code, 429)
        self.assertIn("budget", body["error"])

    def test_standing_preferences_are_recorded_for_the_guarded_draft(self):
        code, body = self.start(remember="never quote the 2,000-user figure")
        self.assertEqual(code, 202, body)
        self.assertEqual(run_registry.get(body["run_id"])["remember"],
                         "never quote the 2,000-user figure")

    def test_revise_requires_a_completed_parent(self):
        code, body = self.start(kind="revise")
        self.assertEqual(code, 409)
        self.assertIn("completed parent", body["error"])


class ContractFileTest(SupervisorCase):
    """Pass A and pass B each owe the supervisor specific files. A pass that
    skips one must fail, because M3 acts on what they say and cannot ask again."""

    def test_pass_a_without_posting_md_fails(self):
        os.environ["FAKE_MODE"] = "noposting"
        code, body = self.start()
        run_id = body["run_id"]
        self.assertEqual(self.wait_phase(run_id, "failed", "awaiting_approval"), "failed")
        self.assertIn("posting.md", run_registry.get(run_id)["error"])

    def test_pass_b_without_drafts_json_fails(self):
        os.environ["FAKE_SKIP"] = "drafts"
        self.addCleanup(os.environ.pop, "FAKE_SKIP", None)
        run_id = self.through_gate()
        self.supervisor.approve(run_id, "awaiting_approval")
        self.assertEqual(self.wait_phase(run_id, "failed", "done"), "failed")
        self.assertIn("drafts.json", run_registry.get(run_id)["error"])

    def test_pass_b_without_verify_request_fails(self):
        os.environ["FAKE_SKIP"] = "verify"
        self.addCleanup(os.environ.pop, "FAKE_SKIP", None)
        run_id = self.through_gate()
        self.supervisor.approve(run_id, "awaiting_approval")
        self.assertEqual(self.wait_phase(run_id, "failed", "done"), "failed")
        self.assertIn("verify_request.json", run_registry.get(run_id)["error"])

    def test_drafts_json_must_name_the_allowlisted_targets(self):
        problems = run_guard.validate_drafts(
            {"schema": "jobflow.drafts/1", "cv_source": "cv/somebody_else.tex",
             "cover_source": "cover_letters/cover_acme.tex"},
            {"cv": "cv/main_acme.tex", "cover": "cover_letters/cover_acme.tex"})
        self.assertEqual(len(problems), 1)
        self.assertIn("cv_source", problems[0])

    def test_a_pass_b_that_writes_nothing_cannot_inherit_last_months_files(self):
        """Re-applying to the same company and role finds the old CV sitting at
        exactly the expected path. `exists()` is not evidence of this run."""
        stale = self.home / "cv" / "main_acme_ml_engineer.tex"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("last month's CV", encoding="utf-8")
        (self.home / "cover_letters" / "cover_acme_ml_engineer.tex").write_text(
            "last month's letter", encoding="utf-8")

        run_id = self.through_gate()
        os.environ["FAKE_MODE"] = "nowrite"
        self.supervisor.approve(run_id, "awaiting_approval")
        self.assertEqual(self.wait_phase(run_id, "failed", "done"), "failed")
        self.assertIn("not this run's output", run_registry.get(run_id)["error"])
        self.assertEqual(stale.read_text(encoding="utf-8"), "last month's CV")


class WriteEvidenceTest(SupervisorCase):
    """Two independent proofs that pass B produced the documents.

    File metadata alone is satisfiable by rewriting last month's bytes; hook
    evidence alone would accept a write the guard approved and the model then
    failed to complete. Both have to hold.
    """

    def test_hook_evidence_is_required_as_well_as_a_changed_file(self):
        run_id = self.through_gate()
        self.supervisor.approve(run_id, "awaiting_approval")
        self.assertEqual(self.wait_phase(run_id, "done", "failed"), "done",
                         run_registry.get(run_id).get("error"))
        record = run_registry.get(run_id)
        logs = sorted(run_registry.state_dir(run_id).glob("hook-*.jsonl"))
        approved = set()
        for log in logs:
            for line in log.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                if entry["decision"] == "allow" and entry.get("resolved"):
                    approved.add(entry["resolved"])
        for target in record["targets"].values():
            self.assertIn(str((self.home / target).resolve()), approved)

    def test_a_write_the_guard_refused_is_not_counted_as_output(self):
        """The guard's `allow` is what counts, not the fact a file exists."""
        from board import run_proc
        job = run_proc.Pass("r-x", "l", [], {}, 0.1, 1, "n0nce",
                            self.home / "hook.jsonl")
        (self.home / "hook.jsonl").write_text("\n".join([
            json.dumps({"tool": "Write", "decision": "deny",
                        "resolved": str(self.home / "denied.tex"), "probe": False}),
            json.dumps({"tool": "Write", "decision": "allow",
                        "resolved": str(self.home / "allowed.tex"), "probe": False}),
            json.dumps({"tool": "WebFetch", "decision": "defer",
                        "resolved": None, "probe": False}),
        ]), encoding="utf-8")
        self.assertEqual(job.allowed_writes(),
                         {str((self.home / "allowed.tex").resolve())})

    def test_activity_command_hides_the_internal_budget_cap(self):
        from board import run_proc
        argv = ["claude", "--max-budget-usd", "0.40", "-p", "long prompt"]
        job = run_proc.Pass("r-x", "l", argv, {}, 0.4, 1, "nonce",
                            self.home / "hook.jsonl", model_argv=argv)
        shown = job._reportable_argv()
        self.assertNotIn("--max-budget-usd", shown)
        self.assertNotIn("0.40", shown)
        self.assertEqual(shown[-1], "<prompt 11 chars>")


class VerifyRequestTest(SupervisorCase):
    def test_the_schema_and_keywords_are_both_checked(self):
        check = self.supervisor._validate_verify_request
        self.assertEqual(check({"schema": "jobflow.verify/1", "keywords": ["LLM"]}), [])
        self.assertTrue(check({"schema": "wrong", "keywords": ["LLM"]}))
        self.assertTrue(check({"schema": "jobflow.verify/1", "keywords": [1]}))
        self.assertTrue(check({"schema": "jobflow.verify/1", "keywords": []}))
        self.assertTrue(check({"schema": "jobflow.verify/1"}))
        self.assertTrue(check("not an object"))


class LedgerTest(SupervisorCase):
    def test_provider_429_is_not_overwritten_by_the_missing_canary(self):
        os.environ["FAKE_MODE"] = "rate_limit"
        code, body = self.start()
        self.assertEqual(code, 202)
        run_id = body["run_id"]
        self.assertEqual(self.wait_phase(run_id, "failed", "awaiting_approval"), "failed")
        record = run_registry.get(run_id)
        self.assertEqual(record["failure_code"], "provider_rate_limit")
        self.assertTrue(record["retryable"])
        self.assertFalse(record["model_started"])
        self.assertIn("session limit", record["error"])
        self.assertNotIn("canary", record["error"])
        self.assertEqual(record["cost"]["total_usd"], 0)

    def test_retry_creates_a_linked_attempt_with_a_fresh_session(self):
        os.environ["FAKE_MODE"] = "rate_limit"
        code, body = self.start(base_cv="ai")
        failed_id = body["run_id"]
        self.assertEqual(self.wait_phase(failed_id, "failed"), "failed")
        failed = run_registry.get(failed_id)
        os.environ["FAKE_MODE"] = "auto"

        code, retried = self.supervisor.retry(failed_id)
        self.assertEqual(code, 202, retried)
        new_id = retried["run_id"]
        current = run_registry.get(new_id)
        self.assertNotEqual(new_id, failed_id)
        self.assertNotEqual(current["session_id"], failed["session_id"])
        self.assertEqual(current["application_id"], failed_id)
        self.assertEqual(current["retry_of"], failed_id)
        self.assertEqual(current["attempt"], 2)
        self.assertEqual(current["base_cv"], "ai")
        self.assertEqual(current["targets"], failed["targets"])
        self.assertEqual(run_registry.get(failed_id)["phase"], "failed")

        # A rapid second click returns the in-flight attempt instead of buying
        # another model session.
        code, duplicate = self.supervisor.retry(failed_id)
        self.assertEqual(code, 200)
        self.assertEqual(duplicate["run_id"], new_id)

    def test_queued_runs_reserve_their_worst_case(self):
        """Five queued runs each passing the same "there is room for one" test is
        how a $10 cap becomes $14 of committed spend."""
        (self.home / "job_scraper" / "board_config.json").write_text(json.dumps(
            {"claude_bin": str(self.fake), "canary_timeout_s": 8,
             "timeout_s": {"pass_a": 25, "pass_b": 25},
             "daily_budget_usd": 5.0}), encoding="utf-8")
        admitted = []
        for n in range(4):
            code, body = self.start(url="https://example.com/jobs/%d" % n)
            admitted.append(code)
        # Pass A+B plus the bounded inspection/repair loop is $4.15 worst case;
        # a $5 daily ceiling therefore admits exactly one, not four.
        self.assertEqual(admitted[0], 202)
        self.assertEqual(admitted[1], 429)
        self.assertIn("in flight", self.supervisor.start(
            {"job_url": "https://example.com/jobs/9", "company": "A", "role": "B"})[1]["error"])

    def test_a_failed_run_is_still_charged(self):
        """A run killed for a missing canary reported a cost, and that money is
        spent whether or not its output is trusted."""
        os.environ["FAKE_MODE"] = "crash"
        code, body = self.start()
        run_id = body["run_id"]
        self.assertEqual(self.wait_phase(run_id, "failed", "awaiting_approval"), "failed")
        self.assertAlmostEqual(run_registry.spent_today(), 0.01, places=4)
        self.assertAlmostEqual(run_registry.get(run_id)["cost"]["total_usd"], 0.01, places=4)

    def test_the_ledger_resets_on_a_new_day(self):
        run_registry.debit(3.0)
        with run_registry.runs_lock():
            data = run_registry.load()
            data["ledger"]["date"] = "2020-01-01"
            run_registry.store(data)
        self.assertEqual(run_registry.spent_today(), 0.0)
        self.assertAlmostEqual(run_registry.debit(1.0), 1.0, places=4)

    def test_a_live_orphan_keeps_its_reservation(self):
        """Orphaned means a live model process from a dead board - the one state
        where something is spending that nobody is watching. Releasing its
        reservation is exactly backwards."""
        stub = self.spawn_stub()
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000002-orph-eeeeee", "phase": "orphaned",
                "pid": stub.pid, "pgid": stub.pid, "session_id": "x",
                "job_url": "https://x", "company": "A", "role": "B",
                "budget_usd": {"pass_a": 0.40, "pass_b": 2.00}, "cost": {"total_usd": 0.0}})
            run_registry.store(data)
        self.assertAlmostEqual(run_registry.reserved(), 2.40, places=4)

    def test_a_run_past_its_evaluation_reserves_only_the_pass_b_cap(self):
        """Pass A's spend is already in the ledger; holding its cap as well
        blocks admission against money nothing can spend."""
        run_id = self.through_gate()
        self.assertAlmostEqual(run_registry.reserved(), 2.00, places=4)

    def test_reserved_ignores_finished_runs(self):
        run_id = self.through_gate()
        self.assertGreater(run_registry.reserved(), 0)
        self.supervisor.cancel(run_id)
        self.assertEqual(self.wait_phase(run_id, "cancelled", "done"), "cancelled")
        self.assertEqual(run_registry.reserved(), 0.0)


class CancelRaceTest(SupervisorCase):
    def test_a_cancel_during_preparation_never_reaches_popen(self):
        """Cancel arriving while pass A is still resolving the salary benchmark
        left `_active` empty, so nothing killed the process that started a
        moment later."""
        started = threading.Event()
        original = self.supervisor._salary

        def slow(record):
            started.set()
            time.sleep(1.5)
            return original(record)

        self.supervisor._salary = slow
        code, body = self.start()
        run_id = body["run_id"]
        self.assertTrue(started.wait(20))
        self.supervisor.cancel(run_id)
        self.assertEqual(self.wait_phase(run_id, "cancelled", "awaiting_approval", "failed"),
                         "cancelled")
        self.assertFalse((run_registry.run_dir(run_id) / "fit.json").exists(),
                         "a cancelled run still produced model output")


class TwoBoardsTest(SupervisorCase):
    def test_a_second_board_leaves_a_live_boards_runs_alone(self):
        """Two boards on two ports is a normal thing to do. A second board that
        adopts the first one's records marks its live runs failed, then kills
        them on the way out."""
        other_board = self.spawn_stub()
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20260101-000000-other-aaaa", "phase": "drafting",
                "pid": other_board.pid, "pgid": other_board.pid, "session_id": "x",
                "owner": "some-other-board", "owner_pid": other_board.pid,
                "owner_started": run_registry.process_start_time(other_board.pid),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)

        other = runs.Supervisor()
        self.assertEqual(other.reconcile(), [])
        self.assertEqual(self.phase_of("r-20260101-000000-other-aaaa"), "drafting")

    def test_shutdown_kills_only_this_boards_runs(self):
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20260101-000001-other-bbbb", "phase": "drafting",
                "pid": os.getpid(), "pgid": os.getpid(), "session_id": "x",
                "owner": "some-other-board", "owner_pid": os.getpid(),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        runs._SUPERVISOR = self.supervisor
        self.addCleanup(setattr, runs, "_SUPERVISOR", None)
        runs.shutdown()   # must not touch a run this board does not own
        self.assertEqual(self.phase_of("r-20260101-000001-other-bbbb"), "drafting")


class OwnershipMigrationTest(SupervisorCase):
    """What happens to a run written before ownership fields existed."""

    def test_a_record_with_no_owner_is_reconciled_but_never_killed(self):
        stub = self.spawn_stub(marker="legacy")
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000000-legacy-cccccc", "phase": "drafting",
                "pid": stub.pid, "pgid": stub.pid, "session_id": "legacy",
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)

        # No owner recorded, so it is reconciled - surfaced, with a Kill button.
        self.supervisor.reconcile()
        self.assertEqual(self.phase_of("r-20250101-000000-legacy-cccccc"), "orphaned")

        # Reconciliation must not have killed it, and neither may shutdown: the
        # run carries no `owner`, so it is not this board's to terminate.
        self.assertIsNone(stub.poll())
        runs._SUPERVISOR = self.supervisor
        self.addCleanup(setattr, runs, "_SUPERVISOR", None)
        runs.shutdown()
        self.assertIsNone(stub.poll(), "shutdown killed a run this board does not own")

        # And Kill must refuse too. An ownerless record may belong to an older
        # board that is still running; signalling it would kill another window's
        # work. The run stays orphaned so the button still works if the owner
        # later turns out to be gone.
        code, body = self.supervisor.kill("r-20250101-000000-legacy-cccccc")
        self.assertEqual(code, 409)
        self.assertIn("which board owns", body["error"])
        self.assertEqual(self.phase_of("r-20250101-000000-legacy-cccccc"), "orphaned")
        self.assertIsNone(stub.poll(), "an ownerless run was signalled")

    def test_a_recorded_owner_that_is_gone_is_reconciled(self):
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000001-legacy-dddddd", "phase": "drafting",
                "pid": 999999, "pgid": 999999, "session_id": "x",
                "owner": "dead", "owner_pid": 999999,
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        self.supervisor.reconcile()
        self.assertEqual(self.phase_of("r-20250101-000001-legacy-dddddd"), "failed")


class BoardOwnershipTest(SupervisorCase):
    """Ownership checks are three-valued too, for the same reason identity is."""

    def test_an_unreadable_ps_leaves_another_boards_run_alone(self):
        """`ps` failing while checking board A's pid is not evidence that A died.
        Reading it that way hands A's live run to this board, which marks it
        orphaned and offers a Kill button pointed at another window's work."""
        other = self.spawn_stub()
        record = {"owner_pid": other.pid, "owner": "board-a",
                  "owner_started": run_registry.process_start_time(other.pid)}
        self.assertIs(run_registry.board_alive(record), True)
        with mock.patch.object(run_registry, "_ps", return_value=None):
            # Unknown, not False. Saying "dead" hands a live board's run to this
            # one; saying "alive" would let a recycled owner pid permanently
            # block Kill on a genuine orphan. Reconciliation surfaces an unknown;
            # Kill refuses to signal one.
            self.assertIsNone(run_registry.board_alive(record))

    def test_a_genuinely_different_start_time_means_the_board_is_gone(self):
        other = self.spawn_stub()
        self.assertIs(run_registry.board_alive(
            {"owner_pid": other.pid, "owner": "board-a",
             "owner_started": "Mon Jan  1 00:00:00 2001"}), False)

    def test_a_record_with_no_owner_at_all_is_unknown_not_unowned(self):
        """The distinction B1 turned on: an ownerless record may belong to an
        older board that is still running."""
        self.assertIsNone(run_registry.board_alive({"pid": 1234}))

    def test_kill_refuses_when_the_owning_board_turns_out_to_be_alive(self):
        """A run can only be `orphaned` if its board looked dead at startup - and
        "looked dead" can mean `ps` was briefly unreadable."""
        other = self.spawn_stub(marker="sess-live")
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000004-owned-999999", "phase": "orphaned",
                "pid": other.pid, "pgid": other.pid, "session_id": "sess-live",
                "proc_started": run_registry.process_start_time(other.pid),
                "owner": "another-board", "owner_pid": other.pid,
                "owner_started": run_registry.process_start_time(other.pid),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)

        code, body = self.supervisor.kill("r-20250101-000004-owned-999999")
        self.assertEqual(code, 409)
        self.assertIn("still running", body["error"])
        self.assertIsNone(other.poll(), "another board's process was signalled")


class RunIdTest(SupervisorCase):
    def test_the_registry_refuses_a_duplicate_id(self):
        """The random suffix makes a same-second collision unlikely; this is what
        makes it impossible. `start()` re-rolls until the insert is accepted."""
        record = {"id": "r-20260101-000000-acme-abcdef", "phase": "queued"}
        run_registry.append(record)
        with self.assertRaises(ValueError):
            run_registry.append(dict(record))

    def test_two_roles_at_one_company_do_not_share_a_run_directory(self):
        first = self.start(url="https://example.com/a", role="ML Engineer")[1]
        second = self.start(url="https://example.com/b", role="Data Engineer")[1]
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["targets"]["cv"], second["targets"]["cv"])


class TerminationTest(SupervisorCase):
    def test_a_child_that_ignores_sigterm_is_still_killed(self):
        """`claude` exits promptly on SIGTERM; a descendant that ignores it does
        not. Waiting on the group leader alone declares victory too early."""
        from board import run_proc
        script = ("import os, signal, time, sys\n"
                  "os.setsid()\n"
                  "pid = os.fork()\n"
                  "if pid == 0:\n"
                  "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "    time.sleep(60)\n"
                  "    sys.exit(0)\n"
                  "print(os.getpgid(0), flush=True)\n"
                  "time.sleep(60)\n")
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)
        pgid = int(proc.stdout.readline().strip())

        sent = run_proc.terminate(pgid, proc.pid, grace=1.0)
        self.assertEqual(sent, ["SIGTERM", "SIGKILL"],
                         "escalation stopped while a group member was still alive")
        self.assertIsNone(wait_for(lambda: not run_proc.group_alive(pgid) or None,
                                   timeout=10) and None)

    def test_group_alive_reports_the_group_not_the_leader(self):
        from board import run_proc
        self.assertFalse(run_proc.group_alive(999999, 999999))
        self.assertTrue(run_proc.group_alive(os.getpgid(0)))


class RestartTest(SupervisorCase):
    def test_an_approval_that_never_reached_the_queue_returns_to_the_gate(self):
        """Kill the board between `approve()` persisting `queued` and the worker
        dequeuing it. The evaluation was paid for; it must not be discarded - and
        the new board must not silently start spending on boot either."""
        run_id = self.through_gate()
        with run_registry.runs_lock():
            data = run_registry.load()
            for run in data["runs"]:
                if run["id"] == run_id:
                    run["phase"] = "queued"
                    run["approved_at"] = "2026-08-19T12:00:00"
                    run["owner_pid"] = 999999      # a board that is gone
                    run["owner"] = "dead-board"
            run_registry.store(data)

        fresh = runs.Supervisor()
        self.addCleanup(fresh.stop)
        fresh.reconcile()
        record = run_registry.get(run_id)
        self.assertEqual(record["phase"], "awaiting_approval")
        self.assertIsNone(record["approved_at"])
        self.assertIsNotNone(record["fit"], "the evaluation was thrown away")

    def test_a_cancelled_run_is_not_resurrected_by_its_own_output(self):
        """Cancel lands between the process exiting and the phase being
        published. The run must stay cancelled."""
        run_id = self.through_gate()
        run_registry.update(run_id, phase="cancelled", error="cancelled by hand")
        self.supervisor._read_fit(run_id)
        self.assertEqual(self.phase_of(run_id), "cancelled")

    def test_a_cancelled_run_is_not_marked_done_by_a_finished_pass_b(self):
        run_id = self.through_gate()
        run_registry.update(run_id, phase="cancelled", error="cancelled by hand")
        self.supervisor._settle_ok(run_id, {"cv": "cv/x.tex"})
        self.assertEqual(self.phase_of(run_id), "cancelled")


class CancelPersistenceTest(SupervisorCase):
    """Cancel has to survive the window between a pass exiting and its phase
    being published - the window in which the run is not queued, not active, and
    still about to be written."""

    def test_cancel_after_the_process_exits_is_not_overwritten_by_the_fit(self):
        run_id = self.through_gate()
        # Put the run back in the state it is in the instant pass A exits.
        run_registry.update(run_id, phase="evaluating")
        code, _ = self.supervisor.cancel(run_id)
        self.assertEqual(code, 200)
        self.assertEqual(self.phase_of(run_id), "cancelled")
        # ...and now the pipeline's next line runs.
        self.supervisor._read_fit(run_id)
        self.assertEqual(self.phase_of(run_id), "cancelled")

    def test_cancel_is_not_overwritten_by_a_finished_pass_b(self):
        run_id = self.through_gate()
        run_registry.update(run_id, phase="drafting")
        self.supervisor.cancel(run_id)
        self.supervisor._settle_ok(run_id, {"cv": "cv/x.tex"})
        self.assertEqual(self.phase_of(run_id), "cancelled")

    def test_a_late_failure_does_not_relabel_a_cancelled_run(self):
        run_id = self.through_gate()
        run_registry.update(run_id, phase="drafting")
        self.supervisor.cancel(run_id)
        self.supervisor._fail(run_id, "some later failure")
        record = run_registry.get(run_id)
        self.assertEqual(record["phase"], "cancelled")
        self.assertEqual(record["error"], "cancelled")

    def test_the_reviewer_sub_phase_cannot_revive_a_cancelled_run(self):
        run_id = self.through_gate()
        run_registry.update(run_id, phase="drafting")
        self.supervisor.cancel(run_id)
        run_registry.transition(run_id, "reviewing", ("evaluating", "drafting"))
        self.assertEqual(self.phase_of(run_id), "cancelled")

    def test_cancelling_twice_reports_the_second_as_a_conflict(self):
        run_id = self.through_gate()
        self.assertEqual(self.supervisor.cancel(run_id)[0], 200)
        self.assertEqual(self.supervisor.cancel(run_id)[0], 409)


class OrphanIdentityTest(SupervisorCase):
    def test_identity_that_cannot_be_proven_is_not_a_match(self):
        """A record with neither a start time nor a session marker names a pid
        and nothing else. Signalling on that is signalling a number."""
        self.assertFalse(run_registry.process_matches({"pid": os.getpid()}))

    def test_both_recorded_checks_must_pass_for_a_strict_match(self):
        stub = self.spawn_stub(marker="sess-abc")
        base = {"pid": stub.pid, "session_id": "sess-abc",
                "proc_started": run_registry.process_start_time(stub.pid)}
        self.assertTrue(run_registry.process_matches(base))
        self.assertFalse(run_registry.process_matches(dict(base, session_id="other")))
        self.assertFalse(run_registry.process_matches(
            dict(base, proc_started="Mon Jan  1 00:00:00 2001")))

    def test_an_unreadable_ps_surfaces_but_does_not_signal(self):
        """`ps` failing under load is "cannot tell", not "not ours". Treating it
        as a negative made a live orphan's classification depend on how busy the
        machine was; treating it as a positive would let Kill signal on a guess.
        """
        stub = self.spawn_stub(marker="sess-xyz")
        record = {"pid": stub.pid, "session_id": "sess-xyz",
                  "proc_started": run_registry.process_start_time(stub.pid)}
        with mock.patch.object(run_registry, "_ps", return_value=None):
            self.assertTrue(run_registry.process_matches(record, strict=False))
            self.assertFalse(run_registry.process_matches(record, strict=True))

    def test_a_disproved_check_is_not_surfaced_either(self):
        stub = self.spawn_stub(marker="sess-xyz")
        record = {"pid": stub.pid, "session_id": "definitely-not-in-the-argv"}
        self.assertFalse(run_registry.process_matches(record, strict=False))

    def test_kill_revalidates_before_signalling(self):
        """Minutes pass while the orphan card sits on screen. By the time Kill is
        clicked the pid may belong to something else entirely."""
        stub = self.spawn_stub(marker="not-this-run")
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000003-recyc-ffffff", "phase": "orphaned",
                "pid": stub.pid, "pgid": stub.pid, "session_id": "a-different-session",
                "proc_started": run_registry.process_start_time(stub.pid),
                "owner": run_registry.OWNER, "owner_pid": os.getpid(),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)

        code, body = self.supervisor.kill("r-20250101-000003-recyc-ffffff")
        self.assertEqual(code, 200)
        self.assertEqual(body["signals"], [])
        self.assertIsNone(stub.poll(), "an unrelated process was signalled")
        self.assertIn("no longer identifiable",
                      run_registry.get("r-20250101-000003-recyc-ffffff")["error"])


class WrapperIsolationTest(SupervisorCase):
    def test_the_wrapper_refuses_to_run_without_its_own_session(self):
        """Without `setsid` the recorded pgid is the *board's* group, and the
        first Cancel `killpg`s the board itself."""
        import subprocess
        spec = Path(self.tmp.name) / "spec.json"
        spec.write_text(json.dumps({
            "run_id": "r-x", "argv": [sys.executable, "-c", "print('should not run')"],
            "cwd": str(self.home), "registry": str(run_registry.REGISTRY),
            "runs_lock": str(run_registry.RUNS_LOCK),
            "model_lock": str(run_registry.MODEL_LOCK), "env": {}}), encoding="utf-8")

        wrapper = ROOT / "tools" / "board" / "run_wrapper.py"
        # Run it as a process-group leader already, so `setsid()` fails with EPERM.
        code = ("import os, runpy, sys\n"
                "os.setsid()\n"
                "sys.argv = ['run_wrapper.py', '--spec', %r]\n"
                "runpy.run_path(%r, run_name='__main__')\n" % (str(spec), str(wrapper)))
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("should not run", proc.stdout)
        self.assertIn("session", proc.stderr.lower())


class StopTest(SupervisorCase):
    def test_stop_during_preparation_never_spawns(self):
        """The worker had dequeued the run and was resolving the salary benchmark;
        `stop()` used to see neither a queued nor an active run and return."""
        started = threading.Event()
        original = self.supervisor._salary

        def slow(record):
            started.set()
            time.sleep(2.0)
            return original(record)

        self.supervisor._salary = slow
        code, body = self.start()
        run_id = body["run_id"]
        self.assertTrue(started.wait(20))
        self.supervisor.stop(timeout=20)
        self.assertFalse((run_registry.run_dir(run_id) / "fit.json").exists(),
                         "a model process ran after stop() returned")
        self.assertIn(self.phase_of(run_id), ("cancelled", "failed"))


class ReconcileTest(SupervisorCase):
    def test_a_dead_run_from_a_previous_board_becomes_failed(self):
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({"id": "r-20260101-000000-acme", "phase": "drafting",
                                 "pid": 999999, "pgid": 999999, "session_id": "gone",
                                 "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        self.assertEqual(self.supervisor.reconcile(), [])
        self.assertEqual(self.phase_of("r-20260101-000000-acme"), "failed")

    def test_a_live_orphan_is_surfaced_rather_than_adopted(self):
        # `spawn_stub` starts it in its own session - what `run_wrapper.py`'s
        # `setsid` does for a real run, and what makes `killpg(pgid)` reach it -
        # and waits until its argv is visible to `ps`.
        marker = "jobflow-orphan-marker"
        proc = self.spawn_stub(marker=marker)
        with run_registry.runs_lock():
            data = run_registry.load()
            # A board that is genuinely gone: its pid is dead, so ownership is
            # *disproved* rather than unknown, and Kill is allowed to act.
            data["runs"].append({"id": "r-20260101-000001-acme", "phase": "drafting",
                                 "pid": proc.pid, "pgid": proc.pid, "session_id": marker,
                                 "owner": "a-dead-board", "owner_pid": 999999,
                                 "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        # The observable contract is the end state, not this call's return value:
        # `reconcile()` reports what *it* adopted, so anything that reconciled
        # first (the module singleton, in a full-suite run) makes the list empty
        # while the run is correctly orphaned.
        self.supervisor.reconcile()
        self.assertEqual(self.phase_of("r-20260101-000001-acme"), "orphaned")

        # Kill refuses to signal when it cannot confirm the identity - `ps` can be
        # briefly unreadable under load - and answers 409 so the button still
        # works. Retrying is what a person does, and it is what the test does.
        code = None
        for _ in range(10):
            code, _body = self.supervisor.kill("r-20260101-000001-acme")
            if code != 409:
                break
            time.sleep(0.2)
        self.assertEqual(code, 200)
        self.assertEqual(self.phase_of("r-20260101-000001-acme"), "failed")
        self.assertIsNotNone(wait_for(lambda: proc.poll() is not None))

    def test_kill_neither_signals_nor_gives_up_when_identity_is_unreadable(self):
        """`ps` failing is not "gone" and not "ours" - the run stays orphaned and
        the button stays usable."""
        stub = self.spawn_stub(marker="sess-unreadable")
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000005-unread-777777", "phase": "orphaned",
                "pid": stub.pid, "pgid": stub.pid, "session_id": "sess-unreadable",
                "proc_started": run_registry.process_start_time(stub.pid),
                "owner": run_registry.OWNER, "owner_pid": os.getpid(),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)

        with mock.patch.object(run_registry, "_ps", return_value=None):
            code, body = self.supervisor.kill("r-20250101-000005-unread-777777")
        self.assertEqual(code, 409)
        self.assertIn("Try again", body["error"])
        self.assertEqual(self.phase_of("r-20250101-000005-unread-777777"), "orphaned")
        self.assertIsNone(stub.poll())


class PreflightTest(unittest.TestCase):
    def test_the_shipped_settings_file_is_the_shape_the_guard_expects(self):
        self.assertTrue(run_guard.validate_settings())

    def test_a_malformed_settings_file_is_caught_here_because_the_cli_will_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "settings.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(run_guard.PreflightError) as caught:
                run_guard.validate_settings(bad)
            self.assertIn("no guard", str(caught.exception))

    def test_a_narrowed_matcher_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            narrow = Path(tmp) / "settings.json"
            narrow.write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": "Write", "hooks": [
                    {"type": "command", "command": "python3 tools/board/guard_write.py"}]}]}}),
                encoding="utf-8")
            with self.assertRaises(run_guard.PreflightError):
                run_guard.validate_settings(narrow)

    def test_the_guard_is_executed_for_real_before_every_run(self):
        self.assertTrue(run_guard.probe_guard())


class SlugTest(unittest.TestCase):
    def test_the_slug_matches_the_documents_convention(self):
        self.assertEqual(run_registry.slugify("DeepJudge", "Applied AI Engineer"),
                         "deepjudge_applied_ai_engineer")
        self.assertEqual(run_registry.slugify("Zürich Bank AG", "Data/ML Eng."),
                         "z_rich_bank_ag_data_ml_eng")

    def test_a_run_id_is_a_run_id(self):
        from board import server
        run_id = run_registry.new_run_id("Acme AG")
        self.assertEqual(server.run_route("/api/runs/" + run_id), (run_id, None))
        self.assertEqual(server.run_route("/api/runs/" + run_id + "/approve"),
                         (run_id, "approve"))
        self.assertEqual(server.run_route("/api/runs/" + run_id + "/retry"),
                         (run_id, "retry"))
        self.assertEqual(server.run_route("/api/runs/../../etc/passwd"), (None, None))


if __name__ == "__main__":
    unittest.main()
