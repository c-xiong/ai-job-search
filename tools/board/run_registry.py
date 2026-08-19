"""Where a run's state lives: `runs.json`, the locks, the ledger, the names.

Split out of `runs.py` so the supervisor, the guard and the process wrapper all
read one definition of "where things are" instead of three. The wrapper in
particular is a separate process that has to agree with the board about the
registry path down to the byte, and it is handed those paths in its spec file.

Three locks, because three things have three different lifetimes (DESIGN §2.7):

| lock | held by | for | guards |
|---|---|---|---|
| `.model.lock` | `run_wrapper.py` | the `claude` process's life | one model process at a time |
| `.pipeline.lock` | the board | pass B onward | two pipelines interleaving side effects |
| `.runs.lock` | anyone writing `runs.json` | one read-modify-write | the registry itself |

**Ownership.** Every run record carries the `owner` id and `owner_pid` of the
board that created it. Two boards on two ports are a thing people do, and a
second board that adopts the first one's runs will mark them failed, then kill
their processes on exit. So reconciliation and shutdown both ignore records whose
owning board is still alive.

Stdlib only, Python 3.9+.
"""

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

ROOT = jobs_md.ROOT

REGISTRY = ROOT / "job_scraper" / "runs.json"
# Supervisor-owned, and deliberately *not* under documents/runs/: the allowlist
# and the hook log are what the guard is made of, so they must sit outside every
# path the guarded model can write.
RUN_STATE = ROOT / "job_scraper" / "run_state"
RUN_DIRS = ROOT / "documents" / "runs"

MODEL_LOCK = ROOT / "job_scraper" / ".model.lock"
PIPELINE_LOCK = ROOT / "job_scraper" / ".pipeline.lock"
RUNS_LOCK = ROOT / "job_scraper" / ".runs.lock"

CONFIG = ROOT / "job_scraper" / "board_config.json"

PHASES = ("evaluating", "awaiting_approval", "queued", "drafting", "reviewing",
          "compiling", "inspecting", "done", "failed", "cancelled", "orphaned")
TERMINAL = ("done", "failed", "cancelled")
KINDS = ("apply", "revise", "redraft")

# This board process. Written onto every run it starts.
OWNER = uuid.uuid4().hex[:12]

DEFAULT_CONFIG = {
    # "stops at about", not "will not exceed": `--max-budget-usd` stops the run
    # after the turn that crosses the line, ~1.75x over in the measured sample.
    "budget_usd": {"pass_a": 0.40, "pass_b": 2.00, "pass_c": 0.35,
                   "revise": 1.00, "redraft": 1.50},
    "daily_budget_usd": 10.0,
    "session_budget_usd": 6.0,
    "timeout_s": {"pass_a": 300, "pass_b": 900, "pass_c": 300},
    "inspection_enabled": True,
    "canary_timeout_s": 120,
    "queue_depth": 5,
    "claude_bin": "claude",
}


def config():
    """Defaults, overlaid with `job_scraper/board_config.json` when it exists."""
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        override = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return merged
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        elif key in merged:
            merged[key] = value
    return merged


# --------------------------------------------------------------------- lock

_REGISTRY_LOCAL = threading.RLock()
_REGISTRY_DEPTH = 0


@contextmanager
def runs_lock():
    """Hold `.runs.lock` across a read-modify-write of `runs.json`.

    Two processes write that file - the board's phase transitions and the
    wrapper's PID registration - and the wrapper writes *before* the model
    exists. Without a lock spanning the read as well as the write, a board
    working from a stale snapshot erases the registration of a process that is
    already spending money.

    Re-entrant for the same reason `jobs_md.board_lock()` is: `flock` is per open
    file description, so a nested acquire on a fresh handle would deadlock.
    """
    global _REGISTRY_DEPTH
    with _REGISTRY_LOCAL:
        if fcntl is None or _REGISTRY_DEPTH > 0:
            _REGISTRY_DEPTH += 1
            try:
                yield
            finally:
                _REGISTRY_DEPTH -= 1
            return
        RUNS_LOCK.parent.mkdir(parents=True, exist_ok=True)
        handle = open(RUNS_LOCK, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _REGISTRY_DEPTH += 1
            try:
                yield
            finally:
                _REGISTRY_DEPTH -= 1
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# ----------------------------------------------------------------- registry

def load():
    """`{"runs": [...], "ledger": {...}}`. A corrupt registry raises rather than
    silently starting from empty - it is the only record of what has been spent."""
    if not REGISTRY.exists():
        return {"runs": [], "ledger": {"date": date.today().isoformat(), "spent_usd": 0.0}}
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    data.setdefault("runs", [])
    data.setdefault("ledger", {"date": date.today().isoformat(), "spent_usd": 0.0})
    return data


def store(data):
    jobs_md.write_json_atomic(REGISTRY, data)


def get(run_id):
    for run in load()["runs"]:
        if run["id"] == run_id:
            return run
    return None


def update(run_id, **fields):
    """Read-modify-write one run under the lock. Returns the updated record."""
    with runs_lock():
        data = load()
        for run in data["runs"]:
            if run["id"] == run_id:
                run.update(fields)
                store(data)
                return run
    return None


def transition(run_id, to, expect, **fields):
    """Compare-and-set one run's phase. True when it moved.

    Every phase change that happens *after* a model process has exited goes
    through this. The reason is a race with `cancel()`: the pass ends, the owner
    presses Cancel, the run settles as `cancelled` - and then the pipeline's next
    line unconditionally publishes `awaiting_approval` and resurrects it. A
    cancelled run must stay cancelled.
    """
    with runs_lock():
        data = load()
        for run in data["runs"]:
            if run["id"] != run_id:
                continue
            if run.get("phase") not in expect:
                return False
            run["phase"] = to
            run.update(fields)
            store(data)
            return True
    return False


def append(record):
    """Add a run, refusing an id that is already taken."""
    with runs_lock():
        data = load()
        if any(run["id"] == record["id"] for run in data["runs"]):
            raise ValueError("run id %s already exists" % record["id"])
        data["runs"].append(record)
        store(data)
        return record


def register_process(run_id, pid, pgid, argv, started_at):
    """Called by `run_wrapper.py` before it `exec`s, so the record beats the model."""
    with runs_lock():
        data = load()
        for run in data["runs"]:
            if run["id"] == run_id:
                run["pid"] = pid
                run["pgid"] = pgid
                run["argv"] = argv
                run["proc_started"] = process_start_time(pid)
                run["process_started_at"] = started_at
                store(data)
                return run
    return None


# ------------------------------------------------------------------- ledger

def debit(amount, run_id=None):
    """Add a run's *reported* cost to today's ledger. Returns the new total.

    Never the cap that was requested: `--max-budget-usd` overshoots, so the only
    honest number is the one the result envelope reports.
    """
    try:
        amount = float(amount or 0.0)
    except (TypeError, ValueError):
        return None
    with runs_lock():
        data = load()
        ledger = data["ledger"]
        today = date.today().isoformat()
        if ledger.get("date") != today:
            ledger = {"date": today, "spent_usd": 0.0}
        ledger["spent_usd"] = round(float(ledger.get("spent_usd", 0.0)) + amount, 4)
        data["ledger"] = ledger
        if run_id:
            for run in data["runs"]:
                if run["id"] == run_id:
                    run.setdefault("cost", {})["total_usd"] = round(
                        float(run.get("cost", {}).get("total_usd", 0.0)) + amount, 4)
        store(data)
        return ledger["spent_usd"]


def spent_today(data=None):
    ledger = (data or load())["ledger"]
    if ledger.get("date") != date.today().isoformat():
        return 0.0
    return float(ledger.get("spent_usd", 0.0))


def reserved(data=None):
    """Worst-case **future** spend already committed to unfinished runs.

    Admission checks `spent + reserved + this run` against the daily cap. Without
    the middle term, five queued runs each pass the same "there is room for one"
    test and the cap is blown by the time any of them reports a cost.

    Two subtleties, both found in review:

    * An `orphaned` run is **still counted**. Orphaned means a live model process
      from a dead board - it is the one state where something is spending that
      nobody is watching, so releasing its reservation is exactly backwards.
      It stops counting when it is killed or reaped, not when it is noticed.
    * A run that has produced its `fit` has finished pass A, and pass A's spend is
      already in the ledger. Its future worst case is the pass-B cap alone, not
      both caps - otherwise a card left open all afternoon holds $0.40 of the
      day's allowance that nothing can ever spend.
    """
    data = data or load()
    total = 0.0
    for run in data["runs"]:
        if run.get("phase") in TERMINAL:
            continue
        budget = run.get("budget_usd") or {}
        if run.get("kind") in ("revise", "redraft"):
            spent = float((run.get("cost") or {}).get("total_usd", 0.0))
            total += max(0.0, sum(float(v or 0) for v in budget.values()) - spent)
            continue
        pass_b = float(budget.get("pass_b", 0.0))
        if run.get("fit"):
            total += pass_b
        else:
            spent = float((run.get("cost") or {}).get("total_usd", 0.0))
            total += max(0.0, float(budget.get("pass_a", 0.0)) + pass_b - spent)
    return round(total, 4)


def session_spent(run):
    """This run plus every ancestor it forked from - the §2.7 session budget."""
    index = {r["id"]: r for r in load()["runs"]}
    total, seen, cursor = 0.0, set(), run.get("id")
    while cursor and cursor not in seen:
        seen.add(cursor)
        record = index.get(cursor)
        if not record:
            break
        total += float((record.get("cost") or {}).get("total_usd", 0.0))
        cursor = record.get("parent")
    return round(total, 4)


# ----------------------------------------------------------------- identity

def slugify(company, role):
    """`<company>_<role>` - lowercase, underscores, the convention in documents/README.md."""
    raw = "%s_%s" % (company or "unknown", role or "role")
    slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return (slug or "application")[:80]


def new_run_id(company):
    """Unique to the second *and* to the click: two roles at the same company
    started in the same second must not share a run directory."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = re.sub(r"[^a-z0-9]+", "", (company or "job").lower())[:16] or "job"
    return "r-%s-%s-%s" % (stamp, tag, uuid.uuid4().hex[:6])


def run_dir(run_id):
    return RUN_DIRS / run_id


def state_dir(run_id):
    return RUN_STATE / run_id


# ---------------------------------------------------------------- processes

def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _ps(fields, pid):
    """`ps` output for one pid, or None.

    `-ww` because the default truncates a long command line to the terminal
    width on Linux, and this argv carries a multi-thousand-character prompt.
    `LC_ALL=C` because `lstart` is formatted in the current locale, and an
    identity check that compares two strings must not depend on which locale the
    board happened to start in.
    """
    if not pid:
        return None
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        proc = subprocess.run(["ps", "-ww", "-o", fields, "-p", str(pid)],
                              capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def process_start_time(pid):
    """The process's start timestamp, as `ps` reports it.

    (pid, start time) is the identity pair that survives PID recycling: the
    kernel can hand the number back out, but not with the same start instant.
    The design asked for an argv hash; `ps -o args=` re-joins argv on spaces, so
    a hash of it can never equal a hash of the argv we passed. This compares
    something `ps` reports exactly.
    """
    out = _ps("lstart=", pid)
    return (out or "").strip() or None


def process_matches(record, strict=True):
    """Is this record's process still the one it registered?

    Two answers, because the two callers are asking different questions and the
    costs of being wrong are not symmetric:

    * `strict=True` — **Kill.** Every check the record supports must pass. This
      is the call that sends `SIGKILL` to a number a human clicked next to, and
      a pid recycled onto someone else's process is unrecoverable. `lstart` has
      one-second resolution, so it can match a same-second recycle; a `ps` line
      can in principle still be cut short. Requiring both, where both were
      recorded, is what makes this an assertion rather than a guess.
    * `strict=False` — **startup reconciliation.** The absence of *disproof* is
      enough to surface the run as `orphaned`: no recorded check may be false,
      but a check that could not be read counts as unknown rather than against.
      Being wrong here shows a Kill button next to a run that had already died,
      which costs a click; being wrong the other way writes off a live `claude`
      that keeps spending and keeps holding `.model.lock`, and says nothing about
      it. Surface generously, signal strictly.

    A record with neither field is never matched, at either strictness:
    unprovable identity is not a licence to signal.
    """
    pid = record.get("pid")
    if not process_alive(pid):
        return False
    recorded = record.get("proc_started")
    marker = record.get("session_id")
    if not recorded and not marker:
        return False

    # Each check is True, False, or **None for "could not tell"** - `ps` can fail
    # or time out under load, and treating that as a negative made a live orphan's
    # classification depend on how busy the machine was.
    checks = []
    if recorded:
        observed = process_start_time(pid)
        checks.append(None if observed is None else observed == recorded)
    if marker:
        checks.append(_argv_contains(pid, marker))

    if strict:
        # Nothing unknown, nothing false. Signalling needs proof.
        return all(check is True for check in checks)
    # Surfacing needs only the absence of disproof: a live pid we cannot rule out
    # is shown as an orphan, and Kill re-checks strictly before touching it.
    return not any(check is False for check in checks)


def _argv_contains(pid, marker):
    """True, False, or None when `ps` could not be read at all.

    The supervisor puts `--session-id <uuid>` at the **head** of the argv, before
    the multi-thousand-character prompt, precisely so that this stays answerable
    when `ps` does truncate.
    """
    if not marker:
        return None
    out = _ps("args=", pid)
    return None if out is None else (marker in out)


def board_alive(record):
    """Is the board that started this run still running? True, False, or None.

    Three-valued for the same reason process identity is: "cannot tell" is a
    distinct answer from "no", and the two callers need it. `None` means either
    no ownership was recorded at all (a run from before this existed) or `ps`
    could not be read. Reconciliation may surface an unknown-owner run;
    **Kill must never signal one**.


    A second board on a second port must not adopt, fail, or kill the first
    board's runs. `owner_pid` alone is not enough - PIDs recycle - so the owning
    board's own start time is recorded alongside it.
    """
    pid = record.get("owner_pid")
    if not pid:
        # Written by a board from before ownership was recorded. Its owner cannot
        # be identified at all - which is **unknown**, not "nobody". Reconciliation
        # may surface such a run; Kill must not signal it.
        return None
    if int(pid) == os.getpid():
        return False
    if not process_alive(pid):
        return False
    recorded = record.get("owner_started")
    if not recorded:
        return True          # unknown vintage: assume alive rather than adopt it
    observed = process_start_time(pid)
    if observed is None:
        # `ps` could not be read - which happens under load, and is not evidence
        # either way. Saying "dead" hands a live board's run to this one; saying
        # "alive" would let a *recycled* owner pid permanently block Kill on a
        # genuine orphan. Unknown is the honest answer, and both callers handle
        # it: reconciliation surfaces, Kill refuses and leaves the run orphaned.
        return None
    return observed == recorded
