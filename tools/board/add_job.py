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
import ipaddress
import re
import subprocess
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


def clean_title(value):
    """Reject browser/error banners instead of treating them as job names."""
    if not isinstance(value, str):
        return ""
    title = " ".join(value.split())
    if not title or len(title) > 200:
        return ""
    if re.search(r"(?i)internet explorer|browser.{0,40}(?:not supported|unsupported|out.of.date)|"
                 r"access denied|just a moment|page not found|enable javascript|"
                 r"checking your browser|verify you are human", title):
        return ""
    return title


def refresh_title(url):
    """Resolve a title only; the editor previews it before saving."""
    job_id, company_url = classify(url)
    found = resolve(job_id, company_url)
    title = clean_title(found.get("title"))
    if not title:
        raise AddError(422, "Could not retrieve a reliable job title. Enter it manually.")
    return title


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


class LinkedInDetailError(RuntimeError):
    """A guest-detail failure that the inbox can retry or leave for its owner."""

    def __init__(self, reason, retryable=True, rate_limited=False):
        super().__init__(reason)
        self.retryable = retryable
        self.rate_limited = rate_limited


def external_apply_url(value):
    """Validate an external link without fetching it or accepting login wrappers."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        return ""
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").lower().rstrip(".")
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            # LinkedIn's redirect wrapper sometimes retains the destination.
            if parts.path.startswith("/redir/"):
                target = (parse_qs(parts.query).get("url") or [""])[0]
                if target and target != value:
                    return external_apply_url(target) if not _is_linkedin(target) else ""
            return ""
        if (parts.scheme not in ("http", "https") or not host or "." not in host
                or parts.username or parts.password or parts.port not in (None, 80, 443)
                or host.endswith((".localhost", ".local", ".internal"))):
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (not address.is_global or address.is_multicast):
            return ""
        return jobs_md.canonical_url(value.strip())
    except ValueError:
        return ""


def _linkedin_fields(data):
    def value(key):
        found = data.get(key)
        return found.strip() if isinstance(found, str) else ""

    title = value("title")
    row = {"title": "" if title == "(untitled)" else title,
           "company": value("company"), "location": value("location"),
           "posted": value("date")[:10], "description": value("description")}
    apply_url = external_apply_url(data.get("applyUrl"))
    if apply_url:
        row["apply_url"] = apply_url
    return row


def resolve_linkedin(job_id):
    data = collectors.bun([collectors.LINKEDIN, "detail", job_id, "--format", "json"],
                          _quiet, timeout=45)
    if not isinstance(data, dict) or not data.get("title"):
        return {}
    return _linkedin_fields(data)


def resolve_linkedin_detail(job_id):
    """Same guest resolver, with structured failures for durable inbox processing."""
    if not isinstance(job_id, str) or not LINKEDIN_ID.fullmatch(job_id):
        raise LinkedInDetailError("Invalid LinkedIn job ID", retryable=False)
    try:
        collectors.linkedin_pace()
        proc = subprocess.run(["bun", "run", collectors.LINKEDIN, "detail", job_id,
                               "--format", "json"], cwd=str(jobs_md.ROOT),
                              timeout=45, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LinkedInDetailError("LinkedIn detail request unavailable or timed out") from exc
    if proc.returncode != 0:
        code = ""
        for line in (proc.stderr or "").splitlines():
            try:
                error = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(error, dict):
                code = error.get("code") or ""
                # Retain a class, never arbitrary server text or URLs in logs.
                if code == "DETAIL_FAILED" and "429" in str(error.get("error", "")):
                    code = "RATE_LIMITED"
        if code in ("BAD_ID", "NOT_FOUND"):
            raise LinkedInDetailError("Posting unavailable; open it manually", retryable=False)
        if code == "RATE_LIMITED":
            raise LinkedInDetailError("LinkedIn rate limited the detail request", rate_limited=True)
        raise LinkedInDetailError("LinkedIn detail request failed")
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise LinkedInDetailError("LinkedIn detail response was malformed") from exc
    if not isinstance(data, dict):
        raise LinkedInDetailError("LinkedIn detail response was malformed")
    row = _linkedin_fields(data)
    if not row["title"] and not row["description"]:
        raise LinkedInDetailError("LinkedIn detail response did not contain a posting")
    return row


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


# Where a posting names its place when it has no JSON-LD: a labelled field
# ("Location", "Standort", ...) in a definition list or table, a JSON key the
# page's own data carries, or a "Location: ..." line in the text.
_PLACE_LABEL = r"(?:job\s+)?locations?|standort(?:e)?|arbeitsort|einsatzort|office|lieu|place\s+of\s+work"
_PLACE_HTML = (
    re.compile(r"(?is)<(dt|th)[^>]*>\s*(?:<[^>]+>\s*)*(?:%s)\s*:?\s*(?:<[^>]+>\s*)*</\1>\s*"
               r"<(?:dd|td)[^>]*>(.*?)</(?:dd|td)>" % _PLACE_LABEL),
    re.compile(r'(?i)"(?:addressLocality|jobLocation|job_location|location(?:Name)?)"\s*:\s*"([^"]{2,120})"'),
)
_PLACE_TEXT = re.compile(r"(?im)^\s*(?:%s)\s*[:|\-–]\s*(.{2,120})$" % _PLACE_LABEL)
# Names an ATS or job board gives itself - never the employer.
_NOT_EMPLOYERS = {"greenhouse", "lever", "workday", "ashby", "smartrecruiters", "personio",
                  "recruitee", "teamtailor", "workable", "linkedin", "indeed", "join", "jobs",
                  "careers", "career", "bamboohr", "jobvite", "softgarden", "successfactors",
                  "icims", "taleo", "stepstone", "glassdoor", "welcome to the jungle", "jobs.ch",
                  "freehire", "arbeitnow", "myworkdayjobs", "breezy", "comeet", "pinpoint"}
# Hosts whose first path segment (or subdomain) is the employer's slug.
_ATS_PATH = ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io",
             "jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com", "apply.workable.com",
             "jobs.smartrecruiters.com", "careers.smartrecruiters.com", "jobs.jobvite.com",
             "join.com", "app.join.com", "jobs.personio.de", "www.comeet.com")
_ATS_SUBDOMAIN = ("recruitee.com", "personio.de", "personio.com", "teamtailor.com",
                  "bamboohr.com", "myworkdayjobs.com", "softgarden.io", "breezy.hr",
                  "pinpointhq.com", "workable.com", "factorial.co", "rippling-ats.com")
_HOST_NOISE = {"www", "jobs", "job", "careers", "career", "karriere", "apply", "recruiting",
               "join", "work", "talent", "hiring", "en", "de", "boards", "app", "stellen", "eu"}


def _clean_html(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return " ".join(text.split())[:120]


def page_location(html, text=""):
    """A posting's place from its HTML or text when there is no JSON-LD."""
    for rx in _PLACE_HTML:
        match = rx.search(html or "")
        if match:
            value = _clean_html(match.group(match.lastindex))
            if value and len(value) <= 120:
                return value
    match = _PLACE_TEXT.search(text or "")
    return " ".join(match.group(1).split()) if match else ""


def _pretty(slug):
    words = re.split(r"[-_+.]+|%20", slug or "")
    words = [w for w in words if w and not w.isdigit()]
    if not words:
        return ""
    return " ".join(w if (w.isupper() or any(c.isupper() for c in w[1:])) else w.capitalize()
                    for w in words)[:80]


def company_from_url(url):
    """The employer a job URL names: an ATS slug, or the careers site's domain."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host == "linkedin.com" or host.endswith(".linkedin.com"):
        return ""
    segments = [seg for seg in parts.path.split("/") if seg]
    if host in _ATS_PATH:
        skip = {"companies", "company", "o", "en", "de", "jobs", "j"}
        slug = next((seg for seg in segments if seg.lower() not in skip), "")
        return _pretty(slug) if slug else ""
    for suffix in _ATS_SUBDOMAIN:
        if host.endswith("." + suffix):
            label = host[:-len(suffix) - 1].split(".")[0]
            return _pretty(label) if label not in _HOST_NOISE else ""
    labels = host.split(".")
    if len(labels) < 2:
        return ""
    # careers.acme.com / acme.com/careers / jobs.acme.co.uk: the registrable name.
    core = labels[:-1]
    if len(core) >= 2 and core[-1] in ("co", "com", "org", "ac"):
        core = core[:-1]
    name = next((label for label in reversed(core) if label not in _HOST_NOISE), "")
    return _pretty(name) if name and name not in _NOT_EMPLOYERS else ""


def page_company(html):
    """The site's own name (og:site_name / application-name), unless it is an ATS."""
    for attr in ("og:site_name", "application-name"):
        match = re.search(r'(?is)<meta[^>]+(?:property|name)=["\']%s["\'][^>]*>' % re.escape(attr),
                          html or "")
        if match:
            content = re.search(r'(?is)content=["\']([^"\']{2,80})["\']', match.group(0))
            value = _clean_html(content.group(1)) if content else ""
            value = re.sub(r"(?i)\s*(?:[-|·–]\s*)?(careers?|jobs?|karriere)\b.*$", "", value).strip()
            value = re.sub(r"(?i)^(careers?|jobs?)\s+(at|bei)\s+", "", value).strip()
            if value and value.lower() not in _NOT_EMPLOYERS:
                return value
    return ""


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
    # No JSON-LD (many careers sites): the page's labelled place and own name.
    if not found.get("location"):
        place = page_location(html, found.get("description", ""))
        if place:
            found["location"] = place
    if not found.get("company"):
        name = page_company(html)
        if name:
            found["company"] = name
    return found


def resolve(job_id, company_url):
    """Both sides, merged: the company posting leads, LinkedIn fills its gaps."""
    company_side = {}
    if company_url:
        if jobs_md.composite_id_for_url(company_url):
            company_side = resolve_ats(company_url)
        # Ashby, for one, does not name the employer in its API.
        company_side["title"] = clean_title(company_side.get("title"))
        if not company_side.get("description") or not company_side.get("company") or not company_side.get("title"):
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
    merged["title"] = clean_title(merged.get("title"))
    return merged


# ---------------------------------------------------------------------- write

def _entry_key(seen, url):
    return jobs_md.entry_key(seen, url)


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
        company_url = company_url or external_apply_url(found.get("apply_url"))
        # What the owner typed (or the bookmarklet carried) fills gaps only.
        for key in ("title", "company", "location"):
            if not found.get(key) and _text(payload, key):
                found[key] = _text(payload, key)
        found["title"] = clean_title(found.get("title"))
        # Last resort for the employer: the link itself usually names it
        # (jobs.northwind.com, boards.greenhouse.io/acme, acme.recruitee.com).
        if not found.get("company"):
            guess = company_from_url(company_url) or company_from_url(_text(payload, "job_url", 2000))
            if guess:
                found["company"] = guess
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
