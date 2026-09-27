"""Deleting applications and failed attempts, with undo.

A delete is soft: the run's folders move under `.trash/` and its registry record
gains a `deleted_at` tombstone. The record is never removed, because it carries
the reported cost that `application_spent()` and the daily ledger add up -
deleting an attempt must not hand its budget back. Tombstoned runs are left out
of `/api/runs` and refused by every run action except undo. Trash older than
`TRASH_DAYS` is purged at board start; the tombstones stay.

What a delete never touches: the published `cv/main_*.tex` and cover letter,
`documents/applications/<slug>/job_posting.md`, the tracker row and the Notion
page. Those are the record of what was sent.

Stdlib only, Python 3.9+.
"""

import os
import shutil
import time
from datetime import datetime

from . import activity, docs, run_registry

TRASH_DAYS = 14
# A single attempt can be deleted only when it produced nothing you may have
# sent. A finished attempt is a version, and goes with its application.
ATTEMPT_PHASES = ("failed", "cancelled")
# Nothing that is (or may still be) doing work: `orphaned` means a model
# process may be alive.
BUSY_OK = run_registry.TERMINAL + ("awaiting_approval",)


class TrashError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def is_deleted(record):
    return bool(record and record.get("deleted_at"))


def _application_of(record):
    return record.get("application_id") or record["id"]


def family(runs, record):
    """Every attempt of `record`'s application: the same application id, plus
    anything that continues or regenerates an attempt already in the set."""
    key = _application_of(record)
    found = {r["id"]: r for r in runs if _application_of(r) == key}
    found[record["id"]] = record
    grew = True
    while grew:
        grew = False
        for r in runs:
            if r["id"] not in found and (r.get("continue_of") in found
                                         or r.get("retry_of") in found
                                         or r.get("parent") in found):
                found[r["id"]] = r
                grew = True
    return list(found.values())


def _sent(record):
    """Tracker status past `drafted` - the application went out."""
    status, _since = docs.tracker_statuses().get(
        docs._tracker_key(record.get("company"), record.get("role")), ("", ""))
    return status not in ("", "drafted")


def _trash_paths(run_id):
    return ((run_registry.run_dir(run_id), run_registry.RUN_DIRS / ".trash" / run_id),
            (run_registry.state_dir(run_id), run_registry.RUN_STATE / ".trash" / run_id))


def _move(source, target):
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    source.rename(target)


def delete(run_id, scope, confirm=""):
    """Tombstone and trash an application ("application") or one failed attempt
    ("attempt"). Returns the deleted ids."""
    if scope not in ("application", "attempt"):
        raise TrashError("scope must be application or attempt")
    with run_registry.runs_lock():
        data = run_registry.load()
        record = next((r for r in data["runs"] if r["id"] == run_id), None)
        if record is None or is_deleted(record):
            raise TrashError("unknown run", 404)
        alive = [r for r in data["runs"] if not is_deleted(r)]
        if scope == "attempt":
            if record.get("phase") not in ATTEMPT_PHASES:
                raise TrashError("only a failed or cancelled attempt can be deleted on its "
                                 "own; delete the whole application instead", 409)
            targets = [record]
        else:
            targets = family(alive, record)
        busy = [r for r in targets if r.get("phase") not in BUSY_OK]
        if busy:
            raise TrashError("an attempt is still %s - cancel or kill it first"
                             % busy[0].get("phase", "running").replace("_", " "), 409)
        if scope == "application" and _sent(record):
            wanted = (record.get("company") or "").strip().lower()
            if not wanted or (confirm or "").strip().lower() != wanted:
                raise TrashError("this application was sent; type the company name to "
                                 "delete it", 409)
        stamp = datetime.now().isoformat(timespec="seconds")
        ids = {r["id"] for r in targets}
        for r in data["runs"]:
            if r["id"] in ids:
                r["deleted_at"] = stamp
        run_registry.store(data)
    for target_id in ids:
        for source, target in _trash_paths(target_id):
            _move(source, target)
            if target.exists():
                # A rename keeps the folder's old mtime; the purge clock has to
                # start at the delete, not at the run.
                os.utime(target)
    activity.emit("board", "deleted %s: %s - %s (%d attempt%s, moved to trash)"
                  % (scope, record.get("company"), record.get("role"), len(ids),
                     "" if len(ids) == 1 else "s"), run_id=run_id)
    return sorted(ids)


def undo(run_ids):
    """Bring tombstoned runs back: folders out of the trash, tombstones cleared."""
    ids = set(run_ids or ())
    if not ids:
        raise TrashError("nothing to restore")
    restored = []
    with run_registry.runs_lock():
        data = run_registry.load()
        for r in data["runs"]:
            if r["id"] in ids and is_deleted(r):
                trashed = run_registry.RUN_DIRS / ".trash" / r["id"]
                if not trashed.exists() and not run_registry.run_dir(r["id"]).exists():
                    continue  # purged: the record stays a tombstone
                r.pop("deleted_at", None)
                restored.append(r["id"])
        if restored:
            run_registry.store(data)
    for run_id in restored:
        for target, source in _trash_paths(run_id):
            _move(source, target)
    if not restored:
        raise TrashError("nothing could be restored - the trash was already emptied", 410)
    activity.emit("board", "restored %d deleted attempt%s from the trash"
                  % (len(restored), "" if len(restored) == 1 else "s"))
    return restored


def purge(days=TRASH_DAYS, now=None):
    """Remove trashed folders older than `days`. Returns how many were removed."""
    cutoff = (now or time.time()) - days * 86400
    removed = 0
    for base in (run_registry.RUN_DIRS / ".trash", run_registry.RUN_STATE / ".trash"):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                    removed += 1
            except OSError:
                continue
    return removed
