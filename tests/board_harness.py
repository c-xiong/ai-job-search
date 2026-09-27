"""Shared harness for the run-supervisor tests.

Every path the supervisor touches is redirected into a temporary directory that
holds an anonymous master CV, cover base, profile and CLAUDE.md - never the
owner's private files. The model is `tests/fake_claude.py`, which calls the real
write guard, so the boundary is exercised for real. Compilation and text
extraction are replaced by deterministic fakes (their real behaviour is covered
in `test_board_docs.py` and by CI's LaTeX smoke job).
"""

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from board import activity, checkpoint, docs, run_registry, runs  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_claude.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "latex"
POSTING = ("# Acme - ML Engineer\n\nWe are hiring an ML engineer in Zurich. "
           "Requirements: 3+ years of Python, experience with LLM systems and RAG, "
           "and clear written communication. You will build and evaluate retrieval "
           "pipelines together with the platform team.\n") * 3


def wait_for(predicate, timeout=60, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


class SupervisorCase(unittest.TestCase):
    maxDiff = None
    inspection = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "repo"
        for sub in ("job_scraper", "cv", "cover_letters",
                    ".claude/skills/job-application-assistant"):
            (home / sub).mkdir(parents=True)
        # The master is a symlink into a separate "upstream" directory, exactly
        # like the owner's setup - so write-through protection is tested on the
        # real shape, not on a plain file.
        upstream = Path(self.tmp.name) / "upstream"
        upstream.mkdir()
        shutil.copy(FIXTURES / "cv_fixture.tex", upstream / "main.tex")
        (home / "cv" / "my_cv.tex").symlink_to(upstream / "main.tex")
        self.master = upstream / "main.tex"
        shutil.copy(FIXTURES / "cover_fixture.tex", home / "cover_letters" / "my_cover.tex")
        (home / ".claude/skills/job-application-assistant/01-candidate-profile.md").write_text(
            "# Profile\n\nAnonymous test profile.\n", encoding="utf-8")
        (home / "CLAUDE.md").write_text("# Test\n\n- Email: jane.placeholder@example.com\n",
                                        encoding="utf-8")

        fake = home / "fake-claude"
        shutil.copy(FAKE, fake)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.fake = fake
        self.calls = Path(self.tmp.name) / "calls.jsonl"

        self.write_config()
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
                           ("FAKE_MODE", "auto"), ("FAKE_CALLS", str(self.calls))):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        for key in ("FAKE_ONLY", "FAKE_DRAFT", "FAKE_REVIEW", "FAKE_INSPECT",
                    "FAKE_INSPECT_DOC", "FAKE_PREPARE"):
            os.environ.pop(key, None)
            self.addCleanup(os.environ.pop, key, None)

        self.home = home
        self.compile_fail = set()
        self.compiled = []
        self.fetch_result = (POSTING, "")

        def fake_compile(kind, source, build=None):
            source = Path(source)
            if not source.is_absolute():
                source = run_registry.ROOT / source
            if kind in self.compile_fail:
                raise docs.DocumentError("%s compile failed (exit 1): ! Undefined "
                                         "control sequence." % kind)
            build = Path(build) if build else source.parent / "build"
            build.mkdir(parents=True, exist_ok=True)
            pdf = build / (source.stem + ".pdf")
            digest = hashlib.sha256(source.read_bytes()).hexdigest().encode()
            pdf.write_bytes(b"%PDF-1.4\n1 0 obj <</Type /Page>> endobj\n% " + digest +
                            b"\n%%EOF")
            self.compiled.append((kind, str(source)))
            return pdf, {"cmd": ["pdflatex", str(source)], "exit": 0, "toolchain": "latex",
                         "stdout_tail": [], "stderr_tail": [], "errors": []}

        patches = [
            mock.patch.object(runs.docs, "compile_one", side_effect=fake_compile),
            mock.patch.object(runs.docs, "extract_text", return_value=(
                "Jane Placeholder\njane.placeholder@example.com\nExperience\nLLM and RAG "
                "pipelines\nEducation", {"exit": 0})),
            mock.patch.object(runs.docs, "archive_posting",
                              return_value="documents/applications/test/job_posting.md"),
            mock.patch.object(runs.docs, "merge_tracker", return_value="appended"),
            mock.patch.object(runs, "fetch_posting_text",
                              side_effect=lambda url: self.fetch_result),
        ]
        self.mocks = {}
        for patcher in patches:
            self.mocks[patcher.attribute] = patcher.start()
            self.addCleanup(patcher.stop)
        self.supervisor = runs.Supervisor()
        self.supervisor.ensure_worker()
        self.addCleanup(self.supervisor.stop)
        activity.reset_for_tests()

    def write_config(self, **extra):
        stage = 25
        config = {"claude_bin": str(self.fake), "canary_timeout_s": 8,
                  "timeout_s": {k: stage for k in ("prepare", "draft", "review", "fix",
                                                   "pass_c", "pass_a", "pass_b")},
                  "inspection_enabled": self.inspection,
                  # Most suites exercise the automated review loop; the manual
                  # default has its own tests.
                  "automated_review": getattr(self, "automated_review", True),
                  "daily_budget_usd": 50.0, "session_budget_usd": 50.0}
        config.update(extra)
        path = (getattr(self, "home", None) or Path(self.tmp.name) / "repo") / \
            "job_scraper" / "board_config.json"
        path.write_text(json.dumps(config), encoding="utf-8")

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

    def wait_phase(self, run_id, *phases, **kw):
        return wait_for(lambda: self.phase_of(run_id) if self.phase_of(run_id) in phases
                        else None, timeout=kw.get("timeout", 60))

    def run_to_end(self, **extra):
        code, body = self.start(**extra)
        self.assertEqual(code, 202, body)
        run_id = body["run_id"]
        phase = self.wait_phase(run_id, "done", "failed", "cancelled")
        self.assertIsNotNone(phase, "run never settled")
        return run_id, phase

    def settle(self, run_id):
        return self.wait_phase(run_id, "done", "failed", "cancelled")

    def stages(self, run_id=None):
        if not self.calls.exists():
            return []
        rows = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return [r["stage"] for r in rows if run_id is None or r["run"] == run_id]

    def call_rows(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def manifest(self, run_id):
        data, problem = checkpoint.load(run_id)
        self.assertIsNone(problem)
        return data

    def spawn_stub(self, seconds=30, marker=None):
        import subprocess
        argv = [sys.executable, "-c", "import time; time.sleep(%d)" % seconds]
        if marker:
            argv.append(marker)
        proc = subprocess.Popen(argv, start_new_session=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        if marker:
            self.assertIsNotNone(
                wait_for(lambda: marker in (run_registry._ps("args=", proc.pid) or "")),
                "the stub's argv never became visible to ps")
        return proc
