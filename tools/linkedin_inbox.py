"""Durable, local staging for LinkedIn cards, alert emails and saved-job URLs.

Capture is offline. Processing uses bounded guest detail requests and the existing
board merge; no authenticated browsing, credentials, or model calls are involved.
"""

import copy
import json
import re
import sys
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_linkedin_browser as importer  # noqa: E402
import jobs_md  # noqa: E402
import posting_text  # noqa: E402
import postings  # noqa: E402
from board import add_job  # noqa: E402

INBOX = jobs_md.ROOT / "job_scraper" / "linkedin_inbox.json"
MAX_CARDS = 250
MAX_BYTES = 256 * 1024
DETAIL_CEILING = 20
STATES = ("pending", "processing", "imported", "excluded", "retry", "needs_manual")
ID = re.compile(r"[0-9]{6,20}")
InputError = importer.InputError
_STATE_LOCK = threading.RLock()
_PROCESS_LOCK = threading.Lock()


class AlreadyProcessing(RuntimeError):
    """Another inbox processor is active."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _locked(process=False, disk=True):
    local = _PROCESS_LOCK if process else _STATE_LOCK
    if not local.acquire(blocking=not process):
        raise AlreadyProcessing("LinkedIn inbox processing is already running")
    handle = None
    try:
        if disk and jobs_md.fcntl is not None:
            INBOX.parent.mkdir(parents=True, exist_ok=True)
            name = ".linkedin-process.lock" if process else ".linkedin-inbox.lock"
            handle = (INBOX.parent / name).open("a+")
            operation = jobs_md.fcntl.LOCK_EX
            if process:
                operation |= jobs_md.fcntl.LOCK_NB
            try:
                jobs_md.fcntl.flock(handle.fileno(), operation)
            except BlockingIOError as exc:
                raise AlreadyProcessing("LinkedIn inbox processing is already running") from exc
        yield
    finally:
        if handle is not None:
            handle.close()
        local.release()


def _load():
    if not INBOX.exists():
        return {"version": 1, "last_capture_at": None, "items": {}}
    try:
        data = json.loads(INBOX.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise InputError("LinkedIn inbox state is unreadable; refusing to overwrite it") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        raise InputError("LinkedIn inbox state is malformed")
    for job_id, item in data["items"].items():
        if (not isinstance(job_id, str) or not ID.fullmatch(job_id)
                or not isinstance(item, dict) or item.get("job_id") != job_id
                or item.get("state") not in STATES
                or isinstance(item.get("attempts"), bool)
                or not isinstance(item.get("attempts"), int) or item["attempts"] < 0
                or not isinstance(item.get("origins"), list)
                or any(not isinstance(value, str) for value in item["origins"])
                or item.get("linkedin_url") != "https://www.linkedin.com/jobs/view/" + job_id):
            raise InputError("LinkedIn inbox contains malformed cards; refusing to overwrite it")
    return data


def _save(data):
    jobs_md.write_json_atomic(INBOX, data)


def _counts(data):
    counts = dict.fromkeys(STATES, 0)
    for item in data["items"].values():
        if item.get("state") in counts:
            counts[item["state"]] += 1
    return counts


def listing():
    # Atomic replacement makes a read coherent without changing any lock files.
    data = _load()
    items = sorted(data["items"].values(),
                   key=lambda item: item.get("last_captured_at", ""), reverse=True)
    return {"items": items, "counts": _counts(data),
            "last_capture_at": data.get("last_capture_at")}


def _string(raw, key, limit, index):
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise InputError("card %d: invalid or oversized %s" % (index, key))
    return value.strip()


def _cards(payload):
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise InputError("capture must contain JSON data") from exc
    if size > MAX_BYTES:
        raise InputError("capture exceeds the 256 KB limit")
    if isinstance(payload, dict):
        if set(payload) - {"jobs"}:
            raise InputError("capture object only supports the jobs key")
        payload = payload.get("jobs")
    if not isinstance(payload, list) or len(payload) > MAX_CARDS:
        raise InputError("capture must contain at most 250 cards")
    cards = []
    for index, raw in enumerate(payload, 1):
        if not isinstance(raw, dict):
            raise InputError("card %d: expected an object" % index)
        raw_id = raw.get("job_id") or raw.get("id")
        if raw_id is not None and (isinstance(raw_id, bool) or not ID.fullmatch(str(raw_id))):
            raise InputError("card %d: invalid LinkedIn job ID" % index)
        raw_url = raw.get("linkedin_url") or raw.get("url") or ""
        linked_id = None
        if raw_url:
            raw_url = _string({"url": raw_url}, "url", 4000, index)
            try:
                parts = urlsplit(raw_url)
                port = parts.port
            except ValueError as exc:
                raise InputError("card %d: invalid URL" % index) from exc
            host = (parts.hostname or "").lower()
            if host == "linkedin.com" or host.endswith(".linkedin.com"):
                if (parts.scheme not in ("http", "https") or parts.username or parts.password
                        or port not in (None, 80, 443)):
                    raise InputError("card %d: invalid LinkedIn URL" % index)
                linked_id = add_job.linkedin_id(raw_url)
                if linked_id is None:
                    raise InputError("card %d: URL is not a LinkedIn posting" % index)
            elif not raw_id:
                raise InputError("card %d: a LinkedIn URL or job ID is required" % index)
        job_id = str(raw_id) if raw_id is not None else linked_id
        if not job_id or not ID.fullmatch(job_id) or (linked_id and linked_id != job_id):
            raise InputError("card %d: missing or conflicting LinkedIn job ID" % index)
        card = {"job_id": job_id,
                "linkedin_url": "https://www.linkedin.com/jobs/view/" + job_id}
        for field, maximum in (("title", 500), ("company", 300), ("location", 500),
                               ("posted", 100), ("posted_date", 100), ("description", 60000)):
            card[field] = _string(raw, field, maximum, index)
        if not card["posted"] and "date" in raw:
            card["posted"] = _string(raw, "date", 100, index)
        badges = raw.get("badges") or []
        if isinstance(badges, str):
            badges = [badges]
        if (not isinstance(badges, list) or len(badges) > 12
                or any(not isinstance(badge, str) or len(badge) > 100 for badge in badges)):
            raise InputError("card %d: invalid badges" % index)
        card["badges"] = list(dict.fromkeys(badge.strip() for badge in badges if badge.strip()))
        apply = raw.get("apply_url") or raw.get("applyUrl") or ""
        if not apply and raw_id and raw_url and not linked_id:
            apply = raw_url
        if apply:
            card["apply_url"] = add_job.external_apply_url(apply)
            if not card["apply_url"]:
                raise InputError("card %d: invalid external application URL" % index)
        cards.append(card)
    return cards


def capture(payload, dry_run=False, origin="bookmarklet"):
    cards = _cards(payload)  # Validate the whole batch before any mutation.
    if not isinstance(origin, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", origin):
        raise InputError("invalid capture origin")
    stamp = _now()
    triage = importer.check_cards(importer._load_seen(), cards)
    possible = {card["linkedin_url"]: card["matches"] for card in triage["likely_known"]}
    known = {card["linkedin_url"] for card in triage["known"]}
    with _locked(disk=not dry_run):
        data = _load()
        added = updated = duplicates = 0
        for card in cards:
            item = data["items"].get(card["job_id"])
            if item is None:
                item = dict(card, state="pending", attempts=0, reason="",
                            next_retry_at=None, first_captured_at=stamp,
                            last_captured_at=stamp, origins=[origin])
                data["items"][card["job_id"]] = item
                added += 1
            else:
                duplicates += 1
                changed = False
                for key, value in card.items():
                    if value and item.get(key) != value:
                        if key == "badges":
                            value = list(dict.fromkeys((item.get(key) or []) + value))[:12]
                        item[key] = value
                        changed = True
                if changed:
                    updated += 1
                    if item["state"] == "needs_manual":
                        item.update(state="pending", reason="", next_retry_at=None)
                item["last_captured_at"] = stamp
                if origin not in item["origins"]:
                    item["origins"].append(origin)
            item["dedup_status"] = ("known" if card["linkedin_url"] in known else
                                    "likely_known" if card["linkedin_url"] in possible else "new")
            if card["linkedin_url"] in possible:
                item["possible_duplicate"] = possible[card["linkedin_url"]]
        data["last_capture_at"] = stamp
        if not dry_run:
            _save(data)
        return {"received": len(cards), "added": added, "updated": updated,
                "duplicates": duplicates, "dry_run": dry_run,
                "counts": _counts(data), "last_capture_at": stamp}


def resolve_detail(job_id):
    """Network seam shared by the inbox worker and fixture-based tests."""
    return add_job.resolve_linkedin_detail(job_id)


def _known(seen, card):
    entry = importer._entry_for_source(seen, card["linkedin_url"])
    try:
        if entry is not None and posting_text.check(postings.load(entry))[0]:
            return entry
    except (OSError, ValueError):
        pass
    return None


def _import(card, detail, dry_run, simulated_seen=None):
    row = dict(card)
    if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}", card.get("posted_date", "")):
        row["posted"] = card["posted_date"][:10]
    for field in ("title", "company", "location", "posted", "description"):
        if detail.get(field):
            row[field] = detail[field]
    if not row.get("title") or not row.get("company"):
        raise add_job.LinkedInDetailError("Missing title or company; complete the posting manually",
                                         retryable=False)
    valid, reason = posting_text.check(row.get("description"))
    if not valid:
        raise add_job.LinkedInDetailError("Posting body unavailable: " + reason, retryable=False)
    row["url"] = (add_job.external_apply_url(detail.get("apply_url"))
                  or card.get("apply_url") or card["linkedin_url"])
    rows, _ = importer.normalize_rows([row])
    pending = []
    with nullcontext() if dry_run else jobs_md.board_lock():
        seen = simulated_seen if dry_run else importer._load_seen()
        stats = importer.import_rows(seen, rows, _now()[:10], pending=pending)
        entry = importer._entry_for_source(seen, row["url"])
        if entry is None:
            raise InputError("Imported posting could not be found")
        if not dry_run:
            postings.commit_all(pending)
            importer.persist(seen)
        hard_german = importer.ats_fetch.collectors.german_hit(row["description"])[0] == "hard"
        result = {"state": "excluded" if hard_german else "imported",
                "reason": "German stated as a job condition" if hard_german else "",
                "board_url": jobs_md.entry_key(seen, row["url"]),
                "apply_url": row["url"] if row["url"] != card["linkedin_url"] else "",
                "next_retry_at": None, "merge_stats": stats}
        result.update({field: row[field] for field in ("title", "company", "location", "posted")})
        return result


def _retry(attempts, reason):
    delay = min(24 * 60, 15 * (2 ** min(max(attempts - 1, 0), 7)))
    return {"state": "retry", "reason": reason,
            "next_retry_at": (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat()}


def process(limit=15, dry_run=False):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= DETAIL_CEILING:
        raise InputError("detail limit must be an integer from 0 to 20")
    summary = dict(processed=0, imported=0, excluded=0, retry=0, needs_manual=0,
                   known=0, added=0, collapsed=0, already_known=0,
                   detail_calls=0, dry_run=dry_run)
    with _locked(process=True, disk=not dry_run):
        with _locked(disk=not dry_run):
            data = _load()
            # Owning the process lock proves any previous processing marks were
            # interrupted. A retry after board commit is safe because IDs dedup.
            for item in data["items"].values():
                if item.get("state") == "processing":
                    item.update(state="retry", reason="Previous processing was interrupted",
                                next_retry_at=None)
            if not dry_run:
                _save(data)
            candidates = sorted((copy.deepcopy(item) for item in data["items"].values()
                                 if item["state"] in ("pending", "retry")
                                 and (not item.get("next_retry_at") or item["next_retry_at"] <= _now())),
                                key=lambda item: item.get("first_captured_at", ""))[:MAX_CARDS]
        simulated_seen = importer._load_seen() if dry_run else None
        for card in candidates:
            seen = simulated_seen if dry_run else importer._load_seen()
            known = _known(seen, card)
            if known is None and summary["detail_calls"] >= limit:
                break
            card["attempts"] += 1
            if not dry_run:
                with _locked():
                    data = _load()
                    data["items"][card["job_id"]].update(
                        state="processing", attempts=card["attempts"], started_at=_now())
                    _save(data)
            stop = False
            try:
                if known is not None:
                    result = {"state": "excluded" if jobs_md.user_status(known) == "gate" else "imported",
                              "reason": "Already on the board",
                              "next_retry_at": None,
                              "board_url": jobs_md.entry_key(seen, card["linkedin_url"])}
                    result.update({field: card.get(field) or known.get(field) or ""
                                   for field in ("title", "company", "location", "posted")})
                    summary["known"] += 1
                    summary["already_known"] += 1
                else:
                    summary["detail_calls"] += 1
                    detail = resolve_detail(card["job_id"])
                    result = _import(card, detail, dry_run, simulated_seen)
            except add_job.LinkedInDetailError as exc:
                result = (_retry(card["attempts"], str(exc)) if exc.retryable else
                          {"state": "needs_manual", "reason": str(exc), "next_retry_at": None})
                stop = exc.rate_limited
            except (OSError, ValueError, RuntimeError):
                result = _retry(card["attempts"], "Import could not be persisted; retry pending")
            summary["processed"] += 1
            summary[result["state"]] += 1
            merge_stats = result.pop("merge_stats", {})
            for key in ("added", "collapsed", "already_known"):
                summary[key] += merge_stats.get(key, 0)
            with _locked(disk=not dry_run):
                if not dry_run:
                    data = _load()  # Preserve captures made during the network call.
                data["items"][card["job_id"]].update(result, attempts=card["attempts"],
                                                     finished_at=_now())
                if not dry_run:
                    _save(data)
            if stop:
                summary["rate_limited"] = True
                break
        summary["counts"] = _counts(data)
    return summary
