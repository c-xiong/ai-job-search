"""Optional provider contracts against anonymous temporary workspaces."""
import json
import os
import shutil
from pathlib import Path
from unittest import mock

from tests.board_harness import SupervisorCase, ROOT, wait_for
from board import checkpoint, docs, providers, run_registry, runs
from board.run_proc import RunFailure


class CodexProviderTest(SupervisorCase):
    automated_review = False

    def setUp(self):
        super().setUp()
        self.codex = self.home / "fake-codex"
        shutil.copy(ROOT / "tests/fake_codex.py", self.codex)
        self.codex.chmod(0o755)
        self.write_config(codex_bin=str(self.codex))
        self.addCleanup(os.environ.pop, "FAKE_CODEX", None)

    def run_to_end(self, **extra):
        run_id, _ = super().run_to_end(**extra)
        return run_registry.get(run_id)

    def test_optional_default_and_unknown_cost(self):
        run = self.run_to_end(provider="codex")
        self.assertEqual(run["phase"], "done", run.get("error"))
        self.assertEqual(run["provider"], "codex")
        self.assertIsNone(run["cost"]["total_usd"])
        self.assertIsNone(run["usage"][0]["cost_usd"])
        self.assertEqual(run["usage"][0]["tokens"]["input_tokens"], 100)
        self.assertEqual(run_registry.application_spent(run["id"]), 0)
        self.assertTrue(list(run_registry.state_dir(run["id"]).glob("codex-*.json")))
        self.assertFalse(list(run_registry.state_dir(run["id"]).glob("hook-*.jsonl")))
        self.assertEqual(self.supervisor.snapshot()["default_provider"], "claude")

    def test_claude_remains_default(self):
        run = self.run_to_end()
        self.assertEqual(run["provider"], "claude")
        self.assertEqual(run["phase"], "done", run.get("error"))

    def test_invalid_provider_and_unavailable_binary(self):
        self.assertEqual(self.start(provider="other")[0], 400)
        self.assertEqual(self.start(provider="")[0], 400)
        self.write_config(codex_bin="missing-jobflow-cli")
        self.assertEqual(self.start(provider="codex")[0], 503)

    def test_codex_does_not_use_claude_dollar_admission(self):
        self.write_config(codex_bin=str(self.codex), daily_budget_usd=0, session_budget_usd=0)
        run = self.run_to_end(provider="codex")
        self.assertEqual(run["phase"], "done", run.get("error"))
        self.assertTrue(run["usage"])

    def test_invalid_outputs_never_publish(self):
        for index, mode in enumerate(("wrong_nonce", "extra", "missing", "null", "truncated", "no_result")):
            with self.subTest(mode=mode):
                os.environ["FAKE_CODEX"] = mode
                run = self.run_to_end(provider="codex", url="https://example.com/jobs/%d" % index)
                self.assertEqual(run["phase"], "failed")
                self.assertFalse(run.get("artefacts"))
                self.assertFalse(list(run_registry.state_dir(run["id"]).glob("codex-*.json")))

    def test_successful_document_can_mention_quota(self):
        os.environ["FAKE_CODEX"] = "quota_text"
        run = self.run_to_end(provider="codex")
        self.assertEqual(run["phase"], "done", run.get("error"))

    def test_quota_is_actionable_without_claude_canary(self):
        os.environ["FAKE_CODEX"] = "quota"
        run = self.run_to_end(provider="codex")
        self.assertEqual(run["failure_code"], "quota_exhausted")
        self.assertNotIn("canary", run["error"])

    def test_claude_to_codex_continue(self):
        os.environ["FAKE_MODE"] = "quota"
        old = self.run_to_end()
        self.assertEqual(old["phase"], "failed")
        code, response = self.supervisor.continue_run(old["id"], {"provider": "codex"})
        self.assertEqual(code, 202, response)
        self.wait_phase(response["run_id"], "done", "failed")
        run = run_registry.get(response["run_id"])
        self.assertEqual(run["phase"], "done", run.get("error"))
        self.assertEqual(run["provider"], "codex")
        self.assertEqual(run["continue_of"], old["id"])
        code2, again = self.supervisor.continue_run(old["id"], {"provider": "claude"})
        self.assertEqual(again["run_id"], run["id"])
        self.assertEqual(code2, 200)

    def test_regenerate_inherits_provider(self):
        old = self.run_to_end(provider="codex")
        code, response = self.supervisor.retry(old["id"])
        self.assertEqual(code, 202)
        self.wait_phase(response["run_id"], "done", "failed")
        self.assertEqual(run_registry.get(response["run_id"])["provider"], "codex")

    def test_hard_conflict_withholds_documents(self):
        os.environ["FAKE_CODEX"] = "conflict"
        run = self.run_to_end(provider="codex", scope="cover")
        self.assertEqual(run["failure_code"], "hard_conflict", run.get("error"))

    def test_automated_review_uses_fresh_codex_pass(self):
        self.write_config(codex_bin=str(self.codex), automated_review=True)
        run = self.run_to_end(provider="codex")
        self.assertEqual(run["phase"], "done", run.get("error"))
        self.assertIn("review", [u["stage"] for u in run["usage"]])

    def test_timeout_and_cancel(self):
        os.environ["FAKE_CODEX"] = "hang"
        self.write_config(codex_bin=str(self.codex), timeout_s={"draft": 1})
        run = self.run_to_end(provider="codex")
        self.assertEqual(run["failure_code"], "timeout")
        self.assertFalse(run.get("artefacts"))

    def test_input_change_rejected_before_any_write(self):
        target = self.home / "output.tex"
        target.write_text("seed")
        record = {"id": "contract", "provider": "codex"}
        request = providers.CodexRequest(record, "draft", "", [target], "nonce")
        self.addCleanup(request.close)
        target.write_text("changed by owner")
        with self.assertRaisesRegex(RunFailure, "input changed"):
            request.accept(json.dumps({"response_id": "nonce", "artifact_0":
                                       "\\begin{document}Hello\\end{document}"}))
        self.assertEqual(target.read_text(), "changed by owner")

    def test_inspection_accepts_only_current_attached_hashes(self):
        pdf = self.home / "test.pdf"
        pdf.write_bytes(b"current")
        payload = {"schema": "jobflow.inspect/1", "verdict": "clean", "issues": []}
        digest = providers._digest(pdf)
        self.assertTrue(docs.judge_inspection({"cover": pdf}, payload, True, "unused",
                        attached_pdf_hashes={str(pdf.resolve()): digest})[0])
        pdf.write_bytes(b"changed")
        self.assertFalse(docs.judge_inspection({"cover": pdf}, payload, True, "unused",
                         attached_pdf_hashes={str(pdf.resolve()): digest})[0])

    def test_process_identity_does_not_use_previous_thread_id(self):
        record = {"pid": 123, "provider": "codex", "session_id": "old-thread",
                  "proc_started": "start", "process_marker": "/tmp/unique-stage"}
        with mock.patch.object(run_registry, "process_alive", return_value=True), \
                mock.patch.object(run_registry, "process_start_time", return_value="start"), \
                mock.patch.object(run_registry, "_argv_contains",
                                  side_effect=lambda pid, marker: marker == "/tmp/unique-stage"):
            self.assertTrue(run_registry.process_matches(record))
            self.assertTrue(run_registry.process_matches(record, strict=False))

    def test_admission_freezes_selection(self):
        self.supervisor.stop()
        self.supervisor = runs.Supervisor()
        self.addCleanup(self.supervisor.stop)
        with mock.patch.object(self.supervisor, "ensure_worker"):
            code, body = self.start(provider="codex")
        self.assertEqual(code, 202)
        self.write_config(provider="claude", codex_bin="now-unavailable")
        self.supervisor.ensure_worker()
        self.settle(body["run_id"])
        run = run_registry.get(body["run_id"])
        self.assertEqual(run["phase"], "done", run.get("error"))
        self.assertEqual(run["execution"]["binary"], str(self.codex))

    def test_codex_provenance_is_invalidated_by_changed_bytes(self):
        run = self.run_to_end(provider="codex")
        outputs = checkpoint.approved_writes(run["id"])
        self.assertTrue(outputs)
        target = next(iter(outputs))
        Path(target).write_text("changed")
        self.assertNotIn(target, checkpoint.approved_writes(run["id"]))

    def test_mechanical_continue_does_not_require_codex(self):
        self.compile_fail.add("cv")
        failed = self.run_to_end(provider="codex")
        self.assertEqual(failed["phase"], "failed")
        self.compile_fail.clear()
        self.write_config(codex_bin="missing-cli")
        code, body = self.supervisor.continue_run(failed["id"])
        self.assertEqual(code, 202, body)
        self.settle(body["run_id"])
        run = run_registry.get(body["run_id"])
        self.assertEqual(run["phase"], "done", run.get("error"))
        self.assertEqual(run["usage"], [])


class ProviderRoutesTest(SupervisorCase):
    def test_all_provider_routes_forward_selection(self):
        from tests.test_board_inbox_routes import request
        from board import server
        run_id = "r-20261003-120000-acme-abcdef"
        supervisor = mock.Mock()
        for method in ("start", "continue_run", "retry"):
            getattr(supervisor, method).return_value = (202, {"run_id": run_id})
        with mock.patch.object(server.runs, "supervisor", return_value=supervisor):
            for suffix, method in (("", "start"), ("/" + run_id + "/continue", "continue_run"),
                                   ("/" + run_id + "/retry", "retry")):
                code, _ = request("POST", "/api/runs" + suffix, {"provider": "codex"})
                self.assertEqual(code, 202)
                self.assertEqual(getattr(supervisor, method).call_args.args[-1]["provider"], "codex")
