#!/usr/bin/env python3
"""Shared collection primitives for every job source.

There is one collection path, not two. `tools/scrape_cron.py` (the manual
command) and `tools/fetch_jobs.py` (the on-demand orchestrator behind the job
board's Fetch button) both import from here, so a fix to the German screen or to
the CLI runner lands in both at once.

Nothing in this module writes anything. It runs portal CLIs, screens text, and
returns records; deciding what to keep and persisting it is the caller's job.

Stdlib only, Python 3.9+. Requires `bun` on PATH for the portal CLIs.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobs_md  # noqa: E402

ROOT = jobs_md.ROOT

# Set by the board for the duration of one collection run; None everywhere else.
#
# It is a module hook rather than a parameter threaded through `collect_linkedin`,
# `collect_freehire` and `ats_fetch.collect` because those three signatures are
# also the terminal path (`tools/scrape_cron.py`), and the point of this hook is
# that the terminal path does not change at all. One fetch runs at a time -
# `fetch_jobs.RunLock` guarantees it - so a single hook is unambiguous.
#
# What it buys: a *successful* run used to leave no trace of what it executed.
# `bun()` returned parsed JSON and nothing else, and the exit code was logged
# only on failure, so "which command produced these 48 rows, and how long did it
# take" was unanswerable. Now every call emits start and finish.
EMITTER = None

LINKEDIN = ".agents/skills/linkedin-search/cli/src/cli.ts"
FREEHIRE = ".agents/skills/freehire-search/cli/src/cli.ts"
ATS = ".agents/skills/ats-search/cli/src/cli.ts"

# German stated as a job condition. Conservative on purpose: these are phrasings
# that appear in a requirements list, not any mention of the word "German".
GERMAN_PATTERNS = [
    r"verhandlungssicher\w*\s+(?:in\s+)?Deutsch",
    r"(?:flie(?:ss|ß)end\w*|sehr\s+gute?|gute?)\s+Deutsch\w*",
    r"Deutsch\w*\s+auf\s+\w+[- ]?Niveau",
    r"Deutschkenntnisse",
    r"Deutsch\s*(?:-|\s)?(?:und|&|/)\s*Englisch\w*\s*(?:kenntnisse|in Wort)",
    r"(?:fluent|native|business[- ]level|professional|excellent|strong|good)\s+"
    r"(?:command\s+of\s+|written\s+and\s+spoken\s+|proficiency\s+in\s+|in\s+)?German",
    r"German\s+(?:language\s+)?(?:skills|proficiency)\s+(?:are\s+|is\s+)?"
    r"(?:required|essential|a must)",
    r"(?:written\s+)?in\s+German\s+and\s+English",
    r"German\s+is\s+(?:required|mandatory|essential)",
]
GERMAN_RE = re.compile("|".join(GERMAN_PATTERNS), re.I)

# Markers that turn a German mention into a nice-to-have. Only counted when they
# follow closely: "sehr gute Deutschkenntnisse ..., Franzoesisch von Vorteil" puts
# the marker on the *other* language, 70+ characters downstream.
SOFT_MARKERS = re.compile(
    r"(von\s+Vorteil|hilfreich|w[üu]nschenswert|Pluspunkt|nice[- ]to[- ]have|"
    r"is\s+a\s+plus|are\s+a\s+plus|ideally|advantageous|bonus)", re.I)
SOFT_WINDOW = 60


def bun(args, log, timeout=120):
    """Run a portal CLI. Returns parsed JSON, or None when there is nothing usable.

    A non-zero exit does NOT discard stdout. `ats-search` exits 1 only when
    *every* selected company failed, and even then it prints `meta.companies` so
    the caller can see why; the other portal CLIs can also print a usable payload
    alongside a non-zero exit. The previous behaviour - `return None` on any
    non-zero exit, stdout ignored - is what would have thrown away a whole run's
    good results (plan §10.3).
    """
    argv = ["bun", "run"] + list(args)
    emit = EMITTER
    if emit:
        emit("start", argv)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(ROOT), timeout=timeout,
                              capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("  ! cli failed: %s" % exc)
        if emit:
            emit("finish", argv, exit_code=None, ms=(time.monotonic() - started) * 1000,
                 parsed=False, stderr=str(exc))
        return None
    payload = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
    if proc.returncode != 0:
        log("  ! exit %d: %s" % (proc.returncode, (proc.stderr or "").strip()[:160]))
        if payload is not None:
            log("  . stdout still parsed - keeping what the run did produce")
    elif payload is None:
        log("  ! unparseable output (%d bytes)" % len(proc.stdout))
    if emit:
        emit("finish", argv, exit_code=proc.returncode,
             ms=(time.monotonic() - started) * 1000, parsed=payload is not None,
             stderr=(proc.stderr or "").strip()[:400] or None)
    return payload


def results_of(payload):
    """Portal CLIs wrap their results differently; accept the common shapes."""
    if isinstance(payload, dict):
        for key in ("results", "jobs", "data", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return payload if isinstance(payload, list) else []


def german_hit(text):
    """Classify a posting's German requirement.

    Returns (verdict, quote) where verdict is "hard" (stated as a job condition),
    "soft" (mentioned, but marked as a nice-to-have) or None (not mentioned).

    The quote is a window centred on the match, not the enclosing sentence: German
    ads routinely run hundreds of characters without a full stop, so anchoring on
    sentence boundaries and truncating produced quotes that did not contain the
    matched phrase at all.
    """
    match = GERMAN_RE.search(text or "")
    if not match:
        return None, ""
    start = max(0, match.start() - 110)
    end = min(len(text), match.end() + 170)
    quote = re.sub(r"\s+", " ", text[start:end]).strip()
    if start > 0:
        quote = "..." + quote
    if end < len(text):
        quote = quote + "..."
    tail = text[match.end():match.end() + SOFT_WINDOW]
    return ("soft" if SOFT_MARKERS.search(tail) else "hard"), quote[:320]


def screen_text(text):
    """Turn a posting's text into (user_status, note) via the German screen."""
    verdict, quote = german_hit(text)
    if verdict == "hard":
        return "gate", "AUTO-SCREEN: German stated as a job condition - '%s'" % quote
    if verdict == "soft":
        # Not excluded: a nice-to-have German line is exactly the case the rule
        # leaves to the candidate. Flagged so it is visible, not silently passed.
        return "new", "AUTO-SCREEN: German mentioned but looks optional - confirm - '%s'" % quote
    return "new", ""


def collect_linkedin(cfg, log):
    """Run every configured LinkedIn query. Returns {canonical_url: record}."""
    found = {}
    age = str(cfg.get("jobage_days", 7))
    limit = str(cfg.get("limit_per_query", 10))
    for spec in cfg.get("linkedin", []):
        rows = results_of(bun([LINKEDIN, "search", "-q", spec["q"], "-l", spec["l"],
                               "--jobage", age, "-n", limit, "--format", "json"], log))
        log("  linkedin  %-28s %-24s %d" % (spec["q"][:28], spec["l"][:24], len(rows)))
        for row in rows:
            jid = str(row.get("id") or "")
            if not jid and not row.get("url"):
                continue
            url = jobs_md.canonical_url(
                "https://www.linkedin.com/jobs/view/%s" % jid if jid else row["url"])
            found[url] = {"id": jid, "title": row.get("title", ""),
                          "company": row.get("company", ""), "location": row.get("location", ""),
                          "posted": (row.get("date") or "")[:10], "url": url,
                          "portal": "linkedin-search", "description": row.get("description")}
    return found


def collect_freehire(cfg, log):
    """Run every configured freehire query. Returns {canonical_url: record}."""
    found = {}
    age = str(cfg.get("jobage_days", 7))
    limit = str(cfg.get("limit_per_query", 10))
    for spec in cfg.get("freehire", []):
        args = [FREEHIRE, "search", "--jobage", age, "-n", limit, "--format", "json"]
        for flag in ("category", "country", "seniority", "region", "city"):
            if spec.get(flag):
                args += ["--" + flag, spec[flag]]
        if spec.get("q"):
            args += ["-q", spec["q"]]
        rows = results_of(bun(args, log))
        log("  freehire  %-53s %d" % (str(spec)[:53], len(rows)))
        for row in rows:
            slug = str(row.get("id") or row.get("slug") or "")
            if not slug and not row.get("url"):
                continue
            url = jobs_md.canonical_url(
                "https://freehire.me/jobs/%s" % slug if slug else row["url"])
            found[url] = {"id": slug, "title": row.get("title", ""),
                          "company": row.get("company", ""), "location": row.get("location", ""),
                          "posted": (row.get("date") or row.get("published_at") or "")[:10],
                          "url": url, "portal": "freehire-search",
                          # freehire's agent search hydrates the description server-side,
                          # so the screen below costs no extra request.
                          "description": row.get("description")}
    return found


def screen(record, log, budget=None):
    """Fetch a posting's text and apply the German screen.

    Returns `(status, note, text)`. The text is the third element because this
    already pays for it: a LinkedIn row has no description until `detail` is
    called, and that call used to be made purely to run a regex over the result
    and discard it. The board's Job pane and the fit scorer both want the same
    bytes, so they are handed back rather than fetched twice.

    A record that already carries its description is screened for free -
    freehire and four of the five ATS vendors ship one with the listing. Only a
    record without one costs a `detail` request, and `budget` caps how many of
    those a run may make: moving LinkedIn from a cron to a button removed the
    *unattended* burst, not the burst itself, so the per-run cap is what
    actually bounds it.
    """
    if record.get("description"):
        status, note = screen_text(record["description"])
        return status, note, record["description"]
    cli = LINKEDIN if record["portal"] == "linkedin-search" else FREEHIRE
    ident = record.get("id")
    if not ident:
        return "new", "", ""
    if budget is not None and not budget.take():
        return "new", "AUTO-SCREEN: not screened - the run's detail budget was spent", ""
    try:
        proc = subprocess.run(["bun", "run", cli, "detail", ident, "--format", "plain"],
                              cwd=str(ROOT), timeout=90, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return "new", "", ""
    if proc.returncode != 0:
        return "new", "", ""
    status, note = screen_text(proc.stdout)
    return status, note, proc.stdout


class Budget:
    """A countdown of allowed `detail` requests for one run."""

    def __init__(self, limit):
        self.limit = int(limit)
        self.used = 0
        self.deferred = 0

    def take(self):
        if self.used >= self.limit:
            self.deferred += 1
            return False
        self.used += 1
        return True

    def __repr__(self):
        return "Budget(%d/%d used, %d deferred)" % (self.used, self.limit, self.deferred)
