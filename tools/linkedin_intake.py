#!/usr/bin/env python3
"""Optional local email and official SAVED_JOBS intake into the LinkedIn inbox.

No mailbox connection or browser session is used. OAuth provisioning is a user
setup step. Only a token environment-variable name belongs in configuration.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import fcntl
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from board import fetch_url

STATE = ROOT / "job_scraper" / "linkedin_intake_state.json"
LOCK = ROOT / "job_scraper" / ".linkedin-intake.lock"
API = "https://api.linkedin.com/rest/memberSnapshotData"
API_VERSION = "202312"
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_API_BYTES = 2 * 1024 * 1024
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.I)
_INTAKE_LOCK = threading.RLock()


class IntakeError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bounded(value, default, ceiling, floor=1):
    try:
        return max(floor, min(int(value), ceiling))
    except (TypeError, ValueError):
        return default


def job_id(value, depth=0):
    """Read IDs only from LinkedIn job URLs; never follow email tracking links."""
    if not isinstance(value, str) or len(value) > 8192 or depth > 2:
        return None
    value = unescape(value).strip().rstrip(".,;)")
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    if parts.username or parts.password:
        return None
    path = unquote(parts.path)
    match = re.search(r"/(?:comm/)?jobs/view/(?:[^/]*-)?(\d{6,20})(?:/|$)", path)
    if match:
        return match.group(1)
    query = parse_qs(parts.query)
    for key in ("currentJobId", "jobId"):
        for item in query.get(key, []):
            if re.fullmatch(r"\d{6,20}", item):
                return item
    for key in ("url", "redirect", "redirectUrl", "destination"):
        for item in query.get(key, []):
            found = job_id(item, depth + 1)
            if found:
                return found
    return None


class _EmailLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.hidden = 0
        self.visible = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag == "a" and not self.hidden:
            self.current = [dict(attrs).get("href", ""), []]

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "a" and self.current:
            self.links.append((self.current[0], " ".join(self.current[1])))
            self.current = None

    def handle_data(self, data):
        if not self.hidden:
            self.visible.append(data)
        if self.current and not self.hidden:
            self.current[1].append(data.strip())


def _email_parts(part):
    if part.get_content_disposition() == "attachment":
        return
    if part.is_multipart():
        for child in part.iter_parts():
            yield from _email_parts(child)
    else:
        yield part


def parse_email(raw):
    if len(raw) > MAX_MESSAGE_BYTES:
        raise IntakeError("email exceeds the 2 MB limit")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    cards = {}
    for part in _email_parts(message):
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            continue
        if not isinstance(body, str):
            continue
        links = []
        if part.get_content_type() == "text/html":
            parser = _EmailLinks()
            parser.feed(body)
            links = parser.links
            body = " ".join(parser.visible)
        links += [(value, "") for value in URL_PATTERN.findall(unescape(body))]
        for url, text in links:
            ident = job_id(url)
            if not ident:
                continue
            card = cards.setdefault(ident, {"job_id": ident, "linkedin_url": "https://www.linkedin.com/jobs/view/" + ident})
            title = " ".join(text.split())[:250]
            if title and title.lower() not in ("view job", "view jobs", "apply", "apply now", "see job", "save", "view", "see more jobs"):
                if not card.get("title"):
                    card["title"] = title
    return list(cards.values())


@contextmanager
def _lock(disk=True):
    with _INTAKE_LOCK:
        if not disk:
            yield
            return
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with LOCK.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _load_state():
    if not STATE.exists():
        return {"version": 1, "email_digests": []}
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("email_digests", []), list):
            raise ValueError("invalid intake state")
        return state
    except (OSError, ValueError) as exc:
        raise IntakeError("intake state is unreadable; refusing to overwrite it") from exc


def _save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_name(STATE.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(STATE)
    finally:
        temporary.unlink(missing_ok=True)


def _capture(cards, dry_run, origin):
    import linkedin_inbox
    return linkedin_inbox.capture(cards, dry_run=dry_run, origin=origin)


def import_emails(spec, dry_run=False):
    directory = Path(spec.get("directory") or "job_scraper/linkedin_emails").expanduser()
    if not directory.is_absolute():
        directory = ROOT / directory
    if not directory.is_dir():
        return {"status": "unconfigured", "messages": 0, "cards": 0, "note": "email intake directory does not exist"}
    limit = _bounded(spec.get("max_messages"), 30, 100)
    result = {"status": "ok", "messages": 0, "cards": 0, "failed": 0}
    with _lock(disk=not dry_run):
        state = _load_state()
        seen = set(state.get("email_digests", []))
        digests = []
        # The configured directory is explicitly local input. Do not recurse
        # into a mailbox, read attachments or follow filesystem symlinks.
        for path in sorted(directory.glob("*.eml")):
            if result["messages"] >= limit:
                break
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    raw = handle.read(MAX_MESSAGE_BYTES + 1)
                digest = hashlib.sha256(raw).hexdigest()
                if digest in seen:
                    continue
                result["messages"] += 1
                cards = parse_email(raw)
                if cards:
                    _capture(cards, dry_run, "email")
                result["cards"] += len(cards)
                seen.add(digest)
                digests.append(digest)
            except (OSError, ValueError, IntakeError):
                result["failed"] += 1
                continue
        if digests and not dry_run:
            state["email_digests"] = list(dict.fromkeys(state.get("email_digests", []) + digests))[-10000:]
            state["last_email_at"] = _now()
            _save_state(state)
    if result["failed"]:
        result["status"] = "partial"
    return result


def request_snapshot(token, start, count):
    """Fixed public API host, pinned TLS, no redirects or credential-bearing logs."""
    query = urlencode({"q": "criteria", "domain": "SAVED_JOBS", "start": start, "count": count})
    url = API + "?" + query
    connection = None
    response = None
    try:
        parts, addresses = fetch_url.check_url(url)
        deadline = time.monotonic() + 20
        connection = fetch_url.open_connection(parts, addresses[0], 20)
        connection.request("GET", parts.path + "?" + parts.query, headers={
            "Authorization": "Bearer " + token, "Linkedin-Version": API_VERSION,
            "Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close",
            "User-Agent": "job-board-saved-jobs/1.0",
        })
        transport = connection.sock
        fetch_url._retime(transport, deadline, 20)
        response = connection.getresponse()
        chunks, total = [], 0
        while True:
            fetch_url._retime(transport, deadline, 20)
            chunk = fetch_url._read1(response, min(16384, MAX_API_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_API_BYTES:
                raise IntakeError("saved-job API response exceeds the 2 MB limit")
            chunks.append(chunk)
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeError):
            if response.status != 200:
                return response.status, None
            raise IntakeError("saved-job API returned invalid JSON") from None
        if response.status != 200:
            # Includes redirects: never send the bearer token to another host.
            return response.status, data if isinstance(data, dict) else None
        if not isinstance(data, dict):
            raise IntakeError("saved-job API returned an invalid response")
        return 200, data
    except IntakeError:
        raise
    except Exception as exc:
        # Transport exceptions can contain request details. Retain only type.
        raise IntakeError("saved-job API request failed (%s)" % type(exc).__name__) from None
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()


def parse_snapshot(data):
    elements = data.get("elements")
    if not isinstance(elements, list):
        raise IntakeError("saved-job snapshot is missing elements")
    cards = {}
    entries = unparsed = 0
    for element in elements:
        if not isinstance(element, dict) or element.get("snapshotDomain") != "SAVED_JOBS":
            raise IntakeError("unexpected snapshot domain; only SAVED_JOBS is accepted")
        rows = element.get("snapshotData")
        if not isinstance(rows, list):
            raise IntakeError("saved-job snapshotData is not a list")
        for row in rows:
            entries += 1
            if not isinstance(row, dict):
                raise IntakeError("saved-job snapshot entry is not an object")
            normalized = {re.sub(r"[^a-z0-9]", "", str(key).lower()): value for key, value in row.items()}
            ident = next((found for value in row.values() if (found := job_id(value))), None)
            if not ident:
                unparsed += 1
                continue
            card = {"job_id": ident, "linkedin_url": "https://www.linkedin.com/jobs/view/" + ident}
            for target, names in (("title", ("jobtitle", "title")), ("company", ("companyname", "company")),
                                  ("location", ("location", "joblocation"))):
                value = next((normalized[name] for name in names if isinstance(normalized.get(name), str)), "")
                if value:
                    card[target] = value[:300 if target == "company" else 500]
            # Date saved is NOT a posting date. Do not pass it as `posted`.
            cards[ident] = card
    return list(cards.values()), entries, unparsed


def sync_saved_jobs(spec, dry_run=False):
    # A separate process lock prevents a standalone intake command and the
    # scheduled fetch from racing the same bounded pagination cursor.
    with _lock(disk=not dry_run):
        return _sync_saved_jobs_locked(spec, dry_run)


def _sync_saved_jobs_locked(spec, dry_run=False):
    env_name = spec.get("token_env") or "LINKEDIN_PORTABILITY_TOKEN"
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", env_name):
        raise IntakeError("invalid saved-job token environment-variable name")
    token = os.environ.get(env_name, "").strip()
    if not token:
        return {"status": "unconfigured", "pages": 0, "cards": 0, "note": "saved-job OAuth token is not configured"}
    if len(token) > 4096 or any(character.isspace() for character in token):
        raise IntakeError("invalid saved-job OAuth token")
    max_pages = _bounded(spec.get("max_pages"), 3, 10)
    count = _bounded(spec.get("page_size"), 10, 50)
    result = {"status": "ok", "pages": 0, "cards": 0, "deferred": False}
    state = _load_state()
    start = _bounded(state.get("saved_jobs_next_start"), 0, 1000000, floor=0)
    next_start = start
    complete = False
    for page in range(max_pages):
        status, data = request_snapshot(token, start, count)
        result["pages"] += 1
        if status != 200:
            message = str((data or {}).get("message") or "") if isinstance(data, dict) else ""
            if status in (400, 404) and start > 0 and "no data found" in message.lower():
                complete = True
                next_start = 0
                break
            result.update(status="partial" if result["cards"] else "failed", http_status=status)
            if status in (401, 403):
                result["note"] = "OAuth authorization needs attention"
            elif status == 429:
                result["note"] = "rate limited; stopped without retry"
            elif status == 404:
                result["note"] = "snapshot is not available yet or the page has no data"
            break
        cards, entries, unparsed = parse_snapshot(data)
        if cards:
            _capture(cards, dry_run, "saved-jobs-api")
        result["cards"] += len(cards)
        if unparsed:
            result.update(status="partial", unparsed=unparsed,
                          note="saved-job entries have no recognized job URL; verify the account response schema")
            break  # Keep this page due; never advance past unparsed jobs.
        paging = data.get("paging") or {}
        links = (paging.get("links") or []) if isinstance(paging, dict) else []
        if not isinstance(links, list):
            raise IntakeError("saved-job pagination links are invalid")
        next_link = next((link.get("href") for link in links if isinstance(link, dict) and link.get("rel") == "next"), None)
        if next_link:
            # Never follow raw URLs: validate page coordinates and use fixed API.
            parts = urlsplit(next_link)
            if parts.netloc and parts.netloc != "api.linkedin.com":
                raise IntakeError("saved-job pagination points outside the API")
            params = parse_qs(parts.query)
            try:
                next_start = int(params["start"][0])
            except (KeyError, ValueError, TypeError, IndexError):
                raise IntakeError("saved-job pagination has no valid start") from None
            if next_start <= start:
                raise IntakeError("saved-job pagination did not advance")
            start = next_start
        elif entries:
            # Snapshot pages use page numbers. The official API warns that
            # `total` may be incomplete; probe until no data, within our cap.
            start += 1
        else:
            complete = True
            start = 0
            next_start = 0
            break
        next_start = start
        if page == max_pages - 1:
            result["deferred"] = True
    if not dry_run:
        state = _load_state()
        state["saved_jobs_next_start"] = next_start
        if result["status"] == "ok":
            state["last_saved_jobs_at"] = _now()
        _save_state(state)
    result["complete"] = complete
    return result


def ingest_from_config(config, log, dry_run=False):
    intake = config.get("linkedin_intake") or {}
    if not isinstance(intake, dict):
        return {"status": "failed", "note": "linkedin_intake must be an object"}
    result = {}
    for name, function in (("email", import_emails), ("saved_jobs", sync_saved_jobs)):
        spec = intake.get(name) or {}
        if not isinstance(spec, dict) or not spec.get("enabled", False):
            continue
        try:
            result[name] = function(spec, dry_run=dry_run)
        except Exception as exc:
            # Never log exception strings from auth/network/provider code.
            result[name] = {"status": "failed", "error": type(exc).__name__}
        log("LinkedIn %s intake: %s, %s cards" % (name, result[name].get("status"), result[name].get("cards", 0)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "job_scraper" / "scrape_config.json")
    parser.add_argument("--email-dir", help="enable local .eml intake from this directory")
    parser.add_argument("--saved-jobs", action="store_true", help="enable official saved-job sync using the configured token env")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
        intake = config.setdefault("linkedin_intake", {})
        if args.email_dir:
            intake["email"] = {"enabled": True, "directory": args.email_dir}
        if args.saved_jobs:
            intake.setdefault("saved_jobs", {})["enabled"] = True
        result = ingest_from_config(config, lambda _message: None, args.dry_run)
        print(json.dumps(result, indent=2))
        return 1 if any(item.get("status") == "failed" for item in result.values()) else 0
    except (ValueError, OSError, IntakeError):
        print("LinkedIn intake failed; check local configuration", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
