"""Concurrency-safe, ownership-aware edits for the target-company registry."""

import json
import os
import re
import threading
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collectors  # noqa: E402
import fetch_jobs  # noqa: E402
from company_lock import registry_lock  # noqa: E402

from . import activity


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "job_scraper" / "companies.json"
LOCK = ROOT / "job_scraper" / ".companies.lock"
EDITABLE = {"name", "careers_url", "domain", "countries"}
ROUTES = {"ats", "linkedin", "manual"}
COUNTRIES = {"CH", "DE"}
SUPPORTED_VENDORS = {"greenhouse", "ashby", "personio", "lever", "smartrecruiters", "workday"}
# Rows added from the Companies page are checked daily: the point of saving a
# company is hearing about its jobs first (COMPANIES_PLAN §3.5).
ADDED_CADENCE_DAYS = 1
RESOLVE_LOCK = threading.Lock()
FETCH_TEST_LOCK = threading.Lock()


def _board_url(vendor, token):
    if vendor == "workday":
        # "<tenant>.<pod>/<site>" - the one token with a slash in it.
        match = re.fullmatch(r"([a-z0-9-]+)\.(wd\d+)/([A-Za-z0-9_-]+)", str(token or "").strip())
        return ("https://%s.%s.myworkdayjobs.com/%s" % match.groups()) if match else None
    token = quote(str(token or "").strip(), safe="-._~")
    if not token:
        return None
    return {
        "greenhouse": "https://job-boards.greenhouse.io/%s" % token,
        "ashby": "https://jobs.ashbyhq.com/%s" % token,
        "personio": "https://%s.jobs.personio.de" % token,
        "lever": "https://jobs.lever.co/%s" % token,
        "smartrecruiters": "https://careers.smartrecruiters.com/%s" % token,
    }.get(vendor)


class CompanyError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def company_lock():
    return registry_lock(REGISTRY)


def _load():
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CompanyError("company registry is missing", 404)
    except (OSError, ValueError) as exc:
        raise CompanyError("company registry is unreadable: %s" % exc, 500)
    if not isinstance(data.get("companies"), list):
        raise CompanyError("company registry has no companies list", 500)
    return data


def _mtime():
    return str(REGISTRY.stat().st_mtime_ns)


def _write(data):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    temp = REGISTRY.with_name(REGISTRY.name + ".tmp-%d-%d" %
                              (os.getpid(), threading.get_ident()))
    backup = REGISTRY.with_name("companies.backup.json")
    backup_temp = backup.with_name(backup.name + ".tmp-%d-%d" %
                                   (os.getpid(), threading.get_ident()))
    try:
        if REGISTRY.exists():
            with open(backup_temp, "wb") as handle:
                handle.write(REGISTRY.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(backup_temp, backup)
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, REGISTRY)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        try:
            backup_temp.unlink()
        except FileNotFoundError:
            pass


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).casefold()).strip("-")


def _domain(website):
    value = str(website or "").strip()
    parsed = urlparse(value if "://" in value else "https://" + value)
    host = (parsed.hostname or "").casefold().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or not re.match(r"^[a-z0-9.-]+$", host):
        raise CompanyError("website must contain a valid public domain")
    return host


def _careers_url(value):
    raw = str(value or "").strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CompanyError("careers page must be a valid http(s) URL")
    _domain(raw)
    return parsed._replace(fragment="").geturl()


def _countries(value):
    if not isinstance(value, list) or not value or set(value) - COUNTRIES:
        raise CompanyError("countries must contain CH, DE, or both")
    return [code for code in ("CH", "DE") if code in value]


def _ats_identity(careers_url):
    """Recognize a board URL the user already found, without guessing slugs."""
    parsed = urlparse(careers_url)
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host in ("job-boards.greenhouse.io", "job-boards.eu.greenhouse.io",
                "boards.greenhouse.io") and parts:
        if parts[0] == "embed":
            # .../embed/job_board?for=<board>: the board is in the query.
            board = parse_qs(parsed.query).get("for", [""])[0]
            return ("greenhouse", board) if re.fullmatch(r"[A-Za-z0-9_-]+", board) else None
        return "greenhouse", parts[0]
    if host == "jobs.ashbyhq.com" and parts:
        return "ashby", parts[0]
    match = re.fullmatch(r"([a-z0-9-]+)\.jobs\.personio\.(?:de|com)", host)
    if match:
        return "personio", match.group(1)
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return "lever", parts[0]
    if host in ("careers.smartrecruiters.com", "jobs.smartrecruiters.com") and parts:
        return "smartrecruiters", parts[0]
    match = re.fullmatch(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
    if match:
        site = [part for part in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", part)]
        if site and site[0] not in ("wday", "job"):
            return "workday", "%s.%s/%s" % (match.group(1), match.group(2), site[0])
    match = re.fullmatch(r"(wd\d+)\.myworkdaysite\.com", host)
    if match and len(parts) >= 3 and "recruiting" in parts:
        at = parts.index("recruiting")
        if len(parts) > at + 2:
            return "workday", "%s.%s/%s" % (parts[at + 1].casefold(), match.group(1), parts[at + 2])
    return None


def _vendor_access(defaults, vendor):
    """Mirror the CLI's fail-closed vendor policy for the board UI."""
    configured = (defaults.get("vendor_access") or {}).get(vendor) or {}
    enabled = configured.get("enabled")
    if enabled is None:
        enabled = vendor != "smartrecruiters"
    return bool(enabled), configured.get("why")


def _monitoring_fields(row, defaults):
    """Expose the exact, derived answer to whether `search` may fetch this row."""
    route = row.get("route", "ats")
    status = row.get("status", "unresolved")
    vendor = row.get("vendor")
    token = row.get("token")
    if status == "paused":
        monitoring_status = "paused"
        reason = "status is paused"
    elif route != "ats":
        monitoring_status = "other_route"
        reason = "route is %s, not ats" % route
    elif status == "ambiguous":
        monitoring_status = "needs_confirmation"
        reason = "status is ambiguous; a candidate board needs human confirmation"
    elif status == "unsupported_vendor":
        monitoring_status = "adapter_missing"
        reason = "status is unsupported_vendor; this ATS has no enabled adapter"
    elif status == "no_public_board":
        monitoring_status = "no_public_board"
        reason = "status is no_public_board"
    elif status != "verified":
        monitoring_status = "source_not_detected" if row.get("careers_url") \
            else "careers_url_needed"
        reason = "status is unresolved; %s" % (
            "no supported source was detected from careers_url" if row.get("careers_url")
            else "careers_url is missing")
    elif not vendor or not token:
        monitoring_status = "source_not_detected"
        reason = "status verified requires vendor and token"
    elif vendor not in SUPPORTED_VENDORS:
        monitoring_status = "adapter_missing"
        reason = "vendor %s has no adapter" % vendor
    else:
        enabled, why = _vendor_access(defaults, vendor)
        if not enabled:
            monitoring_status = "policy_disabled"
            reason = "vendor %s is disabled%s" % (vendor, " - " + why if why else "")
        else:
            monitoring_status = "monitoring"
            reason = "route ats + status verified + vendor and token + enabled adapter"
    will_be_searched = monitoring_status == "monitoring"
    attempted = row.get("last_attempt_at")
    succeeded = row.get("last_success_at")
    if not attempted:
        fetch_status = "never"
    elif succeeded == attempted:
        fetch_status = "success"
    else:
        fetch_status = "failed"
    return {"monitoring_status": monitoring_status,
            "watch_state": _watch_state(row, status, route, monitoring_status),
            "will_be_searched": will_be_searched,
            "monitoring_reason": reason,
            "fetch_status": fetch_status}


def _watch_state(row, status, route, monitoring_status):
    """The one question the Companies page answers per row (COMPANIES_PLAN §4)."""
    if status == "paused":
        return "paused"
    if route != "ats":
        return "not_watched"
    if status == "ambiguous":
        return "needs_you"
    if status == "unresolved":
        # `next_resolve_at: null` is the retry queue giving up and asking you;
        # a missing key is a row that has simply not been tried yet.
        return "needs_you" if "next_resolve_at" in row and row["next_resolve_at"] is None \
            else "finding"
    if monitoring_status == "monitoring":
        return "watching"
    return "cant_watch"


def _public(data):
    companies = []
    counts = {}
    routes = {}
    monitoring_counts = {}
    fetch_counts = {}
    defaults = data.get("defaults", {})
    for source in data["companies"]:
        row = dict(source)
        row["board_url"] = _board_url(row.get("vendor"), row.get("token"))
        row.update(_monitoring_fields(row, defaults))
        if "candidates" in row:
            row["candidates"] = [dict(candidate, board_url=_board_url(
                candidate.get("vendor"), candidate.get("token")))
                for candidate in row.get("candidates") or []]
        companies.append(row)
        counts[row.get("status", "unresolved")] = counts.get(row.get("status", "unresolved"), 0) + 1
        routes[row.get("route", "ats")] = routes.get(row.get("route", "ats"), 0) + 1
        monitoring_counts[row["monitoring_status"]] = monitoring_counts.get(row["monitoring_status"], 0) + 1
        fetch_counts[row["fetch_status"]] = fetch_counts.get(row["fetch_status"], 0) + 1
    return {"schema_version": 2, "company_controls": True,
            "companies": companies, "defaults": defaults,
            "mtime": _mtime(), "counts": counts, "routes": routes,
            "monitoring_counts": monitoring_counts, "fetch_counts": fetch_counts,
            "will_be_searched_count": sum(1 for row in companies if row["will_be_searched"]),
            "path": str(REGISTRY.relative_to(ROOT))}


def listing():
    with company_lock():
        return _public(_load())


def _find(data, wanted):
    return next((row for row in data["companies"] if slug(row.get("name")) == wanted), None)


def _fresh(expected):
    if str(expected or "") != _mtime():
        raise CompanyError("registry changed since it was loaded", 409)


def add(payload):
    careers_url = _careers_url(payload.get("careers_url") or payload.get("website"))
    direct_ats = _ats_identity(careers_url)
    name = str(payload.get("name") or "").strip()
    if not name:
        name = direct_ats[1] if direct_ats else _domain(careers_url)
    if not slug(name):
        raise CompanyError("Please enter a company name containing letters or numbers.")
    countries = _countries(payload.get("countries") or ["CH", "DE"])
    domain = None if direct_ats else _domain(careers_url)
    with company_lock():
        data = _load()
        _fresh(payload.get("mtime"))
        names = {slug(value) for row in data["companies"]
                 for value in [row.get("name")] + list(row.get("aliases") or [])}
        domains = {str(row.get("domain") or "").casefold() for row in data["companies"]
                   if row.get("domain")}
        urls = {str(row.get("careers_url") or "").casefold() for row in data["companies"]}
        if slug(name) in names or (domain and domain in domains) or careers_url.casefold() in urls:
            raise CompanyError("company name or careers page already exists", 409)
        row = {"name": name, "careers_url": careers_url, "tier": 3,
               "countries": countries, "status": "unresolved", "route": "ats",
               "cadence_days": ADDED_CADENCE_DAYS}
        if domain:
            row["domain"] = domain
        if direct_ats:
            vendor, token = direct_ats
            row.update({"vendor": vendor, "token": token, "status": "verified",
                        "identity": {"method": "human_confirmed",
                                     "evidence_kind": "human_confirmed",
                                     "evidence": "careers board URL entered in the board UI",
                                     "checked": date.today().isoformat(),
                                     "verified_at": date.today().isoformat()}})
        data["companies"].append(row)
        _write(data)
    activity.emit("registry", "added %s" % name)
    if direct_ats:
        return listing()
    return resolve(slug(name), {"mtime": _mtime()})


def patch_company(company_slug, payload):
    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes or set(changes) - EDITABLE:
        raise CompanyError("changes contain fields the UI does not own")
    if "countries" in changes and changes["countries"] != []:
        changes["countries"] = _countries(changes["countries"])
    if "careers_url" in changes:
        changes["careers_url"] = _careers_url(changes["careers_url"])
    inspect = bool(payload.get("inspect"))
    needs_inspection = False
    with company_lock():
        data = _load(); _fresh(payload.get("mtime")); row = _find(data, company_slug)
        if row is None:
            raise CompanyError("unknown company", 404)
        source_changed = "careers_url" in changes and changes["careers_url"] != row.get("careers_url")
        paused = row.get("status") == "paused"
        if "name" in changes:
            name = str(changes["name"]).strip()
            if not slug(name):
                raise CompanyError("Please enter a company name containing letters or numbers.")
            if any(other is not row and slug(name) in
                   {slug(value) for value in [other.get("name")] + list(other.get("aliases") or [])}
                   for other in data["companies"]):
                raise CompanyError("A company with this name already exists.", 409)
            changes["name"] = name
        row.update(changes)
        if changes.get("countries") == []:
            row.pop("countries", None)
        if source_changed:
            row["route"] = "ats"
            direct_ats = _ats_identity(row["careers_url"])
            for key in ("vendor", "token", "identity", "candidates", "last_attempt_at",
                        "last_success_at", "last_status", "resolve_attempts",
                        "last_resolve_at", "next_resolve_at", "resolve_detail",
                        "missing_streak"):
                row.pop(key, None)
            row["stats"] = {"jobs_seen": 0, "german_gated": 0,
                            "eligible_jobs": 0, "last_eligible_at": None,
                            "last_jobs_seen": None, "last_eligible_jobs": None}
            if direct_ats:
                vendor, token = direct_ats
                stamp = date.today().isoformat()
                row.update({"vendor": vendor, "token": token, "status": "verified",
                            "cadence_days": int(data.get("defaults", {}).get(
                                "cadence_days", {}).get(str(row.get("tier", 3)), 3)),
                            "identity": {"method": "human_confirmed",
                                         "evidence_kind": "human_confirmed",
                                         "evidence": "careers board URL entered in the board UI",
                                         "checked": stamp, "verified_at": stamp}})
                row.pop("domain", None)
            else:
                row["domain"] = _domain(row["careers_url"])
                row["status"] = "unresolved"
                needs_inspection = True
        elif inspect and row.get("status") not in ("verified", "paused"):
            needs_inspection = True
        if paused:
            if source_changed:
                row["status_before_pause"] = row["status"]
            row["status"] = "paused"
            needs_inspection = False
        _write(data)
    activity.emit("registry", "updated %s" % row["name"])
    if inspect and needs_inspection:
        return resolve(slug(row["name"]), {"mtime": _mtime()})
    return listing()


def manage(company_slug, payload):
    """User-owned follow controls; keep discovery evidence when pausing."""
    action = payload.get("action")
    if action not in ("pause", "resume", "remove", "watch"):
        raise CompanyError("Unknown company action.")
    with company_lock():
        data = _load(); _fresh(payload.get("mtime")); row = _find(data, company_slug)
        if row is None:
            raise CompanyError("unknown company", 404)
        name = row["name"]
        if action == "remove":
            data["companies"].remove(row)
        elif action == "pause" and row.get("status") != "paused":
            row["status_before_pause"] = row.get("status", "unresolved")
            row["status"] = "paused"
        elif action == "resume" and row.get("status") == "paused":
            row["status"] = row.pop("status_before_pause", "unresolved")
        elif action == "watch" and row.get("route", "ats") != "ats":
            # A company kept for reference (route linkedin/manual) joins the
            # retry queue as if it were new; the caller detects right away.
            row["route"] = "ats"
            if row.get("status") not in ("verified", "paused"):
                row["status"] = "unresolved"
                for key in ("resolve_attempts", "next_resolve_at", "last_resolve_at"):
                    row.pop(key, None)
        _write(data)
    activity.emit("registry", "%s: %s" % (name, action))
    return listing()


def resolve(company_slug, payload):
    with company_lock():
        data = _load(); _fresh(payload.get("mtime")); row = _find(data, company_slug)
        if row is None:
            raise CompanyError("unknown company", 404)
        exact_name = row["name"]
    result = collectors.bun([collectors.ATS, "resolve", "--company", exact_name,
                             "--max-companies", "1", "--max-probes", "6",
                             "--format", "json"], lambda line: activity.emit("registry", line))
    activity.emit("registry", "resolved %s (at most 9 requests)" % exact_name)
    return {"result": result, **listing()}


def test_fetch(company_slug, payload):
    """Run the real search adapter once for one already-fetchable company."""
    if not FETCH_TEST_LOCK.acquire(blocking=False):
        raise CompanyError("a monitoring test is already running; watch Activity for progress", 409)
    try:
        with company_lock():
            data = _load(); _fresh(payload.get("mtime")); row = _find(data, company_slug)
            if row is None:
                raise CompanyError("unknown company", 404)
            fields = _monitoring_fields(row, data.get("defaults", {}))
            if not fields["will_be_searched"]:
                raise CompanyError("company is not fetchable: %s" % fields["monitoring_reason"], 409)
            exact_name = row["name"]
        activity.emit("registry", "monitoring test started for %s" % exact_name)
        try:
            with fetch_jobs.RunLock():
                result = collectors.bun(
                    [collectors.ATS, "search", "--company", exact_name,
                     "--max-companies", "1", "--force-refresh", "--format", "json"],
                    lambda line: activity.emit("registry", line))
        except fetch_jobs.AlreadyRunning as exc:
            raise CompanyError(str(exc), 409)
        if result is None:
            activity.emit("registry", "monitoring test failed for %s" % exact_name, level="error")
            raise CompanyError("monitoring test did not return a usable result", 502)
        report = next(iter(result.get("meta", {}).get("companies", [])), {})
        activity.emit("registry", "monitoring test finished for %s: %s" %
                      (exact_name, report.get("status", "unknown")))
        return {"test_fetch": {"company": exact_name,
                               "status": report.get("status", "unknown"),
                               "message": report.get("message"),
                               "jobs_seen": report.get("jobs_seen", 0),
                               "eligible_jobs": report.get("eligible", 0),
                               "requests": report.get("requests", 0),
                               "generated_at": result.get("meta", {}).get("generated_at"),
                               "sample_jobs": (result.get("results") or [])[:3]},
                **listing()}
    finally:
        FETCH_TEST_LOCK.release()


def health_check(payload):
    """Test every currently fetchable ATS company in one bounded search call."""
    if not FETCH_TEST_LOCK.acquire(blocking=False):
        raise CompanyError("a monitoring test is already running; watch Activity for progress", 409)
    try:
        with company_lock():
            data = _load(); _fresh(payload.get("mtime"))
            targets = [row["name"] for row in data["companies"]
                       if _monitoring_fields(row, data.get("defaults", {}))["will_be_searched"]]
        if not targets:
            return {"health_check": {"tested": 0, "succeeded": 0, "failed": 0}, **listing()}
        activity.emit("registry", "monitoring health check started for %d companies" % len(targets),
                      detail=targets[:20])
        selectors = [value for name in targets for value in ("--company", name)]
        try:
            with fetch_jobs.RunLock():
                result = collectors.bun(
                    [collectors.ATS, "search"] + selectors + [
                        "--max-companies", str(len(targets)), "--force-refresh",
                        "--limit", "1", "--format", "json"],
                    lambda line: activity.emit("registry", line),
                    timeout=max(120, len(targets) * 20))
        except fetch_jobs.AlreadyRunning as exc:
            raise CompanyError(str(exc), 409)
        if result is None:
            activity.emit("registry", "monitoring health check failed", level="error")
            raise CompanyError("monitoring health check did not return a usable result", 502)
        reports = result.get("meta", {}).get("companies", [])
        successful = {"ok", "empty"}
        succeeded = sum(1 for report in reports if report.get("status") in successful)
        response = {"tested": len(reports), "succeeded": succeeded,
                    "failed": len(reports) - succeeded,
                    "generated_at": result.get("meta", {}).get("generated_at"),
                    "reports": reports}
        activity.emit("registry", "monitoring health check finished: %d succeeded, %d failed" %
                      (response["succeeded"], response["failed"]))
        return {"health_check": response, **listing()}
    finally:
        FETCH_TEST_LOCK.release()


def resolve_all(payload):
    if not RESOLVE_LOCK.acquire(blocking=False):
        raise CompanyError("an ATS identity check is already running; watch Activity for progress", 409)
    try:
        return _resolve_all(payload)
    finally:
        RESOLVE_LOCK.release()


def _resolve_all(payload):
    """Resolve each currently unconnected ATS company once."""
    with company_lock():
        data = _load(); _fresh(payload.get("mtime"))
        targets = [row["name"] for row in data["companies"]
                   if _watch_state(row, row.get("status", "unresolved"),
                                   row.get("route", "ats"), "") == "finding"]
        total = len(targets)
    if not total:
        return {"result": {"meta": {"resolved": 0, "status_counts": {}}}, **listing()}
    activity.emit("registry", "ATS identity check started for %d not connected companies" % total,
                  detail=targets[:20])
    selectors = [value for name in targets for value in ("--company", name)]
    result = collectors.bun(
        [collectors.ATS, "resolve"] + selectors + ["--max-companies", str(total),
         "--max-probes", "6", "--format", "json"],
        lambda line: activity.emit("registry", line), timeout=max(120, total * 20))
    if result is None:
        activity.emit("registry", "ATS identity check failed for %d companies" % total,
                      level="error")
        raise CompanyError("automatic identity check did not finish", 502)
    counts = result.get("meta", {}).get("status_counts", {}) if isinstance(result, dict) else {}
    activity.emit(
        "registry",
        ("ATS identity check finished: %d connected, %d need review, "
         "%d unresolved, %d unsupported" %
         (counts.get("verified", 0), counts.get("ambiguous", 0),
          counts.get("unresolved", 0), counts.get("unsupported_vendor", 0))),
        detail=["checked %d companies" % total])
    return {"result": result, **listing()}


def identity(company_slug, payload):
    decision = payload.get("decision")
    with company_lock():
        data = _load(); _fresh(payload.get("mtime")); row = _find(data, company_slug)
        if row is None:
            raise CompanyError("unknown company", 404)
        if row.get("status") != "ambiguous":
            raise CompanyError("identity decisions only apply to ambiguous companies", 409)
        if decision == "confirm":
            candidate = payload.get("candidate") or {}
            found = next((c for c in row.get("candidates") or []
                          if c.get("vendor") == candidate.get("vendor") and
                          c.get("token") == candidate.get("token")), None)
            if found is None:
                raise CompanyError("candidate is not in the current evidence", 409)
            row["vendor"], row["token"], row["status"] = found["vendor"], found["token"], "verified"
            stamp = date.today().isoformat()
            row["identity"] = {"method": "human_confirmed",
                               "evidence_kind": "human_confirmed",
                               "evidence": "confirmed in the board UI",
                               "checked": stamp, "verified_at": stamp}
            row.pop("candidates", None)
            row.setdefault("cadence_days", int(data.get("defaults", {}).get("cadence_days", {}).get(str(row.get("tier", 3)), 3)))
        elif decision in ("neither", "reject"):
            # "None of these is theirs" is not "stop watching": the company still
            # matters, only the guesses were wrong. It waits for its board link
            # (Needs you) instead of being paused or guessed at again.
            row["status"] = "unresolved"
            row.pop("candidates", None)
            row["next_resolve_at"] = None
            row["resolve_detail"] = "you said none of the suggested job boards is theirs"
        else:
            raise CompanyError("decision must be confirm or neither")
        _write(data)
    activity.emit("registry", "identity decision for %s: %s" % (row["name"], decision))
    return listing()


def suggestions():
    payload = collectors.bun([collectors.ATS, "companies", "--suggest", "--format", "json"],
                             lambda line: activity.emit("registry", line))
    return payload or {"suggestions": []}


def never(payload):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise CompanyError("name is required")
    with company_lock():
        data = _load(); _fresh(payload.get("mtime"))
        if any(slug(row.get("name")) == slug(name) for row in data["companies"]):
            raise CompanyError("company already exists", 409)
        data["companies"].append({"name": name, "tier": 3, "status": "paused",
                                  "route": "manual", "note": "dismissed in board suggestions"})
        _write(data)
    return listing()
