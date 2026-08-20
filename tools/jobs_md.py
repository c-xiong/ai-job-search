#!/usr/bin/env python3
"""Board-state primitives plus explicit, read-only job exports.

`job_scraper/seen_jobs.json` is the sole state source. The local board is the
only editing interface for statuses and notes. Markdown and CSV files are
optional snapshots and are never read back into state.

    python3 tools/jobs_md.py export-md   # write job_scraper/jobs.md
    python3 tools/jobs_md.py export-csv  # write active/excluded CSV snapshots

Stdlib only, Python 3.9+.
"""

import csv
import json
import os
import re
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
SEEN = ROOT / "job_scraper" / "seen_jobs.json"
MD = ROOT / "job_scraper" / "jobs.md"
CSV_ACTIVE = ROOT / "job_scraper" / "jobs_active.csv"
CSV_EXCLUDED = ROOT / "job_scraper" / "jobs_excluded.csv"

# Status vocabulary. Order matters: it is the sort order inside a section.
STATUSES = ["star", "yes", "new", "maybe", "gate", "no", "applied", "expired"]
STATUS_HELP = [
    ("star", "top target - apply first"),
    ("yes", "worth applying to"),
    ("new", "not reviewed yet (set automatically for every newly scraped job)"),
    ("maybe", "lower priority - stays visible, sinks to the bottom of the list"),
    ("no", "excluded by me - moves to the `no` section and never resurfaces in /scrape"),
    ("gate", "excluded automatically (language gate, deadline passed, ...) - set by /scrape, still browsable"),
    ("applied", "applied - moves to the `applied` section"),
    ("expired", "posting is dead"),
]
FIT_ORDER = {"high": 0, "medium": 1, "low": 2, "": 3, None: 3}

# Display priority, 0-100, high first. Four sources, in order of authority:
# `/rank`'s LLM score when it has run, `tools/fit_score.py`'s deterministic
# score when it has not, the older `prefit_score` for rows that predate the
# scorer, and the coarse `fit` band for rows that predate all of it.
FIT_SCORE = {"high": 80, "medium": 55, "low": 30, "": 0, None: 0}

# What each displayed band allows a row's priority to reach.
#
# Without this the column contradicts the order it sits in: a title-only row
# scoring 82 is labelled `medium` because it has no posting text to justify
# High, and it would still sort above a fully evidenced 75 labelled `high`.
# Same for a gated row - German stated as a condition forces `low`, but the raw
# number is deliberately preserved, so only a ceiling keeps it out of the top.
# Lives here rather than in fit_score.py because sorting is this module's job
# and fit_score.py already imports it; the other direction would be a cycle.
BAND_CEILING = {"high": 100, "medium": 74, "low": 57}


def priority_score(entry):
    """0-100 display priority, capped by the band the row actually shows.

    rank_score > fit_priority_score > prefit_score > the fit band.
    """
    base = None
    for key in ("rank_score", "fit_priority_score", "prefit_score"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            base = float(value)
            break
    band = (entry.get("fit") or "").lower()
    if base is None:
        base = float(FIT_SCORE.get(band, 0))
    if entry.get("fit_source") in ("ranked", "deterministic"):
        base = min(base, float(BAND_CEILING.get(band, 100)))
    return base

# A section heading is built from the statuses it holds, not hand-written next to
# them: `## `gate` / `expired` - excluded automatically`. Naming a bucket something
# the Status column never says ("Excluded by me" over a column of `no`) reads like a
# second, private vocabulary, and a hand-written heading also drifts the moment a
# status moves between sections. The gloss is the human half; the tokens come from
# the list. Same rule as the board's filter chips in tools/jobs_board.py.
SECTIONS = [
    ("active", "open candidates", ["star", "yes", "new", "maybe"]),
    ("applied", "", ["applied"]),
    ("gate", "excluded automatically (language gate, deadline passed)", ["gate", "expired"]),
    ("no", "excluded by me", ["no"]),
]


def section_heading(gloss, statuses):
    tokens = " / ".join("`%s`" % s for s in statuses)
    return "%s - %s" % (tokens, gloss) if gloss else tokens

COLUMNS = ["Status", "Fit", "Role", "Company", "Location", "Posted", "Link", "Why", "My notes"]


# One posting, many URL spellings - per vendor. Each rule maps a match onto the
# single form the board keys on. `ats-search` composite ids are NOT used as keys:
# the key is the URL, so a posting first seen through LinkedIn and later through
# its own ATS board can be recognised as the same row (tools/ats_fetch.py).
_ATS_RULES = [
    # Greenhouse serves the same board on three hosts (US, EU, and the legacy
    # boards.greenhouse.io); all three fold onto one.
    (re.compile(r"(?:job-boards(?:\.eu)?|boards)\.greenhouse\.io/([^/?#]+)/jobs/(\d+)", re.I),
     "https://job-boards.greenhouse.io/%s/jobs/%s"),
    (re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{16,})", re.I),
     "https://jobs.ashbyhq.com/%s/%s"),
    (re.compile(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)/job/(\d+)", re.I),
     "https://%s.jobs.personio.de/job/%s"),
    (re.compile(r"jobs\.(?:eu\.)?lever\.co/([^/?#]+)/([0-9a-f-]{16,})", re.I),
     "https://jobs.lever.co/%s/%s"),
    (re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)", re.I),
     "https://jobs.smartrecruiters.com/%s/%s"),
]

_TRACKING_QUERY_KEYS = {"trk", "trackingid", "lipi", "refid"}


def canonical_url(url):
    """Collapse the many URL spellings of one posting onto a single key.

    linkedin-search returns country-subdomain, slugged URLs
    (`ch.linkedin.com/jobs/view/ai-engineer-at-foo-4451224579`), which differ per
    query and per locale for the same job. Keying state on the raw URL therefore
    re-adds jobs that are already known. The numeric job id is the stable part.

    The ATS boards have the same problem in a different shape: an EU Greenhouse
    tenant answers on `job-boards.eu.greenhouse.io` while the same posting is
    linked as `boards.greenhouse.io`, and every vendor appends tracking params
    (`?gh_src=`, `utm_*`). One rule per vendor, all keyed on the posting id.
    """
    if not url:
        return url
    m = re.search(r"linkedin\.com/jobs/view/(?:[^/?#]*?-)?(\d{6,})", url)
    if m:
        return "https://www.linkedin.com/jobs/view/%s" % m.group(1)
    m = re.search(r"freehire\.me/jobs/([^/?#]+)", url)
    if m:
        return "https://freehire.me/jobs/%s" % m.group(1)
    for pattern, template in _ATS_RULES:
        m = pattern.search(url)
        if m:
            return template % m.groups()
    parts = urlsplit(url)
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ], doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), query, ""))


# The vendor each URL rule belongs to, in the same order as _ATS_RULES, so a
# canonical ATS URL can be turned back into the `vendor:token:posting_id` the CLI
# uses as an id.
_ATS_VENDORS = ["greenhouse", "ashby", "personio", "lever", "smartrecruiters"]


def composite_id_for_url(url):
    """`vendor:token:posting_id` for an ATS posting URL, else None.

    Rows collected before composite ids existed carry only a URL. Deriving the id
    from it is what lets them count as "already known" - otherwise a board full of
    postings you have had for weeks looks entirely new, and eats the click's
    new-row budget before reaching a company that had something.
    """
    if not url:
        return None
    for vendor, (pattern, _template) in zip(_ATS_VENDORS, _ATS_RULES):
        m = pattern.search(url)
        if m:
            return "%s:%s:%s" % (vendor, m.group(1), m.group(2))
    return None


def write_json_atomic(path, data):
    """Write JSON through a temp file in the same directory, then rename.

    `seen_jobs.json` is the only copy of every status and note you have set. A
    process killed halfway through a plain write leaves a truncated file and
    loses all of it, so the write is never partial: either the old file or the
    new one, never half of either.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_seen(seen):
    """Persist the board state atomically. The one writer every tool goes through."""
    write_json_atomic(SEEN, {"seen": seen})


# Every read-modify-write of seen_jobs.json goes through board_lock(). Three
# processes touch it - the board's HTTP handler, tools/fetch_jobs.py, and
# tools/scrape_cron.py - and a fetch takes a minute or two while you keep
# pressing `s` and `n` in the table. Without a lock spanning the *read* as well
# as the write, whichever process saves last silently reverts the other's work.
#
# An in-process lock is not enough: `python3 tools/fetch_jobs.py` in a terminal
# and an open board are two processes. flock is the portable-enough answer
# (POSIX; on a platform without fcntl this degrades to the in-process lock and
# says so rather than pretending).
_BOARD_LOCK_LOCAL = threading.RLock()
_BOARD_LOCK_DEPTH = 0


def board_lock_path():
    return SEEN.parent / ".board.lock"


@contextmanager
def board_lock():
    """Hold the board-state lock for a read-modify-write.

    Re-entrant. The nesting matters: `flock` is per open file description, not
    per process, so a nested `open()` + `LOCK_EX` on the same file would block
    on a lock this very thread already holds. The depth counter takes the flock
    once, at the outermost entry.
    """
    global _BOARD_LOCK_DEPTH
    with _BOARD_LOCK_LOCAL:
        if fcntl is None or _BOARD_LOCK_DEPTH > 0:
            # Already inside the flock (or on a platform without one, where the
            # in-process lock is all there is - single-process safety only).
            _BOARD_LOCK_DEPTH += 1
            try:
                yield
            finally:
                _BOARD_LOCK_DEPTH -= 1
            return
        path = board_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _BOARD_LOCK_DEPTH += 1
            try:
                yield
            finally:
                _BOARD_LOCK_DEPTH -= 1
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def primary_url(entry):
    """The link to click: the first-party ATS posting when the row has one.

    A row first seen on LinkedIn and later found on the company's own board keeps
    its original key - nothing is ever re-keyed (§11.5) - so the preferred link
    lives in `sources`, not in `url`.
    """
    preferred = entry.get("primary_source")
    if preferred:
        for source in entry.get("sources") or []:
            if source.get("portal") == preferred and source.get("url"):
                return source["url"]
    return entry.get("url", "")


def _cell(text):
    """Make a value safe to put inside a markdown table cell."""
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def sort_key(entry):
    """Your status first, then display priority, then newest.

    Priority is `rank_score` once `/rank` has run, the collector's `prefit_score`
    before that, and the `fit` band for entries that predate both - so a batch
    that has only just been collected still reads top-down.
    """
    status = entry.get("user_status", "new")
    rank = STATUSES.index(status) if status in STATUSES else len(STATUSES)
    return (rank, -priority_score(entry),
            _neg_date(entry.get("posted") or entry.get("first_seen") or ""))


def _neg_date(d):
    """Sort dates newest-first inside a string sort."""
    return "".join(str(9 - int(c)) if c.isdigit() else c for c in d)


def render(seen):
    today = date.today().isoformat()
    counts = {}
    for e in seen.values():
        counts[e.get("user_status", "new")] = counts.get(e.get("user_status", "new"), 0) + 1
    summary = ", ".join("%s %s" % (counts[s], s) for s in STATUSES if s in counts)

    L = []
    L.append("# Job Shortlist Export")
    L.append("")
    L.append("Exported: **%s** - %d jobs (%s)" % (today, len(seen), summary or "none yet"))
    L.append("")
    L.append("## Read-only snapshot")
    L.append("")
    L.append("`job_scraper/seen_jobs.json` is the sole state source. Edit statuses and notes in the")
    L.append("local board. Changes made in this Markdown file are never imported back into state.")
    L.append("")
    L.append("Regenerate this snapshot explicitly with `python3 tools/jobs_md.py export-md`.")
    L.append("Generate separate spreadsheet snapshots with `python3 tools/jobs_md.py export-csv`.")
    L.append("")
    L.append("This export can become stale; the board always shows the current JSON state.")
    L.append("")
    L.append("| Status | Meaning |")
    L.append("|--------|---------|")
    for s, meaning in STATUS_HELP:
        L.append("| `%s` | %s |" % (s, meaning))
    L.append("")
    L.append("Within a section, rows sort by status first (`star` > `yes` > `new` > `maybe`), then by")
    L.append("fit, then newest posting first. Marking something `maybe` is what \"lower its priority\" means.")
    L.append("")

    for _key, gloss, statuses in SECTIONS:
        rows = [e for e in seen.values() if e.get("user_status", "new") in statuses]
        L.append("## %s (%d)" % (section_heading(gloss, statuses), len(rows)))
        L.append("")
        if not rows:
            L.append("*(none)*")
            L.append("")
            continue
        L.append("| " + " | ".join(COLUMNS) + " |")
        L.append("|" + "|".join(["--------"] * len(COLUMNS)) + "|")
        for e in sorted(rows, key=sort_key):
            fit = (e.get("fit") or "").capitalize()
            # Keep the stable row key visible and offer the preferred first-party
            # posting as a second link when one has been discovered.
            key_link = e.get("url", "")
            preferred = primary_url(e)
            link = "[open](%s)" % key_link if key_link else ""
            if preferred and preferred != key_link:
                link += " · [first-party](%s)" % preferred
            L.append("| `%s` | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                e.get("user_status", "new"), fit, _cell(e.get("title")), _cell(e.get("company")),
                _cell(e.get("location")), _cell(e.get("posted") or e.get("first_seen")),
                link, _cell(e.get("note")), _cell(e.get("user_note"))))
        L.append("")
    return "\n".join(L) + "\n"


CSV_COLUMNS = ["status", "fit", "role", "company", "location", "posted",
               "url", "why", "my_notes", "portal"]


def write_csv(seen):
    """Export two spreadsheet views. GENERATED - edits here are overwritten.

    Kept read-only on purpose: a spreadsheet app on a de-CH locale rewrites
    ISO dates on save, and Excel mangles UTF-8 notes without a BOM. The board
    is the only editable surface; these are optional snapshots for sorting and
    sharing.
    """
    active_st = {"star", "yes", "new", "maybe", "applied"}
    buckets = {CSV_ACTIVE: [], CSV_EXCLUDED: []}
    for e in sorted(seen.values(), key=sort_key):
        target = CSV_ACTIVE if e.get("user_status", "new") in active_st else CSV_EXCLUDED
        buckets[target].append([
            e.get("user_status", "new"), e.get("fit", ""), e.get("title", ""),
            e.get("company", ""), e.get("location", ""),
            e.get("posted") or e.get("first_seen", ""), primary_url(e),
            e.get("note", ""), e.get("user_note", ""), e.get("portal", ""),
        ])
    for path, rows in buckets.items():
        # utf-8-sig: the BOM is what stops Excel from turning CJK notes into mojibake.
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLUMNS)
            w.writerows(rows)
    return {p.name: len(r) for p, r in buckets.items()}


def main(argv):
    mode = argv[1] if len(argv) > 1 else None
    if mode not in ("export-md", "export-csv"):
        print(__doc__)
        return 2
    if not SEEN.exists():
        print("no %s yet - run /scrape first" % SEEN.relative_to(ROOT))
        return 1
    with board_lock():
        seen = json.loads(SEEN.read_text(encoding="utf-8")).get("seen", {})
        if mode == "export-md":
            MD.parent.mkdir(parents=True, exist_ok=True)
            MD.write_text(render(seen), encoding="utf-8")
            print("exported: %d jobs -> %s" % (len(seen), MD))
            return 0
        CSV_ACTIVE.parent.mkdir(parents=True, exist_ok=True)
        counts = write_csv(seen)
    print("exported: " + ", ".join("%s (%d rows)" % (n, c) for n, c in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
