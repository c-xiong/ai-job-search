#!/usr/bin/env python3
"""Import jobs collected from an authenticated LinkedIn browser session.

The browser side supplies JSON; this module validates the whole batch before it
touches state, then reuses the repository's canonical merge and persistence
paths. It performs no network access and never handles LinkedIn credentials.
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ats_fetch  # noqa: E402
import jobs_md  # noqa: E402
import postings  # noqa: E402


PORTAL = "linkedin-browser"
COMPANY_PORTAL = "company-careers"
LINKEDIN_JOB_URL = re.compile(r"^https://www\.linkedin\.com/jobs/view/(\d{6,})$")
ATS_HOST_PATTERNS = (
    ("workday", re.compile(r"(?:^|\.)(?:myworkdayjobs\.com|myworkdaysite\.com)$", re.I)),
    ("workable", re.compile(r"(?:^|\.)workable\.com$", re.I)),
    ("teamtailor", re.compile(r"(?:^|\.)teamtailor\.com$", re.I)),
    ("recruitee", re.compile(r"(?:^|\.)recruitee\.com$", re.I)),
    ("bamboohr", re.compile(r"(?:^|\.)bamboohr\.com$", re.I)),
    ("successfactors", re.compile(r"(?:^|\.)successfactors\.com$", re.I)),
    ("icims", re.compile(r"(?:^|\.)icims\.com$", re.I)),
    ("taleo", re.compile(r"(?:^|\.)taleo\.net$", re.I)),
    ("jobvite", re.compile(r"(?:^|\.)jobvite\.com$", re.I)),
)


class InputError(ValueError):
    """The browser export is malformed; no state should be written."""


def load_payload(path):
    if str(path) == "-":
        return json.load(sys.stdin)
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def _optional_text(row, field, index):
    value = row.get(field)
    if value is None:
        return None if field == "deadline" else ""
    if not isinstance(value, str):
        raise InputError("job %d: %s must be a string or null" % (index, field))
    return value.strip()


def identify_ats(url):
    """Return (vendor, stable ATS id) when the final application URL reveals one."""
    composite_id = jobs_md.composite_id_for_url(url)
    if composite_id:
        return composite_id.split(":", 1)[0], composite_id
    hostname = (urlsplit(url).hostname or "").lower()
    for vendor, pattern in ATS_HOST_PATTERNS:
        if pattern.search(hostname):
            return vendor, ""
    return "", ""


def _canonical_linkedin_url(url, index):
    canonical = jobs_md.canonical_url(url.strip())
    match = LINKEDIN_JOB_URL.fullmatch(canonical or "")
    if not match:
        raise InputError(
            "job %d: linkedin_url must be a LinkedIn job-detail URL with a numeric ID" % index)
    return canonical, match.group(1)


def _canonical_application_url(url, index):
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InputError("job %d: url must be an absolute HTTP(S) URL" % index)
    canonical = jobs_md.canonical_url(url.strip())
    if "linkedin.com" in (urlsplit(canonical).hostname or "") and not LINKEDIN_JOB_URL.fullmatch(canonical):
        raise InputError("job %d: url is a LinkedIn wrapper, not a job-detail or company URL" % index)
    return canonical


def normalize_rows(payload):
    """Validate an entire browser payload and return canonical merge rows."""
    if isinstance(payload, dict):
        unknown = set(payload) - {"jobs"}
        if unknown:
            raise InputError("top-level object only supports the 'jobs' key")
        payload = payload.get("jobs")
    if not isinstance(payload, list):
        raise InputError("input must be a JSON array or an object with a 'jobs' array")

    rows = []
    seen_ids = set()
    duplicates_in_input = 0
    for index, raw in enumerate(payload, 1):
        if not isinstance(raw, dict):
            raise InputError("job %d: expected an object" % index)
        for field in ("title", "company", "url"):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise InputError("job %d: %s is required" % (index, field))

        application_url = _canonical_application_url(raw["url"], index)
        raw_linkedin_url = raw.get("linkedin_url")
        if raw_linkedin_url is None and LINKEDIN_JOB_URL.fullmatch(application_url):
            raw_linkedin_url = application_url
        if not isinstance(raw_linkedin_url, str) or not raw_linkedin_url.strip():
            raise InputError("job %d: linkedin_url is required when url is a company page" % index)
        linkedin_url, job_id = _canonical_linkedin_url(raw_linkedin_url, index)
        if job_id in seen_ids:
            duplicates_in_input += 1
            continue
        seen_ids.add(job_id)

        row = {
            "id": job_id,
            "title": raw["title"].strip(),
            "company": raw["company"].strip(),
            "url": application_url,
            "linkedin_url": linkedin_url,
            "location": _optional_text(raw, "location", index),
            "posted": _optional_text(raw, "posted", index),
            "deadline": _optional_text(raw, "deadline", index),
            "description": _optional_text(raw, "description", index),
        }
        row["ats_vendor"], row["ats_id"] = identify_ats(application_url)
        if "prefit_score" in raw:
            score = raw["prefit_score"]
            if (not isinstance(score, (int, float)) or isinstance(score, bool)
                    or not 0 <= score <= 100):
                raise InputError("job %d: prefit_score must be a number from 0 to 100" % index)
            row["prefit_score"] = score
        if "prefit_reasons" in raw:
            reasons = raw["prefit_reasons"]
            if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
                raise InputError("job %d: prefit_reasons must be an array of strings" % index)
            row["prefit_reasons"] = reasons
        rows.append(row)

    return rows, duplicates_in_input


def _add_stats(total, current):
    for key, value in current.items():
        if key == "german_gated_by_company":
            for company, count in value.items():
                total[key][company] = total[key].get(company, 0) + count
        else:
            total[key] += value


def _entry_for_source(seen, url):
    canonical = jobs_md.canonical_url(url)
    matches = []
    for key, entry in seen.items():
        known = {key, entry.get("url")}
        known.update(source.get("url") for source in entry.get("sources") or [])
        if canonical in {jobs_md.canonical_url(item) for item in known if item}:
            matches.append(entry)
    return matches[0] if len(matches) == 1 else None


def import_rows(seen, rows, today, log=lambda _message: None, pending=None, portal=PORTAL):
    """Merge browser rows, preferring final company URLs while retaining LinkedIn provenance.

    `pending` collects the posting bodies whose sidecars still need writing, so
    a `--dry-run` merges in memory and leaves the filesystem untouched.
    `portal` names the LinkedIn side: the board's Add job uses `manual`, so a
    posting the owner added by hand says so.
    """
    total = {"added": 0, "gated": 0, "already_known": 0, "collapsed": 0,
             "possible_duplicates": 0, "german_gated_by_company": {}}
    # One import is one batch, so the board shows the whole paste as a block
    # rather than as one row per second of merging.
    stamp = ats_fetch.run_stamp()
    for row in rows:
        linkedin_url = row["linkedin_url"]
        if row["url"] == linkedin_url:
            _add_stats(total, ats_fetch.merge(seen, [row], today, log, portal=portal,
                                              pending=pending, stamp=stamp))
            continue

        source_portal = "ats-search" if row.get("ats_id") else COMPANY_PORTAL
        application_row = dict(row, id=row.get("ats_id") or "")
        _add_stats(total, ats_fetch.merge(
            seen, [application_row], today, log, portal=source_portal, pending=pending,
            stamp=stamp))
        entry = _entry_for_source(seen, row["url"])
        if entry is None:
            raise InputError("could not resolve imported company URL after merge: %s" % row["url"])

        linkedin_source = {
            "portal": portal,
            "url": linkedin_url,
            "id": row["id"],
            "first_seen": today,
        }
        ats_fetch.seed_history(entry, entry.get("url") or row["url"])
        ats_fetch.append_source(entry, linkedin_source)
        entry["primary_source"] = source_portal
        entry["last_seen_at"] = today
        if row.get("ats_vendor"):
            entry["ats_vendor"] = row["ats_vendor"]
            for source in entry.get("sources") or []:
                if source.get("portal") == source_portal and source.get("url") == row["url"]:
                    source["ats_vendor"] = row["ats_vendor"]
    return total


def persist(seen):
    """Write the sole canonical state; the board reads this file directly."""
    with jobs_md.board_lock():
        jobs_md.save_seen(seen)


def _load_seen():
    if not jobs_md.SEEN.exists():
        return {}
    payload = json.loads(jobs_md.SEEN.read_text(encoding="utf-8"))
    seen = payload.get("seen", {})
    if not isinstance(seen, dict):
        raise InputError("job_scraper/seen_jobs.json has no valid 'seen' object")
    return seen


def _known_linkedin_ids(seen):
    """Every LinkedIn job id the board already holds, under any key or source."""
    ids = set()
    for key, entry in seen.items():
        urls = [key, entry.get("url")]
        for source in entry.get("sources") or []:
            urls.append(source.get("url"))
            if source.get("portal") == PORTAL and source.get("id"):
                ids.add(str(source["id"]))
        for url in urls:
            match = LINKEDIN_JOB_URL.fullmatch(jobs_md.canonical_url(url or "") or "")
            if match:
                ids.add(match.group(1))
    return ids


def check_cards(seen, payload, aliases=None):
    """Triage list-page cards before any detail page is opened.

    Detail pages are the expensive part of a browser import, so the browser
    side sends only what a results list shows (title, company, LinkedIn URL)
    and opens the pages this returns as `new`. `known` is an exact LinkedIn id
    hit. `likely_known` is the same company and title already on the board
    from another source; it is reported rather than hidden, because two
    requisitions can share a title.
    """
    if isinstance(payload, dict):
        payload = payload.get("jobs")
    if not isinstance(payload, list):
        raise InputError("input must be a JSON array or an object with a 'jobs' array")
    if aliases is None:
        aliases = ats_fetch.alias_map(ats_fetch.load_registry())
    known_ids = _known_linkedin_ids(seen)
    result = {"new": [], "known": [], "likely_known": []}
    batch = set()
    for index, raw in enumerate(payload, 1):
        if not isinstance(raw, dict):
            raise InputError("job %d: expected an object" % index)
        raw_url = raw.get("linkedin_url") or raw.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise InputError("job %d: linkedin_url is required" % index)
        linkedin_url, job_id = _canonical_linkedin_url(raw_url, index)
        if job_id in batch:
            continue
        batch.add(job_id)
        card = {"linkedin_url": linkedin_url, "title": (raw.get("title") or "").strip(),
                "company": (raw.get("company") or "").strip()}
        if job_id in known_ids:
            result["known"].append(card)
            continue
        match = next((key for key, entry in seen.items()
                      if card["title"] and ats_fetch._norm(entry.get("title")) == ats_fetch._norm(card["title"])
                      and ats_fetch.same_company(entry.get("company"), card["company"], aliases)), None)
        if match:
            result["likely_known"].append(dict(card, matches=match))
        else:
            result["new"].append(card)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", help="browser-export JSON file, or - for stdin")
    parser.add_argument("--dry-run", action="store_true", help="validate and merge in memory only")
    parser.add_argument("--check", action="store_true",
                        help="triage list-page cards against the board; writes nothing")
    parser.add_argument("--date", default=date.today().isoformat(), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.check:
        try:
            result = check_cards(_load_seen(), load_payload(args.input))
        except (InputError, OSError, json.JSONDecodeError) as exc:
            print("check failed: %s" % exc, file=sys.stderr)
            return 2
        result["counts"] = {k: len(v) for k, v in result.items()}
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    try:
        rows, duplicates_in_input = normalize_rows(load_payload(args.input))
        messages = []
        pending = []
        with jobs_md.board_lock():
            seen = _load_seen()
            stats = import_rows(seen, rows, args.date, messages.append, pending)
            if not args.dry_run:
                # Bodies before state, and neither on a dry run.
                postings.commit_all(pending)
                persist(seen)
    except (InputError, OSError, json.JSONDecodeError) as exc:
        print("import failed: %s" % exc, file=sys.stderr)
        return 2

    for message in messages:
        print(message, file=sys.stderr)
    summary = dict(stats)
    summary.update({
        "received": len(rows) + duplicates_in_input,
        "accepted": len(rows),
        "duplicates_in_input": duplicates_in_input,
        "dry_run": args.dry_run,
    })
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
