"""The run supervisor: spawn a guarded `claude -p`, gate it, and account for it.

One **run** is one application's pass through `/apply`, driven from the board
instead of from a terminal. It is two model processes, not one, because
`/apply` Step 1 ends by asking "should I proceed with drafting?" and a headless
process has nobody to ask:

    pass A  ->  fit.json  ->  [you approve, in the browser]  ->  pass B

Pass A evaluates and stops. The supervisor validates its `fit.json` against a
schema and moves the run to `awaiting_approval`. Approving puts the run **back on
the queue** for pass B, which resumes the same session (`--resume`) so the
posting and the evaluation are not paid for twice.

Approval re-queues rather than blocking, and that is a deliberate departure from
the first draft of DESIGN §2.7, which held `.pipeline.lock` across the gate. A
lock held across a human decision is a lock held for as long as the human is at
lunch; every later run waits, and closing that with a timeout only trades a
hang for silently discarding a valid evaluation. The lock exists to stop two
pipelines interleaving their *side effects*, and every side effect happens in
pass B and after - one contiguous locked section either way.

What this module refuses to trust:

* **The settings file, every single time.** A `--settings` path that does not
  exist is a hard error before any API call; a `--settings` file containing
  malformed JSON is accepted silently and the run proceeds *with no hook at
  all*. Both measured on CLI 2.1.159. So preflight runs before **every** spawn -
  not once at admission - and every pass carries its own nonced canary write
  that the allowlist excludes. Pass B cannot inherit pass A's proof: the minutes
  a human spends on the approval card are exactly when a settings file could
  change.
* **The budget.** `--max-budget-usd` hard-stops a run and does count subagent
  spend, but it stops *after* the turn that crosses the line (~1.75x overshoot
  in the measured sample). Caps are described as "stops at about"; the ledger is
  debited with the run's **reported** cost, including when the run then fails;
  and admission counts what queued runs have already reserved, not just what has
  been spent.
* **Its own liveness, or its own uniqueness.** The board does not hold the
  model's lock and does not spawn `claude` directly - `run_wrapper.py` does both,
  before the model exists. And a second board on a second port is a thing people
  do, so every run records which board owns it: reconciliation and shutdown both
  leave another live board's runs alone.

Stdlib only, Python 3.9+.
"""

import json
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

from . import activity, run_guard, run_proc, run_registry, templates  # noqa: E402
from .run_guard import PreflightError, preflight, validate_fit  # noqa: F401,E402
from .run_proc import RunFailure, terminate  # noqa: F401,E402
# Functions and immutable constants only. The path constants stay behind
# `run_registry.` at every use site on purpose: rebinding them here would make
# `runs.REGISTRY` and `run_registry.REGISTRY` two different values the moment
# anything redirected one of them, and "the registry is in two places" is the
# exact bug this module exists to prevent.
from .run_registry import (  # noqa: F401,E402
    KINDS, PHASES, TERMINAL, config, debit, get, load, new_run_id, register_process,
    reserved, run_dir, runs_lock, session_spent, slugify, spent_today, state_dir,
    transition, update,
)

HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "run_wrapper.py"
# No local `SETTINGS` binding. The file that pre-flight validates and the file
# the CLI is handed must be the same one by construction, not by two modules
# agreeing at import time - see `run_guard.SETTINGS`.

# Read tools are allowlisted explicitly because a headless run under
# `--permission-mode acceptEdits` denies non-edit tools by default - measured for
# Bash on 2.1.159, and WebSearch is how the reviewer step does company research.
# Bash is one fixed argv shape; guard_write.py re-parses it.
ALLOWED_TOOLS = ("Read", "Glob", "Grep", "WebSearch", "WebFetch", "Task", "TodoWrite",
                 "Write", "Edit", "MultiEdit",
                 "Bash(python3 tools/board/fetch_url.py:*)")


def _store(data):
    """Kept for callers that already hold `runs_lock()`."""
    run_registry.store(data)


# Every phase a run can be in while it is still going. Used as the `expect` set
# for compare-and-set writes, so that a phase written after a terminal one - a
# cancel racing a pass that has just exited - is dropped rather than applied.
ACTIVE_PHASES = tuple(p for p in PHASES if p not in TERMINAL)


def _stat(path):
    """(exists, mtime_ns, size) - enough to tell "written" from "already there"."""
    try:
        info = os.stat(str(path))
    except OSError:
        return (False, 0, 0)
    return (True, info.st_mtime_ns, info.st_size)


# ------------------------------------------------------------- the supervisor

class Supervisor:
    """One queue, one pipeline at a time, and the phase machine in between."""

    def __init__(self):
        self._queue = deque()
        self._cv = threading.Condition()
        self._active = None            # the Pass currently running, for cancel
        self._cancelled = set()
        self._worker = None
        self._stopping = threading.Event()
        self._worker_lock = threading.Lock()
        # Held across "is this run cancelled?" and `Popen`. Two threads deciding
        # that independently is how a cancelled run still costs money.
        self._admission = threading.Lock()
        self._current = None
        self._owner_started = run_registry.process_start_time(os.getpid())

    # -- public API ---------------------------------------------------------

    def ensure_worker(self):
        # Two concurrent POSTs both seeing "no worker" would start two pumps, and
        # `stop()` could only ever join the one that happened to be stored last.
        with self._worker_lock:
            if self._stopping.is_set() or (self._worker and self._worker.is_alive()):
                return
            self._worker = threading.Thread(target=self._pump, daemon=True,
                                            name="jobflow-runs")
            self._worker.start()

    def stop(self, timeout=15):
        """Drain the queue, cancel what is running, and join the worker.

        A daemon thread that outlives the thing that created it keeps acting on
        state nobody expects it to touch any more - which is a nuisance in tests
        and a way to spend money twice in production.
        """
        self._stopping.set()
        with self._cv:
            pending = list(self._queue)
            self._queue.clear()
            self._cv.notify_all()
        # `_current` is the run the worker has dequeued but may not have spawned
        # yet - the window in which `stop()` used to see no work at all and
        # return, leaving the worker to reach `Popen` afterwards.
        with self._admission:
            for run_id in filter(None, pending + [self._current]):
                self._cancelled.add(run_id)
            active = self._active
        for run_id in pending:
            self._settle(run_id, "cancelled", "the board stopped before this run started")
        if active is not None:
            active.cancel()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=timeout)

    def start(self, payload):
        """`POST /api/runs`. Returns (status, body)."""
        settings = config()
        job_url = (payload.get("job_url") or "").strip()
        kind = payload.get("kind") or "apply"
        if not job_url.startswith(("http://", "https://")):
            return 400, {"error": "job_url must be an http(s) URL"}
        if kind not in KINDS:
            return 400, {"error": "kind must be one of %s" % list(KINDS)}
        if kind != "apply":
            # Revise and redraft are M4; they need fork-resume and the version
            # store, and half of that is worse than none.
            return 501, {"error": "%s re-entry lands in M4; use Tailor for now" % kind}
        if (payload.get("remember") or "").strip():
            # The run would have to write `01-candidate-profile.md`, which is not
            # on this milestone's allowlist. Refusing is better than sending an
            # instruction the guard will deny halfway through a paid run.
            return 501, {"error": "standing preferences land in M4 - the profile file is "
                                  "not writable by a run yet, so this would fail mid-draft"}

        try:
            preflight()
        except PreflightError as exc:
            activity.emit("claude", "preflight refused a run: %s" % exc, level="error")
            return 503, {"error": "preflight failed: %s" % exc}

        entry = self._board_entry(job_url)
        company = (payload.get("company") or entry.get("company") or "").strip()
        role = (payload.get("role") or entry.get("title") or "").strip()
        if not company or not role:
            return 400, {"error": "this URL is not on the board and no company/role was "
                                  "supplied; the supervisor needs both to compute the "
                                  "target file paths before the run starts"}

        cv_ext, cover_ext, problems = templates.resolve_extensions()
        if problems:
            return 503, {"error": " / ".join(problems)}

        budget = {"pass_a": settings["budget_usd"]["pass_a"],
                  "pass_b": settings["budget_usd"]["pass_b"]}
        worst_case = budget["pass_a"] + budget["pass_b"]
        slug = slugify(company, role)

        with self._cv:
            if len(self._queue) >= settings["queue_depth"]:
                return 429, {"error": "the queue is full (%d waiting)" % len(self._queue)}

            # Admission is one atomic read-modify-write: the duplicate check, the
            # budget check and the insert have to see the same registry, or two
            # requests a millisecond apart both pass a test only one of them
            # should.
            with runs_lock():
                data = load()
                live = [r for r in data["runs"]
                        if r["phase"] not in TERMINAL and r["phase"] != "orphaned"]
                duplicate = next((r for r in live if r["job_url"] == job_url), None)
                if duplicate:
                    return 409, {"error": "a run for this posting is already %s"
                                          % duplicate["phase"], "run_id": duplicate["id"]}

                committed = run_registry.reserved(data)
                remaining = settings["daily_budget_usd"] - spent_today(data) - committed
                if remaining < worst_case:
                    return 429, {"error": "today's budget has $%.2f left once the %d run(s) "
                                          "already in flight are counted, and this run could "
                                          "cost about $%.2f. Raise daily_budget_usd in "
                                          "job_scraper/board_config.json to continue."
                                          % (max(0.0, remaining), len(live), worst_case)}

                run_id = new_run_id(company)
                while any(r["id"] == run_id for r in data["runs"]):
                    run_id = new_run_id(company)
                record = {
                    "id": run_id,
                    "session_id": str(uuid.uuid4()),
                    "job_url": job_url,
                    "company": company,
                    "role": role,
                    "slug": slug,
                    "kind": kind,
                    "parent": payload.get("parent"),
                    "phase": "queued",
                    "owner": run_registry.OWNER,
                    "owner_pid": os.getpid(),
                    "owner_started": self._owner_started,
                    "note": (payload.get("note") or "")[:1000],
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "ended_at": None,
                    "approved_at": None,
                    "pid": None, "pgid": None, "exit_code": None,
                    "error": None,
                    "targets": {"cv": "cv/main_%s%s" % (slug, cv_ext),
                                "cover": "cover_letters/cover_%s%s" % (slug, cover_ext)},
                    "artefacts": {},
                    "budget_usd": budget,
                    "cost": {"total_usd": 0.0},
                }
                data["runs"].append(record)
                _store(data)

            self._queue.append(run_id)
            position = len(self._queue)
            self._cv.notify_all()

        self.ensure_worker()
        activity.emit("claude", "queued %s - %s at %s (position %d)"
                      % (run_id, role, company, position), run_id=run_id)
        return 202, {"run_id": run_id, "position": position, "phase": "queued",
                     "targets": record["targets"]}

    def approve(self, run_id, expected_phase):
        """Compare-and-set, then re-queue for pass B.

        Two rapid clicks cost one pass B: the second finds the phase already
        moved and gets a 409. Re-queueing rather than signalling a waiting thread
        also means there is no window in which an approval can arrive before
        anything is listening for it.
        """
        with self._cv:
            with runs_lock():
                data = load()
                record = next((r for r in data["runs"] if r["id"] == run_id), None)
                if record is None:
                    return 404, {"error": "unknown run"}
                if record["phase"] != "awaiting_approval":
                    return 409, {"error": "run is %s, not awaiting_approval" % record["phase"],
                                 "phase": record["phase"]}
                if expected_phase and expected_phase != record["phase"]:
                    return 409, {"error": "the page is showing %s; the run is %s"
                                          % (expected_phase, record["phase"]),
                                 "phase": record["phase"]}
                record["phase"] = "queued"
                record["approved_at"] = datetime.now().isoformat(timespec="seconds")
                record["owner"] = run_registry.OWNER
                record["owner_pid"] = os.getpid()
                record["owner_started"] = self._owner_started
                _store(data)
            self._queue.append(run_id)
            position = len(self._queue)
            self._cv.notify_all()

        self.ensure_worker()
        activity.emit("claude", "approved %s - pass B queued (position %d)"
                      % (run_id, position), run_id=run_id)
        return 200, {"ok": True, "phase": "queued", "position": position}

    def cancel(self, run_id):
        record = get(run_id)
        if record is None:
            return 404, {"error": "unknown run"}
        if record["phase"] in TERMINAL:
            return 409, {"error": "run is already %s" % record["phase"]}
        # Recorded under the admission lock, which a spawn holds across its last
        # cancellation check and `Popen`. So either this lands first and no
        # process is created, or the process already exists and `active` below
        # can see it.
        with self._admission:
            self._cancelled.add(run_id)
            # Persisted here, immediately, and not left for the pipeline to do.
            # A pass that has already exited is between `Popen` returning and its
            # phase being published; if the cancellation is only a flag in
            # memory, that publication overwrites it and the run the owner
            # cancelled comes back as `awaiting_approval` or `done`. Every
            # post-process write is a CAS out of a *running* phase, so once this
            # lands nothing can move the run again.
            moved = transition(run_id, "cancelled", ACTIVE_PHASES,
                               ended_at=datetime.now().isoformat(timespec="seconds"),
                               error="cancelled")
            active = self._active
        with self._cv:
            while run_id in self._queue:
                self._queue.remove(run_id)
        if active is not None and active.run_id == run_id:
            active.cancel()
        if not moved:
            return 409, {"error": "run is already %s" % (get(run_id) or {}).get("phase")}
        activity.emit("claude", "cancel requested for %s" % run_id, level="warn", run_id=run_id)
        return 200, {"ok": True}

    def kill(self, run_id):
        """Terminate an `orphaned` run found at startup."""
        record = get(run_id)
        if record is None:
            return 404, {"error": "unknown run"}
        if record["phase"] != "orphaned":
            return 409, {"error": "only an orphaned run is killed this way; this one is %s"
                                  % record["phase"]}
        # Re-checked *now*, not when it was labelled orphaned. Minutes may have
        # passed while the card sat on screen; the orphan may have exited and its
        # pid and pgid been handed to something else entirely, and this is the
        # one place that sends SIGKILL to a number the user clicked next to.
        # Ownership is re-checked here too, not just at reconciliation. A run can
        # only have been labelled `orphaned` if its owning board looked dead at
        # startup - but "looked dead" can mean `ps` was briefly unreadable, and by
        # the time this button is pressed the other board may be plainly alive.
        # Signalling then kills another window's work.
        owner = run_registry.board_alive(record)
        if owner is True:
            # Confirmed live owner. Give the run back: it is not an orphan, it is
            # another window's work, and only a confirmed answer justifies
            # rewriting the phase.
            update(run_id, phase="drafting", error=None)
            activity.emit("claude", "%s belongs to a board that is still running; not "
                                    "killing it" % run_id, level="warn", run_id=run_id)
            return 409, {"error": "this run belongs to another board that is still "
                                  "running; stop that board instead"}
        if owner is None:
            # Ownership unknown - either recorded by a board from before ownership
            # existed, or `ps` was unreadable. Neither signal nor rewrite the
            # phase: an unknown owner may be a live board, and a run whose phase
            # we changed on a guess is a run whose Kill button we removed.
            activity.emit("claude", "%s: cannot establish which board owns this run; "
                                    "nothing signalled" % run_id, level="warn", run_id=run_id)
            return 409, {"error": "cannot establish which board owns this run, so nothing "
                                  "was signalled. If no other board is running, try again "
                                  "in a moment."}
        # Strict here, generous in `reconcile()`. Mislabelling a live process as
        # an orphan is recoverable; sending SIGKILL to a recycled pid is not.
        # Three outcomes, and the middle one is the one worth separating:
        if not run_registry.process_matches(record, strict=True):
            if not run_registry.process_matches(record, strict=False):
                # Disproved: gone, or demonstrably somebody else's now.
                self._settle(run_id, "failed",
                             "the orphaned process is gone (or is no longer identifiable "
                             "as this run); nothing was signalled")
                activity.emit("claude", "%s: orphan already gone, nothing signalled" % run_id,
                              level="warn", run_id=run_id)
                return 200, {"ok": True, "signals": []}
            # Alive, and not disproved - `ps` was simply unreadable this moment.
            # Neither signal it nor write the run off: leave it orphaned so the
            # button still works, and say so.
            activity.emit("claude", "%s: could not confirm the orphan's identity just now; "
                                    "nothing signalled" % run_id, level="warn", run_id=run_id)
            return 409, {"error": "could not confirm that process is still this run - "
                                  "nothing was signalled. Try again in a moment."}
        did = run_proc.terminate(record.get("pgid"), record.get("pid"))
        self._settle(run_id, "failed", "killed as an orphan (%s)"
                     % (", ".join(did) or "already gone"))
        activity.emit("claude", "killed orphan %s (%s)"
                      % (run_id, ", ".join(did) or "already gone"), level="warn", run_id=run_id)
        return 200, {"ok": True, "signals": did}

    def reconcile(self):
        """At startup: a run marked running is either a live orphan or a corpse.

        Runs belonging to a **different board that is still alive** are left
        entirely alone. Two boards on two ports is a normal thing to do, and a
        second board that adopts the first one's records will mark its live runs
        failed and then kill them on exit.
        """
        adopted, restored, skipped = [], [], 0
        with runs_lock():
            data = load()
            changed = False
            for record in data["runs"]:
                if record["phase"] in TERMINAL or record["phase"] == "orphaned":
                    continue
                if run_registry.board_alive(record) is True:
                    skipped += 1
                    continue
                was = record["phase"]
                # Generous on purpose: a process that *might* still be this run
                # is surfaced as `orphaned` with a Kill button, not silently
                # written off as dead while it keeps spending and holding
                # `.model.lock`. Kill itself is strict - see below.
                if record.get("pid") and run_registry.process_matches(record, strict=False):
                    record["phase"] = "orphaned"
                    record["error"] = ("a model process from a previous board is still "
                                       "running; kill it or wait for it to finish")
                    adopted.append(record["id"])
                elif was == "awaiting_approval":
                    # Nothing was running. The evaluation is still valid and still
                    # on disk, so the gate survives a board restart rather than
                    # throwing away a pass A that was already paid for.
                    continue
                elif was == "queued":
                    # Approved, then the board died before the worker picked it
                    # up. The evaluation is intact and the approval was real, so
                    # the run is *not* failed - but it is not silently resumed
                    # either: a board that starts spending on boot is exactly the
                    # surprise §3.3 refuses for automatic retries. It goes back to
                    # the gate, one click from where it was.
                    record["phase"] = "awaiting_approval"
                    record["approved_at"] = None
                    record["error"] = ("the board restarted before this approval reached "
                                       "the queue; the evaluation is unchanged - approve "
                                       "it again to draft")
                    restored.append(record["id"])
                else:
                    record["phase"] = "failed"
                    record["ended_at"] = datetime.now().isoformat(timespec="seconds")
                    record["error"] = ("the board restarted while this run was %s; its "
                                       "process is gone" % was)
                changed = True
            if changed:
                _store(data)
        if adopted:
            activity.emit("claude", "startup: %d run(s) still have a live model process - "
                                    "kill them from the run panel before starting another"
                          % len(adopted), level="warn", detail=adopted)
        if restored:
            activity.emit("claude", "startup: %d approved run(s) were re-opened at the "
                                    "approval gate - the board restarted before they "
                                    "started drafting" % len(restored),
                          level="warn", detail=restored)
        if skipped:
            activity.emit("claude", "startup: %d run(s) belong to another board that is "
                                    "still running; left alone" % skipped)
        return adopted

    def snapshot(self):
        settings = config()
        data = load()
        with self._cv:
            queued = list(self._queue)
        return {
            "runs": sorted(data["runs"], key=lambda r: r.get("started_at") or "",
                           reverse=True)[:50],
            "queue": queued,
            "queue_depth": settings["queue_depth"],
            "ledger": {"spent_today_usd": spent_today(data),
                       "reserved_usd": run_registry.reserved(data),
                       "daily_budget_usd": settings["daily_budget_usd"]},
            "budget_usd": settings["budget_usd"],
        }

    # -- internals ----------------------------------------------------------

    def _board_entry(self, job_url):
        from . import state as board_state
        seen = board_state.load()
        entry = seen.get(job_url)
        if entry:
            return entry
        for url, candidate in seen.items():
            if board_state.primary_url(candidate) == job_url:
                return candidate
        return {}

    def _settle(self, run_id, phase, message):
        """Move a still-running run to a terminal phase. Never re-labels one."""
        transition(run_id, phase, ACTIVE_PHASES,
                   ended_at=datetime.now().isoformat(timespec="seconds"), error=message)
        self._cancelled.discard(run_id)

    def _pump(self):
        while not self._stopping.is_set():
            with self._cv:
                while not self._queue:
                    if self._stopping.is_set():
                        return
                    self._cv.wait(timeout=5)
                run_id = self._queue.popleft()
            self._current = run_id
            try:
                if run_id in self._cancelled or self._stopping.is_set():
                    self._settle(run_id, "cancelled", "cancelled before it started")
                    continue
                self._pipeline(run_id)
            except Exception as exc:  # a crashed pipeline must not stop the queue
                self._settle(run_id, "failed", "%s: %s" % (type(exc).__name__, exc))
                activity.emit("claude", "run %s failed: %s: %s"
                              % (run_id, type(exc).__name__, exc), level="error", run_id=run_id)
            finally:
                self._current = None

    @contextmanager
    def _pipeline_lock(self):
        """Held for one pass, not across the approval gate - see the module
        docstring. Two pipelines still never interleave their side effects."""
        if fcntl is None:
            yield
            return
        lock_path = run_registry.PIPELINE_LOCK
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                activity.emit("claude", "waiting for the pipeline lock - another run owns it",
                              level="warn")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _pipeline(self, run_id):
        """One queue turn: pass A for a fresh run, pass B for an approved one."""
        with self._pipeline_lock():
            record = get(run_id)
            if record is None or record["phase"] in TERMINAL:
                return
            settings = config()
            state_dir(run_id).mkdir(parents=True, exist_ok=True)
            run_dir(run_id).mkdir(parents=True, exist_ok=True)
            if record.get("approved_at"):
                self._pass_b(record, settings)
            else:
                self._pass_a(record, settings)

    # -- the two passes -----------------------------------------------------

    def _spawn(self, record, label, prompt_builder, budget, timeout, targets=(), resume=False):
        """Preflight, allowlist, spawn, stream. Returns the `Pass`.

        `prompt_builder` is a callable rather than a string because the prompt
        embeds this pass's canary nonce, and the nonce is minted here so that no
        caller can accidentally reuse one.
        """
        run_id = record["id"]

        # Before *every* spawn, not once at admission. The settings file and the
        # guard can change while a human looks at the approval card, and pass B
        # is the expensive one.
        preflight()

        nonce = run_guard.new_nonce()
        allowlist = run_guard.write_allowlist(run_id, targets=targets, nonce=nonce)
        hook_log = state_dir(run_id) / ("hook-%s.jsonl" % nonce)
        directory = run_dir(run_id)
        settings = config()

        # Argument order is load-bearing at both ends.
        #
        # The session uuid goes **first**, because it is also this run's process
        # identity: `ps` truncates long command lines, and the prompt is several
        # thousand characters, so a uuid placed after it can be cut out of view -
        # at which point a live orphan cannot be told from a dead one.
        #
        # The prompt goes **last**, after `--allowedTools`, because that option is
        # variadic (`<tools...>`) and anything following it is at the mercy of
        # where the parser decides the list ends. `-p` is a flag, which is what
        # terminates the list; the prompt is then the positional argument.
        # Verified against CLI 2.1.159: this order parses, reaching session-id
        # validation (and erroring there on a bad uuid) before any API call.
        argv = [settings["claude_bin"]]
        argv += ["--resume", record["session_id"]] if resume else \
                ["--session-id", record["session_id"]]
        argv += ["--output-format", "stream-json", "--verbose",
                 "--permission-mode", "acceptEdits",
                 "--settings", str(run_guard.SETTINGS),
                 "--max-budget-usd", "%.2f" % budget,
                 "--allowedTools", ",".join(ALLOWED_TOOLS),
                 "-p", prompt_builder(nonce)]

        env = dict(os.environ)
        env.update({
            "JOBFLOW_RUN": "1",
            "JOBFLOW_RUN_ID": run_id,
            "JOBFLOW_RUN_DIR": str(directory),
            "JOBFLOW_ALLOWLIST": str(allowlist),
            "JOBFLOW_HOOK_LOG": str(hook_log),
            "JOBFLOW_CV_TARGET": record["targets"]["cv"],
            "JOBFLOW_COVER_TARGET": record["targets"]["cover"],
        })

        spec_path = state_dir(run_id) / ("spec-%s.json" % nonce)
        jobs_md.write_json_atomic(spec_path, {
            "run_id": run_id, "argv": argv, "cwd": str(run_registry.ROOT),
            "registry": str(run_registry.REGISTRY),
            "runs_lock": str(run_registry.RUNS_LOCK),
            "model_lock": str(run_registry.MODEL_LOCK),
            "env": {k: env[k] for k in env if k.startswith("JOBFLOW_")},
        })
        wrapper_argv = [sys.executable, str(WRAPPER), "--spec", str(spec_path)]

        job = run_proc.Pass(run_id, label, wrapper_argv, env, budget, timeout, nonce,
                            hook_log, model_argv=argv,
                            cancelled=lambda: (run_id in self._cancelled
                                               or self._stopping.is_set()),
                            admission=self._admission)
        self._active = job
        try:
            job.run(on_task=lambda: transition(run_id, "reviewing",
                                               ("evaluating", "drafting")),
                    expected_writes=[str(Path(t).relative_to(run_registry.ROOT))
                                     for t in targets])
            return job
        finally:
            self._active = None
            update(run_id, pid=None, pgid=None, exit_code=job.exit_code)

    def _pass_a(self, record, settings):
        run_id = record["id"]
        if run_id in self._cancelled:
            self._settle(run_id, "cancelled", "cancelled before it started")
            return
        update(run_id, phase="evaluating")
        salary = self._salary(record)
        try:
            self._spawn(record, "pass A (evaluate)",
                        lambda nonce: self._prompt_a(record, salary, nonce),
                        settings["budget_usd"]["pass_a"], settings["timeout_s"]["pass_a"])
        except (RunFailure, PreflightError) as exc:
            self._fail(run_id, str(exc))
            return
        self._read_fit(run_id)

    def _pass_b(self, record, settings):
        run_id = record["id"]
        if run_id in self._cancelled:
            self._settle(run_id, "cancelled", "cancelled before pass B started")
            return
        update(run_id, phase="drafting")
        targets = [run_registry.ROOT / record["targets"]["cv"],
                   run_registry.ROOT / record["targets"]["cover"]]
        # Snapshotted before the run, because "the file exists" is not evidence
        # that *this* run wrote it: re-applying to the same company and role
        # finds last month's CV sitting at exactly the expected path.
        before = {str(path): _stat(path) for path in targets}
        try:
            job = self._spawn(record, "pass B (draft and review)",
                              lambda nonce: self._prompt_b(record, nonce),
                              settings["budget_usd"]["pass_b"],
                              settings["timeout_s"]["pass_b"],
                              targets=targets, resume=True)
        except (RunFailure, PreflightError) as exc:
            self._fail(run_id, str(exc))
            return

        # Two independent proofs that *this* pass produced the documents, because
        # neither is sufficient alone. File metadata alone can be satisfied by
        # rewriting last month's bytes; hook evidence alone would accept a write
        # the guard approved and the model then failed to complete.
        approved = job.allowed_writes()
        missing_evidence = [str(path) for path in targets
                            if os.path.realpath(str(path)) not in approved]
        unchanged = [str(path) for path in targets if _stat(path) == before[str(path)]]
        if missing_evidence or unchanged:
            names = sorted({Path(p).name for p in missing_evidence + unchanged})
            self._fail(run_id, "pass B finished without this run having written %s - a file "
                               "that was already at that path is not this run's output."
                               % ", ".join(names))
            return
        problems = self._read_drafts(record)
        if problems:
            self._fail(run_id, "; ".join(problems))
            return
        produced = {name: path for name, path in record["targets"].items()}
        self._settle_ok(run_id, produced)

    def _settle_ok(self, run_id, produced):
        if not transition(run_id, "done", ("drafting", "reviewing", "compiling", "inspecting"),
                          ended_at=datetime.now().isoformat(timespec="seconds"),
                          artefacts=produced, error=None):
            activity.emit("claude", "%s finished but is %s; leaving it there"
                          % (run_id, (get(run_id) or {}).get("phase")),
                          level="warn", run_id=run_id)
            return
        self._cancelled.discard(run_id)
        activity.emit("claude", "%s done - %s" % (run_id, ", ".join(sorted(produced.values()))),
                      run_id=run_id)

    def _fail(self, run_id, message):
        """Settle a failure, unless the run is already settled.

        The check-then-write is inside `transition()`, under `.runs.lock`: a
        `_fail("cancelled")` arriving after `cancel()` already persisted the
        cancellation must not relabel it, and neither must a late failure
        overwrite a `done`.
        """
        phase = "cancelled" if message == "cancelled" else "failed"
        if not transition(run_id, phase, ACTIVE_PHASES,
                          ended_at=datetime.now().isoformat(timespec="seconds"),
                          error=message):
            self._cancelled.discard(run_id)
            return
        self._cancelled.discard(run_id)
        activity.emit("claude", "%s %s: %s" % (run_id, phase, message),
                      level="error" if phase == "failed" else "warn", run_id=run_id)

    # -- the contract files -------------------------------------------------

    def _read_json(self, run_id, name):
        """(payload, problem). A missing or unparseable contract file is a failure,
        never an empty default - the supervisor acts on what these say."""
        path = run_dir(run_id) / name
        if not path.exists():
            return None, ("the pass exited cleanly but wrote no %s. The headless contract "
                          "in .claude/commands/apply.md is what produces it." % name)
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except ValueError as exc:
            return None, "%s is not valid JSON: %s" % (name, exc)

    def _read_fit(self, run_id):
        payload, problem = self._read_json(run_id, "fit.json")
        if problem:
            self._fail(run_id, problem)
            return
        problems = run_guard.validate_fit(payload)
        if problems:
            self._fail(run_id, "fit.json does not match jobflow.fit/1: " + "; ".join(problems[:6]))
            return
        # M3 archives the posting from this file, and a pass A that did not keep
        # it means the archive would have to be reconstructed from memory later -
        # which apply.md Step 6b explicitly forbids. Better to fail now.
        posting = run_dir(run_id) / "posting.md"
        if not posting.exists() or not posting.stat().st_size:
            self._fail(run_id, "pass A wrote no posting.md. The verbatim posting is what "
                               "gets archived; it cannot be reconstructed later.")
            return
        # Compare-and-set: Cancel can land between the process exiting and this
        # line, and a cancelled run that comes back as `awaiting_approval` is a
        # run the owner will be asked to pay for after saying no.
        if not transition(run_id, "awaiting_approval", ("evaluating", "reviewing"),
                          fit=payload, company=payload["company"], role=payload["role"]):
            activity.emit("claude", "%s produced an evaluation but is %s; not publishing it"
                          % (run_id, (get(run_id) or {}).get("phase")),
                          level="warn", run_id=run_id)
            return
        activity.emit("claude", "%s evaluated - %s, overall %s (%s)"
                      % (run_id, payload["company"], payload["overall"], payload["verdict"]),
                      run_id=run_id)

    def _read_drafts(self, record):
        """The pass-B contract: `drafts.json` naming exactly the two allowlisted
        targets, and the keyword list M3's verification pass reads."""
        run_id = record["id"]
        payload, problem = self._read_json(run_id, "drafts.json")
        if problem:
            return [problem]
        problems = run_guard.validate_drafts(payload, record["targets"])
        request, missing = self._read_json(run_id, "verify_request.json")
        if missing:
            problems.append(missing)
        else:
            problems += self._validate_verify_request(request)
        return problems

    @staticmethod
    def _validate_verify_request(payload):
        """M3's keyword check reads this, so it is a contract like the others."""
        if not isinstance(payload, dict):
            return ["verify_request.json is not a JSON object"]
        problems = []
        if payload.get("schema") != "jobflow.verify/1":
            problems.append("verify_request.json schema is %r, expected 'jobflow.verify/1'"
                            % (payload.get("schema"),))
        keywords = payload.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            problems.append("verify_request.json needs a non-empty `keywords` list")
        elif not all(isinstance(k, str) and k.strip() for k in keywords):
            problems.append("verify_request.json `keywords` must all be non-empty strings")
        return problems

    def _salary(self, record):
        """Deterministic, so the supervisor runs it and injects the JSON. One
        fewer reason for the model to want a shell."""
        script = run_registry.ROOT / "salary_lookup.py"
        if not script.exists():
            return None
        argv = [sys.executable, str(script), record["company"], "--json"]
        try:
            proc = subprocess.run(argv, cwd=str(run_registry.ROOT), capture_output=True,
                                  text=True,
                                  timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            activity.emit("claude", "salary lookup unavailable: %s" % exc, level="warn",
                          run_id=record["id"])
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            activity.emit("claude", "salary lookup returned nothing for %s" % record["company"],
                          level="warn", cmd=" ".join(argv), exit_code=proc.returncode,
                          run_id=record["id"])
            return None
        activity.emit("claude", "salary benchmark resolved for %s" % record["company"],
                      cmd=" ".join(argv), exit_code=0, run_id=record["id"])
        return proc.stdout.strip()[:4000]

    # -- prompts ------------------------------------------------------------

    CANARY = (
        "Before anything else, use the Write tool once on `{probe}` with the text `probe`.\n"
        "This write is **expected to be refused** by this run's write guard. The refusal is\n"
        "the signal that the guard is installed, so when it is refused, continue immediately\n"
        "with the rest of this prompt. Do not retry it, do not work around it, and do not\n"
        "treat it as an error.\n\n")

    def _canary(self, record, nonce):
        return self.CANARY.format(
            probe=run_dir(record["id"]) / run_guard.probe_name(nonce))

    def _prompt_a(self, record, salary, nonce):
        parts = [self._canary(record, nonce)]
        parts.append("/apply %s\n\n" % record["job_url"])
        parts.append("Headless mode is active (`JOBFLOW_RUN=1`). Follow the `## Headless mode` "
                     "section of `.claude/commands/apply.md`: run Step 0 and Step 1 only, write "
                     "`$JOBFLOW_RUN_DIR/posting.md` and `$JOBFLOW_RUN_DIR/fit.json`, and stop. "
                     "Do not draft anything.\n\n")
        parts.append("The posting is at %s. Fetch it. If the fetch returns 403 or a login "
                     "wall, the one shell command available to you is "
                     "`python3 tools/board/fetch_url.py \"<https url>\"` - quote the URL, "
                     "or a `&` in it makes it two commands and the guard refuses it.\n\n"
                     % record["job_url"])
        if salary:
            parts.append("Salary benchmark, already looked up by the supervisor - use it in "
                         "Step 1 and do not run the tool yourself:\n```json\n%s\n```\n\n" % salary)
        else:
            parts.append("No salary benchmark is available for this company; skip that part "
                         "of Step 1.\n\n")
        if record.get("note"):
            parts.append("The owner added: %s\n\n" % record["note"])
        return "".join(parts)

    def _prompt_b(self, record, nonce):
        # Pass B carries its own canary. Inheriting pass A's proof would mean
        # trusting a guard that was working before the approval card was opened.
        parts = [self._canary(record, nonce)]
        parts.append("Approved. Continue from Step 2 of `/apply`.\n\n")
        parts.append("Write exactly these two files and no others:\n"
                     "- CV: `%s`\n- Cover letter: `%s`\n\n"
                     % (record["targets"]["cv"], record["targets"]["cover"]))
        parts.append("Still headless (`JOBFLOW_RUN=1`): the supervisor compiles, cleans up, "
                     "archives the posting and writes the tracker row. Do Steps 2, 3 and 4, "
                     "then write `$JOBFLOW_RUN_DIR/drafts.json` and "
                     "`$JOBFLOW_RUN_DIR/verify_request.json` and stop - do not compile and do "
                     "not edit `job_search_tracker.csv`.\n\n")
        if record.get("note"):
            parts.append("The owner added: %s\n\n" % record["note"])
        # `REMEMBER:` is deliberately absent. A standing preference is written
        # into `01-candidate-profile.md` by the run itself (§5), and that file is
        # not on this milestone's allowlist - so sending the instruction would
        # produce a guard denial rather than a preference. It lands in M4 with
        # the allowlist entry that makes it possible.
        return "".join(parts)


_SUPERVISOR = None
_SUPERVISOR_LOCK = threading.Lock()


def supervisor():
    """The process-wide singleton. Reconciles orphans on first use."""
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR is None:
            _SUPERVISOR = Supervisor()
            _SUPERVISOR.reconcile()
            _SUPERVISOR.ensure_worker()
        return _SUPERVISOR


def shutdown():
    """`atexit`/SIGTERM: never leave a model process spending with nobody watching.

    Only this board's own runs. A second board on a second port has its own live
    children, and killing them on the way out is how one accidental `Ctrl-C`
    destroys another window's work.
    """
    if _SUPERVISOR is None:
        return
    _SUPERVISOR.stop(timeout=8)
    for record in load()["runs"]:
        if record["phase"] in TERMINAL or record["phase"] == "orphaned":
            continue
        if record.get("owner") != run_registry.OWNER:
            continue    # not ours to kill - including runs with no owner at all
        if record.get("pid") and run_registry.process_alive(record["pid"]):
            run_proc.terminate(record.get("pgid"), record.get("pid"), grace=2.0)
            update(record["id"], phase="failed",
                   ended_at=datetime.now().isoformat(timespec="seconds"),
                   error="the board shut down and killed this run")
