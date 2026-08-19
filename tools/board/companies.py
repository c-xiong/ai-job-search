"""Concurrency-safe, ownership-aware edits for the target-company registry."""

import json
import os
import re
import threading
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collectors  # noqa: E402
from company_lock import registry_lock  # noqa: E402

from . import activity


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "job_scraper" / "companies.json"
LOCK = ROOT / "job_scraper" / ".companies.lock"
EDITABLE = {"name", "aliases", "domain", "tier", "route", "flags", "note",
            "countries", "cities"}
ROUTES = {"ats", "linkedin", "manual"}


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
    try:
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


def _public(data):
    companies = data["companies"]
    counts = {}
    routes = {}
    for row in companies:
        counts[row.get("status", "unresolved")] = counts.get(row.get("status", "unresolved"), 0) + 1
        routes[row.get("route", "ats")] = routes.get(row.get("route", "ats"), 0) + 1
    return {"companies": companies, "defaults": data.get("defaults", {}),
            "mtime": _mtime(), "counts": counts, "routes": routes,
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
    name = str(payload.get("name") or "").strip()
    if not name:
        raise CompanyError("name is required")
    domain = _domain(payload.get("website"))
    tier = payload.get("tier", 3)
    if not isinstance(tier, int) or isinstance(tier, bool) or not 1 <= tier <= 5:
        raise CompanyError("tier must be an integer 1-5")
    with company_lock():
        data = _load()
        _fresh(payload.get("mtime"))
        names = {slug(value) for row in data["companies"]
                 for value in [row.get("name")] + list(row.get("aliases") or [])}
        domains = {str(row.get("domain") or "").casefold() for row in data["companies"]}
        if slug(name) in names or domain in domains:
            raise CompanyError("company name, alias or domain already exists", 409)
        data["companies"].append({"name": name, "domain": domain, "tier": tier,
                                  "status": "unresolved", "route": "ats"})
        _write(data)
    activity.emit("registry", "added %s" % name)
    return resolve(slug(name), {"mtime": _mtime()})


def patch_company(company_slug, payload):
    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes or set(changes) - EDITABLE:
        raise CompanyError("changes contain fields the UI does not own")
    if "tier" in changes and (not isinstance(changes["tier"], int) or
                              isinstance(changes["tier"], bool) or not 1 <= changes["tier"] <= 5):
        raise CompanyError("tier must be an integer 1-5")
    if "route" in changes and changes["route"] not in ROUTES:
        raise CompanyError("route is invalid")
    for key in ("aliases", "flags", "countries", "cities"):
        if key in changes and (not isinstance(changes[key], list) or
                               not all(isinstance(v, str) for v in changes[key])):
            raise CompanyError("%s must be a list of strings" % key)
    with company_lock():
        data = _load(); _fresh(payload.get("mtime")); row = _find(data, company_slug)
        if row is None:
            raise CompanyError("unknown company", 404)
        row.update(changes); _write(data)
    activity.emit("registry", "updated %s" % row["name"])
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
            row["identity"] = {"method": "human_confirmed",
                               "evidence": "confirmed in the board UI",
                               "checked": date.today().isoformat()}
            row.pop("candidates", None)
            row.setdefault("cadence_days", int(data.get("defaults", {}).get("cadence_days", {}).get(str(row.get("tier", 3)), 3)))
        elif decision in ("neither", "reject"):
            row["status"] = "paused"
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
