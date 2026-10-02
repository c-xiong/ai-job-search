"""The board's HTTP server: routing, token auth, static files, collection.

    python3 tools/jobs_board.py            # opens http://127.0.0.1:8765/?t=<token>
    python3 tools/jobs_board.py --port 9000 --no-open

**Fetch new jobs** runs every enabled source once, on demand, in a background
thread: `POST /api/fetch` starts it, `GET /api/fetch/status` reports progress.
Collection itself lives in `tools/fetch_jobs.py` and works the same from a
terminal.

Security: binds 127.0.0.1 only, and every API call must carry a random token.
Without it any web page you happen to have open could POST to localhost and
silently rewrite your job list - or make your machine crawl five vendors, or
spend your model budget. The token lives in `job_scraper/.board-token` (0600)
and survives a restart, so the URL you bookmarked keeps working; `--new-token`
rotates it.

Stdlib only, Python 3.9+.
"""

import argparse
import atexit
import errno
import json
import ipaddress
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_jobs  # noqa: E402
import jobs_md  # noqa: E402
import ats_fetch  # noqa: E402
import linkedin_inbox  # noqa: E402

from . import (activity, add_job, companies, docs, notion, review, run_registry, runs,  # noqa: E402
               state, trash)

TOKEN_FILE = jobs_md.ROOT / "job_scraper" / ".board-token"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
# The process-local fallback, replaced in main() by the persisted token.
# Importing this module - which the tests do - must never create a file, so
# only a board that actually serves reads or writes one.
TOKEN = secrets.token_urlsafe(16)
STATIC = Path(__file__).resolve().parent / "static"


def load_token(new=False):
    """The board's token, persisted across restarts.

    It used to be minted per process, and that quietly broke every tab you had
    open whenever the board restarted: the page keeps polling, the API answers
    403, and `{"error": "forbidden"}` is valid JSON - so the client parsed it,
    found no `runs` key, and rendered a board with nothing on it. Two tabs, two
    different pasts, and no error anywhere on screen.

    A token that outlives the process is the same protection without that. The
    secret still never leaves this machine and the file is 0600. `--new-token`
    exists for the one case where rotation is the point: a token that has been
    pasted somewhere it should not have been.
    """
    if not new:
        try:
            existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if TOKEN_RE.fullmatch(existing):
            return existing
    token = secrets.token_urlsafe(16)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 0600 at creation rather than write-then-chmod: the second leaves a
        # window in which anyone with an account on this box can read it.
        handle = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
    except OSError as exc:
        print("could not save the board token to %s (%s) - this board works, but "
              "its URL will stop working when it restarts" % (TOKEN_FILE, exc),
              file=sys.stderr)
    return token


FETCH_LOCK = threading.Lock()
FETCH = {"running": False, "log": [], "sources": [], "error": None, "finished_at": None}
INBOX_LOCK = threading.Lock()
INBOX_PROCESS = {"running": False, "error": None, "result": None, "finished_at": None}


def inbox_status():
    result = linkedin_inbox.listing()
    with INBOX_LOCK:
        result["process_status"] = dict(INBOX_PROCESS)
    return result


def inbox_worker(limit):
    try:
        with fetch_jobs.RunLock():
            result = linkedin_inbox.process(limit=limit)
        with INBOX_LOCK:
            INBOX_PROCESS["result"] = result
        activity.emit("board", "LinkedIn inbox: %d imported, %d retry" %
                      (result.get("imported", 0), result.get("retry", 0)))
    except Exception as exc:
        with INBOX_LOCK:
            INBOX_PROCESS["error"] = str(exc)
        activity.emit("board", "LinkedIn inbox: %s" % exc, level="warn")
    finally:
        with INBOX_LOCK:
            INBOX_PROCESS.update(running=False, finished_at=datetime.now().isoformat())


def start_inbox_process(payload):
    limit = payload.get("limit", 15)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 20:
        return 400, {"error": "limit must be an integer from 0 to 20"}
    with FETCH_LOCK:
        if FETCH["running"]:
            return 409, {"error": "A job fetch is already running. Process the inbox after it finishes."}
    with INBOX_LOCK:
        if INBOX_PROCESS["running"]:
            return 409, {"error": "the inbox is already being processed"}
        INBOX_PROCESS.update(running=True, error=None, result=None, finished_at=None)
    threading.Thread(target=inbox_worker, args=(limit,), daemon=True).start()
    return 202, {"started": True, "limit": limit}


# --------------------------------------------------------------- collection

def fetch_worker(sources, max_companies, max_new_jobs, detail_fetches):
    lines = []
    activity.emit("collect", "fetch start - sources: " + ", ".join(sources),
                  detail=["max_companies=%d" % max_companies,
                          "max_new_jobs=%d" % max_new_jobs,
                          "linkedin_details=%d" % detail_fetches])
    summaries = []
    try:
        with activity.Timer() as timer:
            summaries, lines = fetch_jobs.fetch(sources, max_companies, max_new_jobs,
                                                detail_fetches, False, lines,
                                                emit=activity.collector_emitter())
        with FETCH_LOCK:
            FETCH["sources"] = summaries
            FETCH["error"] = None
        added = sum(s.get("added", 0) for s in summaries)
        failed = [name for s in summaries for name in (s.get("failed") or [])]
        activity.emit("collect",
                      "fetch done - %d new rows%s" % (
                          added, ", degraded: " + ", ".join(failed) if failed else ""),
                      level="warn" if failed else "info", ms=timer.ms,
                      detail=failed)
    except fetch_jobs.AlreadyRunning as exc:
        with FETCH_LOCK:
            FETCH["error"] = str(exc)
        activity.emit("collect", str(exc), level="warn")
    except Exception as exc:  # a crashed fetch must not leave the UI spinning
        traceback.print_exc()
        with FETCH_LOCK:
            FETCH["error"] = "%s: %s" % (type(exc).__name__, exc)
        activity.emit("collect", "%s: %s" % (type(exc).__name__, exc), level="error")
    finally:
        with FETCH_LOCK:
            FETCH["log"] = lines[-40:]
            FETCH["running"] = False
            FETCH["finished_at"] = datetime.now().isoformat()


def start_fetch(payload):
    """Validate the request and start the worker. Returns (http_status, body)."""
    with FETCH_LOCK:
        if FETCH["running"]:
            return 409, {"error": "a fetch is already running"}
        # An *absent* `sources` means "all of them"; an explicitly empty list
        # means the caller deselected everything, which is a mistake to report
        # rather than to reinterpret as "fetch everything".
        sources = payload.get("sources")
        if sources is None:
            sources = list(fetch_jobs.SOURCES)
        if not isinstance(sources, list) or not all(s in fetch_jobs.SOURCES for s in sources):
            return 400, {"error": "sources must be a subset of %s" % (list(fetch_jobs.SOURCES),)}
        if not sources:
            return 400, {"error": "select at least one source"}

        def bounded(key, default, low, high):
            try:
                return max(low, min(int(payload.get(key, default)), high))
            except (TypeError, ValueError):
                return default

        max_companies = bounded("max_companies", 8, 1, 20)
        max_new_jobs = bounded("max_new_jobs", 40, 1, 200)
        detail_fetches = bounded("linkedin_detail_fetches", fetch_jobs.LINKEDIN_DETAIL_DEFAULT,
                                 0, fetch_jobs.LINKEDIN_DETAIL_CEILING)
        FETCH.update({"running": True, "log": [], "sources": [], "error": None,
                      "finished_at": None})
    threading.Thread(target=fetch_worker, daemon=True,
                     args=(sources, max_companies, max_new_jobs, detail_fetches)).start()
    return 202, {"started": True, "sources": sources, "max_companies": max_companies,
                 "max_new_jobs": max_new_jobs, "linkedin_detail_fetches": detail_fetches}


def fetch_status():
    with FETCH_LOCK:
        status = dict(FETCH)
    # While a run is in flight the worker's own status file is fresher than the
    # in-process copy, because fetch_jobs writes it after every source.
    on_disk = {}
    if fetch_jobs.STATUS.exists():
        try:
            on_disk = json.loads(fetch_jobs.STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            on_disk = {}
    if status["running"] and on_disk.get("log"):
        status["log"] = on_disk["log"]
        status["sources"] = on_disk.get("sources") or status["sources"]
    # The run stamp belongs to the worker, not to this process: `fetch_jobs`
    # allocates it, writes it onto every row that run inserts as `first_seen_at`,
    # and records it here. The board reads it back to mark what the last fetch
    # brought in - which has to keep working after a restart, when the
    # in-process dict above is empty and the file is all there is.
    status["started_at"] = on_disk.get("started_at")
    status["finished_at"] = status.get("finished_at") or on_disk.get("finished_at")
    status["degraded"] = any(s.get("degraded") for s in status["sources"] or [])
    return status


# ---------------------------------------------------------------------- runs

# `/api/runs/<id>/<action>`. The id is generated by `runs.new_run_id()`, so the
# pattern is a validator as well as a parser: a path segment that is not a run
# id never reaches the supervisor.
RUN_PATH = re.compile(r"^/api/runs/(?P<id>r-[0-9]{8}-[0-9]{6}-[a-z0-9]{1,16}-[0-9a-f]{6})"
                      r"(?:/(?P<action>approve|cancel|kill|fit|verify|compile|restore|retry|continue"
                      r"|marks|reveal|applied|owner|delete))?$")
PDF_PATH = re.compile(r"^/api/pdf/(?P<id>r-[0-9]{8}-[0-9]{6}-[a-z0-9]{1,16}-[0-9a-f]{6})"
                      r"/(?P<kind>cv|cover)$")
COMPANY_PATH = re.compile(r"^/api/companies/(?P<slug>[a-z0-9][a-z0-9-]{0,120})"
                          r"(?:/(?P<action>resolve|identity|test-fetch|manage))?$")


# Run actions a POST may reach; anything else is refused before the body is read.
RUN_POST_ACTIONS = ("approve", "cancel", "kill", "compile", "restore", "retry", "continue",
                    "marks", "reveal", "applied", "owner", "delete")


def run_route(path):
    """(run_id, action) or (None, None) when this is not a run path."""
    match = RUN_PATH.fullmatch(path)
    if not match:
        return None, None
    return match.group("id"), match.group("action")


OWNER_LIMITS = {"portal_url": 2000, "apply_email": 254,   # RFC 5321 path limit
                "my_notes": 2000}                         # Notion's text-object cap
EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def owner_values(payload):
    """({column: text}, error) for the Send step's portal link and notes."""
    values = {}
    for column, limit in OWNER_LIMITS.items():
        if column not in payload:
            continue
        value = payload[column]
        if not isinstance(value, str):
            return None, "%s must be a string" % column
        value = value.rstrip() if column == "my_notes" else value.strip()
        if len(value) > limit:
            return None, "%s is longer than %d characters" % (column, limit)
        if (column == "portal_url" and value
                and urlsplit(value).scheme not in ("http", "https")):
            return None, "the portal link must start with http:// or https://"
        if column == "apply_email" and value and not EMAIL_SHAPE.fullmatch(value):
            return None, "the application email is not an email address"
        values[column] = value
    if not values:
        return None, "nothing to save"
    return values, None


def mark_board_applied(job_url):
    """Mark applied also moves the board row, so Status and the Applications
    list tell the same story. Returns "updated", "unchanged" or "missing"."""
    if not job_url:
        return "missing"
    with jobs_md.board_lock():
        seen = state.load()
        key = jobs_md.entry_key(seen, job_url)
        if key is None:
            return "missing"
        if jobs_md.user_status(seen[key]) == "applied":
            return "unchanged"
        seen[key]["user_status"] = "applied"
        state.save(seen, "%s -> applied" % (seen[key].get("company") or "job"))
    return "updated"


def posting_url_value(value):
    """Validate an owner's saved destination; an empty string restores the source link."""
    error = "Posting URL must be a web address (https://…) without spaces or sign-in details"
    if not isinstance(value, str):
        return None, "Posting URL must be a string"
    if not value:
        return "", None
    if len(value) > 8192:
        return None, "Posting URL is longer than 8192 characters"
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
           for char in value):
        return None, error
    try:
        parts = urlsplit(value)
        hostname, _port = parts.hostname, parts.port
        if (parts.scheme not in ("http", "https") or not hostname
                or parts.username is not None or parts.password is not None
                or parts.netloc.endswith(":")):
            return None, error
        # Python versions differ in how strictly urlsplit checks IPv6 brackets.
        # Check them explicitly, and reject escaped separators in DNS labels.
        if ":" in hostname or parts.netloc.startswith("["):
            if not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parts.netloc):
                return None, error
            ipaddress.IPv6Address(hostname)
        else:
            host = hostname.encode("idna").decode("ascii").rstrip(".")
            if not host or any(not re.fullmatch(r"(?!-)[a-zA-Z0-9-]{1,63}(?<!-)", label)
                               for label in host.split(".")):
                return None, error
    except (ValueError, UnicodeError):
        return None, error
    return value, None


# ------------------------------------------------------------------- static

def read_static(name):
    """One file from tools/board/static/, or None. Path traversal is refused."""
    target = (STATIC / name).resolve()
    try:
        target.relative_to(STATIC.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target.read_bytes()


# ------------------------------------------------------------------ routing

class Handler(BaseHTTPRequestHandler):
    server_version = "jobs-board"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _authed(self, query):
        return secrets.compare_digest((query.get("t") or [""])[0], TOKEN)

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parts = urlparse(self.path)
        query = parse_qs(parts.query)

        if parts.path == "/":
            if not self._authed(query):
                return self._send(403, "Missing or wrong token. Use the URL printed in the "
                                       "terminal.", "text/plain; charset=utf-8")
            page = read_static("index.html")
            if page is None:
                return self._send(500, "tools/board/static/index.html is missing",
                                  "text/plain; charset=utf-8")
            # The token reaches the page as a meta tag rather than being baked
            # into app.js, so the script stays a plain cacheable file.
            page = page.replace(b"__TOKEN__", TOKEN.encode("ascii"))
            return self._send(200, page, "text/html; charset=utf-8")

        if parts.path.startswith("/static/"):
            # Static assets carry no secrets; the token guards the data, not the
            # stylesheet. Keeping them unauthenticated means a refresh works.
            body = read_static(parts.path[len("/static/"):])
            if body is None:
                return self._send(404, "not found", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(parts.path)[0] or "application/octet-stream"
            return self._send(200, body, ctype + "; charset=utf-8")

        if not self._authed(query):
            return self._send(403, json.dumps({"error": "forbidden"}))

        if parts.path == "/api/jobs":
            return self._send(200, json.dumps(state.jobs_payload(), ensure_ascii=False))
        if parts.path == "/api/job":
            row = state.job_payload((query.get("url") or [""])[0])
            if row is None:
                return self._send(404, json.dumps({"error": "unknown job"}))
            return self._send(200, json.dumps(row, ensure_ascii=False))
        if parts.path == "/api/fetch/status":
            return self._send(200, json.dumps(fetch_status(), ensure_ascii=False))
        if parts.path == "/api/linkedin/inbox":
            try:
                body = inbox_status()
            except (ValueError, OSError) as exc:
                return self._send(500, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))
        if parts.path == "/api/activity":
            try:
                seq = int((query.get("since") or ["0"])[0])
            except ValueError:
                seq = 0
            epoch = (query.get("epoch") or [None])[0]
            return self._send(200, json.dumps(activity.since(seq, epoch), ensure_ascii=False))
        if parts.path == "/api/runs":
            return self._send(200, json.dumps(runs.supervisor().snapshot(), ensure_ascii=False))
        if parts.path == "/api/prefs":
            return self._send(200, json.dumps(docs.standing_preferences(), ensure_ascii=False))
        if parts.path == "/api/companies":
            try:
                body = companies.listing()
            except companies.CompanyError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))
        if parts.path == "/api/companies/suggest":
            return self._send(200, json.dumps(companies.suggestions(), ensure_ascii=False))

        run_id, action = run_route(parts.path)
        if run_id and action in (None, "fit", "verify"):
            record = runs.get(run_id)
            if record is None or trash.is_deleted(record):
                return self._send(404, json.dumps({"error": "unknown run"}))
            if action == "fit":
                fit = record.get("fit")
                if fit is None:
                    return self._send(404, json.dumps(
                        {"error": "this run has no evaluation yet", "phase": record["phase"]}))
                return self._send(200, json.dumps(fit, ensure_ascii=False))
            if action == "verify":
                path = run_registry.run_dir(run_id) / "verify.json"
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    # A run stopped before its build still has a screening.
                    payload = {"schema": "jobflow.verify/1", "run_id": run_id, "checks": []}
                except (OSError, ValueError):
                    return self._send(500, json.dumps({"error": "verification is unreadable"}))
                return self._send(200, json.dumps(review.view(record, payload),
                                                  ensure_ascii=False))
            return self._send(200, json.dumps(record, ensure_ascii=False))

        pdf_match = PDF_PATH.fullmatch(parts.path)
        if pdf_match:
            record = runs.get(pdf_match.group("id"))
            if record is None or trash.is_deleted(record):
                return self._send(404, json.dumps({"error": "unknown run"}))
            relative = (record.get("artefacts") or {}).get(pdf_match.group("kind") + "_pdf")
            if not relative:
                return self._send(404, json.dumps({"error": "PDF is not available"}))
            path = (run_registry.ROOT / relative).resolve()
            expected = run_registry.run_dir(record["id"]).resolve()
            try:
                path.relative_to(expected)
            except ValueError:
                return self._send(403, json.dumps({"error": "PDF path escaped its run"}))
            if not path.is_file():
                return self._send(404, json.dumps({"error": "PDF is missing"}))
            return self._send(200, path.read_bytes(), "application/pdf",
                              {"Content-Disposition": "inline; filename=%s.pdf"
                               % pdf_match.group("kind")})

        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        parts = urlparse(self.path)
        run_id, action = run_route(parts.path)
        company_match = COMPANY_PATH.fullmatch(parts.path)
        known = parts.path in ("/api/update", "/api/jobs/add", "/api/fetch", "/api/runs",
                               "/api/linkedin/capture", "/api/linkedin/process",
                               "/api/runs/undelete",
                               "/api/companies", "/api/companies/resolve-all",
                               "/api/companies/follow", "/api/companies/capture",
                               "/api/companies/health-check") or \
            (run_id and action in RUN_POST_ACTIONS)
        known = known or bool(company_match and company_match.group("action") in
                              ("resolve", "identity", "test-fetch", "manage"))
        if not known or not self._authed(parse_qs(parts.query)):
            # The token is what stops any web page you happen to have open from
            # POSTing to localhost - and for /api/fetch that means it is what
            # stops a web page from making your machine crawl five vendors. For
            # /api/runs it is what stops one from spending money on model calls.
            return self._send(403, json.dumps({"error": "forbidden"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or (parts.path in ("/api/linkedin/capture", "/api/companies/capture")
                              and length > 262144):
                return self._send(413, json.dumps({"error": "capture payload exceeds 256 KB"}))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "bad json"}))

        if not isinstance(payload, dict):
            payload = {}

        if parts.path == "/api/runs/undelete":
            try:
                restored = trash.undo([str(i) for i in payload.get("ids") or []])
            except trash.TrashError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps({"restored": restored}))
        if run_id and trash.is_deleted(runs.get(run_id)):
            # A deleted run answers to nothing but undo.
            return self._send(404, json.dumps({"error": "this run was deleted"}))
        if run_id and action == "delete":
            try:
                deleted = trash.delete(run_id, payload.get("scope") or "application",
                                       str(payload.get("confirm") or ""))
            except trash.TrashError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps({"deleted": deleted}))

        if parts.path == "/api/jobs/add":
            with activity.Timer() as timer:
                code, body = add_job.add(payload)
            activity.emit("board", "add job: %s" % (
                "%s - %s (%s)" % (body.get("company"), body.get("title"), body.get("outcome"))
                if code == 200 else body.get("error")),
                level="info" if code == 200 else "warn", ms=timer.ms)
            return self._send(code, json.dumps(body, ensure_ascii=False))

        if parts.path == "/api/fetch":
            code, body = start_fetch(payload)
            return self._send(code, json.dumps(body, ensure_ascii=False))

        if parts.path == "/api/linkedin/capture":
            try:
                body = linkedin_inbox.capture(payload)
            except linkedin_inbox.InputError as exc:
                return self._send(400, json.dumps({"error": str(exc)}))
            except (OSError, ValueError) as exc:
                return self._send(500, json.dumps({"error": "Could not save LinkedIn capture: %s" % exc}))
            return self._send(200, json.dumps(body, ensure_ascii=False))
        if parts.path == "/api/linkedin/process":
            code, body = start_inbox_process(payload)
            return self._send(code, json.dumps(body, ensure_ascii=False))

        if parts.path == "/api/runs":
            code, body = runs.supervisor().start(payload)
            return self._send(code, json.dumps(body, ensure_ascii=False))

        if parts.path == "/api/companies":
            try:
                body = companies.never(payload) if payload.get("decision") == "never" \
                    else companies.add(payload)
            except companies.CompanyError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if parts.path in ("/api/companies/follow", "/api/companies/capture"):
            try:
                if parts.path.endswith("/follow"):
                    job = state.job_payload(str(payload.get("url") or ""))
                    if job is None:
                        return self._send(404, json.dumps({"error": "unknown job"}))
                    body = companies.follow(payload, job)
                else:
                    body = companies.capture(payload)
            except companies.CompanyError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if parts.path == "/api/companies/resolve-all":
            try:
                body = companies.resolve_all(payload)
            except companies.CompanyError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if parts.path == "/api/companies/health-check":
            try:
                body = companies.health_check(payload)
            except companies.CompanyError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if company_match:
            try:
                if company_match.group("action") == "resolve":
                    body = companies.resolve(company_match.group("slug"), payload)
                elif company_match.group("action") == "test-fetch":
                    body = companies.test_fetch(company_match.group("slug"), payload)
                elif company_match.group("action") == "manage":
                    body = companies.manage(company_match.group("slug"), payload)
                else:
                    body = companies.identity(company_match.group("slug"), payload)
            except companies.CompanyError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if run_id and action == "applied":
            record = runs.get(run_id)
            if record is None:
                return self._send(404, json.dumps({"error": "unknown run"}))
            # Notion first: the tracker is its cache, so it moves only once the
            # Stage it mirrors has.
            try:
                stage = notion.mark_applied(record)
            except notion.NotionError as exc:
                return self._send(502, json.dumps({"error": "Notion: %s" % exc}))
            body = {"notion": stage, "tracker": docs.mark_applied(record),
                    "board": mark_board_applied(record.get("job_url"))}
            activity.emit("board", "marked applied: %s - %s (Notion %s, tracker %s)"
                          % (record["company"], record["role"], stage or "off",
                             body["tracker"]), run_id=run_id)
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if run_id and action == "owner":
            record = runs.get(run_id)
            if record is None:
                return self._send(404, json.dumps({"error": "unknown run"}))
            values, error = owner_values(payload)
            if error:
                return self._send(400, json.dumps({"error": error}))
            # Notion first, as for Mark applied: the tracker is its cache.
            try:
                synced = notion.save_owner_fields(record, values)
            except notion.NotionError as exc:
                return self._send(502, json.dumps({"error": "Notion: %s" % exc}))
            body = {"notion": synced, "tracker": docs.save_owner_fields(record, values),
                    **values}
            activity.emit("board", "saved portal/email/notes: %s - %s (Notion %s, tracker %s)"
                          % (record["company"], record["role"], synced or "off",
                             body["tracker"]), run_id=run_id)
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if run_id and action in ("marks", "reveal"):
            record = runs.get(run_id)
            if record is None:
                return self._send(404, json.dumps({"error": "unknown run"}))
            try:
                if action == "marks":
                    body = {"manual": review.mark(record, str(payload.get("id") or ""),
                                                  payload.get("done") is True)}
                else:
                    kind = payload.get("kind")
                    if kind not in ("cv", "cover"):
                        return self._send(400, json.dumps({"error": "kind must be cv or cover"}))
                    body = {"revealed": review.reveal(record, kind)}
            except review.ReviewError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if run_id:
            supervisor = runs.supervisor()
            if action == "approve":
                # Compare-and-set: the client sends the phase it last rendered,
                # so two rapid clicks buy one pass B rather than two.
                code, body = supervisor.approve(run_id, payload.get("phase"),
                                                payload.get("base_cv"),
                                                payload.get("scope"))
            elif action == "cancel":
                code, body = supervisor.cancel(run_id)
            elif action == "compile":
                code, body = supervisor.compile(run_id)
            elif action == "restore":
                code, body = supervisor.restore(run_id)
            elif action == "retry":
                # "Regenerate": a fresh attempt that keeps only the saved posting.
                code, body = supervisor.retry(run_id)
            elif action == "continue":
                # Resume from the saved checkpoint; `proceed` is the owner's
                # explicit override of a surfaced hard conflict.
                code, body = supervisor.continue_run(run_id, {
                    "proceed": payload.get("proceed") is True,
                    "scope": payload.get("scope"), "base_cv": payload.get("base_cv")})
            else:
                code, body = supervisor.kill(run_id)
            return self._send(code, json.dumps(body, ensure_ascii=False))

        url = payload.get("url")
        # Read-modify-write under the same lock the background fetch takes, so a
        # status set while a fetch is in flight cannot be reverted by the fetch's
        # copy of the board (or the other way round).
        with jobs_md.board_lock():
            seen = state.load()
            if not isinstance(url, str) or url not in seen:
                return self._send(404, json.dumps({"error": "unknown job"}))
            why = []
            posting_url = None
            if "posting_url" in payload:
                posting_url, error = posting_url_value(payload["posting_url"])
                if error:
                    return self._send(400, json.dumps({"error": error}))
                if posting_url:
                    canonical = jobs_md.canonical_url(posting_url)
                    if any(key != url and canonical in {
                            jobs_md.canonical_url(link) for link in jobs_md.known_urls(entry, key)}
                           for key, entry in seen.items()):
                        return self._send(409, json.dumps({
                            "error": "This URL belongs to another job on the board"}))
            if "status" in payload:
                status = payload["status"]
                if status not in state.LOCK_STATUSES:
                    return self._send(400, json.dumps({"error": "bad status"}))
                seen[url]["user_status"] = status
                why.append("%s -> %s" % (seen[url].get("company") or "job", status))
            if "note" in payload:
                seen[url]["user_note"] = str(payload["note"])[:500]
                why.append("note on %s" % (seen[url].get("company") or "job"))
            if posting_url is not None:
                entry = seen[url]
                if posting_url:
                    ats_fetch.seed_history(entry, url)
                    entry["user_posting_url"] = posting_url
                    ats_fetch.append_source(entry, {
                        "portal": "manual", "url": posting_url, "id": "",
                        "first_seen": datetime.now().date().isoformat()})
                else:
                    entry.pop("user_posting_url", None)
                why.append("posting link on %s" % (entry.get("company") or "job"))
            state.save(seen, "; ".join(why))
            result = {"ok": True, "open_url": state.primary_url(seen[url]),
                      "posting_url": seen[url].get("user_posting_url") or ""}
        self._send(200, json.dumps(result))

    def do_PATCH(self):
        parts = urlparse(self.path)
        match = COMPANY_PATH.fullmatch(parts.path)
        if not match or match.group("action") or not self._authed(parse_qs(parts.query)):
            return self._send(403, json.dumps({"error": "forbidden"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError()
            body = companies.patch_company(match.group("slug"), payload)
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "bad json"}))
        except companies.CompanyError as exc:
            return self._send(exc.status, json.dumps({"error": str(exc)}))
        return self._send(200, json.dumps(body, ensure_ascii=False))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local job triage board")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--new-token", action="store_true",
                    help="mint a fresh token; every board tab you have open stops working")
    args = ap.parse_args(argv)

    if not jobs_md.SEEN.exists():
        print("no job_scraper/seen_jobs.json yet - run /scrape first")
        return 1

    # Before anything emits: the ring takes history only while it is empty, and
    # orphan reconciliation below is itself an emitter.
    activity.seed()

    # Belt to the wrapper's braces: the model process holds its own lock and
    # survives us, which is what makes orphan detection sound - but a board that
    # is being shut down deliberately should not leave one spending in the dark.
    atexit.register(runs.shutdown)
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)

        def handler(signum, frame, _previous=previous):
            runs.shutdown()
            if callable(_previous):
                return _previous(signum, frame)
            raise KeyboardInterrupt

        signal.signal(sig, handler)

    runs.supervisor()          # reconcile orphans before the first request
    trash.purge()              # deleted runs older than trash.TRASH_DAYS
    notion.start_background_pull()   # no-op unless job_scraper/notion_sync.json is set

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        # Almost always a board you already have open - including a **suspended**
        # one, which still holds the socket while ignoring Ctrl-C and SIGTERM.
        # A stack trace here says nothing a person can act on.
        if exc.errno != errno.EADDRINUSE:
            raise
        print("port %d is already in use - you probably have a board open already."
              % args.port, file=sys.stderr)
        print("  find it:  lsof -nP -iTCP:%d -sTCP:LISTEN" % args.port, file=sys.stderr)
        print("  stop it:  kill -CONT <pid>; kill <pid>      "
              "(CONT first: a Ctrl-Z'd board ignores TERM)", file=sys.stderr)
        print("  or:       python3 tools/jobs_board.py --port %d" % (args.port + 1),
              file=sys.stderr)
        return 1

    # After the bind, so a board that could not start - the common case being
    # a board already running on this port - never rotates the token out from
    # under the one that did.
    global TOKEN
    TOKEN = load_token(new=args.new_token)
    url = "http://127.0.0.1:%d/?t=%s" % (args.port, TOKEN)
    activity.emit("server", "board listening on 127.0.0.1:%d" % args.port)
    # flush: stdout is block-buffered when piped, and a user redirecting the
    # output must still be able to see the URL.
    print("job board: %s" % url, flush=True)
    print("(localhost only, token-protected; the URL survives a restart; Ctrl-C to stop)",
          flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
