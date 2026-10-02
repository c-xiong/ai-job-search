#!/usr/bin/env python3
"""On-demand job collection across every source. One button, one run, one log.

    python3 tools/fetch_jobs.py                                    # all sources
    python3 tools/fetch_jobs.py --sources ats --max-companies 8
    python3 tools/fetch_jobs.py --sources linkedin,freehire --dry-run

This is what the job board's **Fetch new jobs** button and optional launchd
schedule call. Both triggers use the same bounded run, configuration and lock.

Why an orchestrator rather than three buttons: the budgets that keep this polite
are per *run*, not per source. One lock, one combined log, one place that knows
LinkedIn's `detail` cap. The per-run cap bounds collection regardless of trigger.

Every source is individually deselectable, and adding one means adding a module
here, not rewriting the trigger.

Stdlib only, Python 3.9+. Requires `bun` on PATH for the portal CLIs.
"""

import argparse
import json
import os
import re
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
import postings  # noqa: E402

ROOT = jobs_md.ROOT
CONFIG = ROOT / "job_scraper" / "scrape_config.json"
LOG = ROOT / "job_scraper" / "scrape.log"
LOCK = ROOT / "job_scraper" / ".fetch.lock"
STATUS = ROOT / "job_scraper" / "fetch_status.json"

SOURCES = ("ats", "freehire", "linkedin", "arbeitnow")

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

    def __init__(self, path=None):
        self.path = Path(path if path is not None else LOCK)
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


def make_logger(lines, persist=True):
    def log(message):
        line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M"), message)
        lines.append(line)
        print(line, flush=True)
        if persist:
            try:
                with LOG.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
    return log


def write_status(running, lines, summaries, error=None, started_at=None):
    """A tiny status file the board polls while a background run is in flight.

    `started_at` is the run stamp every row this run inserts carries as
    `first_seen_at`, and it outlives the run on purpose: it is how the board
    knows which rows the *last* fetch brought in, including across a restart of
    the server, whose in-process copy of this dict starts empty. A finished run
    also records when it finished, so "Last checked" survives the same restart.
    """
    try:
        jobs_md.write_json_atomic(STATUS, {
            "running": running,
            "updated_at": datetime.now().isoformat(),
            "started_at": started_at,
            "finished_at": None if running else datetime.now().isoformat(),
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
        failed_before = budget.failed if budget is not None else 0
        deferred_before = budget.deferred if budget is not None else 0
        known_key = jobs_md.entry_key(known_urls, url) if isinstance(known_urls, dict) else (url if url in known_urls else None)
        known_entry = known_urls.get(known_key, {}) if isinstance(known_urls, dict) else {}
        if known_key is not None and (not isinstance(known_urls, dict) or postings.has_body(known_entry)):
            # A row already on the board is not re-screened: the detail budget
            # exists for postings nobody has judged yet. It still carries any
            # description the listing shipped inline, which is what lets an
            # already-known freehire or ATS row gain a body it was missing.
            status, note, text = None, None, record.get("description") or ""
        else:
            status, note, text = collectors.screen(record, log, budget)
        if known_key is not None:
            status, note = None, None  # merge enriches bodies without replacing decisions.
        rows.append({
            "id": record.get("id", ""),
            "title": record.get("title", ""),
            "company": record.get("company", ""),
            "registry_company": record.get("company", ""),
            "location": record.get("location", ""),
            "posted": record.get("posted", ""),
            "deadline": None,
            "url": url,
            # `text` over `record["description"]`: for LinkedIn the listing has
            # no description and the screen's `detail` call is the only place
            # the posting body exists.
            "description": text or record.get("description") or "",
            "status": status,
            "note": note,
            "_detail_failed": bool(budget is not None and budget.failed > failed_before),
            "_detail_deferred": bool(budget is not None and budget.deferred > deferred_before),
        })
    return rows


def collection_state_path():
    return jobs_md.SEEN.parent / "collection_state.json"


def load_collection_state():
    try:
        state = json.loads(collection_state_path().read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _linkedin_backfill(found, seen, budget, session):
    """Three fresh details then one backlog detail, with a rotating backlog cursor."""
    fresh, backlog = [], []
    seen_links = set(found)
    for url, record in found.items():
        if jobs_md.entry_key(seen, url) is None:
            fresh.append((url, record))
        else:
            backlog.append((url, record))
    for key, entry in seen.items():
        if postings.has_body(entry):
            continue
        for link in jobs_md.known_urls(entry, key):
            canonical = jobs_md.canonical_url(link)
            match = re.fullmatch(r"https://www\.linkedin\.com/jobs/view/(\d{6,})", canonical or "")
            if match and canonical not in seen_links:
                backlog.append((canonical, {"id": match.group(1), "url": canonical,
                    "title": entry.get("title") or "", "company": entry.get("company") or "",
                    "location": entry.get("location") or "", "posted": entry.get("posted") or "",
                    "portal": "linkedin-search", "description": ""}))
                seen_links.add(canonical)
                break
    backlog.sort(key=lambda item: item[0])
    start = collectors.bounded(session.state.get("backfill_cursor", 0), 0, 0, 10**9) % max(1, len(backlog))
    backlog = backlog[start:] + backlog[:start]
    terms = session.cfg.get("detail_priority_terms") or ["engineer", "data", "research", "developer", "scientist"]
    fresh.sort(key=lambda item: -sum(term.lower() in item[1].get("title", "").lower() for term in terms))
    ordered, attempted_backlog = {}, 0
    while fresh or backlog:
        for _ in range(3):
            if fresh:
                url, row = fresh.pop(0)
                ordered[url] = row
        if backlog:
            url, row = backlog.pop(0)
            ordered[url] = row
            attempted_backlog += int(len(ordered) <= max(0, budget.limit - budget.used))
        if not fresh and len(ordered) >= max(len(found), budget.limit - budget.used):
            break
    session.state["backfill_cursor"] = start + attempted_backlog
    return ordered, len(backlog)


def _yield_counts(before, seen, urls):
    keys = {jobs_md.entry_key(seen, url) for url in urls}
    keys.discard(None)
    return {"new": sum(key not in before for key in keys),
            "enriched": sum(key in before and (seen[key].get("posting_chars") or 0) > (before[key].get("posting_chars") or 0) for key in keys),
            "gated": sum(seen[key].get("user_status") == "gate" for key in keys)}


def _gate_unreviewed_backfills(before, seen, rows):
    """A newly hydrated hard language requirement may gate an untouched backlog row.

    Human decisions/notes and ranked rows remain authoritative. This transition
    is deliberately outside merge(), whose append-only protection stays intact.
    """
    for row in rows:
        key = jobs_md.entry_key(seen, row["url"])
        if key not in before or not row.get("description"):
            continue
        entry = seen[key]
        if entry.get("user_status", entry.get("status")) not in ("new", "backlog"):
            continue
        if entry.get("fit_source") == "ranked" or any(entry.get(field) is not None for field in ("rank_score", "rank_date", "rank_verdict")):
            continue
        if any(entry.get(field) and not str(entry[field]).startswith("AUTO-SCREEN:") for field in ("note", "user_note")):
            continue
        verdict, note = collectors.screen_text(row["description"])
        if verdict == "gate":
            entry["user_status"] = "gate"
            entry["note"] = note


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
    log = make_logger(lines, persist=not dry_run)
    cfg = load_config()
    today = date.today().isoformat()
    # One run, one stamp, shared by every source below: the board groups rows by
    # it to show what this fetch - rather than this *day* - brought in, and two
    # fetches in one afternoon are otherwise indistinguishable.
    stamp = ats_fetch.run_stamp()
    summaries = []
    session = collectors.CollectionSession(cfg, sources, load_collection_state())
    def status(running):
        if not dry_run:
            write_status(running, lines, summaries, started_at=stamp)

    with RunLock(), activity_hook(emit), session:
        status(True)
        log("fetch start - sources: %s%s" % (", ".join(sources), " (dry run)" if dry_run else ""))
        budget = _detail_budget(cfg, detail_budget)
        intake_cfg = cfg.get("linkedin_intake", {})
        if not isinstance(intake_cfg, dict):
            summaries.append({"source": "linkedin-intake", "error": "configuration must be an object", "degraded": True, "added": 0})
            intake_cfg = {}
        if any(isinstance(block, dict) and block.get("enabled") for block in intake_cfg.values()):
            try:
                from linkedin_intake import ingest_from_config
                result = ingest_from_config(cfg, log, dry_run=dry_run)
                failed = [name for name, item in result.items() if isinstance(item, dict)
                          and (item.get("status") in ("failed", "partial") or item.get("failed"))]
                summaries.append(dict(result, source="linkedin-intake", failed=failed,
                                      degraded=bool(failed), added=0))
            except Exception as exc:
                log("  ! LinkedIn intake failed: %s" % exc)
                summaries.append({"source": "linkedin-intake", "error": str(exc), "degraded": True, "added": 0})
        inbox_cfg = cfg.get("linkedin_inbox", {})
        if not isinstance(inbox_cfg, dict):
            summaries.append({"source": "linkedin-inbox", "error": "configuration must be an object", "degraded": True, "added": 0})
            inbox_cfg = {}
        if inbox_cfg.get("auto_process", False):
            allocation = min(budget.limit, collectors.bounded(inbox_cfg.get("process_limit", 5), 5, 0, 5))
            try:
                from linkedin_inbox import process
                if allocation:
                    log("  LinkedIn detail allocation: inbox up to %d, shared total %d" % (allocation, budget.limit))
                    result = process(limit=allocation, dry_run=dry_run)
                    budget.used = collectors.bounded(result.get("detail_calls", result.get("processed", allocation)), allocation, 0, budget.limit)
                    budget.blocked = bool(result.get("rate_limited"))
                    if budget.blocked:
                        session.blocked.add("linkedin")
                    summaries.append(dict(result, source="linkedin-inbox", added=result.get("added", 0),
                                          degraded=bool(result.get("retry") or result.get("needs_manual"))))
            except Exception as exc:
                log("  ! LinkedIn inbox failed: %s" % exc)
                # A processor failure can follow requests; conservatively reserve its allocation.
                budget.used = min(budget.limit, allocation)
                summaries.append({"source": "linkedin-inbox", "error": str(exc), "degraded": True, "added": 0})

        # ---- collect (slow, network) ---------------------------------------
        ats_rows, ats_meta = [], {}
        if "ats" in sources:
            ats_cfg = cfg.get("ats", {}) or {}
            # Companies you saved whose board is not known yet get another try
            # first; a board found here is fetched in this same run.
            detected = {"checked": 0, "found": [], "ask": []}
            if not dry_run:
                try:
                    detected = ats_fetch.detect(log, per_fetch=_bound(
                        None, ats_cfg, "resolve_per_fetch", 5, 0, 20))
                except Exception as exc:
                    detected["error"] = str(exc)
                    log("  ! ATS detection failed: %s" % exc)
            try:
                ats_rows, ats_meta = ats_fetch.collect(
                    log,
                    max_companies=_bound(max_companies, ats_cfg, "max_companies",
                                         ATS_COMPANY_DEFAULT, 1, ATS_COMPANY_CEILING),
                    max_new_jobs=_bound(max_new_jobs, ats_cfg, "max_new_jobs",
                                        ATS_NEW_JOBS_DEFAULT, 1, ATS_NEW_JOBS_CEILING),
                    dry_run=dry_run,
                    known_ids=ats_fetch.known_ids_from(load_seen()),
                )
                if not ats_meta:
                    ats_meta = {"degraded": True, "error": "ATS collector produced no usable metadata"}
            except Exception as exc:
                ats_meta = {"degraded": True, "error": str(exc)}
                log("  ! ATS failed: %s" % exc)
            ats_meta = dict(ats_meta or {}, detected=detected)
            status(True)

        known_urls = load_seen()
        plain = []
        for name, collect in (("freehire", collectors.collect_freehire),
                              ("linkedin", collectors.collect_linkedin),
                              ("arbeitnow", collectors.collect_arbeitnow)):
            if name not in sources:
                continue
            try:
                found = collect(cfg, log)
            except Exception as exc:
                session.outcome(name, name + "-collector", 0, [], error=str(exc))
                log("  ! %s collector failed: %s" % (name, exc))
                found = {}
            if name == "linkedin":
                budget.blocked = budget.blocked or "linkedin" in session.blocked
                found, backfill_deferred = _linkedin_backfill(found, known_urls, budget, session)
            rows = _screened_rows(found, budget if name == "linkedin" else None, log, known_urls)
            plain.append((name, rows))
            status(True)

        if budget.deferred:
            log("  . %d LinkedIn postings stored without a description - the run's detail "
                "budget (%d) was spent; they are stored unscreened and can be screened next run"
                % (budget.deferred, budget.limit))

        # ---- merge and write (fast, locked) --------------------------------
        with jobs_md.board_lock():
            seen = load_seen()
            before = len(seen)
            # `new` means "arrived in this fetch": last run's unreviewed rows
            # step down to `backlog` before this run's rows land.
            demoted = jobs_md.demote_unreviewed(seen)
            if demoted:
                log("  %d unreviewed rows from earlier fetches moved to backlog" % demoted)
            # Posting bodies collect here and are written only in the branch
            # below that also saves the state. merge() runs in full on a dry
            # run - only save_seen() is skipped - so a sidecar written inside it
            # would litter a run that is supposed to touch nothing.
            pending = []
            if "ats" in sources:
                before_source = {key: dict(entry) for key, entry in seen.items()}
                summary = ats_fetch.summarize(
                    ats_fetch.merge(seen, ats_rows, today, log, pending=pending,
                                    stamp=stamp),
                    ats_meta, len(ats_rows), log, dry_run)
                summary.update(_yield_counts(before_source, seen, [row["url"] for row in ats_rows]))
                if ats_meta.get("error"):
                    summary.update(error=ats_meta["error"], degraded=True)
                summaries.append(summary)
            for name, rows in plain:
                portal = name + "-search" if name != "arbeitnow" else "arbeitnow"
                # Same conservative merge as the ATS source, so a job that an ATS
                # board and LinkedIn both return in one click is one row, not two.
                before_source = {key: dict(entry) for key, entry in seen.items()}
                stats = ats_fetch.merge(seen, rows, today, log, portal=portal,
                                        pending=pending, stamp=stamp)
                _gate_unreviewed_backfills(before_source, seen, rows)
                stats.pop("german_gated_by_company", None)
                summary = {"source": portal, "found": len(rows),
                           "deferred_descriptions": budget.deferred if name == "linkedin" else 0}
                summary.update(stats)
                summary.update(_yield_counts(before_source, seen, [row["url"] for row in rows]))
                summary.update(session.summary(name))
                for outcome in summary["queries"]:
                    outcome.update(_yield_counts(before_source, seen, outcome["urls"]))
                    matched_rows = [row for row in rows if row["url"] in outcome["urls"]]
                    outcome["failed_descriptions"] = sum(row["_detail_failed"] for row in matched_rows)
                    outcome["deferred_descriptions"] = sum(row["_detail_deferred"] for row in matched_rows)
                if name == "linkedin":
                    summary.update(detail_calls=budget.used, failed_descriptions=budget.failed,
                                   deferred_backfill=backfill_deferred,
                                   detail_http_attempts=budget.http_attempts, detail_retries=budget.retries,
                                   rate_limited=budget.blocked)
                    summary["degraded"] = summary["degraded"] or bool(budget.failed)
                if name == "arbeitnow" and not (cfg.get("arbeitnow") or {}).get("enabled", False):
                    summary.update(skipped=True, enabled=False)
                summaries.append(summary)
            log("  %d new rows (%d in the board)" % (len(seen) - before, len(seen)))
            if dry_run:
                log("dry run - nothing written (%d posting bodies not stored)" % len(pending))
            else:
                # Bodies before state: an orphan sidecar is inert, while a saved
                # `posting_path` with no file behind it is a row that claims a
                # posting it cannot show.
                written = postings.commit_all(pending)
                if written:
                    log("  %d posting bodies stored" % written)
                jobs_md.save_seen(seen)
                jobs_md.write_json_atomic(collection_state_path(), session.persisted_state(stamp))
                log("done - %d jobs in the board" % len(seen))

    status(False)
    return summaries, lines


def degraded_reason(summary):
    """Format heterogeneous source outcomes without masking a persisted run."""
    reasons = []
    failed = summary.get("failed")
    if isinstance(failed, (list, tuple)):
        reasons.extend(str(item) for item in failed if item)
    elif isinstance(failed, str) and failed:
        reasons.append(failed)
    elif isinstance(failed, (int, float)) and failed > 0:
        reasons.append("%d failed" % failed)
    if summary.get("error"):
        reasons.append(str(summary["error"]))
    for key, label in (("retry", "retries pending"), ("needs_manual", "need manual review"),
                       ("failed_descriptions", "descriptions failed")):
        value = summary.get(key)
        if isinstance(value, (int, float)) and value > 0:
            reasons.append("%d %s" % (value, label))
    return ", ".join(reasons) or "source incomplete; check fetch log"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", default=",".join(SOURCES),
                        help="comma-separated: ats, freehire, linkedin, arbeitnow (default: all)")
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
              + ("  DEGRADED: " + degraded_reason(summary) if summary.get("degraded") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
