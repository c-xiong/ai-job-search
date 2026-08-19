#!/usr/bin/env python3
"""Owns the model process, its lock and its registration - then `exec`s it.

    python3 tools/board/run_wrapper.py --spec job_scraper/run_state/<id>/spec.json

The board does not spawn `claude` directly, and the reason is a race the obvious
design cannot close. A PID does not exist until after `Popen` returns, so a board
killed in between has spawned a spender it never recorded. And a `flock` held by
the board dies with the board, which would free the lock while the model process
it was protecting is still running and still spending.

So the process that owns the model takes the lock and writes the record, before
the model exists:

1. `flock` `job_scraper/.model.lock`, non-blocking. Held: another model process
   is alive, so this one refuses rather than doubling the spend.
2. `setsid`, so the whole run is one process group and `cancel` is one `killpg`
   rather than a hunt for children.
3. Write `pid`, `pgid`, the argv and the process's own start time into
   `runs.json` under `.runs.lock`. (pid, start time) is the pair that survives
   PID recycling: the kernel can hand the number back out, but not with the same
   start instant.
4. `exec` claude - same PID, same process group, same lock. The lock's fd has
   `FD_CLOEXEC` cleared explicitly, which is what lets it survive the `exec`.

After step 4 this file is not running any more; the kernel holds the lock on
behalf of the model process, and releases it when that process dies however it
dies. That is what makes orphan detection sound: a lock that is free means no
model process, with no bookkeeping to trust.

Stdlib only, Python 3.9+.
"""

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from board import run_registry  # noqa: E402


def _clear_cloexec(fd):
    """Let the lock survive `exec`. Without this the model runs unlocked."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Spawn one guarded claude run")
    parser.add_argument("--spec", required=True,
                        help="JSON written by the supervisor: run_id, argv, env, cwd")
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    run_id = spec["run_id"]
    child_argv = list(spec["argv"])

    # The spec is this process's entire input, paths included. Taking them from
    # the file rather than from the module's own defaults is what lets the
    # supervisor be exercised end to end against a temporary state directory -
    # and it keeps the wrapper honest about where it is writing.
    run_registry.REGISTRY = Path(spec["registry"])
    run_registry.RUNS_LOCK = Path(spec["runs_lock"])
    run_registry.MODEL_LOCK = Path(spec["model_lock"])

    run_registry.MODEL_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = open(run_registry.MODEL_LOCK, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another model process already holds %s - refusing to start a second "
              "spender" % run_registry.MODEL_LOCK, file=sys.stderr)
        return 75  # EX_TEMPFAIL: the caller may retry once the other run ends
    _clear_cloexec(handle.fileno())

    # Fail closed. Without our own session the recorded pgid is the *board's*
    # group, and the first Cancel or timeout `killpg`s the board itself along
    # with every sibling it started. A run that cannot be isolated is a run that
    # must not start; nothing has been registered or exec'd at this point.
    try:
        os.setsid()
    except OSError as exc:
        print("cannot create a new session (%s); refusing to run in the board's own "
              "process group, where cancelling this run would kill the board"
              % exc, file=sys.stderr)
        return 71  # EX_OSERR
    if os.getpgid(0) != os.getpid():
        print("setsid returned but this process is not its own group leader; refusing "
              "to run un-isolated", file=sys.stderr)
        return 71

    run_registry.register_process(run_id, pid=os.getpid(), pgid=os.getpgid(0),
                                  argv=child_argv,
                                  started_at=datetime.now().isoformat())

    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
    cwd = spec.get("cwd")
    if cwd:
        os.chdir(cwd)

    try:
        os.execvpe(child_argv[0], child_argv, env)
    except OSError as exc:
        print("cannot exec %s: %s" % (child_argv[0], exc), file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
