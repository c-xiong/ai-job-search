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
    row, append to a row's source history, set `last_seen_at`, fill in a posting
    body the row did not have, and refresh the *computed* fit fields. It may
    never touch a title, a company, a URL, your status, your note, or any
    `rank_*` field - the full list is `PROTECTED`, and `enrich_existing()` now
    proves it on every call rather than leaving it to a test watching from
    outside (plan §11.6).

    `fit` used to be on that untouchable list. It no longer is, and the change
    is deliberate: `fit` is now a column *derived* from the row by
    `tools/fit_score.py`, not a judgement typed into it, so recomputing it is
    no more an overwrite than re-deriving a sort order. The judgement case is
    carved out explicitly instead - once `/rank` has spoken it sets
    `fit_source: "ranked"`, and from then on the band it chose is preserved
    through every collect and every recompute.
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
import fit_score  # noqa: E402
import jobs_md  # noqa: E402
import postings  # noqa: E402
from company_lock import registry_lock  # noqa: E402

ROOT = jobs_md.ROOT
REGISTRY = ROOT / "job_scraper" / "companies.json"
PORTAL = "ats-search"

# Fields a merge is allowed to touch on an entry that already exists. Everything
# else on an existing row belongs to you or to /rank.
APPENDABLE = ("sources", "also_seen", "possible_duplicate_of", "last_seen_at",
              "primary_source") + postings.ENTRY_FIELDS

# The other half of the same rule, and the half worth enforcing: what a merge
# must never touch on a row that already exists. `user_*` is your decision,
# `rank_*` and its companions are /rank's, and `url`/`first_seen`/`title` are
# the row's identity - the key never moves (§11.5).
#
# This was a promise in a docstring with one test watching from outside.
# enrich_existing() now proves it on every call: it snapshots these before it
# starts and refuses to return if any of them moved. A dozen dict lookups per
# row is a cheap price for the only copy of every status and note you have set.
PROTECTED = ("user_status", "user_note", "note", "first_seen", "first_seen_at",
             "url", "title", "company", "posted", "rank_score", "rank_verdict",
             "rank_date", "strengths", "gaps", "language_gate", "language_note")


class ProtectedFieldWritten(RuntimeError):
    """A merge changed a field that belongs to you or to /rank."""


# How much a body's provenance is worth when a row is seen again carrying one.
# A vendor's own API field beats the publisher's JSON-LD, which beats our guess
# at which container held the description, which beats whole-page text.
_EXTRACTOR_RANK = {"inline": 4, "json-ld": 3, "container": 2, "fallback": 1, "": 0}

# Cross-source collapse needs all four of these to agree (plan §11.3). Two
# signals - the old (company, title) key - would have deleted real jobs: one
# company genuinely posts the same title for different cities, teams and
# requisitions.
DATE_WINDOW_DAYS = 14
# Owned by tools/postings.py, which builds the signatures. Aliased rather than
# restated so the window a fingerprint covers and the window it is compared
# over can never drift apart.
FINGERPRINT_CHARS = postings.FINGERPRINT_CHARS
FINGERPRINT_THRESHOLD = postings.FINGERPRINT_THRESHOLD


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


def _fingerprint_agrees(stored, incoming):
    """Do two posting bodies look like the same posting? None when unknown.

    Both sides are MinHash signatures, not text. Before bodies were stored this
    read `entry["description"]`, which no entry has ever had - so the answer was
    always None and the check never fired. Now the stored side comes off the
    row (`posting_fingerprint`) and the incoming side is computed once per row
    by the caller, which keeps this out of find_duplicate's inner loop.

    None means "no opinion" and must never be read as disagreement: collapse
    requires `fingerprint is not False`, so an unknown never blocks one.
    """
    return postings.fingerprints_agree(stored, incoming, FINGERPRINT_THRESHOLD)


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


def find_duplicate(seen, row, aliases, portal, row_fingerprint=None):
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
        fingerprint = _fingerprint_agrees(entry.get("posting_fingerprint"), row_fingerprint)
        collapse = signals == 4 and fingerprint is not False
        if signals > best[1] or (collapse and not best[2]):
            best = (key, signals, collapse)
        if collapse:
            break
    return best


def run_stamp():
    """One fetch run's identity, to the second.

    Deliberately not a counter: a number would need a home of its own in a file
    whose writer only persists `{"seen": ...}`, and would have to be allocated
    under the board lock to stay unique. A timestamp is already unique per run -
    `RunLock` makes two fetches in the same second impossible - and it sorts,
    groups and prints without a lookup table behind it.
    """
    return datetime.now().isoformat(timespec="seconds")


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


def _body_is_better(entry, fields):
    """Should a newly seen body replace the one this row already has?"""
    if not entry.get("posting_path"):
        return True
    new_rank = _EXTRACTOR_RANK.get(fields.get("posting_extractor") or "", 0)
    old_rank = _EXTRACTOR_RANK.get(entry.get("posting_extractor") or "", 0)
    if new_rank != old_rank:
        return new_rank > old_rank
    # Same provenance, so length is the only signal left: a LinkedIn detail
    # page over a truncated search snippet. The margin stops a one-word
    # difference from rewriting the sidecar on every run.
    return fields.get("posting_chars", 0) > (entry.get("posting_chars") or 0) * 1.1


def _snapshot(entry):
    return {field: json.dumps(entry.get(field), sort_keys=True)
            for field in PROTECTED if field in entry}


def _rescore(entry, ctx, body=None):
    """Refresh the deterministic fit fields, if a scoring context is available.

    Optional by design: `merge()` is importable and testable without a scoring
    profile on disk, and a malformed profile must degrade to "no band" rather
    than taking a collect run down with it. A row with no band still sorts, it
    just sorts on whatever it had before.
    """
    if ctx is None:
        return
    try:
        fit_score.apply_to(entry, ctx, body=body)
    except Exception:  # pragma: no cover - scoring must never break collection
        pass


def scoring_context(log=None):
    """Load the fit profile once per run, or None if it cannot be read."""
    try:
        return fit_score.Context()
    except (fit_score.ConfigError, OSError) as exc:
        if log:
            log("  ! fit scoring disabled: %s" % exc)
        return None


def enrich_existing(entry, row, key, today, portal, pending=None, ctx=None):
    """Fold a fresh sighting into a row that is already on the board.

    merge() reaches an existing row two ways - under its own key, and as a
    collapsed cross-source duplicate - and both paths used to record the
    sighting and drop everything else it carried. That is where posting bodies
    were being lost, and it lost them in exactly the case worth having: an ATS
    sweep finding, *with* the full description, a row LinkedIn first saw
    without one.

    Adds only what the row is missing. Everything in PROTECTED is yours or
    /rank's, and this checks rather than promises: the snapshot around the body
    is the enforcement.
    """
    before = _snapshot(entry)

    seed_history(entry, key)
    append_source(entry, source_record(row, today, portal))
    entry["last_seen_at"] = today

    body = row.get("description") or ""
    if body:
        # Keyed by the row's own URL, never the sighting's. A collapsed
        # duplicate arrives under a different URL, and the key never moves, so
        # neither may the sidecar its body lives in.
        fields = postings.describe(
            key, body,
            source=row.get("posting_source") or "collector",
            extractor=row.get("posting_extractor") or "inline")
        if fields and _body_is_better(entry, fields):
            entry.update(fields)
            if pending is not None:
                pending.append((key, body))

    # After the body, never before it: the text this sighting brought is
    # precisely what turns a title-only guess into an evidenced one.
    _rescore(entry, ctx, body=body or None)

    after = _snapshot(entry)
    if after != before:
        moved = sorted(f for f in before if before[f] != after.get(f))
        raise ProtectedFieldWritten(
            "merge changed %s on %s, which belongs to you or to /rank"
            % (", ".join(moved), key))
    return entry


def merge(seen, rows, today, log, portal=PORTAL, pending=None, stamp=None):
    """Fold one source's results into the board. Append-only (§11.6).

    Portal-agnostic on purpose: LinkedIn and freehire rows go through the same
    conservative dedup as ATS ones, so a job that both an ATS board and LinkedIn
    return in the *same* click becomes one row rather than two.

    A row may arrive pre-screened (`status`/`note`), which is how the sources
    that need a network call to read a description keep that call outside the
    board's write lock.

    `pending` is an out-parameter: pass a list and it collects the (url, body)
    pairs whose sidecars still need writing. Nothing here touches the
    filesystem, because a `--dry-run` runs this function in full and only skips
    `save_seen()` - so the caller commits the bodies in the same branch that
    saves the state, or not at all.

    `stamp` is the *run's* timestamp, written onto every row this merge inserts
    as `first_seen_at`. It is what lets the board answer "which of these did the
    last fetch bring in": `first_seen` is a date, and a day with two fetches in
    it collapses into one indistinguishable block. One run means one stamp, so a
    caller that merges several sources passes the same value to each call;
    omitting it stamps this call's own clock, which is right for a lone merge
    and wrong for a fan-out, hence the parameter.
    """
    stamp = stamp or run_stamp()
    registry = load_registry()
    aliases = alias_map(registry)
    ctx = scoring_context(log)
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

        row_fingerprint = postings.fingerprint(row.get("description") or "")

        existing = seen.get(key)
        if existing is not None:
            # Already in the board under this exact key: record that we saw it
            # again, take any posting body it is still missing, and move on.
            # Nothing else about the row is ours to change.
            enrich_existing(existing, row, key, today, portal, pending, ctx)
            stats["already_known"] += 1
            continue

        dup_key, signals, collapse = find_duplicate(seen, row, aliases, portal, row_fingerprint)
        if collapse:
            entry = seen[dup_key]
            enrich_existing(entry, row, dup_key, today, portal, pending, ctx)
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
            "first_seen_at": stamp,
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
        # The body the collector already had in hand. describe() is pure - the
        # sidecar is written by the caller, and only on a run that is allowed
        # to write at all, so a --dry-run leaves nothing behind.
        body = row.get("description") or ""
        if body:
            entry.update(postings.describe(
                key, body,
                source=row.get("posting_source") or "collector",
                extractor=row.get("posting_extractor") or "inline"))
            if pending is not None:
                pending.append((key, body))
        _rescore(entry, ctx, body=body or None)
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
                "stats", {"jobs_seen": 0, "german_gated": 0, "eligible_jobs": 0,
                          "last_eligible_at": None, "last_jobs_seen": None,
                          "last_eligible_jobs": None})
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


def detect(log, per_fetch=5, runner=None):
    """Retry ATS detection for the companies whose turn it is (COMPANIES_PLAN §3.1).

    Runs before `collect`, so a company verified here - never fetched, hence the
    stalest - is searched in the same fetch. The CLI owns the queue and the
    backoff (`resolve --due`); this only bounds it and reports what changed.
    Returns {"checked": n, "found": [names], "ask": [names]}.
    """
    if per_fetch <= 0:
        return {"checked": 0, "found": [], "ask": []}
    args = [collectors.ATS, "resolve", "--due", "--max-companies", str(per_fetch),
            "--max-probes", "6", "--format", "json"]
    run = runner or (lambda a: collectors.bun(a, log, timeout=max(120, per_fetch * 30)))
    payload = run(args)
    results = (payload or {}).get("results") or [] if isinstance(payload, dict) else []
    found = [r.get("name") for r in results if r.get("status") == "verified"]
    ask = [r.get("name") for r in results if r.get("status") == "ambiguous"]
    if results:
        log("  detect    %d companies checked: %d boards found%s"
            % (len(results), len(found), ", %d need you" % len(ask) if ask else ""))
    return {"checked": len(results), "found": found, "ask": ask}


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
        # Boards the detection step found this run (COMPANIES_PLAN §3.1).
        "boards_found": list((meta.get("detected") or {}).get("found") or []),
    }
    summary.update(stats)
    return summary


def run(log, seen, max_companies=8, max_new_jobs=40, runner=None, dry_run=False,
        pending=None):
    """Collect and merge in one call. Convenience for the standalone command.

    `tools/fetch_jobs.py` deliberately does NOT use this: it calls `collect` and
    `merge` separately so the slow network half happens outside the board's write
    lock, and the board state is read at merge time rather than an hour earlier.
    """
    rows, meta = collect(log, max_companies, max_new_jobs, runner, dry_run,
                         known_ids=known_ids_from(seen))
    stats = merge(seen, rows, date.today().isoformat(), log, pending=pending)
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
        pending = []
        summary = run(log, seen, args.max_companies, args.max_new_jobs,
                      dry_run=args.dry_run, pending=pending)
        log("  %d added, %d gated, %d already known, %d collapsed, %d flagged as possible duplicates"
            % (summary["added"], summary["gated"], summary["already_known"],
               summary["collapsed"], summary["possible_duplicates"]))
        if args.dry_run:
            log("dry run - nothing written")
            return 0
        # Bodies before state: an orphan sidecar is inert, while a saved
        # `posting_path` with no file behind it is a row that claims a posting
        # it cannot show.
        written = postings.commit_all(pending)
        if written:
            log("  %d posting bodies stored" % written)
        jobs_md.save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
