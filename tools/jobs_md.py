#!/usr/bin/env python3
"""Two-way sync between the scraper's machine state and a hand-editable job list.

`job_scraper/seen_jobs.json` is what /scrape writes and reads for deduplication.
`job_scraper/jobs.md` is what *you* read and edit: one row per job, grouped into
sections, with a Status column you own.

    python3 tools/jobs_md.py sync     # merge both directions, rewrite jobs.md
    python3 tools/jobs_md.py check    # parse only, report what would change

Ownership is split so neither side clobbers the other:

  * The scraper owns   - title, company, location, posted/first-seen dates, url,
                         fit, portal, deadline and the auto "Why" note.
  * You own            - the Status cell and the "My notes" cell. Sync reads them
                         out of jobs.md, stores them back into seen_jobs.json as
                         `user_status` / `user_note`, and never overwrites them.

A row whose URL is not in seen_jobs.json is treated as one you added by hand: it
is kept and written into seen_jobs.json with portal "manual", so /scrape stops
re-suggesting it. Delete a row to make it reappear on the next scrape.

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

# Display priority, 0-100, high first. Three sources, in order of authority:
# `/rank`'s LLM score when it has run, the collector's deterministic `prefit_score`
# when it has not, and the coarse `fit` band for entries that predate both.
# Without this a freshly collected batch sinks to the bottom of the board - a
# collector cannot fill `fit`, and an empty fit is the lowest bucket.
FIT_SCORE = {"high": 80, "medium": 55, "low": 30, "": 0, None: 0}


def priority_score(entry):
    """0-100 display priority. rank_score > prefit_score > the fit band."""
    for key in ("rank_score", "prefit_score"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return float(FIT_SCORE.get((entry.get("fit") or "").lower(), 0))

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
    return url.split("?")[0].rstrip("/")


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


def key_for_url(seen, url):
    """Map any URL a row is known by back onto that row's key.

    A row's key is what jobs.md's first link carries, so this is a safety net for
    the other spellings a row is known by (its `sources`). It **fails closed**:
    when two rows claim the same URL there is no honest way to choose, and
    guessing applies one job's status to another while the intended edit
    disappears. Ambiguity returns None, and the caller reports it.
    """
    if url in seen:
        return url
    canonical = canonical_url(url)
    if canonical in seen:
        return canonical
    matches = set()
    for key, entry in seen.items():
        if entry.get("url") in (url, canonical):
            matches.add(key)
            continue
        for source in entry.get("sources") or []:
            if source.get("url") in (url, canonical):
                matches.add(key)
                break
    if len(matches) == 1:
        return matches.pop()
    return None  # nothing matched, or several did


def _cell(text):
    """Make a value safe to put inside a markdown table cell."""
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def parse_md(path=MD):
    """Return {url: {"status": str, "user_note": str}} from a hand-edited jobs.md."""
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| ---"):
            continue
        # Split on unescaped pipes only. render() writes a literal "|" inside a
        # cell as "\|", so a naive split shifts every later column - which would
        # read some other cell as the Status and silently rewrite the job's state.
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if len(cells) < len(COLUMNS):
            continue
        if cells[0].strip("`").lower() == "status":  # header row
            continue
        m = re.search(r"\((https?://[^)\s]+)\)", cells[6])
        if not m:
            continue
        status = cells[0].strip("` ").lower()
        out[m.group(1)] = {
            "status": status if status in STATUSES else "new",
            "user_note": cells[8],
        }
    return out


def merge(seen, edits):
    """Fold hand edits into the scraper state. Returns (state, added_by_hand).

    An edit whose URL matches several rows is left out entirely and reported by
    the caller: applying it to whichever row happened to come first would move a
    status onto the wrong job, silently.
    """
    added = 0
    resolved, ambiguous = {}, []
    for url, edit in edits.items():
        key = key_for_url(seen, url)
        if key is None and any(
            url in {e.get("url")} | {s.get("url") for s in (e.get("sources") or [])}
            for e in seen.values()
        ):
            ambiguous.append(url)
            continue
        resolved[key or url] = edit
    merge.ambiguous = ambiguous
    edits = resolved
    for url, entry in seen.items():
        edit = edits.get(url)
        if edit:
            entry["user_status"] = edit["status"]
            entry["user_note"] = edit["user_note"]
        else:
            entry.setdefault("user_status", "gate" if entry.get("status") == "skipped" else "new")
            entry.setdefault("user_note", "")
    for url, edit in edits.items():
        if url in seen:
            continue
        seen[url] = {
            "title": "(added by hand)", "company": "", "location": "", "url": url,
            "first_seen": date.today().isoformat(), "deadline": None, "fit": "",
            "status": "new", "portal": "manual",
            "user_status": edit["status"], "user_note": edit["user_note"],
        }
        added += 1
    return seen, added


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
    L.append("# Job Shortlist")
    L.append("")
    L.append("Last synced: **%s** - %d jobs (%s)" % (today, len(seen), summary or "none yet"))
    L.append("")
    L.append("## How to use this file")
    L.append("")
    L.append("Edit the **Status** cell and the **My notes** cell. Save. That is the whole workflow -")
    L.append("everything else is regenerated. Your edits survive: `/scrape` runs")
    L.append("`python3 tools/jobs_md.py sync` afterwards, which reads your statuses back, adds newly")
    L.append("found jobs as `new`, re-sorts, and moves rows into the right section.")
    L.append("")
    L.append("Two CSV exports sit next to this file - `jobs_active.csv` and `jobs_excluded.csv` - for")
    L.append("sorting and filtering in a spreadsheet. They are **generated**: edit them and the next sync")
    L.append("overwrites your changes. Edit here, or in the board (`python3 tools/jobs_board.py`).")
    L.append("")
    L.append("A job you mark `no` never comes back in a future scrape. A job you delete outright *will*")
    L.append("come back next time it is found - deleting is how you say \"show me this again\".")
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
            # The FIRST link in this cell is the row's key, always: parse_md
            # reads it back to decide which row an edit belongs to, and a cell
            # whose first URL is not the key turns an edit into a duplicate row -
            # or, when two rows prefer the same URL, into an edit applied to the
            # wrong job. The first-party posting is the second link, one click
            # away, which is what `primary_source` is for.
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
    and jobs.md are the editable surfaces; these are for sorting and sharing.
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
    mode = argv[1] if len(argv) > 1 else "sync"
    if mode not in ("sync", "check"):
        print(__doc__)
        return 2
    if not SEEN.exists():
        print("no %s yet - run /scrape first" % SEEN.relative_to(ROOT))
        return 1
    if mode == "check":
        seen = json.loads(SEEN.read_text(encoding="utf-8")).get("seen", {})
        edits = parse_md()
        seen, added = merge(seen, edits)
        print("%d jobs in state, %d rows parsed from jobs.md, %d added by hand"
              % (len(seen), len(edits), added))
        return 0
    # A sync is a read-modify-write like any other, and the board or a fetch may
    # be writing the same file right now.
    with board_lock():
        seen = json.loads(SEEN.read_text(encoding="utf-8")).get("seen", {})
        edits = parse_md()
        seen, added = merge(seen, edits)
        for url in getattr(merge, "ambiguous", []):
            print("! skipped an edit: %s matches more than one row, so there is no "
                  "safe way to tell which job you meant. Edit it in the board instead." % url)
        save_seen(seen)
        MD.write_text(render(seen), encoding="utf-8")
        counts = write_csv(seen)
    print("synced: %d jobs -> %s (%d hand-added)" % (len(seen), MD.relative_to(ROOT), added))
    print("exported: " + ", ".join("%s (%d rows)" % (n, c) for n, c in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
