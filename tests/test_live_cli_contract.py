"""The five CLI behaviours this design rests on, checked against the real binary.

`tests/fake_claude.py` reproduces the CLI's *shape*. It cannot tell you whether
the CLI still fails open on a malformed `--settings` file, still propagates a
`PreToolUse` hook into a `Task` subagent, or still overshoots `--max-budget-usd` -
because the fake is written to the same assumptions the supervisor is. A fake
agreeing with the code that wrote it is not evidence.

DESIGN §12 lists these as the contract tests that gate M2. They were measured by
hand on CLI 2.1.159 on 2026-08-19. This file makes them re-runnable, so the next
CLI upgrade is a test failure rather than a surprise during a paid run.

**They are skipped by default, and two of them cost money.**

    JOBFLOW_LIVE_CLI=1 python3 -m unittest tests.test_live_cli_contract      # free
    JOBFLOW_LIVE_CLI=1 JOBFLOW_LIVE_CLI_SPEND=1 python3 -m unittest tests.test_live_cli_contract

The free ones exercise paths that abort before any API call. The paid ones cost
a few cents each and are capped with `--max-budget-usd` regardless.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from board import run_guard  # noqa: E402

LIVE = os.environ.get("JOBFLOW_LIVE_CLI") == "1"
SPEND = os.environ.get("JOBFLOW_LIVE_CLI_SPEND") == "1"
CLAUDE = shutil.which("claude")

live = unittest.skipUnless(LIVE and CLAUDE,
                           "set JOBFLOW_LIVE_CLI=1 (and have `claude` on PATH)")
paid = unittest.skipUnless(LIVE and CLAUDE and SPEND,
                           "costs money: also set JOBFLOW_LIVE_CLI_SPEND=1")


def run_cli(args, cwd, timeout=180, env=None):
    return subprocess.run([CLAUDE] + args, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout, env=env or dict(os.environ))


def events(stdout):
    out = []
    for line in stdout.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def result_of(stdout):
    return next((e for e in reversed(events(stdout)) if e.get("type") == "result"), None)


class Sandbox(unittest.TestCase):
    """A throwaway directory with a real hook wired up, exactly as a run has."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.run_dir = self.home / "run"
        self.run_dir.mkdir()

        self.allowlist = self.home / "allowlist.json"
        self.allowlist.write_text(json.dumps({
            "dirs": [str(self.run_dir.resolve())], "files": [],
            "deny": [str((self.run_dir / ".guard-probe-live").resolve())]}), encoding="utf-8")
        self.hook_log = self.home / "hook.jsonl"

        self.settings = self.home / "settings.json"
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": run_guard.EXPECTED_MATCHER,
             "hooks": [{"type": "command",
                        "command": "python3 %s" % run_guard.GUARD}]}]}}), encoding="utf-8")

        self.env = dict(os.environ,
                        JOBFLOW_ALLOWLIST=str(self.allowlist),
                        JOBFLOW_HOOK_LOG=str(self.hook_log),
                        JOBFLOW_RUN_ID="live-contract")

    def hook_entries(self):
        if not self.hook_log.exists():
            return []
        return [json.loads(l) for l in self.hook_log.read_text(encoding="utf-8").splitlines()]


@live
class SettingsContractTest(Sandbox):
    def test_a_missing_settings_file_aborts_before_any_api_call(self):
        """Fails closed. This is why a wrong path is safe."""
        proc = run_cli(["-p", "say hi", "--settings", str(self.home / "nope.json")], self.home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ettings file not found", proc.stdout + proc.stderr)

    def test_a_malformed_settings_file_does_not_abort(self):
        """Fails **open**, which is the entire reason `run_guard.validate_settings()`
        exists. If this test ever starts failing, the CLI got safer and the
        pre-flight can be relaxed - but not before."""
        broken = self.home / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        proc = run_cli(["-p", "say hi", "--settings", str(broken),
                        "--max-budget-usd", "0.01"], self.home)
        blob = proc.stdout + proc.stderr
        self.assertNotIn("ettings file not found", blob)
        # It got far enough to hit the budget or to answer - either way, it ran
        # with no hook installed.
        self.assertTrue(proc.returncode != 0 or blob.strip(),
                        "expected the run to proceed past settings parsing")


@paid
class HookEnforcementTest(Sandbox):
    def test_a_pretooluse_deny_blocks_a_write_under_accept_edits(self):
        target = self.run_dir / ".guard-probe-live"
        proc = run_cli([
            "-p", "Use the Write tool to create the file %s containing the word probe. "
                  "Report whether it succeeded." % target,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits",
            "--settings", str(self.settings),
            "--max-budget-usd", "0.30",
            "--allowedTools", "Write",
        ], self.home, env=self.env)
        self.assertFalse(target.exists(), "the guard did not stop the write")
        probes = [e for e in self.hook_entries() if e["probe"]]
        self.assertTrue(probes, "the hook never fired: %s" % (proc.stdout[-500:],))
        self.assertEqual(probes[0]["decision"], "deny")

    def test_the_hook_reaches_a_task_subagent(self):
        """`/apply` Step 3 does its heaviest work inside a `Task`. If hooks did
        not follow a subagent's tool calls, every claim about the write boundary
        would be false for exactly that step."""
        target = self.run_dir / ".guard-probe-live"
        proc = run_cli([
            "-p", "Use the Task tool to dispatch a general-purpose subagent, and instruct "
                  "it to use the Write tool to create %s containing the word probe. "
                  "Report what the subagent said happened." % target,
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits",
            "--settings", str(self.settings),
            "--max-budget-usd", "1.00",
            "--allowedTools", "Task,Write",
        ], self.home, timeout=300, env=self.env)
        self.assertFalse(target.exists(), "a subagent wrote outside the allowlist")
        probes = [e for e in self.hook_entries() if e["probe"]]
        self.assertTrue(probes, "the hook did not reach the subagent: %s" % (proc.stdout[-500:],))
        self.assertEqual(probes[0]["decision"], "deny")

    def test_the_budget_cap_stops_the_run_and_overshoots(self):
        """Both halves matter. The cap works, and it stops *after* the turn that
        crosses the line - which is why `runs.py` says "stops at about" and
        debits the reported figure rather than the cap."""
        proc = run_cli([
            "-p", "Count from 1 to 400, writing out each number in words, one per line.",
            "--output-format", "stream-json", "--verbose",
            "--max-budget-usd", "0.02",
        ], self.home, timeout=300)
        result = result_of(proc.stdout)
        self.assertIsNotNone(result, proc.stdout[-800:] + proc.stderr[-400:])
        self.assertEqual(result.get("subtype"), "error_max_budget_usd")
        self.assertGreater(float(result.get("total_cost_usd") or 0), 0.02,
                           "the cap no longer overshoots - the headroom in "
                           "board_config.json can be reduced")


@paid
class ResumeContractTest(Sandbox):
    """DESIGN §12 item 5, the one that was never measured.

    It degrades safely - §4 probes resumability and falls back to a full re-run -
    so it gates nothing. Recorded here so the answer stops being folklore.
    """

    def test_a_session_can_be_resumed(self):
        session = "00000000-0000-4000-8000-%012d" % (os.getpid() % 10 ** 12)
        first = run_cli(["-p", "Remember the word 'marzipan'. Reply with just: ok",
                         "--session-id", session, "--max-budget-usd", "0.20"], self.home)
        self.assertEqual(first.returncode, 0, first.stderr[-400:])
        second = run_cli(["-p", "What word did I ask you to remember? Reply with just that word.",
                          "--resume", session, "--max-budget-usd", "0.20"], self.home)
        self.assertEqual(second.returncode, 0, second.stderr[-400:])
        self.assertIn("marzipan", second.stdout.lower())


if __name__ == "__main__":
    unittest.main()
