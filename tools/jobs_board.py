#!/usr/bin/env python3
"""A local triage board for scraped jobs. Dense table, keyboard-driven, no dependencies.

    python3 tools/jobs_board.py            # opens http://127.0.0.1:8765/?t=<token>
    python3 tools/jobs_board.py --port 9000 --no-open

Reads `job_scraper/seen_jobs.json` and writes every change straight back to it,
then regenerates `jobs.md` and the two CSV exports through tools/jobs_md.py - so
the board, the markdown and the spreadsheets can never drift apart.

**Fetch new jobs** runs every enabled source once, on demand, in a background
thread: `POST /api/fetch` starts it, `GET /api/fetch/status` reports progress.
(The plan called this route `/api/ats/fetch`; the button drives freehire and
LinkedIn as well, so the ATS-specific name would have been wrong.) Collection
itself lives in `tools/fetch_jobs.py` and works the same from a terminal.

Security: binds 127.0.0.1 only, and every API call must carry a random token
minted at startup. Without the token any web page you happen to have open could
POST to localhost and silently rewrite your job list - or, now, make your machine
crawl five vendors.

Stdlib only, Python 3.9+.
"""

import argparse
import json
import secrets
import sys
import threading
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_jobs  # noqa: E402
import jobs_md  # noqa: E402

TOKEN = secrets.token_urlsafe(16)
LOCK_STATUSES = set(jobs_md.STATUSES)

# The page is a constant baked into this process at import time, so editing this
# file has no effect on an already-running board and a browser refresh re-fetches
# the same stale HTML. That is invisible and genuinely confusing - you press a new
# shortcut, nothing happens, and the obvious fix (refresh) is the wrong one. So the
# page carries its build time, and a run whose source has since changed says so.
SOURCE = Path(__file__).resolve()
START_MTIME = SOURCE.stat().st_mtime
BUILD_LABEL = datetime.fromtimestamp(START_MTIME).strftime("%b %d %H:%M")

STALE_BANNER = (
    '<div style="background:#b4462f;color:#fff;padding:8px 14px;font-size:13px;'
    'position:sticky;top:0;z-index:9">'
    '<b>This page is from an older build.</b> tools/jobs_board.py changed after this '
    'server started, so the running process is still serving the old page - refreshing '
    'will not pick it up. Stop the server (Ctrl-C) and start it again.</div>')


def source_changed():
    try:
        return SOURCE.stat().st_mtime > START_MTIME
    except OSError:
        return False


def load():
    if not jobs_md.SEEN.exists():
        return {}
    return json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})


def save(seen):
    # Atomic: this file is the only copy of every status and note you have set,
    # and a half-written one loses all of it.
    jobs_md.save_seen(seen)
    jobs_md.MD.write_text(jobs_md.render(seen), encoding="utf-8")
    jobs_md.write_csv(seen)


# The link to click: the first-party ATS posting when the row has one. Shared
# with jobs_md so the board, jobs.md and the CSV exports all point at the same
# place.
primary_url = jobs_md.primary_url


def jobs_payload():
    seen = load()
    rows = []
    for url, e in seen.items():
        why = e.get("note", "")
        # A collector cannot fill `fit`, so a freshly fetched row would otherwise
        # arrive with no visible reason for its position. The prefit reasons are
        # that reason, and they are shown as such - never as a fit assessment.
        if not why and e.get("prefit_reasons"):
            why = "PREFIT: " + "; ".join(e["prefit_reasons"])
        rows.append({
            "url": url,
            "open_url": primary_url(e),
            "status": e.get("user_status", "new"),
            "fit": (e.get("fit") or "").lower(),
            "score": jobs_md.priority_score(e),
            "ranked": isinstance(e.get("rank_score"), (int, float)),
            "title": e.get("title", ""),
            "company": e.get("company", ""),
            "location": e.get("location", ""),
            "posted": e.get("posted") or e.get("first_seen", ""),
            "why": why,
            "note": e.get("user_note", ""),
            "portal": e.get("portal", ""),
            "dupes": e.get("possible_duplicate_of") or [],
        })
    # Your status first, then display priority (rank_score > prefit_score > fit),
    # then newest. Same order as jobs_md.sort_key, so the board and the markdown
    # can never disagree about what is at the top.
    rows.sort(key=lambda r: (jobs_md.STATUSES.index(r["status"]) if r["status"] in LOCK_STATUSES
                             else 99,
                             -r["score"],
                             jobs_md._neg_date(r["posted"])))
    return {"jobs": rows, "statuses": jobs_md.STATUSES, "sources": list(fetch_jobs.SOURCES),
            "detail_ceiling": fetch_jobs.LINKEDIN_DETAIL_CEILING}


# The background fetch. One at a time, enforced by fetch_jobs' own lock file as
# well as this flag - the flag is for a fast answer to the second click, the lock
# is what actually holds when two processes race.
FETCH = {"running": False, "log": [], "sources": [], "error": None, "finished_at": None}
FETCH_LOCK = threading.Lock()

# Every read-modify-write of seen_jobs.json goes through jobs_md.board_lock() -
# an OS-level lock, not just an in-process one. A fetch runs for a minute or two
# in a background thread while you keep pressing `s` and `n` in the table, and
# `python3 tools/fetch_jobs.py` in a terminal is a third writer. The fetch does
# its network half outside the lock and only takes it to merge and write.


def fetch_worker(sources, max_companies, max_new_jobs, detail_fetches):
    lines = []
    try:
        summaries, lines = fetch_jobs.fetch(sources, max_companies, max_new_jobs,
                                            detail_fetches, False, lines)
        with FETCH_LOCK:
            FETCH["sources"] = summaries
            FETCH["error"] = None
    except fetch_jobs.AlreadyRunning as exc:
        with FETCH_LOCK:
            FETCH["error"] = str(exc)
    except Exception as exc:  # a crashed fetch must not leave the UI spinning
        traceback.print_exc()
        with FETCH_LOCK:
            FETCH["error"] = "%s: %s" % (type(exc).__name__, exc)
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

        max_companies = bounded("max_companies", fetch_jobs.ATS_COMPANY_DEFAULT,
                                1, fetch_jobs.ATS_COMPANY_CEILING)
        max_new_jobs = bounded("max_new_jobs", fetch_jobs.ATS_NEW_JOBS_DEFAULT,
                               1, fetch_jobs.ATS_NEW_JOBS_CEILING)
        detail_fetches = bounded("linkedin_detail_fetches", fetch_jobs.LINKEDIN_DETAIL_DEFAULT,
                                 0, fetch_jobs.LINKEDIN_DETAIL_CEILING)
        FETCH.update({"running": True, "log": [], "sources": [], "error": None, "finished_at": None})
    threading.Thread(target=fetch_worker, daemon=True,
                     args=(sources, max_companies, max_new_jobs, detail_fetches)).start()
    return 202, {"started": True, "sources": sources, "max_companies": max_companies,
                 "max_new_jobs": max_new_jobs, "linkedin_detail_fetches": detail_fetches}


def fetch_status():
    with FETCH_LOCK:
        state = dict(FETCH)
    # While a run is in flight the worker's own status file is fresher than the
    # in-process copy, because fetch_jobs writes it after every source.
    on_disk = {}
    if fetch_jobs.STATUS.exists():
        try:
            on_disk = json.loads(fetch_jobs.STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            on_disk = {}
    if state["running"] and on_disk.get("log"):
        state["log"] = on_disk["log"]
        state["sources"] = on_disk.get("sources") or state["sources"]
    state["degraded"] = any(s.get("degraded") for s in state["sources"] or [])
    return state


class Handler(BaseHTTPRequestHandler):
    server_version = "jobs-board"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _authed(self, query):
        return query.get("t", [""])[0] == TOKEN

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parts = urlparse(self.path)
        query = parse_qs(parts.query)
        if parts.path == "/":
            if not self._authed(query):
                return self._send(403, "Missing or wrong token. Use the URL printed in the "
                                       "terminal.", "text/plain; charset=utf-8")
            page = PAGE.replace("__TOKEN__", TOKEN).replace("__BUILD__", BUILD_LABEL)
            if source_changed():
                page = page.replace("<body>", "<body>" + STALE_BANNER, 1)
            return self._send(200, page, "text/html; charset=utf-8")
        if parts.path == "/api/jobs":
            if not self._authed(query):
                return self._send(403, json.dumps({"error": "forbidden"}))
            return self._send(200, json.dumps(jobs_payload(), ensure_ascii=False))
        if parts.path == "/api/fetch/status":
            if not self._authed(query):
                return self._send(403, json.dumps({"error": "forbidden"}))
            return self._send(200, json.dumps(fetch_status(), ensure_ascii=False))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        parts = urlparse(self.path)
        if parts.path not in ("/api/update", "/api/fetch") or not self._authed(parse_qs(parts.query)):
            # The token is what stops any web page you happen to have open from
            # POSTing to localhost - and for /api/fetch that means it is what
            # stops a web page from making your machine crawl five vendors.
            return self._send(403, json.dumps({"error": "forbidden"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "bad json"}))

        if parts.path == "/api/fetch":
            code, body = start_fetch(payload if isinstance(payload, dict) else {})
            return self._send(code, json.dumps(body, ensure_ascii=False))

        url = payload.get("url")
        # Read-modify-write under the same lock the background fetch takes, so a
        # status set while a fetch is in flight cannot be reverted by the fetch's
        # copy of the board (or the other way round).
        with jobs_md.board_lock():
            seen = load()
            if url not in seen:
                return self._send(404, json.dumps({"error": "unknown job"}))
            if "status" in payload:
                status = payload["status"]
                if status not in LOCK_STATUSES:
                    return self._send(400, json.dumps({"error": "bad status"}))
                seen[url]["user_status"] = status
            if "note" in payload:
                seen[url]["user_note"] = str(payload["note"])[:500]
            save(seen)
        self._send(200, json.dumps({"ok": True}))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Board</title>
<style>
:root{
  --bg:#fbfbfa; --panel:#fff; --line:#e4e2dd; --text:#22201c; --dim:#6f6b64;
  --sel:#eef3fb; --selbar:#3b6fd4;
  --high:#1a7f4b; --medium:#9a6a12; --low:#8a8580;
  --chip:#f1efeb;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#16171a; --panel:#1d1f23; --line:#2e3137; --text:#e6e4e0; --dim:#9a958c;
  --sel:#22293a; --selbar:#5b8ef0;
  --high:#5fcf95; --medium:#e0b25c; --low:#8a8580; --chip:#262a30;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:13px/1.45 ui-sans-serif,-apple-system,"SF Pro Text",Inter,system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--panel);
  border-bottom:1px solid var(--line);padding:10px 14px}
.row1{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h1{font-size:14px;margin:0 8px 0 0;font-weight:650;letter-spacing:-.01em}
.chips{display:flex;gap:5px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--chip);color:var(--text);
  border-radius:999px;padding:3px 10px;cursor:pointer;font-size:12px}
.chip.on{background:var(--selbar);border-color:var(--selbar);color:#fff}
.chip .n{opacity:.65;margin-left:4px}
input[type=search]{flex:1;min-width:160px;background:var(--bg);color:var(--text);
  border:1px solid var(--line);border-radius:6px;padding:5px 9px;font:inherit}
table{width:100%;border-collapse:collapse}
th{position:sticky;top:0;text-align:left;font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;color:var(--dim);font-weight:600;padding:7px 8px;
  background:var(--bg);border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr.sel td{background:var(--sel)}
tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--selbar)}
.fit{font-weight:650;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.fit.high{color:var(--high)} .fit.medium{color:var(--medium)} .fit.low{color:var(--low)}
.role{font-weight:600}
.co{color:var(--dim)}
.why{color:var(--dim);font-size:12px;max-width:38ch;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.note{font-size:12px;max-width:24ch;color:var(--text)}
.note:empty::before{content:"+ note";color:var(--dim);opacity:.5}
.note{cursor:text}
select{background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:5px;padding:2px 4px;font:inherit;font-size:12px}
a.open{color:var(--selbar);text-decoration:none;font-size:12px;white-space:nowrap}
a.open:hover{text-decoration:underline}
footer{position:sticky;bottom:0;background:var(--panel);border-top:1px solid var(--line);
  padding:7px 14px;color:var(--dim);font-size:12px;display:flex;gap:14px;flex-wrap:wrap}
kbd{background:var(--chip);border:1px solid var(--line);border-radius:4px;
  padding:0 4px;font:inherit;font-size:11px}
#toast{position:fixed;right:14px;bottom:46px;background:var(--selbar);color:#fff;
  padding:7px 12px;border-radius:6px;opacity:0;pointer-events:none;
  transition:opacity .18s;font-size:12px;display:flex;align-items:center;gap:10px}
#toast.on{opacity:1;pointer-events:auto}
#toast.warn{background:#b4462f}
#toastundo{background:rgba(255,255,255,.18);color:#fff;border:1px solid rgba(255,255,255,.45);
  border-radius:5px;padding:2px 8px;font:inherit;font-size:12px;cursor:pointer}
#toastundo:hover{background:rgba(255,255,255,.3)}
.empty{padding:40px;text-align:center;color:var(--dim)}
.row2{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px;
  padding-top:8px;border-top:1px solid var(--line)}
button.go{background:var(--selbar);color:#fff;border:1px solid var(--selbar);border-radius:6px;
  padding:5px 12px;font:inherit;font-size:12px;font-weight:600;cursor:pointer}
button.go:disabled{opacity:.55;cursor:progress}
label.src{display:inline-flex;align-items:center;gap:4px;font-size:12px;color:var(--dim);cursor:pointer}
label.src input{accent-color:var(--selbar)}
.adv{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim)}
.adv input[type=number]{width:52px;background:var(--bg);color:var(--text);
  border:1px solid var(--line);border-radius:5px;padding:2px 5px;font:inherit;font-size:12px}
#fstat{font-size:12px;color:var(--dim);flex:1;min-width:180px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#fstat.bad{color:#b4462f}
.banner{background:#8a5a12;color:#fff;padding:6px 14px;font-size:12px}
.dupe{color:var(--medium);font-size:11px;margin-left:4px}
</style></head><body>
<div class="banner" id="degraded" hidden></div>
<header>
  <div class="row1">
    <h1>Job Board</h1>
    <div class="chips" id="chips"></div>
    <input type="search" id="q" placeholder="filter by role, company, location…">
  </div>
  <div class="row2">
    <button class="go" id="fetch">Fetch new jobs</button>
    <label class="src"><input type="checkbox" id="s-ats" checked> ATS boards</label>
    <label class="src"><input type="checkbox" id="s-freehire" checked> freehire</label>
    <label class="src"><input type="checkbox" id="s-linkedin" checked> LinkedIn</label>
    <details class="adv"><summary style="cursor:pointer">advanced</summary>
      <span class="adv" style="margin-left:8px">
        companies <input type="number" id="mc" value="8" min="1" max="20">
        rows <input type="number" id="mn" value="40" min="1" max="200">
        LinkedIn details <input type="number" id="md" value="15" min="0" max="20">
      </span>
    </details>
    <span id="fstat"></span>
  </div>
</header>
<table><thead><tr>
  <th style="width:52px">Fit</th><th>Role</th><th style="width:15%">Company</th>
  <th style="width:11%">Location</th><th style="width:74px">Posted</th>
  <th style="width:96px">Status</th><th>Why</th><th style="width:16%">My notes</th>
  <th style="width:46px"></th>
</tr></thead><tbody id="tb"></tbody></table>
<div class="empty" id="empty" hidden>Nothing here.</div>
<footer>
  <span><kbd>j</kbd><kbd>k</kbd> move</span>
  <span><kbd>s</kbd> star <kbd>y</kbd> yes <kbd>m</kbd> maybe <kbd>n</kbd> no <kbd>g</kbd> gate <kbd>a</kbd> applied <kbd>u</kbd> new</span>
  <span><kbd>e</kbd> note</span><span><kbd>z</kbd> undo</span><span><kbd>enter</kbd> open posting</span>
  <span><kbd>/</kbd> search</span><span id="count"></span>
  <span style="margin-left:auto;opacity:.6">build __BUILD__</span>
</footer>
<div id="toast"><span id="toastmsg"></span><button id="toastundo" hidden>Undo <span id="toastsec"></span></button></div>
<script>
const T="__TOKEN__";
let JOBS=[], STATUSES=[], FILTERS=[], filter="active", q="", sel=0;
const ACTIVE=["star","yes","new","maybe"];
// The filter chips are the status vocabulary itself, verbatim: one chip per status
// in jobs_md.STATUSES, in the same order and spelled with the same word the row's
// dropdown shows. A chip with a prettier private name ("Excluded" for the `no`
// status, "Gated" for `gate`) reads like a separate concept - you press `n` and land
// in a bucket that never says `no` anywhere. A hand-written chip list also silently
// drops statuses added later, which is how `expired` ended up with no chip at all.
// `active` and `all` are the only extras, and they are groups, not statuses.
const buildFilters = () => [["active","active"], ...STATUSES.map(s=>[s,s]), ["all","all"]];

const esc = s => (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const el = id => document.getElementById(id);
// Status changes write through immediately - no modal, no confirm - so `n`
// (which hides a job from every future scrape) needs a way back. Undo is kept
// available after the toast fades: the toast is the nudge, `z` is the guarantee.
const HISTORY=[];
let toastTimer=null, undoTimer=null;

function toast(msg, opts){
  opts = opts || {};
  const t=el("toast"), btn=el("toastundo");
  el("toastmsg").textContent=msg;
  clearTimeout(toastTimer); clearInterval(undoTimer);
  t.classList.toggle("warn", !!opts.warn);
  t.classList.add("on");
  if(opts.undo){
    let left=opts.seconds||6;
    btn.hidden=false; el("toastsec").textContent="("+left+")";
    undoTimer=setInterval(()=>{ left--; el("toastsec").textContent="("+left+")";
      if(left<=0) clearInterval(undoTimer); },1000);
    toastTimer=setTimeout(()=>{ t.classList.remove("on"); btn.hidden=true; },(opts.seconds||6)*1000);
  } else {
    btn.hidden=true;
    toastTimer=setTimeout(()=>t.classList.remove("on"), opts.ms||1200);
  }
}

function match(j){
  const inFilter = filter==="all" ? true
    : filter==="active" ? ACTIVE.includes(j.status) : j.status===filter;
  if(!inFilter) return false;
  if(!q) return true;
  const hay=(j.title+" "+j.company+" "+j.location+" "+j.why+" "+j.note).toLowerCase();
  return q.toLowerCase().split(/\s+/).every(w=>hay.includes(w));
}
const shown = () => JOBS.filter(match);

function renderChips(){
  const counts={all:JOBS.length, active:JOBS.filter(j=>ACTIVE.includes(j.status)).length};
  for(const s of STATUSES) counts[s]=JOBS.filter(j=>j.status===s).length;
  document.getElementById("chips").innerHTML = FILTERS.map(([k,label])=>
    `<button class="chip ${k===filter?"on":""}" data-f="${k}">${label}<span class="n">${counts[k]||0}</span></button>`
  ).join("");
}

function render(){
  renderChips();
  const rows=shown();
  if(sel>=rows.length) sel=Math.max(0,rows.length-1);
  document.getElementById("tb").innerHTML = rows.map((j,i)=>`
    <tr class="${i===sel?"sel":""}" data-i="${i}">
      <td><span class="fit ${j.fit}">${esc(j.fit)}</span></td>
      <td class="role">${esc(j.title)}</td>
      <td class="co">${esc(j.company)}</td>
      <td class="co">${esc(j.location)}</td>
      <td class="co">${esc(j.posted).slice(5)}</td>
      <td><select data-url="${esc(j.url)}">${STATUSES.map(s=>
          `<option value="${s}" ${s===j.status?"selected":""}>${s}</option>`).join("")}</select></td>
      <td class="why" title="${esc(j.why)}">${esc(j.why)}${j.dupes && j.dupes.length
          ? `<span class="dupe" title="possible duplicate of: ${esc(j.dupes.join(", "))}">· possible dupe</span>` : ""}</td>
      <td class="note" data-url="${esc(j.url)}" title="click to edit">${esc(j.note)}</td>
      <td><a class="open" href="${esc(j.open_url || j.url)}" target="_blank" rel="noopener">open ↗</a></td>
    </tr>`).join("");
  document.getElementById("empty").hidden = rows.length>0;
  document.getElementById("count").textContent = `${rows.length} shown · ${JOBS.length} total`;
  const s=document.querySelector("tr.sel"); if(s) s.scrollIntoView({block:"nearest"});
}

async function update(url, patch, record){
  const j = JOBS.find(x=>x.url===url);
  const prev = j ? j.status : null;
  const r = await fetch("/api/update?t="+T, {method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify({url, ...patch})});
  if(!r.ok){ toast("save failed", {warn:true, ms:2500}); return false; }
  if("status" in patch){
    if(record!==false && patch.status!==prev)
      HISTORY.push({url, prev, title:j.title, company:j.company});
    j.status=patch.status;
  }
  if("note" in patch) j.note=patch.note;
  render();
  return true;
}

async function setStatus(s){
  const j=shown()[sel];
  if(!j) return;
  const label = (j.company||j.title||"job");
  const wasStatus = j.status;
  if(!await update(j.url,{status:s})) return;
  if(s==="no")
    // The only status that removes a job from every future scrape.
    toast(label+" -> no - it will not come back in a scrape", {undo:true, seconds:8, warn:true});
  else if(s!==wasStatus)
    toast(label+" -> "+s);
}

async function undo(){
  const last = HISTORY.pop();
  if(!last){ toast("nothing to undo", {ms:1400}); return; }
  if(await update(last.url,{status:last.prev}, false))
    toast("undone: "+(last.company||last.title)+" back to "+last.prev);
}
function editNote(){ const j=shown()[sel]; if(!j) return;
  const v=prompt("Note for "+j.company+" — "+j.title, j.note||"");
  if(v!==null) update(j.url,{note:v}).then(ok=>{ if(ok) toast("note saved"); }); }

document.addEventListener("click", e=>{
  if(e.target.closest("#toastundo")){ undo(); return; }
  const chip=e.target.closest(".chip");
  if(chip){ filter=chip.dataset.f; sel=0; render(); return; }
  const note=e.target.closest(".note");
  if(note){ const j=JOBS.find(x=>x.url===note.dataset.url);
    const v=prompt("Note for "+j.company+" — "+j.title, j.note||"");
    if(v!==null) update(j.url,{note:v}).then(ok=>{ if(ok) toast("note saved"); }); return; }
  const tr=e.target.closest("tr[data-i]");
  if(tr && !e.target.closest("a")){ sel=+tr.dataset.i; render(); }
});
document.addEventListener("change", e=>{
  if(e.target.tagName==="SELECT") update(e.target.dataset.url,{status:e.target.value});
});
document.addEventListener("keydown", e=>{
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if(e.key==="/" && !typing){ e.preventDefault(); document.getElementById("q").focus(); return; }
  if(typing){ if(e.key==="Escape") e.target.blur(); return; }
  const rows=shown();
  const K={j:1,ArrowDown:1,k:-1,ArrowUp:-1};
  if(e.key in K){ e.preventDefault(); sel=Math.min(rows.length-1,Math.max(0,sel+K[e.key])); render(); return; }
  if(e.key==="Enter"){ const j=rows[sel]; if(j) window.open(j.open_url||j.url,"_blank","noopener"); return; }
  if(e.key==="e"){ e.preventDefault(); editNote(); return; }
  if(e.key==="z"){ e.preventDefault(); undo(); return; }
  const M={s:"star",y:"yes",m:"maybe",n:"no",a:"applied",u:"new",g:"gate"};
  if(e.key in M){ e.preventDefault(); setStatus(M[e.key]); }
});
document.getElementById("q").addEventListener("input", e=>{ q=e.target.value; sel=0; render(); });

// ---------------------------------------------------------------- fetching
// One button drives every source. The run happens in a background thread on the
// server and this polls a status endpoint, rather than holding an HTTP request
// open for the two minutes a five-vendor sweep can take.
let POLL=null;

const chosenSources = () =>
  ["ats","freehire","linkedin"].filter(s=>document.getElementById("s-"+s).checked);

function fstat(msg, bad){
  const el=document.getElementById("fstat");
  el.textContent=msg||""; el.classList.toggle("bad", !!bad);
}

async function reloadJobs(){
  const d = await (await fetch("/api/jobs?t="+T)).json();
  JOBS=d.jobs; STATUSES=d.statuses; FILTERS=buildFilters(); render();
}

async function pollFetch(){
  const s = await (await fetch("/api/fetch/status?t="+T)).json();
  if(s.running){
    const tail=(s.log||[]).slice(-1)[0]||"working…";
    fstat(tail.replace(/^\d{4}-\d\d-\d\d \d\d:\d\d\s+/,""));
    return;
  }
  clearInterval(POLL); POLL=null;
  document.getElementById("fetch").disabled=false;
  await reloadJobs();
  const banner=document.getElementById("degraded");
  const failed=(s.sources||[]).flatMap(x=>x.failed||[]);
  if(s.error){ fstat(s.error, true); }
  else{
    const added=(s.sources||[]).reduce((n,x)=>n+(x.added||0),0);
    const gated=(s.sources||[]).reduce((n,x)=>n+(x.gated||0),0);
    fstat(added+" new · "+gated+" auto-gated on German");
  }
  if(failed.length){
    banner.hidden=false;
    banner.textContent="Degraded run: "+failed.join(", ")+
      " did not answer. Their results are missing, everything else was kept, and they are retried first next time.";
  } else { banner.hidden=true; }
}

document.getElementById("fetch").addEventListener("click", async ()=>{
  const sources=chosenSources();
  if(!sources.length){ fstat("select at least one source", true); return; }
  const btn=document.getElementById("fetch");
  btn.disabled=true; fstat("starting…");
  const body={sources,
    max_companies:+document.getElementById("mc").value||8,
    max_new_jobs:+document.getElementById("mn").value||40,
    linkedin_detail_fetches:+document.getElementById("md").value};
  const r=await fetch("/api/fetch?t="+T,{method:"POST",
    headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  if(!r.ok){ btn.disabled=false; fstat((await r.json()).error||"could not start", true); return; }
  POLL=setInterval(pollFetch, 2000);
  pollFetch();
});

fetch("/api/jobs?t="+T).then(r=>r.json()).then(d=>{
  JOBS=d.jobs; STATUSES=d.statuses; FILTERS=buildFilters(); render();
  // A fetch started before this page loaded may still be running.
  fetch("/api/fetch/status?t="+T).then(r=>r.json()).then(s=>{
    if(s.running){ document.getElementById("fetch").disabled=true; POLL=setInterval(pollFetch,2000); }
  });
});
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Local job triage board")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    if not jobs_md.SEEN.exists():
        print("no job_scraper/seen_jobs.json yet - run /scrape first")
        return 1

    url = "http://127.0.0.1:%d/?t=%s" % (args.port, TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
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


if __name__ == "__main__":
    sys.exit(main())
