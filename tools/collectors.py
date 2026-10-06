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
import hashlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

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
COLLECTION = None

LINKEDIN = ".agents/skills/linkedin-search/cli/src/cli.ts"
FREEHIRE = ".agents/skills/freehire-search/cli/src/cli.ts"
ATS = ".agents/skills/ats-search/cli/src/cli.ts"
ARBEITNOW = "https://www.arbeitnow.com/api/job-board-api"


def bounded(value, default, low, high):
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


class CollectionSession:
    """Pure per-run search plan; checkpoints are committed after board persistence.

    Each query/page costs one keyword call. First pages precede second pages,
    and sources are interleaved so a large query pool cannot starve another.
    The cursor rotates over this plan across successful board writes.
    """

    def __init__(self, cfg, sources, state=None):
        self.cfg, self.state = cfg, dict(state or {})
        self.outcomes, self.blocked = [], set()
        self.last_error = None
        self.last_meta = {}
        specs = {s: [q for q in cfg.get(s, []) if isinstance(q, dict)
                     and q.get("enabled", True)] for s in ("linkedin", "freehire") if s in sources}
        tasks = []
        for page in range(1, 4):
            for index in range(max((len(q) for q in specs.values()), default=0)):
                for source, queries in specs.items():
                    if index >= len(queries):
                        continue
                    spec = queries[index]
                    if page > bounded(spec.get("pages", cfg.get("pages_per_query", 1)), 1, 1, 3):
                        continue
                    key = hashlib.sha256(json.dumps([source, spec, page], sort_keys=True).encode()).hexdigest()[:16]
                    tasks.append({"source": source, "spec": spec, "page": page, "key": key})
        self.tasks = tasks
        self.cursor_key = ",".join(sorted(specs))
        cursors = self.state.get("query_cursors") or {}
        cursors = cursors if isinstance(cursors, dict) else {}
        self.start = bounded(cursors.get(self.cursor_key, self.state.get("query_cursor", 0)), 0, 0, 10**9) % max(1, len(tasks))
        budget = bounded(cfg.get("keyword_search_budget", 12), 12, 0, 12)
        self.selected = [tasks[(self.start + n) % len(tasks)] for n in range(min(budget, len(tasks)))]
        self.next_cursor = (self.start + len(self.selected)) % max(1, len(tasks))

    def __enter__(self):
        global COLLECTION
        self.previous, COLLECTION = COLLECTION, self
        return self

    def __exit__(self, *exc):
        global COLLECTION
        COLLECTION = self.previous
        return False

    def for_source(self, source):
        return [task for task in self.selected if task["source"] == source]

    def outcome(self, source, key, page, rows, error=None, meta=None, deferred=False):
        result = {"source": source, "query_id": key, "page": page,
                  "returned": len(rows), "status": "deferred" if deferred else "failed" if error else "ok",
                  "failed": int(bool(error)), "deferred": int(deferred),
                  "urls": [r["url"] for r in rows],
                  "http_attempts": (meta or {}).get("http_attempts"),
                  "retries": (meta or {}).get("retries", 0)}
        if error:
            result["error"] = str(error)[:300]
        self.outcomes.append(result)
        return result

    def summary(self, source):
        outcomes = [o for o in self.outcomes if o["source"] == source]
        deferred = len([t for t in self.tasks if t["source"] == source and t not in self.selected])
        return {"queries": outcomes, "search_calls": sum(o["status"] != "deferred" for o in outcomes),
                "failed": [o["query_id"] for o in outcomes if o["failed"]],
                "deferred_queries": deferred + sum(o["deferred"] for o in outcomes),
                "degraded": any(o["failed"] for o in outcomes),
                "returned": sum(o["returned"] for o in outcomes),
                "retry_policy": "at most two 5xx retries; 429 stops this source" if source in ("linkedin", "freehire") else "no API retries"}

    def persisted_state(self, stamp):
        state = dict(self.state)
        if self.tasks:
            cursors = self.state.get("query_cursors") or {}
            cursors = dict(cursors) if isinstance(cursors, dict) else {}
            cursors[self.cursor_key] = self.next_cursor
            state.update(query_cursor=self.next_cursor, query_cursors=cursors)
        queries = dict(state.get("queries") or {})
        for outcome in self.outcomes:
            old = dict(queries.get(outcome["query_id"]) or {})
            old.update(outcome, last_attempt_at=stamp)
            if outcome["status"] == "ok":
                old["last_persisted_at"] = stamp
            queries[outcome["query_id"]] = old
        # Removed configurations must not grow an unbounded history.
        # Keep other source selections' checkpoints; cap retired configurations.
        state["queries"] = dict(sorted(queries.items(), key=lambda item: item[1].get("last_attempt_at", ""), reverse=True)[:1000])
        return state

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
    if COLLECTION:
        COLLECTION.last_error = None
        COLLECTION.last_meta = {}
    try:
        proc = subprocess.run(argv, cwd=str(ROOT), timeout=timeout,
                              capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if COLLECTION:
            COLLECTION.last_error = str(exc)
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
        if COLLECTION:
            COLLECTION.last_error = (proc.stderr or "CLI failed").strip()[:400]
            try:
                COLLECTION.last_meta = json.loads(proc.stderr).get("request_meta") or {}
            except (ValueError, AttributeError):
                pass
        log("  ! exit %d: %s" % (proc.returncode, (proc.stderr or "").strip()[:160]))
        if payload is not None:
            log("  . stdout still parsed - keeping what the run did produce")
    elif payload is None:
        if COLLECTION:
            COLLECTION.last_error = "unparseable CLI output"
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
    """Run the selected LinkedIn query/pages. Returns {canonical_url: record}."""
    found = {}
    session = COLLECTION or CollectionSession(cfg, ["linkedin"])
    age = str(bounded(cfg.get("jobage_days", 7), 7, 1, 30))
    limit = str(bounded(cfg.get("limit_per_query", 10), 10, 1, 15))
    for task in session.for_source("linkedin"):
        spec, page = task["spec"], task["page"]
        if "linkedin" in session.blocked:
            session.outcome("linkedin", task["key"], page, [], deferred=True)
            continue
        if not spec.get("l"):
            session.outcome("linkedin", task["key"], page, [], error="query missing location")
            continue
        args = [LINKEDIN, "search", "-q", spec.get("q", ""), "-l", spec["l"],
                "--jobage", age, "--page", str(page), "--sort", "date", "-n", limit, "--format", "json"]
        if spec.get("remote"):
            args.extend(["--remote", spec["remote"]])
        session.last_error = None
        payload = bun(args, log)
        rows = results_of(payload)
        error = session.last_error or ("unusable CLI output" if not isinstance(payload, dict)
                                      or not isinstance(payload.get("results"), list) else None)
        if error and ("429" in error or "RATE_LIMITED" in error):
            session.blocked.add("linkedin")
        log("  linkedin  %-28s %-24s %d" % (spec.get("q", "")[:28], spec["l"][:24], len(rows)))
        returned = []
        for row in rows:
            if not isinstance(row, dict):
                error = "malformed result row"
                continue
            jid = str(row.get("id") or "")
            if not jid and not row.get("url"):
                continue
            url = jobs_md.canonical_url(
                "https://www.linkedin.com/jobs/view/%s" % jid if jid else row["url"])
            found[url] = {"id": jid, "title": row.get("title", ""),
                          "company": row.get("company", ""), "location": row.get("location", ""),
                          "posted": (row.get("date") or "")[:10], "url": url,
                          "portal": "linkedin-search", "description": row.get("description")}
            returned.append(found[url])
        session.outcome("linkedin", task["key"], page, returned, error, payload.get("meta") if isinstance(payload, dict) else session.last_meta)
    return found


def collect_freehire(cfg, log):
    """Run every configured freehire query. Returns {canonical_url: record}."""
    found = {}
    session = COLLECTION or CollectionSession(cfg, ["freehire"])
    age = str(bounded(cfg.get("jobage_days", 7), 7, 1, 30))
    limit = str(bounded(cfg.get("limit_per_query", 10), 10, 1, 15))
    for task in session.for_source("freehire"):
        spec, page = task["spec"], task["page"]
        if "freehire" in session.blocked:
            session.outcome("freehire", task["key"], page, [], deferred=True)
            continue
        args = [FREEHIRE, "search", "--jobage", age, "--page", str(page), "-n", limit, "--format", "json"]
        for flag in ("category", "country", "seniority", "region", "city", "work-mode"):
            if spec.get(flag):
                args += ["--" + flag, spec[flag]]
        if spec.get("q"):
            args += ["-q", spec["q"]]
        session.last_error = None
        payload = bun(args, log)
        rows = results_of(payload)
        error = session.last_error or ("unusable CLI output" if not isinstance(payload, dict)
                                      or not isinstance(payload.get("results"), list) else None)
        if error and ("429" in error or "RATE_LIMITED" in error):
            session.blocked.add("freehire")
        log("  freehire  %-53s %d" % (str(spec)[:53], len(rows)))
        returned = []
        for row in rows:
            if not isinstance(row, dict):
                error = "malformed result row"
                continue
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
            returned.append(found[url])
        session.outcome("freehire", task["key"], page, returned, error, payload.get("meta") if isinstance(payload, dict) else session.last_meta)
    return found


class _PostingHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("p", "li", "div"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def posting_text(html):
    parser = _PostingHTML()
    parser.feed(html or "")
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()


def arbeitnow_access():
    """Injectable policy check; fixed API host, bounded existing robots transport."""
    import robots_check
    return robots_check.gate(ARBEITNOW, cache={})


def collect_arbeitnow(cfg, log):
    """Small bounded keyless API scan; geography/role filters happen locally."""
    spec = cfg.get("arbeitnow") or {}
    if not spec.get("enabled", False):
        return {}
    session = COLLECTION or CollectionSession(cfg, [])
    found = {}
    code, message = arbeitnow_access()
    if code:
        session.outcome("arbeitnow", "arbeitnow-access", 0, [], error=message)
        log("  ! arbeitnow access unconfirmed: %s" % message)
        return found
    age = bounded(spec.get("jobage_days", cfg.get("jobage_days", 7)), 7, 1, 30)
    terms = spec.get("include_titles") or spec.get("query") or []
    terms = [terms] if isinstance(terms, str) else terms
    locations = spec.get("locations") or []
    locations = [locations] if isinstance(locations, str) else locations
    for page in range(1, bounded(spec.get("max_pages", 2), 2, 1, 3) + 1):
        key, rows = "arbeitnow-page-%d" % page, []
        try:
            req = urllib.request.Request(ARBEITNOW + "?page=%d" % page,
                                         headers={"Accept": "application/json", "User-Agent": "ai-job-search/1.0"})
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("API payload exceeds size limit")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("API response has no data array")
            for raw in payload["data"]:
                if not isinstance(raw, dict):
                    raise ValueError("malformed API result")
                if not isinstance(raw.get("slug"), str) or not isinstance(raw.get("url"), str):
                    raise ValueError("API result missing stable slug or URL")
                parts = urlsplit(raw["url"])
                if parts.scheme not in ("https", "http") or not parts.hostname or parts.username:
                    raise ValueError("API result has invalid job URL")
                title, location = raw.get("title") or "", raw.get("location") or ""
                if terms and title and not any(str(t).lower() in title.lower() for t in terms):
                    continue
                if locations and location and not any(str(l).lower() in location.lower() for l in locations):
                    remote_scopes = spec.get("remote_locations") or ["remote", "worldwide", "anywhere", "europe", "european union"]
                    if not (spec.get("include_remote", True) and raw.get("remote")
                            and location.strip().lower() in {str(scope).lower() for scope in remote_scopes}):
                        continue
                posted = ""
                try:
                    timestamp = float(raw.get("created_at"))
                    posted_date = datetime.fromtimestamp(timestamp, timezone.utc).date()
                    posted = posted_date.isoformat()
                    if (date.today() - posted_date).days > age:
                        continue
                except (ValueError, TypeError, OverflowError, OSError):
                    pass  # Unknown posting dates stay unknown and remain eligible.
                url = jobs_md.canonical_url(raw["url"])
                record = {"id": "arbeitnow:" + raw["slug"], "title": title,
                          "company": raw.get("company_name") or "", "location": location,
                          "posted": posted, "url": url, "portal": "arbeitnow",
                          "description": posting_text(raw.get("description") or ""),
                          "remote": raw.get("remote")}
                found[url] = record
                rows.append(record)
            session.outcome("arbeitnow", key, page, rows, meta={"http_attempts": 1})
            if not payload["data"] or not (payload.get("links") or {}).get("next"):
                break
        except (OSError, ValueError, TypeError) as exc:
            session.outcome("arbeitnow", key, page, rows, error=str(exc), meta={"http_attempts": 1})
            log("  ! arbeitnow page %d failed: %s" % (page, exc))
            break
    log("  arbeitnow %d eligible records" % len(found))
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
    if record.get("portal") != "linkedin-search":
        return "new", "AUTO-SCREEN: missing inline description - not screened", ""
    cli = LINKEDIN if record["portal"] == "linkedin-search" else FREEHIRE
    ident = record.get("id")
    if not ident:
        return "new", "", ""
    if budget is not None and not budget.take():
        reason = ("LinkedIn rate-limited this run" if budget.blocked
                  else "the run's detail budget was spent")
        return "new", "AUTO-SCREEN: not screened - %s" % reason, ""
    try:
        proc = subprocess.run(["bun", "run", cli, "detail", ident, "--format", "json"],
                              cwd=str(ROOT), timeout=90, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if budget is not None:
            budget.failed += 1
        log("  ! detail failed: %s" % exc)
        return "new", "", ""
    if proc.returncode != 0:
        if budget is not None:
            budget.failed += 1
            try:
                meta = json.loads(proc.stderr).get("request_meta") or {}
                budget.http_attempts += bounded(meta.get("http_attempts"), 1, 1, 100)
                budget.retries += bounded(meta.get("retries"), 0, 0, 100)
            except (ValueError, AttributeError):
                pass
            if "429" in (proc.stderr or "") or "RATE_LIMITED" in (proc.stderr or ""):
                budget.blocked = True
        log("  ! detail failed: %s" % (proc.stderr or "CLI error")[:200])
        return "new", "", ""
    try:
        payload = json.loads(proc.stdout)
        text = payload.get("description") or ""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("missing readable description")
    except (ValueError, AttributeError):
        if budget is not None:
            budget.failed += 1
        log("  ! detail returned no readable description")
        return "new", "", ""
    if budget is not None:
        meta = payload.get("request_meta") or {}
        budget.http_attempts += bounded(meta.get("http_attempts"), 1, 1, 100)
        budget.retries += bounded(meta.get("retries"), 0, 0, 100)
    status, note = screen_text(text)
    return status, note, text


class Budget:
    """A countdown of allowed `detail` requests for one run."""

    def __init__(self, limit):
        self.limit = int(limit)
        self.used = 0
        self.deferred = 0
        self.failed = 0
        self.blocked = False
        self.http_attempts = 0
        self.retries = 0

    def take(self):
        if self.blocked or self.used >= self.limit:
            self.deferred += 1
            return False
        self.used += 1
        return True

    def __repr__(self):
        return "Budget(%d/%d used, %d deferred)" % (self.used, self.limit, self.deferred)
