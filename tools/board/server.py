"""The board's HTTP server: routing, token auth, static files, collection.

    python3 tools/jobs_board.py            # opens http://127.0.0.1:8765/?t=<token>
    python3 tools/jobs_board.py --port 9000 --no-open

**Fetch new jobs** runs every enabled source once, on demand, in a background
thread: `POST /api/fetch` starts it, `GET /api/fetch/status` reports progress.
Collection itself lives in `tools/fetch_jobs.py` and works the same from a
terminal.

Security: binds 127.0.0.1 only, and every API call must carry a random token
minted at startup. Without the token any web page you happen to have open could
POST to localhost and silently rewrite your job list - or make your machine crawl
five vendors.

Stdlib only, Python 3.9+.
"""

import argparse
import atexit
import errno
import json
import mimetypes
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
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fetch_jobs  # noqa: E402
import jobs_md  # noqa: E402

from . import activity, companies, docs, run_registry, runs, state  # noqa: E402

TOKEN = secrets.token_urlsafe(16)
STATIC = Path(__file__).resolve().parent / "static"

FETCH_LOCK = threading.Lock()
FETCH = {"running": False, "log": [], "sources": [], "error": None, "finished_at": None}


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
    status["degraded"] = any(s.get("degraded") for s in status["sources"] or [])
    return status


# ---------------------------------------------------------------------- runs

# `/api/runs/<id>/<action>`. The id is generated by `runs.new_run_id()`, so the
# pattern is a validator as well as a parser: a path segment that is not a run
# id never reaches the supervisor.
RUN_PATH = re.compile(r"^/api/runs/(?P<id>r-[0-9]{8}-[0-9]{6}-[a-z0-9]{1,16}-[0-9a-f]{6})"
                      r"(?:/(?P<action>approve|cancel|kill|fit|verify|compile|restore|retry))?$")
PDF_PATH = re.compile(r"^/api/pdf/(?P<id>r-[0-9]{8}-[0-9]{6}-[a-z0-9]{1,16}-[0-9a-f]{6})"
                      r"/(?P<kind>cv|cover)$")
COMPANY_PATH = re.compile(r"^/api/companies/(?P<slug>[a-z0-9][a-z0-9-]{0,120})"
                          r"(?:/(?P<action>resolve|identity))?$")


def run_route(path):
    """(run_id, action) or (None, None) when this is not a run path."""
    match = RUN_PATH.fullmatch(path)
    if not match:
        return None, None
    return match.group("id"), match.group("action")


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
            if record is None:
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
                    return self._send(404, json.dumps(
                        {"error": "this run has no verification yet", "phase": record["phase"]}))
                except (OSError, ValueError):
                    return self._send(500, json.dumps({"error": "verification is unreadable"}))
                return self._send(200, json.dumps(payload, ensure_ascii=False))
            return self._send(200, json.dumps(record, ensure_ascii=False))

        pdf_match = PDF_PATH.fullmatch(parts.path)
        if pdf_match:
            record = runs.get(pdf_match.group("id"))
            if record is None:
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
        known = parts.path in ("/api/update", "/api/fetch", "/api/runs", "/api/companies",
                               "/api/companies/resolve-all") or \
            (run_id and action in ("approve", "cancel", "kill", "compile", "restore", "retry"))
        known = known or bool(company_match and company_match.group("action") in
                              ("resolve", "identity"))
        if not known or not self._authed(parse_qs(parts.query)):
            # The token is what stops any web page you happen to have open from
            # POSTing to localhost - and for /api/fetch that means it is what
            # stops a web page from making your machine crawl five vendors. For
            # /api/runs it is what stops one from spending money on model calls.
            return self._send(403, json.dumps({"error": "forbidden"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "bad json"}))

        if not isinstance(payload, dict):
            payload = {}

        if parts.path == "/api/fetch":
            code, body = start_fetch(payload)
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

        if parts.path == "/api/companies/resolve-all":
            try:
                body = companies.resolve_all(payload)
            except companies.CompanyError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if company_match:
            try:
                if company_match.group("action") == "resolve":
                    body = companies.resolve(company_match.group("slug"), payload)
                else:
                    body = companies.identity(company_match.group("slug"), payload)
            except companies.CompanyError as exc:
                return self._send(exc.status, json.dumps({"error": str(exc)}))
            return self._send(200, json.dumps(body, ensure_ascii=False))

        if run_id:
            supervisor = runs.supervisor()
            if action == "approve":
                # Compare-and-set: the client sends the phase it last rendered,
                # so two rapid clicks buy one pass B rather than two.
                code, body = supervisor.approve(run_id, payload.get("phase"),
                                                payload.get("base_cv"))
            elif action == "cancel":
                code, body = supervisor.cancel(run_id)
            elif action == "compile":
                code, body = supervisor.compile(run_id)
            elif action == "restore":
                code, body = supervisor.restore(run_id)
            elif action == "retry":
                code, body = supervisor.retry(run_id)
            else:
                code, body = supervisor.kill(run_id)
            return self._send(code, json.dumps(body, ensure_ascii=False))

        url = payload.get("url")
        # Read-modify-write under the same lock the background fetch takes, so a
        # status set while a fetch is in flight cannot be reverted by the fetch's
        # copy of the board (or the other way round).
        with jobs_md.board_lock():
            seen = state.load()
            if url not in seen:
                return self._send(404, json.dumps({"error": "unknown job"}))
            why = []
            if "status" in payload:
                status = payload["status"]
                if status not in state.LOCK_STATUSES:
                    return self._send(400, json.dumps({"error": "bad status"}))
                seen[url]["user_status"] = status
                why.append("%s -> %s" % (seen[url].get("company") or "job", status))
            if "note" in payload:
                seen[url]["user_note"] = str(payload["note"])[:500]
                why.append("note on %s" % (seen[url].get("company") or "job"))
            state.save(seen, "; ".join(why))
        self._send(200, json.dumps({"ok": True}))

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
    args = ap.parse_args(argv)

    if not jobs_md.SEEN.exists():
        print("no job_scraper/seen_jobs.json yet - run /scrape first")
        return 1

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

    url = "http://127.0.0.1:%d/?t=%s" % (args.port, TOKEN)
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
    activity.emit("server", "board listening on 127.0.0.1:%d" % args.port)
    # flush: stdout is block-buffered when piped, and the URL carries the only copy
    # of the session token - a user redirecting the output must still be able to see it.
    print("job board: %s" % url, flush=True)
    print("(localhost only, token-protected; Ctrl-C to stop)", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
