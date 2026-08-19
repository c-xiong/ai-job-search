"""The structured event log behind the activity strip.

Three things used to happen silently and are the reason this module exists:

1. Setting a status updates the canonical `seen_jobs.json`, and nothing said so.
2. A collection run can succeed overall while one source fails; that partial
   failure was a banner and nothing more.
3. An exit code is the difference between "the CV is two pages" and "nobody
   knows how many pages the CV is".

This is deliberately **not** a tail of `job_scraper/scrape.log`. That file is
prose written for a human, and parsing it back into structure would be inventing
data its writer never committed to. Instead whoever does the work emits an
event, and `scrape.log` keeps being written independently.

Every event carries `(epoch, seq)`. `epoch` is minted at server start, `seq` is
monotonic within it, so a browser that polled across a restart gets a `reset`
rather than silently suppressing every event whose `seq` looks old.

Stdlib only, Python 3.9+.
"""

import json
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

LOG = jobs_md.ROOT / "job_scraper" / "activity.jsonl"

# What the UI can hold without the strip becoming a memory leak, and what a
# person can plausibly scroll back through when something has gone wrong.
RING = 500

# An event whose message is longer than this is truncated rather than allowed to
# push a multi-megabyte line into the log - a LaTeX error can be very long.
MAX_MSG = 2000

SOURCES = ("collect", "board", "claude", "latex", "verify", "registry", "server")
LEVELS = ("info", "warn", "error")

_lock = threading.Lock()
_events = deque(maxlen=RING)
_seq = 0
EPOCH = uuid.uuid4().hex[:12]


def _clip(value, limit=MAX_MSG):
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def emit(source, msg, level="info", cmd=None, exit_code=None, ms=None,
         detail=None, run_id=None):
    """Record one event. Never raises: logging must not break the work it logs."""
    global _seq
    event = {
        "epoch": EPOCH,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": source if source in SOURCES else "server",
        "level": level if level in LEVELS else "info",
        "msg": _clip(msg),
        "run_id": run_id,
    }
    if cmd is not None:
        event["cmd"] = _clip(cmd, 600)
    if exit_code is not None:
        event["exit"] = exit_code
    if ms is not None:
        event["ms"] = int(ms)
    if detail:
        event["detail"] = [_clip(d, 400) for d in detail][:20]

    with _lock:
        _seq += 1
        event["seq"] = _seq
        _events.append(event)
        line = json.dumps(event, ensure_ascii=False)

    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        # The in-memory ring still has it, so the strip keeps working even when
        # the disk does not. A logger that raises is worse than a lossy one.
        pass
    return event


def since(seq=0, epoch=None):
    """Events after `seq`. A mismatched epoch resets the client to the full ring."""
    with _lock:
        events = list(_events)
        current_seq, current_epoch = _seq, EPOCH
    reset = epoch is not None and epoch != current_epoch
    if reset or not seq:
        selected = events
    else:
        selected = [e for e in events if e["seq"] > seq]
    return {
        "epoch": current_epoch,
        "seq": current_seq,
        "reset": reset,
        "events": selected,
        "counts": _counts(events),
    }


def _counts(events):
    counts = {"all": len(events)}
    for event in events:
        counts[event["source"]] = counts.get(event["source"], 0) + 1
        if event["level"] == "error":
            counts["errors"] = counts.get("errors", 0) + 1
    return counts


class Timer:
    """`with Timer() as t: ...` then `t.ms`. Used to make durations honest."""

    def __enter__(self):
        self._start = time.monotonic()
        self.ms = 0
        return self

    def __exit__(self, *exc):
        self.ms = int((time.monotonic() - self._start) * 1000)
        return False


def collector_emitter(run_id=None):
    """The callback `tools/collectors.bun()` takes, bound to a run.

    Returns None when nothing should be emitted, which is what keeps the
    terminal path (`tools/scrape_cron.py`) byte-identical to before.
    """

    def emitter(phase, argv, **kw):
        cmd = " ".join(argv)
        if phase == "start":
            return emit("collect", "run " + Path(argv[-1]).name if argv else "run",
                        cmd=cmd, run_id=run_id)
        level = "error" if kw.get("exit_code") else "info"
        parsed = kw.get("parsed")
        msg = "exit %s" % kw.get("exit_code")
        if parsed is False:
            msg += " - output did not parse"
            level = "warn" if level == "info" else level
        return emit("collect", msg, level=level, cmd=cmd,
                    exit_code=kw.get("exit_code"), ms=kw.get("ms"),
                    detail=[d for d in [kw.get("stderr")] if d], run_id=run_id)

    return emitter


def reset_for_tests():
    """Drop the in-memory ring. Tests only; the file on disk is left alone."""
    global _seq
    with _lock:
        _events.clear()
        _seq = 0
