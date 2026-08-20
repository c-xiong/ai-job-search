"""One `claude -p` process: spawn it, read its stream, account for it, kill it.

Split out of `runs.py` so the part that owns a subprocess is separate from the
part that owns a workflow. Everything here is about a single model process and
the three ways it can end badly: it hangs, it runs without a guard, or it spends
more than it was supposed to.

**The cost is recorded before the verdict.** A run that is killed for a missing
canary still reported a `total_cost_usd`, and that money is spent whether or not
its output is trusted. Debiting only successful runs makes the ledger read low
and admits the next run against money that is already gone.

Stdlib only, Python 3.9+.
"""

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import activity, run_guard, run_registry


def _signal(pgid, pid, sig):
    """Signal the group *and* the leader. True when either delivery succeeded.

    Both, not one or the other: `killpg` fails as a unit, and it can be refused
    (`EPERM` on macOS when a member cannot be signalled) while the leader itself
    is perfectly signallable. Treating that refusal as "nothing to do" is how the
    `SIGTERM` → `SIGKILL` escalation silently stops after the first step and
    leaves a model process running. Signalling the leader twice is harmless.
    """
    delivered = False
    for target, call in ((pgid, os.killpg), (pid, os.kill)):
        if not target:
            continue
        try:
            call(int(target), sig)
            delivered = True
        except (OSError, ValueError, TypeError):
            continue
    return delivered


def group_alive(pgid, pid=None):
    """Is anything still running in the run's process group?

    Not "is the leader still running". `claude` exits promptly on `SIGTERM`; a
    descendant that ignores it does not, and waiting on the leader alone means
    `terminate()` returns satisfied while the thing actually spending is still
    alive. `killpg(pgid, 0)` asks about the whole group.
    """
    if pgid:
        try:
            os.killpg(int(pgid), 0)
            return True
        except ProcessLookupError:
            return False
        except (OSError, ValueError, TypeError):
            pass
    return run_registry.process_alive(pid)


def terminate(pgid, pid=None, grace=5.0):
    """SIGTERM the group, SIGKILL whatever survives. Returns what it had to send."""
    sent = []
    if not _signal(pgid, pid, signal.SIGTERM):
        return sent
    sent.append("SIGTERM")
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not group_alive(pgid, pid):
            return sent
        time.sleep(0.1)
    if _signal(pgid, pid, signal.SIGKILL):
        sent.append("SIGKILL")
    return sent


def git_snapshot(root):
    """`git status --porcelain` as a set, or None when git is unavailable.

    DESIGN §2.5's fourth layer: not the boundary, but the detection that says so
    when the boundary has failed. Comparing before and after a pass turns "the
    guard regressed" from something you find out weeks later into a warning on
    the activity strip.
    """
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(root),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {line[3:] for line in proc.stdout.splitlines() if len(line) > 3}


class RunFailure(Exception):
    """A run cannot continue. The message is what the UI shows."""

    def __init__(self, message, code="run_failed", retryable=False,
                 model_started=True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.model_started = model_started


class Pass:
    """One `claude -p` process: spawn it, read its stream, account for it."""

    def __init__(self, run_id, label, argv, env, budget, timeout, nonce,
                 hook_log, model_argv=None, cancelled=None, admission=None):
        self.run_id = run_id
        self.label = label
        self.argv = argv                  # the wrapper's argv, which is what we spawn
        self.model_argv = model_argv or argv   # the claude argv, which is what we report
        self.env = env
        self.budget = budget
        self.timeout = timeout
        self.nonce = nonce
        self.hook_log = Path(hook_log)
        self.session_id = None
        self.result = None
        self.cost = 0.0
        self.cancelled = False
        self.canary_seen = False
        self.canary_failed = False
        self.timed_out = False
        self.exit_code = None
        self._is_cancelled = cancelled or (lambda: False)
        # Shared with the supervisor's `cancel()`: the last cancellation check
        # and `Popen` have to be one indivisible step, or a cancel that lands
        # between them still buys a model process.
        self._admission = admission or threading.Lock()
        self._proc = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._on_task = None

    # -- the canary ---------------------------------------------------------

    def _refresh_canary(self):
        """Has *this pass's* probe been refused?

        The nonce is what makes that question answerable. A shared hook log with
        a bare `.guard-probe` name would let pass B read pass A's denial and
        conclude the guard is installed - which is exactly the window in which
        someone could have broken it, because it is the window a human spends
        looking at the approval card.
        """
        if self.canary_seen:
            return True
        try:
            text = self.hook_log.read_text(encoding="utf-8")
        except OSError:
            return False
        wanted = run_guard.probe_name(self.nonce)
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (entry.get("decision") == "deny"
                    and str(entry.get("target") or "").endswith(wanted)):
                self.canary_seen = True
                return True
        return False

    # -- lifecycle ----------------------------------------------------------

    def cancel(self):
        with self._lock:
            self.cancelled = True
            proc = self._proc
        if proc and proc.poll() is None:
            record = run_registry.get(self.run_id) or {}
            terminate(record.get("pgid"), record.get("pid") or proc.pid)

    def run(self, on_task=None, expected_writes=()):
        """Blocks until the process ends. Returns the parsed `result` event."""
        self._on_task = on_task
        directory = run_registry.run_dir(self.run_id)
        directory.mkdir(parents=True, exist_ok=True)
        stream_path = directory / "stream.jsonl"

        activity.emit("claude", "%s starting - %ds wall clock" %
                      (self.label, self.timeout),
                      cmd=" ".join(self._reportable_argv()), run_id=self.run_id)

        before = git_snapshot(run_registry.ROOT)

        with activity.Timer() as timer:
            # The admission lock is the supervisor's, and `cancel()` holds it
            # while it records the cancellation. So the check and the `Popen`
            # cannot be interleaved by a cancel: either the cancel lands first
            # and no process is created, or the process exists and `cancel()`
            # can see it to kill it.
            with self._admission:
                with self._lock:
                    if self.cancelled or self._is_cancelled():
                        self.cancelled = True
                        raise RunFailure("cancelled")
                    self._proc = subprocess.Popen(
                        self.argv, cwd=str(run_registry.ROOT), env=self.env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
            proc = self._proc

            stderr_tail = deque(maxlen=40)
            reader = threading.Thread(target=self._drain_stderr, args=(proc, stderr_tail),
                                      daemon=True)
            reader.start()
            # The watchdog runs on its own clock rather than inside the read
            # loop: a process that hangs having printed nothing produces no
            # lines, and a deadline that is only checked when a line arrives
            # never fires on exactly the run that needs it most.
            watchdog = threading.Thread(target=self._watchdog, daemon=True)
            watchdog.start()

            try:
                with open(stream_path, "a", encoding="utf-8") as sink:
                    for line in proc.stdout:
                        sink.write(line if line.endswith("\n") else line + "\n")
                        self._handle_line(line)
                proc.wait()
                reader.join(timeout=2)
            finally:
                self._done.set()
                watchdog.join(timeout=2)
                for pipe in (proc.stdout, proc.stderr):
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass

        self.exit_code = proc.returncode
        self._refresh_canary()
        tail = [line for line in stderr_tail if line.strip()]

        # Before any verdict: the money is spent either way, and a ledger that
        # only counts successes admits the next run against money already gone.
        if self.result is not None:
            self.cost = float(self.result.get("total_cost_usd") or 0.0)
            run_registry.debit(self.cost, self.run_id)

        self._report_drift(before, expected_writes)
        activity.emit("claude", "%s ended - %s" %
                      (self.label, (self.result or {}).get("subtype", "no result event")),
                      level="info" if self.exit_code == 0 else "error",
                      cmd=" ".join(self._reportable_argv()),
                      exit_code=self.exit_code, ms=timer.ms, run_id=self.run_id,
                      detail=tail[-5:])

        if self.timed_out:
            raise RunFailure("%s exceeded its %ds wall clock and was killed"
                             % (self.label, self.timeout))
        # A provider can reject the request before the model gets a turn, so no
        # tool call (including the canary) is possible. Preserve that actionable
        # verdict instead of replacing it with the secondary "canary unseen"
        # symptom. Other failures still have to prove the guard was active.
        provider_status = (self.result or {}).get("api_error_status")
        provider_text = str((self.result or {}).get("result") or "")
        provider_lower = provider_text.lower()
        if provider_status == 429 or any(token in provider_lower for token in
                                         ("out_of_credits", "session limit",
                                          "rate limit", "rate_limit")):
            message = provider_text.strip() or "The model provider rejected this run (HTTP 429)."
            raise RunFailure(message[:500], code="provider_rate_limit",
                             retryable=True, model_started=False)
        if self.canary_failed or not self.canary_seen:
            raise RunFailure(
                "this pass's canary write was never refused, so the PreToolUse hook cannot "
                "be shown to have been enforcing during it. The run was killed rather than "
                "left writing under acceptEdits with no boundary, and its output is not "
                "trusted.", code="guard_unverified")
        if self.exit_code == 75:
            raise RunFailure("another model process already holds the model lock; this run "
                             "was not started. Kill the orphan from the run panel, or wait "
                             "for it to finish.")
        if self.cancelled:
            raise RunFailure("cancelled")
        if self.result is None:
            raise RunFailure("%s produced no result event (exit %s). stderr: %s"
                             % (self.label, self.exit_code, " / ".join(tail[-3:]) or "empty"))
        if self.result.get("subtype") == "error_max_budget_usd":
            raise RunFailure(
                "%s reached the local supervisor cap ($%.2f; provider reported $%.2f). "
                "Raise the corresponding limit in job_scraper/board_config.json before "
                "retrying." % (self.label, self.budget, self.cost),
                code="budget_cap", retryable=False)
        if self.result.get("is_error") or self.exit_code != 0:
            raise RunFailure("%s failed: %s" % (self.label, str(
                self.result.get("result") or tail[-1:] or "exit %s" % self.exit_code)[:500]))
        return self.result

    def _reportable_argv(self):
        """The real claude argv, with the prompt elided - it is thousands of
        characters and the point of showing the command is the flags."""
        out = []
        skip = False
        hide_value = False
        for index, item in enumerate(self.model_argv):
            if hide_value:
                hide_value = False
                continue
            if item == "--max-budget-usd":
                hide_value = True
                continue
            if skip:
                out.append("<prompt %d chars>" % len(item))
                skip = False
                continue
            out.append(item)
            skip = item == "-p"
        return out

    def allowed_writes(self):
        """Paths this pass's hook actually approved a write to.

        Positive evidence, from the same log the canary is read out of. The
        supervisor uses it to tell "this run produced the CV" from "a CV happened
        to be at that path already" - a question file metadata alone answers
        badly in both directions.
        """
        approved = set()
        try:
            text = self.hook_log.read_text(encoding="utf-8")
        except OSError:
            return approved
        for line in text.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("decision") == "allow" and entry.get("tool") in (
                    "Write", "Edit", "MultiEdit", "NotebookEdit"):
                # `resolved` is the path the guard actually decided on. A raw
                # relative path would resolve against whichever cwd this happens
                # to run in, which is not necessarily the hook's.
                target = entry.get("resolved") or entry.get("target")
                if target:
                    approved.add(os.path.realpath(str(target)))
        return approved

    def _report_drift(self, before, expected=()):
        """DESIGN §2.5's fourth layer: detection, not the boundary.

        `expected` is the run's own directory and its two targets - paths the run
        was authorised to change. Reporting those as drift would make the warning
        fire on every successful run, and a warning that always fires is not read.
        """
        if before is None:
            return
        after = git_snapshot(run_registry.ROOT)
        if after is None:
            return
        allowed = {str(Path(p)) for p in expected}
        run_prefix = str(run_registry.run_dir(self.run_id).relative_to(run_registry.ROOT)) \
            if str(run_registry.run_dir(self.run_id)).startswith(str(run_registry.ROOT)) else None
        new = []
        for path in sorted(after - before):
            if path in allowed:
                continue
            if run_prefix and path.startswith(run_prefix):
                continue
            new.append(path)
        if new:
            activity.emit("claude", "%s changed %d path(s) it was not allowed to write - "
                                    "the write guard may have regressed"
                                    % (self.label, len(new)),
                          level="warn", run_id=self.run_id, detail=new[:20])

    def _watchdog(self):
        """Kill the run when its wall clock runs out, when the guard proves
        absent, or when someone pressed Cancel."""
        canary_timeout = run_registry.config()["canary_timeout_s"]
        started = time.monotonic()
        while not self._done.wait(0.5):
            elapsed = time.monotonic() - started
            if not self.canary_seen:
                self._refresh_canary()
            if not self.canary_seen and elapsed > canary_timeout:
                self.canary_failed = True
                self.cancel()
                return
            if elapsed > self.timeout:
                self.timed_out = True
                self.cancel()
                return
            if self._is_cancelled() and not self.cancelled:
                self.cancel()
                return

    def _drain_stderr(self, proc, sink):
        try:
            for line in proc.stderr:
                sink.append(line.rstrip())
        except (OSError, ValueError):
            pass

    def _handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")

        if kind == "system" and event.get("subtype") == "init":
            # `--fork-session` mints a new session id and this is where it
            # appears; the design does not get to assume it is the one we asked
            # for. It is the first event of every run.
            self.session_id = event.get("session_id")
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._emit_tool(block)
        elif kind == "result":
            self.result = event

    def _emit_tool(self, block):
        name = block.get("name") or "tool"
        payload = block.get("input") if isinstance(block.get("input"), dict) else {}
        target = (payload.get("file_path") or payload.get("notebook_path")
                  or payload.get("command") or payload.get("description")
                  or payload.get("query") or payload.get("url") or "")
        activity.emit("claude", ("%s %s" % (name, str(target)[:160])).strip(),
                      run_id=self.run_id)
        if name == "Task" and self._on_task:
            self._on_task()
