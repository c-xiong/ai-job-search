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
import re
import sys
from datetime import date
from pathlib import Path

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


def canonical_url(url):
    """Collapse the many URL spellings of one posting onto a single key.

    linkedin-search returns country-subdomain, slugged URLs
    (`ch.linkedin.com/jobs/view/ai-engineer-at-foo-4451224579`), which differ per
    query and per locale for the same job. Keying state on the raw URL therefore
    re-adds jobs that are already known. The numeric job id is the stable part.
    """
    if not url:
        return url
    m = re.search(r"linkedin\.com/jobs/view/(?:[^/?#]*?-)?(\d{6,})", url)
    if m:
        return "https://www.linkedin.com/jobs/view/%s" % m.group(1)
    m = re.search(r"freehire\.me/jobs/([^/?#]+)", url)
    if m:
        return "https://freehire.me/jobs/%s" % m.group(1)
    return url.split("?")[0].rstrip("/")


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
    """Fold hand edits into the scraper state. Returns (state, added_by_hand)."""
    added = 0
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
    status = entry.get("user_status", "new")
    rank = STATUSES.index(status) if status in STATUSES else len(STATUSES)
    return (rank, FIT_ORDER.get((entry.get("fit") or "").lower(), 3),
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
            link = "[open](%s)" % e.get("url", "") if e.get("url") else ""
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
            e.get("posted") or e.get("first_seen", ""), e.get("url", ""),
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
    seen = json.loads(SEEN.read_text(encoding="utf-8")).get("seen", {})
    edits = parse_md()
    seen, added = merge(seen, edits)
    if mode == "check":
        print("%d jobs in state, %d rows parsed from jobs.md, %d added by hand"
              % (len(seen), len(edits), added))
        return 0
    SEEN.write_text(json.dumps({"seen": seen}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    MD.write_text(render(seen), encoding="utf-8")
    counts = write_csv(seen)
    print("synced: %d jobs -> %s (%d hand-added)" % (len(seen), MD.relative_to(ROOT), added))
    print("exported: " + ", ".join("%s (%d rows)" % (n, c) for n, c in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
