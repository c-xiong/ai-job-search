#!/usr/bin/env python3
"""The ATS source module: run `ats-search` once, merge what it found, append-only.

    python3 tools/ats_fetch.py                 # fetch the due companies and merge
    python3 tools/ats_fetch.py --dry-run       # do everything except write

Normally you do not call this directly - `tools/fetch_jobs.py` is the orchestrator
the job board's Fetch button uses, and it calls this as one of its sources. This
file stays importable on its own so that adding or removing a source never means
rewriting the trigger.

Two guarantees this module exists to keep:

  * **Nothing already collected is ever overwritten.** A merge may insert a new
    row, append to a row's source history, and set `last_seen_at`. It may never
    touch a title, a company, a URL, your status, your note, a fit, or any
    `rank_*` field. There is a test for exactly that (plan §11.6).
  * **A degraded run still persists its successes.** `ats-search` exits 0 with
    `meta.degraded` when some companies failed and others did not; the companies
    that worked are merged, the ones that failed keep their old `last_success_at`
    so the next run retries them first.

Stdlib only, Python 3.9+.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collectors  # noqa: E402
import jobs_md  # noqa: E402
from company_lock import registry_lock  # noqa: E402

ROOT = jobs_md.ROOT
REGISTRY = ROOT / "job_scraper" / "companies.json"
PORTAL = "ats-search"

# Fields a merge is allowed to touch on an entry that already exists. Everything
# else on an existing row belongs to you or to /rank.
APPENDABLE = ("sources", "also_seen", "possible_duplicate_of", "last_seen_at")

# Cross-source collapse needs all four of these to agree (plan §11.3). Two
# signals - the old (company, title) key - would have deleted real jobs: one
# company genuinely posts the same title for different cities, teams and
# requisitions.
DATE_WINDOW_DAYS = 14
FINGERPRINT_CHARS = 1500
FINGERPRINT_THRESHOLD = 0.6


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _norm_tight(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _tokens(text):
    return {t for t in _norm(text).split() if len(t) > 2}


def _parse_date(value):
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _dates_close(a, b):
    """Within ±14 days. Two unknown dates are NOT evidence of sameness."""
    da, db = _parse_date(a), _parse_date(b)
    if da is None or db is None:
        return False
    return abs((da - db).days) <= DATE_WINDOW_DAYS


def _locations_overlap(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return bool(ta & tb)


def _fingerprint_agrees(a, b):
    """Jaccard over the first ~1500 characters. Unknown when either side is empty."""
    if not a or not b:
        return None
    ta, tb = _tokens(a[:FINGERPRINT_CHARS]), _tokens(b[:FINGERPRINT_CHARS])
    if not ta or not tb:
        return None
    return len(ta & tb) / float(len(ta | tb)) >= FINGERPRINT_THRESHOLD


def load_registry():
    if not REGISTRY.exists():
        return {"defaults": {}, "companies": []}
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except ValueError:
        return {"defaults": {}, "companies": []}


def alias_map(registry):
    """{normalized alias -> normalized canonical name}, so "Nuance" is Microsoft."""
    out = {}
    for company in registry.get("companies", []):
        canonical = _norm_tight(company.get("name", ""))
        if not canonical:
            continue
        out[canonical] = canonical
        for alias in company.get("aliases", []) or []:
            out[_norm_tight(alias)] = canonical
    return out


def same_company(a, b, aliases):
    na, nb = _norm_tight(a), _norm_tight(b)
    if not na or not nb:
        return False
    return aliases.get(na, na) == aliases.get(nb, nb)


def sources_of(entry):
    """Every portal this row has been seen through, including its original one."""
    portals = {entry.get("portal")} if entry.get("portal") else set()
    for source in entry.get("sources") or []:
        if source.get("portal"):
            portals.add(source["portal"])
    return portals


def find_duplicate(seen, row, aliases, portal):
    """Look for the same posting already in the board under a *different* source.

    Returns (key, signals, collapse). `collapse` is only true when every signal
    agrees - and, when both sides carry a description, when the fingerprints
    agree too. Anything less is recorded as a *possible* duplicate on both rows
    and nothing is merged: a flagged pair is a display concern, never a deletion.

    **Fuzzy matching never runs within one source** (§11.1). The composite id in
    the row's URL is the vendor's own requisition id and is authoritative; a
    company that posts "Software Engineer, Berlin" twice in one week has two
    requisitions, and collapsing them would delete a real job. So any row already
    seen through this portal is skipped entirely - identity inside a source comes
    from the URL, which the exact-key check above has already tried.
    """
    best = (None, 0, False)
    for key, entry in seen.items():
        if portal in sources_of(entry):
            continue
        signals = 0
        company_ok = same_company(entry.get("company"), row.get("company"), aliases)
        title_ok = _norm(entry.get("title")) == _norm(row.get("title"))
        if not (company_ok and title_ok):
            continue
        signals = 2
        if _locations_overlap(entry.get("location"), row.get("location")):
            signals += 1
        if _dates_close(entry.get("posted") or entry.get("first_seen"), row.get("posted")):
            signals += 1
        fingerprint = _fingerprint_agrees(entry.get("description"), row.get("description"))
        collapse = signals == 4 and fingerprint is not False
        if signals > best[1] or (collapse and not best[2]):
            best = (key, signals, collapse)
        if collapse:
            break
    return best


def source_record(row, today, portal=PORTAL):
    return {"portal": portal, "url": row["url"], "id": row.get("id", ""), "first_seen": today}


def seed_history(entry, key):
    """Give a pre-`sources` row its own history before anything is appended.

    Rows collected before source history existed have no `sources` at all.
    Appending to that produces a one-entry history naming only the new source -
    which reads as "this job came from the ATS board", losing the fact that
    LinkedIn found it first and when.
    """
    if entry.get("sources"):
        return
    entry["sources"] = [{
        "portal": entry.get("portal") or "unknown",
        "url": entry.get("url") or key,
        "id": "",
        "first_seen": entry.get("first_seen") or "",
    }]


def append_source(entry, record):
    """Append to the row's source history. Never replaces, never re-keys."""
    sources = entry.setdefault("sources", [])
    if not any(s.get("portal") == record["portal"] and s.get("url") == record["url"] for s in sources):
        sources.append(record)
        return True
    return False


def merge(seen, rows, today, log, portal=PORTAL):
    """Fold one source's results into the board. Append-only (§11.6).

    Portal-agnostic on purpose: LinkedIn and freehire rows go through the same
    conservative dedup as ATS ones, so a job that both an ATS board and LinkedIn
    return in the *same* click becomes one row rather than two.

    A row may arrive pre-screened (`status`/`note`), which is how the sources
    that need a network call to read a description keep that call outside the
    board's write lock.
    """
    registry = load_registry()
    aliases = alias_map(registry)
    stats = {"added": 0, "gated": 0, "already_known": 0, "collapsed": 0,
             "possible_duplicates": 0, "german_gated_by_company": {}}

    for row in rows:
        key = jobs_md.canonical_url(row["url"])
        row = dict(row, url=key)

        # The yield counter is per *run*, like jobs_seen and eligible_jobs on the
        # CLI side, so it counts every gated posting this run saw - not only the
        # ones that happened to be new. Attribution uses the registry name, never
        # the displayed employer: on a venture studio's board those differ by
        # design.
        if row.get("description") and collectors.german_hit(row["description"])[0] == "hard":
            owner = row.get("registry_company") or row.get("company") or ""
            stats["german_gated_by_company"][owner] = \
                stats["german_gated_by_company"].get(owner, 0) + 1

        existing = seen.get(key)
        if existing is not None:
            # Already in the board under this exact key: record that we saw it
            # again and move on. Nothing else about the row is ours to change.
            seed_history(existing, key)
            append_source(existing, source_record(row, today, portal))
            existing["last_seen_at"] = today
            stats["already_known"] += 1
            continue

        dup_key, signals, collapse = find_duplicate(seen, row, aliases, portal)
        if collapse:
            entry = seen[dup_key]
            seed_history(entry, dup_key)
            append_source(entry, source_record(row, today, portal))
            entry["last_seen_at"] = today
            # The key never moves (§11.5) - migrating it would drag user_status,
            # user_note, rank_* and the rest behind it. What changes is which
            # source display and /apply prefer, so the first-party link is the
            # one you click.
            # The first-party link is the one worth clicking, so an ATS sighting
            # takes over `primary_source`; another aggregator finding the same row
            # does not demote it.
            if portal == PORTAL or not entry.get("primary_source"):
                entry["primary_source"] = portal
            stats["collapsed"] += 1
            log("  = %s - %s (already known via %s; %s link recorded)"
                % (row["company"], row["title"][:48], entry.get("portal", "?"), portal))
            continue

        status = row.get("status")
        note = row.get("note")
        if status is None:
            status, note = collectors.screen_text(row.get("description") or "")
        stats["gated"] += status == "gate"

        entry = {
            "title": row.get("title", ""),
            "company": row.get("company", ""),
            "location": row.get("location") or "",
            "url": key,
            "first_seen": today,
            "posted": row.get("posted") or "",
            "deadline": row.get("deadline"),
            "fit": "",
            "status": "new",
            "portal": portal,
            "user_status": status,
            "user_note": "",
            "note": note,
            # The deterministic display prior, with the reasons that produced it.
            # /rank overwrites it as the authority; it never gates anything.
            "prefit_score": row.get("prefit_score"),
            "prefit_reasons": row.get("prefit_reasons") or [],
            "sources": [source_record(row, today, portal)],
            "primary_source": portal,
            "last_seen_at": today,
        }
        if dup_key and signals >= 2:
            # Not enough evidence to collapse: both rows stay visible, and both
            # say so. A flagged pair is something you look at, not something the
            # merge decides for you.
            entry["possible_duplicate_of"] = [dup_key]
            other = seen[dup_key].setdefault("possible_duplicate_of", [])
            if key not in other:
                other.append(key)
            stats["possible_duplicates"] += 1
        seen[key] = entry
        stats["added"] += 1

    return stats


def bump_german_gated(counts, log):
    """Record the German-gate yield per company, for the §18 revisit triggers."""
    if not counts or not REGISTRY.exists():
        return
    with registry_lock(REGISTRY):
        try:
            registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        except ValueError:
            log("  ! companies.json is unreadable - german_gated counters not updated")
            return
        by_name = {c.get("name"): c for c in registry.get("companies", [])}
        touched = 0
        for name, count in counts.items():
            company = by_name.get(name)
            if not company:
                continue
            stats = company.setdefault(
                "stats", {"jobs_seen": 0, "german_gated": 0, "eligible_jobs": 0, "last_eligible_at": None})
            stats["german_gated"] = stats.get("german_gated", 0) + count
            touched += 1
        if touched:
            jobs_md.write_json_atomic(REGISTRY, registry)


def to_rows(payload):
    """Flatten the CLI payload into merge-ready records."""
    rows = []
    for r in collectors.results_of(payload):
        if not r.get("url") or not r.get("id"):
            continue
        rows.append({
            "id": r["id"],
            "title": r.get("title") or "",
            "company": r.get("company") or "",
            # The registry owner, which on a venture studio's board is NOT the
            # displayed employer. Everything that attributes a row back to a
            # registry entry - the German-gate counters especially - uses this.
            "registry_company": r.get("registry_company") or r.get("company") or "",
            "location": r.get("location") or "",
            "posted": (r.get("date") or "")[:10],
            "deadline": r.get("deadline"),
            "url": r["url"],
            "description": r.get("description") or "",
            "prefit_score": r.get("prefit_score"),
            "prefit_reasons": r.get("prefit_reasons") or [],
        })
    return rows


def known_ids_from(seen):
    """Every ats-search composite id already in the board.

    This is what lets `--max-new-jobs` mean *new*: without it the CLI counts
    eligible rows, and a first company whose whole board you have already seen
    would spend the click's budget having added nothing.

    Recorded ids are used where they exist, and derived from the URL where they
    do not - rows collected before composite ids existed, and rows whose source
    entry carries only a bare posting id, are exactly the ones most likely to be
    "already known".
    """
    ids = set()
    for key, entry in seen.items():
        candidates = [key, entry.get("url")]
        for source in entry.get("sources") or []:
            if source.get("portal") != PORTAL:
                continue
            recorded = source.get("id") or ""
            if recorded.count(":") >= 2:
                ids.add(recorded)
            candidates.append(source.get("url"))
        if entry.get("portal") == PORTAL or any(
            (s.get("portal") == PORTAL) for s in entry.get("sources") or []
        ):
            for url in candidates:
                derived = jobs_md.composite_id_for_url(url)
                if derived:
                    ids.add(derived)
    return ids


def collect(log, max_companies=8, max_new_jobs=40, runner=None, dry_run=False, known_ids=None):
    """Run the ATS CLI exactly once. Returns (rows, meta)."""
    args = [collectors.ATS, "search",
            "--max-companies", str(max_companies),
            "--max-new-jobs", str(max_new_jobs),
            "--format", "json"]
    if dry_run:
        # "Do everything except write" has to include the registry: without this a
        # dry run marks the companies it touched as fetched, and the real run five
        # minutes later skips every one of them as not yet due.
        args.append("--no-write")
    tmp = None
    if known_ids:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(sorted(known_ids), tmp)
        tmp.close()
        args += ["--known-ids", tmp.name]
    run = runner or (lambda a: collectors.bun(a, log, timeout=300))
    try:
        payload = run(args)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    if not isinstance(payload, dict):
        log("  ! ats-search produced nothing usable")
        return [], {}
    meta = payload.get("meta") or {}
    for company in meta.get("companies", []):
        if company.get("status") not in ("ok", "empty"):
            log("  ! %s: %s%s" % (company.get("name"), company.get("status"),
                                  (" - " + company["message"]) if company.get("message") else ""))
    if meta.get("note"):
        log("  . %s" % meta["note"])
    log("  ats       %d companies, %d requests, %d rows%s"
        % (len(meta.get("companies", [])), meta.get("requests", 0),
           len(collectors.results_of(payload)), " (DEGRADED)" if meta.get("degraded") else ""))
    return to_rows(payload), meta


def summarize(stats, meta, found, log, dry_run=False):
    """Fold merge stats and CLI metadata into one per-source summary line.

    Also the place the German-gate counters land on the registry: the screen runs
    in the merge, because the description the CLI hands over is what it reads.
    A dry run reports them and writes nothing - "everything except write" has to
    include the registry, or the next real run starts from a moved goalpost.
    """
    stats = dict(stats)
    gated_by_company = stats.pop("german_gated_by_company", {})
    if dry_run:
        if gated_by_company:
            log("  . dry run - german_gated counters not written (%s)"
                % ", ".join("%s +%d" % kv for kv in sorted(gated_by_company.items())))
    else:
        bump_german_gated(gated_by_company, log)
    summary = {
        "source": PORTAL,
        "companies": len(meta.get("companies", [])),
        "requests": meta.get("requests", 0),
        "degraded": bool(meta.get("degraded")),
        "failed": [c["name"] for c in meta.get("companies", []) if c.get("status") not in ("ok", "empty")],
        "found": found,
    }
    summary.update(stats)
    return summary


def run(log, seen, max_companies=8, max_new_jobs=40, runner=None, dry_run=False):
    """Collect and merge in one call. Convenience for the standalone command.

    `tools/fetch_jobs.py` deliberately does NOT use this: it calls `collect` and
    `merge` separately so the slow network half happens outside the board's write
    lock, and the board state is read at merge time rather than an hour earlier.
    """
    rows, meta = collect(log, max_companies, max_new_jobs, runner, dry_run,
                         known_ids=known_ids_from(seen))
    stats = merge(seen, rows, date.today().isoformat(), log)
    return summarize(stats, meta, len(rows), log, dry_run)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-companies", type=int, default=8)
    parser.add_argument("--max-new-jobs", type=int, default=40)
    parser.add_argument("--dry-run", action="store_true", help="collect and merge in memory, write nothing")
    args = parser.parse_args()

    def log(message):
        print(message, flush=True)

    # Read, merge and write inside the board lock: an open job board is another
    # writer of the same file.
    with jobs_md.board_lock():
        seen = {}
        if jobs_md.SEEN.exists():
            seen = json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})
        summary = run(log, seen, args.max_companies, args.max_new_jobs, dry_run=args.dry_run)
        log("  %d added, %d gated, %d already known, %d collapsed, %d flagged as possible duplicates"
            % (summary["added"], summary["gated"], summary["already_known"],
               summary["collapsed"], summary["possible_duplicates"]))
        if args.dry_run:
            log("dry run - nothing written")
            return 0
        jobs_md.save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
