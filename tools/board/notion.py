"""Two-way, field-owned sync between the repo and the owner's Notion database.

Notion is the source of truth for the application lifecycle. The repo owns only
what it produces: the job's identity (title, company, posting URL, country,
source) at the moment documents are published. Every field has exactly one
writer, so the two sides never conflict:

    push (repo -> Notion)   on publish: create the row, or fill its *empty*
                            identity fields. Never touches Stage once set.
    pull (Notion -> repo)   Stage is copied into `job_search_tracker.csv`, which
                            becomes a local cache that /scrape, /rank and
                            /outcome keep reading. While reading, the derivable
                            Notion fields the owner would otherwise type by hand
                            (Application Date, First Response Date, Furthest
                            Stage, Follow-up Date) are filled - only when empty.
    documents (repo -> Notion) on push *and* pull: the compiled CV and cover
                            letter PDFs (`<dir>/build/<stem>.pdf` beside each
                            tracker .tex) are uploaded into the "CV" and
                            "Cover Letter" files properties, created on first
                            use. The repo owns these two fields: an upload's
                            name carries the PDF's short hash, so a rebuilt PDF
                            replaces the stale copy and an unchanged one is
                            never re-sent.

Silently optional: with no token or data source configured every entry point is
a no-op. Configuration lives in the gitignored `job_scraper/notion_sync.json`
({"token": "...", "data_source_id": "..."}); NOTION_TOKEN overrides the token.
Standard library only, like the rest of the board.
"""

import csv
import hashlib
import json
import os
import uuid
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from . import activity, run_registry

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
TIMEOUT = 20
FOLLOW_UP_DAYS = 10
PULL_EVERY = 600            # seconds between background pulls while the board runs
AUTO_PREFIX = "Apply - drafted"
DOC_PROPS = {"cv": "CV", "cover": "Cover Letter"}   # tracker target -> files property

# Notion Stage -> tracker status (the /outcome vocabulary). Interested and an
# empty Stage map to nothing: the repo already records drafts itself.
STAGE_STATUS = {
    "Applied": "applied", "OA": "interview", "Phone Screen": "interview",
    "Onsite": "interview", "Offer": "offer", "Rejected": "rejected",
    "Withdrawn": "withdrawn",
}
RESPONSE_STAGES = {"OA", "Phone Screen", "Onsite", "Offer", "Rejected"}
FURTHEST_RANK = {"OA": 1, "Phone Screen": 2, "Onsite": 3, "Offer": 4}

COUNTRY_WORDS = (
    ("🇨🇭 CH", ("switzerland", "schweiz", "suisse", "zurich", "zürich", "zug", "baden",
               "winterthur", "lucerne", "luzern", "basel", "bern", "geneva", "genève",
               "lausanne", "st. gallen", "lugano", ", ch")),
    ("🇩🇪 DE", ("germany", "deutschland", "berlin", "munich", "münchen", "hamburg",
               "frankfurt", "stuttgart", "cologne", "köln", ", de")),
    ("🇦🇹 AT", ("austria", "österreich", "vienna", "wien", "graz", "linz", ", at")),
    ("🇳🇱 NL", ("netherlands", "amsterdam", "rotterdam", "utrecht", "eindhoven")),
    ("🇫🇷 FR", ("france", "paris", "lyon")),
    ("🇬🇧 UK", ("united kingdom", "london", "england", "cambridge, uk", ", uk")),
    ("🇮🇪 IE", ("ireland", "dublin")),
)
COMPANY_SITE_HINTS = ("greenhouse", "lever", "workday", "ashby", "smartrecruiters",
                      "workable", "personio", "successfactors", "recruitee", "teamtailor",
                      "ats", "career", "company", "direct")


class NotionError(Exception):
    pass


# -- configuration -----------------------------------------------------------

def config_path():
    # Resolved per call: the test harness points run_registry.ROOT at a temp
    # home, which has no config, so tests can never reach the owner's workspace.
    return run_registry.ROOT / "job_scraper" / "notion_sync.json"


def load_config():
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    token = os.environ.get("NOTION_TOKEN") or data.get("token")
    source = data.get("data_source_id")
    if not token or not source:
        return None
    return {"token": token, "data_source_id": source}


def enabled():
    return load_config() is not None


# -- HTTP ----------------------------------------------------------------------

def _call(cfg, method, path, body=None):
    request = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": "Bearer " + cfg["token"],
                 "Notion-Version": VERSION, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise NotionError("HTTP %s on %s %s: %s" % (exc.code, method, path, detail))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NotionError("%s %s failed: %s" % (method, path, exc))


def query_all(cfg):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = _call(cfg, "POST", "/data_sources/%s/query" % cfg["data_source_id"], body)
        pages.extend(data.get("results") or [])
        if not data.get("has_more"):
            return pages
        cursor = data.get("next_cursor")


def _upload(cfg, path, name):
    """Upload one PDF through the File Upload API; returns the file_upload id."""
    created = _call(cfg, "POST", "/file_uploads", {
        "mode": "single_part", "filename": name, "content_type": "application/pdf"})
    boundary = uuid.uuid4().hex
    body = b"".join((
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
         "Content-Type: application/pdf\r\n\r\n" % (boundary, name)).encode("utf-8"),
        path.read_bytes(),
        ("\r\n--%s--\r\n" % boundary).encode("utf-8")))
    request = urllib.request.Request(
        "%s/file_uploads/%s/send" % (API, created["id"]), method="POST", data=body,
        headers={"Authorization": "Bearer " + cfg["token"], "Notion-Version": VERSION,
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT * 3) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise NotionError("HTTP %s uploading %s: %s" % (exc.code, name, detail))
    except (urllib.error.URLError, OSError) as exc:
        raise NotionError("uploading %s failed: %s" % (name, exc))
    return created["id"]


# -- property codecs -----------------------------------------------------------

def read(page, name):
    prop = (page.get("properties") or {}).get(name) or {}
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in prop.get(kind) or []).strip()
    if kind == "select":
        return (prop.get("select") or {}).get("name")
    if kind == "date":
        return (prop.get("date") or {}).get("start")
    if kind == "url":
        return prop.get("url")
    if kind == "files":
        return [f.get("name") for f in prop.get("files") or []]
    return None


def _title(text):
    return {"title": [{"text": {"content": text[:2000]}}]}


def _text(text):
    return {"rich_text": [{"text": {"content": text[:2000]}}]}


def _select(name):
    return {"select": {"name": name}}


def _date(iso):
    return {"date": {"start": iso}}


# -- matching and derivation ---------------------------------------------------

def norm_url(url):
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url.strip())
    return (parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")).lower()


def _fold(text):
    return " ".join((text or "").casefold().split())


def find_page(pages, url, company, role):
    target = norm_url(url)
    if target:
        for page in pages:
            if norm_url(read(page, "Job Posting URL")) == target:
                return page
    for page in pages:
        if (_fold(read(page, "Company")) == _fold(company)
                and _fold(read(page, "Position Title")) == _fold(role)):
            return page
    return None


def country_for(location):
    text = " %s " % _fold(location)
    if not text.strip():
        return None
    for option, words in COUNTRY_WORDS:
        if any(word in text for word in words):
            return option
    return "🌍 Other"


def source_for(portal, url):
    hay = _fold(portal) + " " + (url or "").lower()
    if "linkedin" in hay:
        return "LinkedIn"
    if "indeed" in hay:
        return "Indeed"
    if any(hint in hay for hint in COMPANY_SITE_HINTS):
        return "Company Website"
    return "Other" if hay.strip() else None


# -- documents -----------------------------------------------------------------

_schema_ready = set()     # data sources whose CV/Cover Letter properties exist


def _ensure_doc_properties(cfg):
    source = cfg["data_source_id"]
    if source in _schema_ready:
        return
    have = _call(cfg, "GET", "/data_sources/%s" % source).get("properties") or {}
    missing = {name: {"files": {}} for name in DOC_PROPS.values() if name not in have}
    if missing:
        _call(cfg, "PATCH", "/data_sources/%s" % source, {"properties": missing})
    _schema_ready.add(source)


def _pdf_for(tex):
    """The compiled PDF of a tracker .tex path, or None when it was never built."""
    if not tex:
        return None
    source = run_registry.ROOT / tex
    pdf = source.parent / "build" / (source.stem + ".pdf")
    return pdf if pdf.is_file() and pdf.stat().st_size else None


def _doc_props(cfg, page, targets):
    """Files properties for PDFs that are missing from, or newer than, the page."""
    props = {}
    for kind, prop in DOC_PROPS.items():
        pdf = _pdf_for((targets or {}).get(kind))
        if pdf is None:
            continue
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()[:8]
        name = "%s.%s.pdf" % (pdf.stem, digest)
        if page is not None and name in (read(page, prop) or []):
            continue
        _ensure_doc_properties(cfg)
        props[prop] = {"files": [{"type": "file_upload", "name": name,
                                  "file_upload": {"id": _upload(cfg, pdf, name)}}]}
    return props


def _sync_documents(cfg, pages):
    """Attach every tracker row's PDFs to its Notion row. Returns rows changed."""
    from . import docs     # late, as in _merge_tracker

    if not docs.TRACKER.exists():
        return 0
    with open(docs.TRACKER, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    changed = 0
    for row in rows:
        targets = {"cv": row.get("cv_file"), "cover": row.get("cover_letter_file")}
        if not any(targets.values()):
            continue
        page = find_page(pages, row.get("source"), row.get("company"), row.get("role"))
        if page is None:
            continue    # push creates rows; pull only decorates existing ones
        props = _doc_props(cfg, page, targets)
        if props:
            _call(cfg, "PATCH", "/pages/%s" % page["id"], {"properties": props})
            changed += 1
    return changed


# -- push ----------------------------------------------------------------------

def push(record, entry=None, brief=None):
    """Create or top up the Notion row for a published application.

    Returns "created", "updated", "unchanged" or None when not configured.
    Only empty properties are written on an existing row, and Stage is set only
    when it is empty - the owner's own edits always win.
    """
    cfg = load_config()
    if cfg is None:
        return None
    entry, brief = entry or {}, brief or {}
    url, company, role = record["job_url"], record["company"], record["role"]
    files = [os.path.basename(p) for p in (record.get("targets") or {}).values() if p]
    action_text = AUTO_PREFIX + (": " + ", ".join(files) if files else "")
    wanted = {
        "Position Title": (role, _title),
        "Company": (company, _text),
        "Job Posting URL": (url, lambda v: {"url": v}),
        "Country": (country_for(brief.get("location") or entry.get("location")), _select),
        "Source": (source_for(entry.get("primary_source") or entry.get("portal"), url),
                   _select),
        "Active Status": ("Active", _select),
        "Stage": ("Interested", _select),
        "Next Action": (action_text, _text),
    }
    page = find_page(query_all(cfg), url, company, role)
    documents = _doc_props(cfg, page, record.get("targets"))
    if page is None:
        props = {name: codec(value) for name, (value, codec) in wanted.items() if value}
        props.update(documents)
        _call(cfg, "POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": cfg["data_source_id"]},
            "properties": props})
        return "created"
    stage = read(page, "Stage")
    props = {}
    for name, (value, codec) in wanted.items():
        if not value or read(page, name):
            continue
        if name == "Next Action" and stage not in (None, "Interested"):
            continue
        props[name] = codec(value)
    props.update(documents)
    if not props:
        return "unchanged"
    _call(cfg, "PATCH", "/pages/%s" % page["id"], {"properties": props})
    return "updated"


def push_async(record, entry=None, brief=None):
    """Fire-and-forget push for the publish stage: never blocks or fails a run."""
    if not enabled():
        return

    def work():
        try:
            action = push(record, entry, brief)
            activity.emit("board", "notion: %s %s - %s"
                          % (action, record["company"], record["role"]),
                          run_id=record.get("id"))
        except Exception as exc:  # noqa: BLE001 - a sync failure must stay a log line
            activity.emit("board", "notion push failed: %s" % exc, level="warn",
                          run_id=record.get("id"))

    threading.Thread(target=work, daemon=True, name="notion-push").start()


def mark_applied(record):
    """The owner pressed "Mark applied": Stage Interested -> Applied, now.

    The one repo write to Stage, and only forward from Interested or empty - a
    Stage the owner already moved on in Notion is returned untouched. The
    derived fields (Application Date today, Follow-up Date, clearing the
    automatic Next Action) are written in the same request rather than left to
    the next pull. Returns the row's Stage afterwards, or None when not
    configured.
    """
    cfg = load_config()
    if cfg is None:
        return None
    page = find_page(query_all(cfg), record["job_url"], record["company"], record["role"])
    if page is None:
        push(record)
        page = find_page(query_all(cfg), record["job_url"], record["company"],
                         record["role"])
        if page is None:
            raise NotionError("the Notion row for %s - %s could not be created"
                              % (record["company"], record["role"]))
    stage = read(page, "Stage")
    if stage not in (None, "Interested"):
        return stage
    today = date.today().isoformat()
    props = {"Stage": _select("Applied")}
    if not read(page, "Application Date"):
        props["Application Date"] = _date(today)
    view = {"last_edited_time": today, "properties": dict(page.get("properties") or {})}
    for name, value in props.items():
        kind = next(iter(value))
        view["properties"][name] = {"type": kind, kind: value[kind]}
    props.update(_derive(view, today))
    _call(cfg, "PATCH", "/pages/%s" % page["id"], {"properties": props})
    return "Applied"


# -- pull ----------------------------------------------------------------------

def _derive(page, today):
    """Fill the empty Notion fields that follow from Stage. Returns properties."""
    stage = read(page, "Stage")
    if stage in (None, "Interested"):
        return {}
    edited = (page.get("last_edited_time") or "")[:10] or today
    props = {}
    applied = read(page, "Application Date")
    if not applied:
        applied = edited
        props["Application Date"] = _date(applied)
    if stage == "Applied" and not read(page, "Follow-up Date"):
        due = date.fromisoformat(applied[:10]) + timedelta(days=FOLLOW_UP_DAYS)
        props["Follow-up Date"] = _date(due.isoformat())
    if (stage in RESPONSE_STAGES and not read(page, "First Response Date")
            and read(page, "Outcome Reason") != "No response"):
        props["First Response Date"] = _date(max(edited, applied[:10]))
    furthest = read(page, "Furthest Stage")
    if FURTHEST_RANK.get(stage, 0) > FURTHEST_RANK.get(furthest, 0):
        props["Furthest Stage"] = _select(stage)
    elif stage == "Rejected" and not furthest:
        props["Furthest Stage"] = _select("Screened Out")
    if stage == "Withdrawn" and not read(page, "Outcome Reason"):
        props["Outcome Reason"] = _select("Withdrew by me")
    if (read(page, "Next Action") or "").startswith(AUTO_PREFIX):
        props["Next Action"] = {"rich_text": []}
    return props


def _tracker_status(page):
    status = STAGE_STATUS.get(read(page, "Stage"))
    if status == "rejected" and read(page, "Outcome Reason") == "No response":
        return "no_response"
    return status


def _merge_tracker(pages):
    from . import docs     # late: docs imports this module's siblings at load

    changed = 0
    with docs._tracker_lock():
        header, rows = list(docs.CANONICAL_HEADER), []
        if docs.TRACKER.exists():
            with open(docs.TRACKER, newline="", encoding="utf-8") as handle:
                raw = list(csv.reader(handle))
            if raw:
                header, rows = raw[0], raw[1:]
        ix = {name: i for i, name in enumerate(header)}
        if any(name not in ix for name in docs.CANONICAL_HEADER):
            raise NotionError("tracker header is missing canonical columns")
        rows = [row + [""] * (len(header) - len(row)) for row in rows]
        for page in pages:
            status = _tracker_status(page)
            if not status:
                continue
            url, company = read(page, "Job Posting URL"), read(page, "Company") or ""
            role = read(page, "Position Title") or ""
            applied = (read(page, "Application Date") or "")[:10]
            target = norm_url(url)
            match = None
            for row in reversed(rows):
                if ((target and norm_url(row[ix["source"]]) == target)
                        or (_fold(row[ix["company"]]) == _fold(company)
                            and _fold(row[ix["role"]]) == _fold(role))):
                    match = row
                    break
            if match is None:
                row = [""] * len(header)
                row[ix["date"]] = applied or date.today().isoformat()
                row[ix["company"]], row[ix["role"]] = company, role
                row[ix["status"]], row[ix["source"]] = status, url or ""
                row[ix["channel"]] = read(page, "Source") or ""
                row[ix["notes"]] = "from Notion"
                rows.append(row)
                changed += 1
            elif match[ix["status"]] != status:
                if match[ix["status"]] == "drafted" and applied:
                    match[ix["date"]] = applied   # tracker date = date applied
                match[ix["status"]] = status
                changed += 1
        if changed:
            tmp = docs.TRACKER.with_suffix(".csv.tmp")
            with open(tmp, "w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerows([header] + rows)
            os.replace(tmp, docs.TRACKER)
    return changed


def pull():
    """Mirror Notion Stage into the tracker and fill derivable Notion fields.

    Also attaches the tracker rows' compiled PDFs (see "documents" above).
    Returns {"pages", "derived", "documents", "tracker"} counts, or None when
    not configured.
    """
    cfg = load_config()
    if cfg is None:
        return None
    today = date.today().isoformat()
    pages = query_all(cfg)
    derived = 0
    for page in pages:
        props = _derive(page, today)
        if props:
            _call(cfg, "PATCH", "/pages/%s" % page["id"], {"properties": props})
            for name, value in props.items():   # keep the cached copy current
                page["properties"].setdefault(name, {})
                kind = next(iter(value))
                page["properties"][name].update({"type": kind, kind: value[kind]})
            derived += 1
    return {"pages": len(pages), "derived": derived,
            "documents": _sync_documents(cfg, pages), "tracker": _merge_tracker(pages)}


def start_background_pull():
    """Pull now and every PULL_EVERY seconds while the board runs."""
    if not enabled():
        return

    def loop():
        stop = threading.Event()
        while not stop.is_set():
            try:
                result = pull()
                if result and (result["derived"] or result["documents"]
                               or result["tracker"]):
                    activity.emit("board", "notion pull: %d rows filled in Notion, "
                                  "%d rows got new PDFs, %d tracker rows updated"
                                  % (result["derived"], result["documents"],
                                     result["tracker"]))
            except Exception as exc:  # noqa: BLE001
                activity.emit("board", "notion pull failed: %s" % exc, level="warn")
            stop.wait(PULL_EVERY)

    threading.Thread(target=loop, daemon=True, name="notion-pull").start()
