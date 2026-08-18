#!/usr/bin/env python3
"""Unattended job collector for cron/launchd. No LLM, no tokens, no prompts.

    python3 tools/scrape_cron.py            # collect, screen, write
    python3 tools/scrape_cron.py --dry-run  # do everything except write

This is deliberately NOT `/scrape`. The Claude skill judges fit and reads a
posting's language requirements with real judgment; this script only *collects*,
so it can run every morning without an agent, a token budget, or a permission
prompt to answer. New jobs land in the board as `new` with an empty Fit, for you
(or a later `/rank`) to assess.

The one judgment it makes is mechanical and conservative: a regex screen for
German stated as a job condition. A hit is filed as `gate` with the matched
sentence quoted, so nothing is silently dropped - it shows up under the board's
`gate` filter, one click from view, and you can overrule it.
A posting that only mentions German in passing is left alone for you to judge.

Stdlib only, Python 3.9+. Requires `bun` on PATH for the portal CLIs.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobs_md  # noqa: E402

ROOT = jobs_md.ROOT
CONFIG = ROOT / "job_scraper" / "scrape_config.json"
LOG = ROOT / "job_scraper" / "scrape.log"
LINKEDIN = ".agents/skills/linkedin-search/cli/src/cli.ts"
FREEHIRE = ".agents/skills/freehire-search/cli/src/cli.ts"

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


def log(msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M"), msg)
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def bun(args, timeout=120):
    """Run a portal CLI. Returns parsed JSON, or None on any failure."""
    try:
        proc = subprocess.run(["bun", "run"] + args, cwd=str(ROOT), timeout=timeout,
                              capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log("  ! cli failed: %s" % exc)
        return None
    if proc.returncode != 0:
        log("  ! exit %d: %s" % (proc.returncode, (proc.stderr or "").strip()[:160]))
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        log("  ! unparseable output (%d bytes)" % len(proc.stdout))
        return None


def results_of(payload):
    """Portal CLIs wrap their results differently; accept the common shapes."""
    if isinstance(payload, dict):
        for key in ("results", "jobs", "data", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return payload if isinstance(payload, list) else []


# Markers that turn a German mention into a nice-to-have. Only counted when they
# follow closely: "sehr gute Deutschkenntnisse ..., Franzoesisch von Vorteil" puts
# the marker on the *other* language, 70+ characters downstream.
SOFT_MARKERS = re.compile(
    r"(von\s+Vorteil|hilfreich|w[üu]nschenswert|Pluspunkt|nice[- ]to[- ]have|"
    r"is\s+a\s+plus|are\s+a\s+plus|ideally|advantageous|bonus)", re.I)
SOFT_WINDOW = 60


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


def collect(cfg):
    """Run every configured query. Returns {url: record}."""
    found = {}
    age = str(cfg.get("jobage_days", 7))
    limit = str(cfg.get("limit_per_query", 10))

    for spec in cfg.get("linkedin", []):
        rows = results_of(bun([LINKEDIN, "search", "-q", spec["q"], "-l", spec["l"],
                               "--jobage", age, "-n", limit, "--format", "json"]))
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
                          "portal": "linkedin-search"}

    for spec in cfg.get("freehire", []):
        args = [FREEHIRE, "search", "--jobage", age, "-n", limit, "--format", "json"]
        for flag in ("category", "country", "seniority", "region", "city"):
            if spec.get(flag):
                args += ["--" + flag, spec[flag]]
        if spec.get("q"):
            args += ["-q", spec["q"]]
        rows = results_of(bun(args))
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
                          "url": url, "portal": "freehire-search"}
    return found


def screen(record):
    """Fetch the posting and apply the German screen. Returns (status, note)."""
    cli = LINKEDIN if record["portal"] == "linkedin-search" else FREEHIRE
    ident = record["id"]
    if not ident:
        return "new", ""
    try:
        proc = subprocess.run(["bun", "run", cli, "detail", ident, "--format", "plain"],
                              cwd=str(ROOT), timeout=90, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return "new", ""
    if proc.returncode != 0:
        return "new", ""
    verdict, quote = german_hit(proc.stdout)
    if verdict == "hard":
        return "gate", "AUTO-SCREEN: German stated as a job condition - '%s'" % quote
    if verdict == "soft":
        # Not excluded: a nice-to-have German line is exactly the case the rule
        # leaves to the candidate. Flagged so it is visible, not silently passed.
        return "new", "AUTO-SCREEN: German mentioned but looks optional - confirm - '%s'" % quote
    return "new", ""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="collect and screen, write nothing")
    args = parser.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    log("scrape_cron start%s" % (" (dry run)" if args.dry_run else ""))

    seen = {}
    if jobs_md.SEEN.exists():
        seen = json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})

    found = collect(cfg)
    known = {jobs_md.canonical_url(u) for u in seen}
    fresh = {u: r for u, r in found.items() if u not in known}
    log("  %d results, %d already known, %d new"
        % (len(found), len(found) - len(fresh), len(fresh)))

    gated = 0
    for url, rec in fresh.items():
        status, note = screen(rec)
        gated += status == "gate"
        seen[url] = {
            "title": rec["title"], "company": rec["company"], "location": rec["location"],
            "url": url, "first_seen": date.today().isoformat(), "posted": rec["posted"],
            "deadline": None, "fit": "", "status": "new", "portal": rec["portal"],
            "user_status": status, "user_note": "", "note": note,
        }
    log("  %d added (%d auto-gated on German, %d awaiting fit assessment)"
        % (len(fresh), gated, len(fresh) - gated))

    if args.dry_run:
        log("dry run - nothing written")
        return 0

    jobs_md.SEEN.write_text(json.dumps({"seen": seen}, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    jobs_md.MD.write_text(jobs_md.render(seen), encoding="utf-8")
    jobs_md.write_csv(seen)
    log("done - %d jobs in the board" % len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
