#!/usr/bin/env python3
"""Unattended LinkedIn + freehire collector. No LLM, no tokens, no prompts.

    python3 tools/scrape_cron.py            # collect, screen, write
    python3 tools/scrape_cron.py --dry-run  # do everything except write

This is deliberately NOT `/scrape`. The Claude skill judges fit and reads a
posting's language requirements with real judgment; this script only *collects*,
so it can run without an agent, a token budget, or a permission prompt to answer.
New jobs land in the board as `new` with an empty Fit, for you (or a later
`/rank`) to assess.

The one judgment it makes is mechanical and conservative: a regex screen for
German stated as a job condition. A hit is filed as `gate` with the matched
sentence quoted, so nothing is silently dropped - it shows up under the board's
`gate` filter, one click from view, and you can overrule it.
A posting that only mentions German in passing is left alone for you to judge.

**Prefer `tools/fetch_jobs.py`** - the on-demand orchestrator behind the job
board's Fetch button. It covers ats-search as well, bounds the run, and takes the
board lock. This script stays as a working manual command for the two portals it
always ran, and both share the same collection code in `tools/collectors.py`, so
there is one path rather than two.

Whether the 09:00 launchd job still runs it is a separate question: retiring that
schedule is `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.aijobsearch.scrape.plist`,
and until you run it, this script is still being invoked unattended every morning.

Stdlib only, Python 3.9+. Requires `bun` on PATH for the portal CLIs.
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ats_fetch  # noqa: E402
import collectors  # noqa: E402
import jobs_md  # noqa: E402
import postings  # noqa: E402

ROOT = jobs_md.ROOT
CONFIG = ROOT / "job_scraper" / "scrape_config.json"
LOG = ROOT / "job_scraper" / "scrape.log"

# Kept as module-level names because they were the public surface of this script
# before the shared module existed.
GERMAN_RE = collectors.GERMAN_RE
SOFT_MARKERS = collectors.SOFT_MARKERS
german_hit = collectors.german_hit
results_of = collectors.results_of


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M"), msg)
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def bun(args, timeout=120):
    """Run a portal CLI. Returns parsed JSON, or None when there is nothing usable."""
    return collectors.bun(args, log, timeout=timeout)


def collect(cfg):
    """Run every configured query. Returns {url: record}."""
    found = {}
    found.update(collectors.collect_linkedin(cfg, log))
    found.update(collectors.collect_freehire(cfg, log))
    return found


def screen(record, budget=None):
    """Fetch the posting and apply the German screen. Returns (status, note, text)."""
    return collectors.screen(record, log, budget)


def rows_for(found, screened, known):
    """Shape collector records into merge rows.

    Known URLs are passed through too. They are not re-screened - the detail
    budget is for postings nobody has judged - but any description their
    listing shipped inline still reaches merge(), which is how a row already on
    the board gains a posting body it was missing.
    """
    rows = []
    for url, record in found.items():
        status, note, text = screened.get(url, (None, None, record.get("description") or ""))
        rows.append({
            "id": record.get("id", ""),
            "title": record.get("title", ""),
            "company": record.get("company", ""),
            "registry_company": record.get("company", ""),
            "location": record.get("location", ""),
            "posted": record.get("posted", ""),
            "deadline": None,
            "url": url,
            "description": text or record.get("description") or "",
            "status": None if url in known else status,
            "note": None if url in known else note,
            "portal": record.get("portal", ""),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="collect and screen, write nothing")
    args = parser.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    log("scrape_cron start%s" % (" (dry run)" if args.dry_run else ""))

    found = collect(cfg)
    # Screen before taking the lock: screening can make a request per posting,
    # and an open job board is writing to the same file the whole time.
    seen_snapshot = {}
    if jobs_md.SEEN.exists():
        seen_snapshot = json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})
    known = {jobs_md.canonical_url(u) for u in seen_snapshot}
    fresh = {u: r for u, r in found.items() if u not in known}
    log("  %d results, %d already known, %d new"
        % (len(found), len(found) - len(fresh), len(fresh)))
    try:
        detail_limit = int(cfg.get("linkedin_max_detail_fetches", 15))
    except (TypeError, ValueError):
        detail_limit = 15
    detail_budget = collectors.Budget(max(0, min(detail_limit, 20)))
    screened = {url: screen(rec, detail_budget) for url, rec in fresh.items()}
    if detail_budget.deferred:
        log("  %d LinkedIn postings deferred after the detail budget (%d) was spent"
            % (detail_budget.deferred, detail_budget.limit))

    if args.dry_run:
        gated = sum(1 for status, _, _ in screened.values() if status == "gate")
        log("  %d added (%d auto-gated on German, %d awaiting fit assessment)"
            % (len(fresh), gated, len(fresh) - gated))
        log("dry run - nothing written")
        return 0

    # One writer, shared with the board's Fetch button. This used to build its
    # own `seen[url] = {...}` dict, which meant the unattended path silently
    # skipped cross-source dedup, the prefit prior and - once bodies existed -
    # the posting store. Routing through merge() is what keeps the two paths
    # from drifting into two different schemas.
    rows = rows_for(found, screened, known)
    by_portal = {}
    for row in rows:
        by_portal.setdefault(row.get("portal") or "linkedin-search", []).append(row)

    today = date.today().isoformat()
    added = gated = 0
    pending = []
    with jobs_md.board_lock():
        seen = {}
        if jobs_md.SEEN.exists():
            seen = json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})
        for portal, portal_rows in sorted(by_portal.items()):
            stats = ats_fetch.merge(seen, portal_rows, today, log, portal=portal,
                                    pending=pending)
            added += stats["added"]
            gated += stats["gated"]
        log("  %d added (%d auto-gated on German, %d awaiting fit assessment)"
            % (added, gated, added - gated))
        # Bodies before state: an orphan sidecar is inert, a saved
        # `posting_path` with no file behind it is a row that lies.
        written = postings.commit_all(pending)
        if written:
            log("  %d posting bodies stored" % written)
        jobs_md.save_seen(seen)
        log("done - %d jobs in the board" % len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
