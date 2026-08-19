#!/usr/bin/env python3
"""On-demand job collection across every source. One button, one run, one log.

    python3 tools/fetch_jobs.py                                    # all sources
    python3 tools/fetch_jobs.py --sources ats --max-companies 8
    python3 tools/fetch_jobs.py --sources linkedin,freehire --dry-run

This is what the job board's **Fetch new jobs** button calls. It replaces the
09:00 launchd schedule: collection happens when you ask for it, in a bounded
batch, instead of unattended every morning.

Why an orchestrator rather than three buttons: the budgets that keep this polite
are per *run*, not per source. One lock, one combined log, one place that knows
LinkedIn's `detail` cap - moving from a cron to a button removed the unattended
burst, not the burst itself, and the per-run cap is what actually bounds it.

Every source is individually deselectable, and adding one means adding a module
here, not rewriting the trigger.

Stdlib only, Python 3.9+. Requires `bun` on PATH for the portal CLIs.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ats_fetch  # noqa: E402
import collectors  # noqa: E402
import jobs_md  # noqa: E402

ROOT = jobs_md.ROOT
CONFIG = ROOT / "job_scraper" / "scrape_config.json"
LOG = ROOT / "job_scraper" / "scrape.log"
LOCK = ROOT / "job_scraper" / ".fetch.lock"
STATUS = ROOT / "job_scraper" / "fetch_status.json"

SOURCES = ("ats", "freehire", "linkedin")

ATS_COMPANY_DEFAULT = 8
ATS_COMPANY_CEILING = 20
ATS_NEW_JOBS_DEFAULT = 40
ATS_NEW_JOBS_CEILING = 200

# LinkedIn is the one source that costs a request per new posting, to read the
# description the German screen needs. 15 per run by default, and never more
# than 20 - the ceiling is not configurable upward on purpose.
LINKEDIN_DETAIL_DEFAULT = 15
LINKEDIN_DETAIL_CEILING = 20




class AlreadyRunning(Exception):
    """Raised when another fetch holds the lock."""


class activity_hook:
    """Install the board's activity callback on `collectors` for one run.

    A context manager rather than a bare assignment so an exception cannot leave
    the board's hook attached to a later terminal-driven collection in the same
    process. `None` restores exactly the previous state, which is what keeps the
    terminal path unchanged.
    """

    def __init__(self, emit):
        self.emit = emit

    def __enter__(self):
        self.previous = collectors.EMITTER
        collectors.EMITTER = self.emit
        return self

    def __exit__(self, *exc):
        collectors.EMITTER = self.previous
        return False


class RunLock:
    """One fetch at a time, enforced by the OS rather than by a heuristic.

    The first version of this reasoned about staleness: read the lock file, decide
    whether its holder was dead or merely slow, and unlink it if so. Every version
    of that has the same race - two processes both conclude the lock is stale,
    both unlink, both create - and it has to guess at "slow" in the first place.

    `flock` needs neither. The kernel releases the lock when the holding file
    descriptor closes, including when the process is killed, so there is no stale
    lock to reclaim and nothing to guess. The file's contents are informational
    only: who holds it and since when, for the status display.
    """

    def __init__(self, path=LOCK):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:  # pragma: no cover - Windows
            # No flock: fall back to exclusive create. Weaker (a killed process
            # leaves the lock behind) but never wrong about a live holder.
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise AlreadyRunning("another fetch is already running (%s)" % self.path)
            self.handle = os.fdopen(fd, "w")
            self.handle.write(json.dumps({"pid": os.getpid(),
                                          "started_at": datetime.now().isoformat()}))
            self.handle.flush()
            return self
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning("another fetch is already running (%s)" % self.path)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "started_at": datetime.now().isoformat()}))
        handle.flush()
        self.handle = handle
        return self

    def __exit__(self, *exc):
        if self.handle is None:
            return False
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()
        self.handle = None
        if fcntl is None:  # pragma: no cover - Windows
            try:
                self.path.unlink()
            except OSError:
                pass
        # With flock the file stays; the lock is the flock, not the file's
        # existence, so there is nothing to unlink and no way to unlink someone
        # else's.
        return False


def load_config():
    if not CONFIG.exists():
        return {}
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def make_logger(lines):
    def log(message):
        line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M"), message)
        lines.append(line)
        print(line, flush=True)
        try:
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
    return log


def write_status(running, lines, summaries, error=None):
    """A tiny status file the board polls while a background run is in flight."""
    try:
        jobs_md.write_json_atomic(STATUS, {
            "running": running,
            "updated_at": datetime.now().isoformat(),
            "log": lines[-40:],
            "sources": summaries,
            "error": error,
        })
    except OSError:
        pass


def _detail_budget(cfg, override):
    raw = override if override is not None else cfg.get("linkedin_max_detail_fetches",
                                                        LINKEDIN_DETAIL_DEFAULT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = LINKEDIN_DETAIL_DEFAULT
    return collectors.Budget(max(0, min(value, LINKEDIN_DETAIL_CEILING)))


def _screened_rows(found, budget, log, known_urls):
    """Screen every *new* posting and shape it for the merge.

    This is the slow half - it can make a `detail` request per posting - and it
    is deliberately separate from the merge, which has to hold the board's write
    lock. Holding that lock across a minute of network calls is exactly how a
    status you set mid-run would get clobbered by the run's stale snapshot.
    """
    rows = []
    for url, record in found.items():
        if url in known_urls:
            status, note = None, None
        else:
            status, note = collectors.screen(record, log, budget)
        rows.append({
            "id": record.get("id", ""),
            "title": record.get("title", ""),
            "company": record.get("company", ""),
            "registry_company": record.get("company", ""),
            "location": record.get("location", ""),
            "posted": record.get("posted", ""),
            "deadline": None,
            "url": url,
            "description": record.get("description") or "",
            "status": status,
            "note": note,
        })
    return rows


def _bound(explicit, cfg_block, key, default, low=None, high=None):
    """Explicit argument first, then config, then the built-in default.

    The other order is a trap: the UI sends "fetch 1 company", config says 8, and
    eight boards get fetched while the response reports 1.
    """
    value = explicit if explicit is not None else (cfg_block or {}).get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(value, high)
    return value


def load_seen():
    if not jobs_md.SEEN.exists():
        return {}
    return json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})


def fetch(sources=SOURCES, max_companies=None, max_new_jobs=None, detail_budget=None,
          dry_run=False, lines=None, emit=None):
    """Run the selected sources once and merge everything into the board.

    Collect first, write last. A run takes a minute or two, and the board is
    serving your status changes the whole time; loading `seen_jobs.json` up front
    and saving that snapshot at the end would silently revert every status you
    set while it ran. So the network half produces plain records, and the board
    state is read, merged and written inside one short critical section at the
    end, under `jobs_md.board_lock()` - the same lock the board's `/api/update`
    handler takes, and an OS-level one, because `python3 tools/fetch_jobs.py` in a
    terminal and an open board are two *processes*.

    `emit` is the board's activity hook: a callable taking (phase, argv, **kw),
    installed on `collectors` for the duration of the run and removed afterwards.
    None - the terminal default - leaves the collection path exactly as it was.

    Returns (summaries, log_lines). Raises AlreadyRunning if a fetch is in flight.
    """
    lines = lines if lines is not None else []
    log = make_logger(lines)
    cfg = load_config()
    today = date.today().isoformat()
    summaries = []

    with RunLock(), activity_hook(emit):
        write_status(True, lines, summaries)
        log("fetch start - sources: %s%s" % (", ".join(sources), " (dry run)" if dry_run else ""))

        # ---- collect (slow, network) ---------------------------------------
        ats_rows, ats_meta = [], {}
        if "ats" in sources:
            ats_cfg = cfg.get("ats", {}) or {}
            ats_rows, ats_meta = ats_fetch.collect(
                log,
                max_companies=_bound(max_companies, ats_cfg, "max_companies",
                                     ATS_COMPANY_DEFAULT, 1, ATS_COMPANY_CEILING),
                max_new_jobs=_bound(max_new_jobs, ats_cfg, "max_new_jobs",
                                    ATS_NEW_JOBS_DEFAULT, 1, ATS_NEW_JOBS_CEILING),
                dry_run=dry_run,
                known_ids=ats_fetch.known_ids_from(load_seen()),
            )
            write_status(True, lines, summaries)

        budget = _detail_budget(cfg, detail_budget)
        known_urls = set(load_seen())
        plain = []
        for name, collect in (("freehire", collectors.collect_freehire),
                              ("linkedin", collectors.collect_linkedin)):
            if name not in sources:
                continue
            found = collect(cfg, log)
            rows = _screened_rows(found, budget if name == "linkedin" else None, log, known_urls)
            plain.append((name, rows))
            write_status(True, lines, summaries)

        if budget.deferred:
            log("  . %d LinkedIn postings stored without a description - the run's detail "
                "budget (%d) was spent; they stay `new` and can be screened next run"
                % (budget.deferred, budget.limit))

        # ---- merge and write (fast, locked) --------------------------------
        with jobs_md.board_lock():
            seen = load_seen()
            before = len(seen)
            if "ats" in sources:
                summaries.append(ats_fetch.summarize(
                    ats_fetch.merge(seen, ats_rows, today, log),
                    ats_meta, len(ats_rows), log, dry_run))
            for name, rows in plain:
                portal = name + "-search"
                # Same conservative merge as the ATS source, so a job that an ATS
                # board and LinkedIn both return in one click is one row, not two.
                stats = ats_fetch.merge(seen, rows, today, log, portal=portal)
                stats.pop("german_gated_by_company", None)
                summary = {"source": portal, "found": len(rows),
                           "deferred_descriptions": budget.deferred if name == "linkedin" else 0}
                summary.update(stats)
                summaries.append(summary)
            log("  %d new rows (%d in the board)" % (len(seen) - before, len(seen)))
            if dry_run:
                log("dry run - nothing written")
            else:
                jobs_md.save_seen(seen)
                log("done - %d jobs in the board" % len(seen))

    write_status(False, lines, summaries)
    return summaries, lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", default=",".join(SOURCES),
                        help="comma-separated: ats, freehire, linkedin (default: all)")
    parser.add_argument("--max-companies", type=int, default=None,
                        help="ATS companies per run (default: scrape_config.json's ats block, else 8)")
    parser.add_argument("--max-new-jobs", type=int, default=None,
                        help="stop selecting further ATS companies past this many NEW rows "
                             "(default: scrape_config.json's ats block, else 40)")
    parser.add_argument("--linkedin-detail-fetches", type=int, default=None,
                        help="cap on LinkedIn description fetches (default %d, ceiling %d)"
                             % (LINKEDIN_DETAIL_DEFAULT, LINKEDIN_DETAIL_CEILING))
    parser.add_argument("--dry-run", action="store_true", help="collect and merge, write nothing")
    args = parser.parse_args()

    chosen = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in SOURCES]
    if unknown:
        print("unknown source(s): %s - expected any of %s" % (", ".join(unknown), ", ".join(SOURCES)))
        return 2
    if not chosen:
        print("no sources selected")
        return 2

    try:
        summaries, _ = fetch(chosen, args.max_companies, args.max_new_jobs,
                             args.linkedin_detail_fetches, args.dry_run)
    except AlreadyRunning as exc:
        print(exc)
        return 1
    for summary in summaries:
        print("  %-16s found %-4d added %-4d gated %-3d"
              % (summary["source"], summary.get("found", 0), summary.get("added", 0),
                 summary.get("gated", 0))
              + ("  DEGRADED: " + ", ".join(summary["failed"]) if summary.get("degraded") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
