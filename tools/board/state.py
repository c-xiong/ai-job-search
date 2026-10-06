"""Reading and writing the board's state, and shaping it for the page.

Lifted unchanged from `tools/jobs_board.py` apart from one addition: `save()`
now emits an activity event naming every file it rewrote. Setting a status
touches four files, and the strip is where that stops being invisible.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apply_email  # noqa: E402
import jobs_md  # noqa: E402
import postings  # noqa: E402

from . import activity  # noqa: E402

STATUSES = list(jobs_md.STATUSES)
LOCK_STATUSES = set(STATUSES)

# The link to click: the first-party ATS posting when the row has one. Shared
# with the optional export helpers so every view points at the same place.
primary_url = jobs_md.primary_url


def load():
    if not jobs_md.SEEN.exists():
        return {}
    return json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})


def save(seen, why=""):
    """Atomic: this file is the only copy of every status and note you have set.

    Callers hold `jobs_md.board_lock()` - see tests/test_board_lock_discipline.py.
    """
    with activity.Timer() as timer:
        jobs_md.save_seen(seen)
    activity.emit(
        "board",
        (why + " - " if why else "") + "wrote seen_jobs.json (%d entries)" % len(seen),
        ms=timer.ms)


def _score_source(entry):
    """Which number `jobs_md.priority_score` is actually reading for this row."""
    for key, name in (("rank_score", "rank"), ("fit_priority_score", "fit"),
                      ("prefit_score", "prefit")):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return name
    return "band"


def jobs_payload():
    rows = _sorted(_shape(load().items()))
    # The on-demand evaluation lives in its own sidecar (fit_eval.py); the list
    # only needs each row's verdict to mark it.
    from . import fit_eval
    verdicts = fit_eval.verdicts()
    for row in rows:
        row["evaluation"] = verdicts.get(row["url"]) or ""
    return {"jobs": rows, "statuses": STATUSES}


def _shape(items):
    """Board rows for (url, entry) pairs. No sorting, no sidecar reads."""
    rows = []
    for url, entry in items:
        # Why this row sits where it sits. The screen note and the score answer
        # different questions - "German is a job condition here" and "this
        # scored 70 because the title matched" - so they are joined rather than
        # ranked. Making one win meant a row with any note, including a bland
        # "not screened, budget spent", never showed why it was placed at all.
        # `prefit_reasons` is the older ATS-only prior and only appears for rows
        # collected before the scorer existed.
        parts = [entry.get("note", "")]
        if entry.get("fit_reasons"):
            parts.append("; ".join(entry["fit_reasons"]))
        elif entry.get("prefit_reasons"):
            parts.append("PREFIT: " + "; ".join(entry["prefit_reasons"]))
        why = " · ".join(part for part in parts if part)
        rows.append({
            "url": url,
            "open_url": primary_url(entry),
            "posting_url": entry.get("user_posting_url") or "",
            "default_open_url": primary_url(entry, use_override=False),
            "known_urls": jobs_md.known_urls(entry, url),
            "status": jobs_md.user_status(entry),
            "fit": (entry.get("fit") or "").lower(),
            "score": jobs_md.priority_score(entry),
            "ranked": isinstance(entry.get("rank_score"), (int, float)),
            "title": entry.get("title", ""),
            "company": entry.get("company", ""),
            "location": entry.get("location", ""),
            "posted": entry.get("posted") or entry.get("first_seen", ""),
            # When this row reached *you*, which is a different question from
            # when it was posted and the only one the "what did the last fetch
            # bring in" filter can be built on. `first_seen_at` is the run
            # stamp; rows collected before it existed fall back to their
            # first-seen date, which sorts correctly against a timestamp
            # because both are ISO-8601 and a date is a prefix of one.
            "first_seen": entry.get("first_seen", ""),
            "first_seen_at": entry.get("first_seen_at") or entry.get("first_seen", ""),
            "why": why,
            "note": entry.get("user_note", ""),
            "portal": entry.get("portal", ""),
            "primary_source": entry.get("primary_source") or entry.get("portal", ""),
            "sources": [source.get("portal", "") for source in entry.get("sources") or []
                        if isinstance(source, dict) and source.get("portal")],
            # The list payload carries the excerpt, never the body. Every row
            # goes to the browser on each reload, so a few kilobytes of posting
            # per row would be megabytes on the wire; `job_payload()` serves the
            # full text for the one row you actually opened.
            "description": entry.get("posting_excerpt", ""),
            "has_posting": bool(entry.get("posting_path")),
            "posting_chars": entry.get("posting_chars") or 0,
            # Which of the three scores the row is actually sorted on, so the
            # page can label a reason correctly instead of calling every number
            # a prefit.
            "score_source": _score_source(entry),
            "fit_evidence": entry.get("fit_evidence", ""),
            # The raw keyword score, kept beside an AI band (fit_eval.py) so the
            # page can show both numbers.
            "keyword_score": entry.get("fit_score"),
            "fit_parts": entry.get("fit_parts") or {},
            "fit_losses": entry.get("fit_losses") or [],
            "dupes": entry.get("possible_duplicate_of") or [],
        })
    return rows


def _sorted(rows):
    """Your status first, then display priority, then newest.

    The priority half is `jobs_md.priority_score`, which reads rank_score >
    fit_priority_score > prefit_score > the band, and caps the result at what
    the displayed band allows. Same order as `jobs_md.sort_key`, so the board
    and the markdown export can never disagree about what is at the top.
    """
    return sorted(rows, key=lambda r: (
        STATUSES.index(r["status"]) if r["status"] in LOCK_STATUSES else 99,
        -r["score"],
        jobs_md._neg_date(r["posted"])))


def job_payload(url):
    """One shaped board row, with its full posting body.

    A direct key lookup and exactly one sidecar read. The old shape rebuilt and
    re-sorted the entire table to throw all but one row away, which was merely
    wasteful when rows were small and would now mean touching every posting on
    disk to answer a question about one of them.
    """
    seen = load()
    entry = seen.get(url)
    if entry is None:
        return None
    row = next((r for r in _shape([(url, entry)]) if r["url"] == url), None)
    if row is None:
        return None
    try:
        body = postings.load(entry)
    except postings.UnsafePath:
        # A stored path that does not resolve inside the store is a bug or a
        # tampered state file, never something to serve. The row still renders;
        # only its body is withheld.
        body = ""
    row["description"] = body or row.get("description", "")
    # Read from the body on every open rather than stored: it is one regex pass
    # over one posting, and improving the extractor then fixes every old row.
    row["emails"] = apply_email.extract(row["description"])
    return row
