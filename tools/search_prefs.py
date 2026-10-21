#!/usr/bin/env python3
"""Search preferences: roles x job types x locations, expanded into searches.

    python3 tools/search_prefs.py            # print the searches the preferences generate

`scrape_config.json` holds the choice:

    "search_preferences": {
      "enabled": true,
      "roles": ["AI Engineer", "Software Engineer"],
      "job_types": ["full_time", "intern"],          # + "working_student"
      "locations": ["Switzerland", "Germany"],
      "pages": 2
    }

Every combination becomes a LinkedIn keyword search; the roles also map onto
freehire's categories. The hand-written `linkedin` / `freehire` lists in the
same file stay as custom searches and run alongside, deduplicated against the
generated ones. Nothing is written back: the searches are derived at fetch
time, so a changed preference applies on the next fetch.

Job types become keywords because LinkedIn's guest search ignores its own
job-type and experience filters (f_JT / f_E - checked 2026-10-10): a full-time
search is the role alone (an internship it turns up is still typed on intake by
job_attrs.py), an internship search adds "Intern", and in German-speaking
locations also "Praktikum"; working student adds "Working Student" and, there,
"Werkstudent". Working student is not restricted by country: a German ad that
offers intern *or* working student is one you want, and the fit score already
deducts a German working-student-only role.

Stdlib only, Python 3.9+.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_attrs  # noqa: E402
import jobs_md  # noqa: E402

CONFIG = jobs_md.ROOT / "job_scraper" / "scrape_config.json"

JOB_TYPES = ("full_time", "intern", "working_student", "graduate")
EUROPE_REGIONS = {
    "Nordic": ["Denmark", "Sweden", "Norway", "Finland", "Iceland"],
    "Western Europe": ["Switzerland", "Germany", "Austria", "France", "Netherlands", "Belgium", "Luxembourg", "Ireland", "United Kingdom"],
    "Southern Europe": ["Spain", "Portugal", "Italy", "Greece", "Malta", "Cyprus"],
}
COUNTRY_CODES = dict(zip(
    "Denmark Sweden Norway Finland Iceland Switzerland Germany Austria France Netherlands Belgium Luxembourg Ireland Spain Portugal Italy Greece Malta Cyprus".split(),
    "DK SE NO FI IS CH DE AT FR NL BE LU IE ES PT IT GR MT CY".split()))
COUNTRY_CODES["United Kingdom"] = "GB"
DEFAULTS = {"enabled": False, "roles": [], "job_types": ["full_time"], "locations": [], "pages": 1,
            "europe_enabled": False, "europe_locations": [],
            "europe_job_types": ["graduate", "intern"], "excluded_locations": []}
SEARCH_BUDGET_DEFAULT = 24
SEARCH_BUDGET_CEILING = 40

# freehire categories, chosen by words in the role.
_FREEHIRE_CATEGORIES = (
    (("ai", "ml", "machine learning", "llm", "nlp", "data scien", "deep learning", "genai",
      "computer vision", "research engineer"), "ml_ai"),
    (("software", "backend", "back-end", "full stack", "fullstack", "full-stack", "python",
      "developer", "platform"), "backend,fullstack"),
    (("frontend", "front-end"), "frontend"),
    (("data engineer",), "data"),
    (("devops", "sre", "site reliability"), "devops"),
)
_GERMAN_SPEAKING = ("DE", "AT")


def preferences(cfg):
    """The validated preference block (defaults filled in)."""
    raw = (cfg or {}).get("search_preferences")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["enabled"] = bool(raw.get("enabled", bool(raw)))
    out["europe_enabled"] = bool(raw.get("europe_enabled", False))
    for key in ("roles", "locations", "europe_locations", "excluded_locations"):
        values = raw.get(key)
        if isinstance(values, list):
            seen, clean = set(), []
            for value in values:
                text = " ".join(str(value).split())[:80]
                if text and text.lower() not in seen:
                    seen.add(text.lower())
                    clean.append(text)
            out[key] = clean
    for key in ("job_types", "europe_job_types"):
        types = raw.get(key)
        if isinstance(types, list):
            out[key] = [t for t in JOB_TYPES if t in types]
    try:
        out["pages"] = max(1, min(3, int(raw.get("pages", 1))))
    except (TypeError, ValueError):
        pass
    return out


def clean(raw):
    """Validate a preference block from the board. Returns (block, error)."""
    if not isinstance(raw, dict):
        return None, "search_preferences must be an object"
    for key in ("roles", "locations", "job_types", "europe_locations", "europe_job_types", "excluded_locations"):
        if key in raw and not isinstance(raw[key], list):
            return None, "%s must be a list" % key
    if any(t not in JOB_TYPES for key in ("job_types", "europe_job_types") for t in raw.get(key) or []):
        return None, "job_types must be among %s" % ", ".join(JOB_TYPES)
    block = preferences({"search_preferences": dict(raw, enabled=raw.get("enabled", True))})
    if len(block["roles"]) > 12 or any(len(block[k]) > 50 for k in ("locations", "europe_locations", "excluded_locations")):
        return None, "at most 12 roles and 50 places per list"
    return block, None


def _keywords(role, job_type, country):
    if job_type == "graduate":
        return ["%s Graduate" % role, "%s Graduate Programme" % role]
    if job_type == "intern":
        out = ["%s Intern" % role]
        if country in _GERMAN_SPEAKING:
            out.append("Praktikum %s" % role)
        return out
    if job_type == "working_student":
        out = ["Working Student %s" % role]
        if country in _GERMAN_SPEAKING:
            out.append("Werkstudent %s" % role)
        return out
    return [role]


def linkedin_searches(prefs):
    """[{q, l, pages, generated}] - every role x type x location."""
    if not prefs["enabled"]:
        return []
    out = []
    for location in prefs["locations"]:
        country = job_attrs.country(location)
        for role in prefs["roles"]:
            for job_type in prefs["job_types"]:
                for q in _keywords(role, job_type, country):
                    out.append({"q": q, "l": location, "pages": prefs["pages"],
                                "generated": job_type})
    return out


def _category(role):
    text = role.lower()
    for words, category in _FREEHIRE_CATEGORIES:
        if any(word in text for word in words):
            return category
    return None


def freehire_searches(prefs):
    """freehire queries: role categories x job types, over the countries chosen."""
    if not prefs["enabled"]:
        return []
    countries = sorted({c for c in (COUNTRY_CODES.get(l, job_attrs.country(l)) for l in prefs["locations"])
                        if c in COUNTRY_CODES.values()})
    if not countries:
        return []
    country = ",".join(countries)
    out, seen = [], set()
    for role in prefs["roles"]:
        category = _category(role)
        for job_type in prefs["job_types"]:
            spec = {"country": country, "generated": job_type}
            if category:
                spec["category"] = category
            else:
                spec["q"] = role
            if job_type == "full_time":
                spec["seniority"] = "junior"
            elif job_type == "graduate":
                spec["q"] = ("%s graduate" % spec["q"]) if spec.get("q") else "graduate"
            elif job_type == "intern":
                spec["q"] = ("%s intern" % spec["q"]) if spec.get("q") else "intern"
            else:
                spec["q"] = ("%s working student" % spec["q"]) if spec.get("q") else "working student"
            key = json.dumps({k: v for k, v in spec.items() if k != "generated"}, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(spec)
    return out


def _linkedin_key(spec):
    return (str(spec.get("q", "")).strip().lower(), str(spec.get("l", "")).strip().lower())


def _freehire_key(spec):
    return json.dumps({k: v for k, v in spec.items() if k not in ("generated", "pages", "enabled")},
                      sort_keys=True).lower()


def searches(cfg):
    """{"linkedin": [...], "freehire": [...]}: generated first, then the custom
    ones the generated set does not already cover."""
    prefs = preferences(cfg)
    out = {}
    for source, generate, key in (("linkedin", linkedin_searches, _linkedin_key),
                                  ("freehire", freehire_searches, _freehire_key)):
        generated = generate(prefs)
        if prefs["europe_enabled"]:
            generated += generate(dict(prefs, enabled=True, locations=prefs["europe_locations"],
                                       job_types=prefs["europe_job_types"]))
        unique = {}
        for spec in generated:
            unique.setdefault(key(spec), spec)
        generated = list(unique.values())
        known = {key(spec) for spec in generated}
        custom = [dict(spec, custom=True) for spec in (cfg or {}).get(source) or []
                  if isinstance(spec, dict) and spec.get("enabled", True) and key(spec) not in known]
        out[source] = generated + custom
    return out


def search_budget(cfg):
    try:
        value = int((cfg or {}).get("keyword_search_budget", SEARCH_BUDGET_DEFAULT))
    except (TypeError, ValueError):
        value = SEARCH_BUDGET_DEFAULT
    return max(0, min(value, SEARCH_BUDGET_CEILING))


# --------------------------------------------------------------------------
# The board's view of it


def _task_key(source, spec, page):
    """The same key CollectionSession gives a search page (collectors.py)."""
    import hashlib
    bare = {k: v for k, v in spec.items() if k not in ("custom", "generated")}
    return hashlib.sha256(json.dumps([source, bare, page], sort_keys=True).encode()).hexdigest()[:16]


def _label(source, spec):
    if source == "linkedin":
        return "%s · %s" % (spec.get("q", ""), spec.get("l", ""))
    parts = [spec.get("category"), spec.get("q"), spec.get("seniority"), spec.get("country"),
             spec.get("city")]
    return " · ".join(str(p) for p in parts if p)


def overview(cfg, state=None, seen=None):
    """Everything the Fetch settings panel shows: the preferences, the filter
    switches, and each search with what it brought in last time."""
    import job_filters  # noqa: E402 - job_filters does not import this module

    queries = (state or {}).get("queries") or {}
    seen = seen or {}
    plan = searches(cfg)
    rows, tasks = [], 0
    for source, specs in plan.items():
        for spec in specs:
            pages = max(1, min(3, int(spec.get("pages", cfg.get("pages_per_query", 1)) or 1)))
            tasks += pages
            returned = new = good = gated = 0
            at = ""
            for page in range(1, pages + 1):
                outcome = queries.get(_task_key(source, spec, page)) or {}
                returned += int(outcome.get("returned") or 0)
                at = max(at, outcome.get("last_attempt_at") or "")
                for url in outcome.get("urls") or []:
                    entry = seen.get(jobs_md.canonical_url(url))
                    if not entry:
                        continue
                    new += 1
                    status = jobs_md.user_status(entry)
                    if status == "gate":
                        gated += 1
                    elif (entry.get("fit") or "") in ("high", "medium"):
                        good += 1
            rows.append({"source": source, "label": _label(source, spec),
                         "custom": bool(spec.get("custom")), "job_type": spec.get("generated") or "",
                         "pages": pages, "returned": returned, "on_board": new, "good": good,
                         "gated": gated, "last_run": at})
    opts = job_filters.settings(cfg)
    budget = search_budget(cfg)
    return {
        "preferences": preferences(cfg),
        "job_types": list(JOB_TYPES),
        "europe_regions": EUROPE_REGIONS,
        "budget": budget,
        "budget_default": SEARCH_BUDGET_DEFAULT,
        "budget_ceiling": SEARCH_BUDGET_CEILING,
        "filters": [{"key": key, "label": label, "on": opts["groups"][key], "default": on}
                    for key, label, on, _ in job_filters.TITLE_GROUPS],
        "body_filters": [{"key": key, "label": label, "on": opts["body"][key]}
                         for key, label, _ in job_filters.BODY_RULES],
        "allow": opts["allow"],
        "searches": rows,
        "tasks": tasks,
        "fetches_per_round": -(-tasks // budget) if budget else 0,
    }


def save(payload, path=None):
    """Write the board's edits into scrape_config.json. Returns (cfg, error).

    Only the three blocks the panel owns are touched; every other key in the
    file, including the hand-written searches, is kept as it is."""
    import job_filters  # noqa: E402

    path = path or CONFIG
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        cfg = {}
    except ValueError:
        return None, "scrape_config.json is not valid JSON - fix it by hand first"
    if "search_preferences" in payload:
        block, error = clean(payload["search_preferences"])
        if error:
            return None, error
        cfg["search_preferences"] = block
    if "filters" in payload:
        raw = payload["filters"]
        if not isinstance(raw, dict):
            return None, "filters must be an object"
        groups = raw.get("groups") or {}
        body = raw.get("body") or {}
        if (not isinstance(groups, dict) or any(k not in job_filters.GROUP_KEYS or not isinstance(v, bool)
                                                for k, v in groups.items())):
            return None, "filters.groups must map known groups to true/false"
        body_keys = {key for key, _, _ in job_filters.BODY_RULES}
        if not isinstance(body, dict) or any(k not in body_keys or not isinstance(v, bool)
                                             for k, v in body.items()):
            return None, "filters.body must map known rules to true/false"
        allow = raw.get("allow") or []
        if not isinstance(allow, list) or len(allow) > 50:
            return None, "filters.allow must be a list of at most 50 words"
        block = dict(cfg.get("filters") or {})
        block.update(groups=groups, body=body,
                     allow=[" ".join(str(w).split())[:60] for w in allow if str(w).strip()])
        cfg["filters"] = block
    if "keyword_search_budget" in payload:
        try:
            value = int(payload["keyword_search_budget"])
        except (TypeError, ValueError):
            return None, "keyword_search_budget must be a number"
        cfg["keyword_search_budget"] = max(1, min(value, SEARCH_BUDGET_CEILING))
    jobs_md.write_json_atomic(path, cfg)
    return cfg, None


def main():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    plan = searches(cfg)
    for source, specs in plan.items():
        print("%s: %d searches" % (source, len(specs)))
        for spec in specs:
            print("  %s %s" % ("custom   " if spec.get("custom") else "generated",
                               {k: v for k, v in spec.items() if k not in ("custom", "generated")}))
    print("search budget per fetch: %d" % search_budget(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
