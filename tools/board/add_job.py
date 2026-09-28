"""Add job: put a posting the owner found by hand onto the board (DESIGN.md §17).

    POST /api/jobs/add  {"job_url", "apply_url"?, "title"?, "company"?,
                         "location"?, "description"?, "allow_no_description"?}

Resolution never calls a model. A LinkedIn job goes through `linkedin-search
detail` (the public guest page), an ATS posting through `ats-search detail`, and
anything else through `fetch_url` + `posting_text`. The raw LinkedIn address the
owner copies is usually a search page (`/jobs/search-results/?currentJobId=…`),
which is a login wall; only the job id is taken from it, never the page.

Writing reuses the browser importer and the merge, so dedup, the German gate,
scoring and posting sidecars behave exactly as for fetched rows. Notion is not
touched here: its row is created when documents are published.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ats_fetch  # noqa: E402
import collectors  # noqa: E402
import import_linkedin_browser as importer  # noqa: E402
import jobs_md  # noqa: E402
import posting_text  # noqa: E402
import postings  # noqa: E402

from . import fetch_url, state  # noqa: E402

PORTAL = "manual"
LINKEDIN_VIEW = re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})")
LINKEDIN_ID = re.compile(r"\d{6,}")
# Promoted to `yes` on add: the owner picked this job. Anything else - applied,
# no, expired, a German gate - is a decision already made and is left alone.
PROMOTABLE = ("new", "backlog")
MAX_TEXT = 60000


class AddError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _text(payload, key, limit=500):
    value = payload.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def _is_linkedin(url):
    host = (urlsplit(url).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def linkedin_id(url):
    """The job id in any LinkedIn job address, or None."""
    parts = urlsplit(url)
    match = LINKEDIN_VIEW.search(parts.path)
    if match:
        return match.group(1)
    for key in ("currentJobId", "jobId"):
        value = (parse_qs(parts.query).get(key) or [""])[0]
        if LINKEDIN_ID.fullmatch(value):
            return value
    return None


def _check_link(url, field):
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AddError(400, "%s must be a web address (https://…)" % field)
    return url


def classify(job_url, apply_url=""):
    """(linkedin job id or None, company posting URL or "")."""
    job_url = _check_link(job_url, "Job link")
    apply_url = _check_link(apply_url, "Company apply link")
    if not job_url:
        raise AddError(400, "Job link is required")
    job_id, company_url = None, ""
    if _is_linkedin(job_url):
        job_id = linkedin_id(job_url)
        if not job_id:
            raise AddError(400, "This LinkedIn address is not one job. Open the job itself "
                                "and copy the address bar again.")
    else:
        company_url = job_url
    if apply_url:
        if _is_linkedin(apply_url):
            raise AddError(400, "Company apply link is a LinkedIn address. Put the page "
                                "Apply takes you to, or leave it empty for Easy Apply.")
        if company_url and jobs_md.canonical_url(company_url) != jobs_md.canonical_url(apply_url):
            raise AddError(400, "Both links are company pages. Put the LinkedIn job in "
                                "Job link, or leave Company apply link empty.")
        company_url = apply_url
    return job_id, company_url


# ------------------------------------------------------------------ resolvers
# Each returns a dict with any of title, company, location, posted, deadline,
# description, url - or {} when it found nothing. Tests replace these.

def _quiet(_message):
    pass


def resolve_linkedin(job_id):
    data = collectors.bun([collectors.LINKEDIN, "detail", job_id, "--format", "json"],
                          _quiet, timeout=45)
    if not isinstance(data, dict) or not data.get("title"):
        return {}
    return {"title": data.get("title") or "", "company": data.get("company") or "",
            "location": data.get("location") or "", "posted": (data.get("date") or "")[:10],
            "description": data.get("description") or ""}


def resolve_ats(url):
    data = collectors.bun([collectors.ATS, "detail", url, "--format", "json"],
                          _quiet, timeout=45)
    if not isinstance(data, dict) or not data.get("title"):
        return {}
    places = [p.get("text") for p in data.get("locations") or [] if isinstance(p, dict)]
    return {"title": data.get("title") or "", "company": data.get("company") or "",
            "location": " / ".join(filter(None, places)),
            "posted": (data.get("date") or "")[:10], "deadline": data.get("deadline"),
            "description": data.get("description") or "", "url": data.get("url") or url}


def _json_ld_identity(html):
    """Title, company and place a page declares in its JobPosting JSON-LD."""
    for raw in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>",
                          html or "", re.I | re.S):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                kind = node.get("@type")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    org = node.get("hiringOrganization")
                    place = node.get("jobLocation")
                    place = place[0] if isinstance(place, list) and place else place
                    address = place.get("address") if isinstance(place, dict) else None
                    return {
                        "title": node.get("title") if isinstance(node.get("title"), str) else "",
                        "company": org.get("name", "") if isinstance(org, dict) else "",
                        "location": address.get("addressLocality", "")
                        if isinstance(address, dict) else "",
                    }
                stack.extend(node.get("@graph") or [])
    return {}


def resolve_page(url):
    try:
        final, status, html = fetch_url.fetch(url)
    except Exception:  # Refused, or any transport failure: the page is simply unread
        return {}
    if status != 200:
        return {}
    found = {k: v for k, v in _json_ld_identity(html).items() if v}
    extraction = posting_text.extract(html)
    if extraction.ok:
        found["description"] = extraction.text
    return found


def resolve(job_id, company_url):
    """Both sides, merged: the company posting leads, LinkedIn fills its gaps."""
    company_side = {}
    if company_url:
        if jobs_md.composite_id_for_url(company_url):
            company_side = resolve_ats(company_url)
        # Ashby, for one, does not name the employer in its API.
        if not company_side.get("description") or not company_side.get("company"):
            page = resolve_page(company_url)
            company_side = dict(page, **{k: v for k, v in company_side.items() if v})
    linkedin_side = resolve_linkedin(job_id) if job_id else {}
    merged = dict(linkedin_side)
    for key, value in company_side.items():
        if value and (key != "description" or posting_text.check(value)[0]):
            merged[key] = value
    # LinkedIn's employer name is the one linkedin-search rows carry too, so it
    # is what lets a later fetch of the same job recognise this row.
    if linkedin_side.get("company"):
        merged["company"] = linkedin_side["company"]
    if merged.get("description") and not posting_text.check(merged["description"])[0]:
        merged.pop("description")
    return merged


# ---------------------------------------------------------------------- write

def _entry_key(seen, url):
    canonical = jobs_md.canonical_url(url)
    if canonical in seen:
        return canonical
    for key, entry in seen.items():
        known = [entry.get("url")] + [s.get("url") for s in entry.get("sources") or []]
        if canonical in {jobs_md.canonical_url(u) for u in known if u}:
            return key
    return None


def _write(job_id, company_url, found, today):
    linkedin_url = "https://www.linkedin.com/jobs/view/%s" % job_id if job_id else ""
    primary = company_url or linkedin_url
    row = {"title": found["title"], "company": found["company"],
           "location": found.get("location") or "", "posted": found.get("posted") or "",
           "deadline": found.get("deadline"), "description": found.get("description") or "",
           "url": primary}
    canonical = jobs_md.canonical_url(primary)
    vendor, ats_id = importer.identify_ats(primary) if company_url else ("", "")
    company_portal = ats_fetch.PORTAL if ats_id else importer.COMPANY_PORTAL
    pending = []
    with jobs_md.board_lock():
        seen = state.load()
        key = _entry_key(seen, primary) or (linkedin_url and _entry_key(seen, linkedin_url))
        if key:
            # Already on the board under either link - fetched, or added before.
            # Everything joins that row: keys never move, so a company link
            # becomes its preferred link through `sources`, and a body the row
            # was missing is taken.
            entry = seen[key]
            ctx = ats_fetch.scoring_context(_quiet)
            if company_url:
                ats_fetch.enrich_existing(entry, dict(row, url=canonical, id=ats_id), key,
                                          today, company_portal, pending, ctx)
                entry["primary_source"] = company_portal
                if vendor:
                    entry["ats_vendor"] = vendor
            if linkedin_url:
                ats_fetch.enrich_existing(entry, dict(row, url=linkedin_url, id=job_id), key,
                                          today, PORTAL, pending, ctx)
            stats = {"already_known": 1}
        elif job_id:
            rows, _ = importer.normalize_rows({"jobs": [dict(row, linkedin_url=linkedin_url)]})
            stats = importer.import_rows(seen, rows, today, pending=pending, portal=PORTAL)
        else:
            stats = ats_fetch.merge(seen, [dict(row, id=ats_id)], today, _quiet,
                                    portal=company_portal, pending=pending)
        key = key or _entry_key(seen, primary) or (linkedin_url and _entry_key(seen, linkedin_url))
        if not key:
            raise AddError(500, "the job was merged but could not be found again")
        entry = seen[key]
        # Who put it here: a company-only add has no LinkedIn source to say so.
        ats_fetch.append_source(entry, {"portal": PORTAL, "url": canonical, "id": "",
                                        "first_seen": today})
        before = jobs_md.user_status(entry)
        if before in PROMOTABLE:
            entry["user_status"] = "yes"
        postings.commit_all(pending)
        state.save(seen, "added by hand: %s" % (entry.get("company") or "job"))
    outcome = ("added" if stats.get("added") else
               "collapsed" if stats.get("collapsed") else "already_known")
    return {"outcome": outcome, "url": key, "title": entry.get("title"),
            "company": entry.get("company"), "status": jobs_md.user_status(entry),
            "previous_status": None if outcome == "added" else before,
            "gated": jobs_md.user_status(entry) == "gate",
            "has_description": bool(entry.get("posting_path"))}


def add(payload, today=None):
    """Returns (http_status, body)."""
    try:
        job_id, company_url = classify(_text(payload, "job_url", 2000),
                                       _text(payload, "apply_url", 2000))
        found = resolve(job_id, company_url)
        # What the owner typed (or the bookmarklet carried) fills gaps only.
        for key in ("title", "company", "location"):
            if not found.get(key) and _text(payload, key):
                found[key] = _text(payload, key)
        typed = _text(payload, "description", MAX_TEXT)
        if not found.get("description") and typed:
            ok, reason = posting_text.check(typed)
            if not ok and not payload.get("allow_no_description"):
                raise AddError(422, "The pasted description cannot be used: %s." % reason,
                               need=["description"], found=found)
            if ok:
                found["description"] = typed
        need = [k for k in ("title", "company") if not found.get(k)]
        if not found.get("description") and not payload.get("allow_no_description"):
            need.append("description")
        if need:
            raise AddError(422, "Could not read %s from the link. Fill in the details."
                           % " and ".join(need), need=need, found=found)
        return 200, _write(job_id, company_url, found, today or date.today().isoformat())
    except AddError as exc:
        return exc.status, dict({"error": str(exc)}, **exc.extra)
    except importer.InputError as exc:
        return 400, {"error": str(exc)}
