"""Reading and writing the board's state, and shaping it for the page.

Lifted unchanged from `tools/jobs_board.py` apart from one addition: `save()`
now emits an activity event naming every file it rewrote. Setting a status
touches four files, and the strip is where that stops being invisible.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

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


def jobs_payload():
    seen = load()
    rows = []
    for url, entry in seen.items():
        why = entry.get("note", "")
        # A collector cannot fill `fit`, so a freshly fetched row would otherwise
        # arrive with no visible reason for its position. The prefit reasons are
        # that reason, and they are shown as such - never as a fit assessment.
        if not why and entry.get("prefit_reasons"):
            why = "PREFIT: " + "; ".join(entry["prefit_reasons"])
        rows.append({
            "url": url,
            "open_url": primary_url(entry),
            "status": entry.get("user_status", "new"),
            "fit": (entry.get("fit") or "").lower(),
            "score": jobs_md.priority_score(entry),
            "ranked": isinstance(entry.get("rank_score"), (int, float)),
            "title": entry.get("title", ""),
            "company": entry.get("company", ""),
            "location": entry.get("location", ""),
            "posted": entry.get("posted") or entry.get("first_seen", ""),
            "why": why,
            "note": entry.get("user_note", ""),
            "portal": entry.get("portal", ""),
            "dupes": entry.get("possible_duplicate_of") or [],
        })
    # Your status first, then display priority (rank_score > prefit_score > fit),
    # then newest. Same order as jobs_md.sort_key, so the board and the markdown
    # can never disagree about what is at the top.
    rows.sort(key=lambda r: (STATUSES.index(r["status"]) if r["status"] in LOCK_STATUSES
                             else 99,
                             -r["score"],
                             jobs_md._neg_date(r["posted"])))
    return {"jobs": rows, "statuses": STATUSES}
