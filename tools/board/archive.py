"""Keep unsubmitted application artifacts while moving them out of the queue."""

from copy import deepcopy
from datetime import datetime
import uuid

from . import activity, run_registry, state, trash
import jobs_md


class ArchiveError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def set_archived(run_id, archived, reason=""):
    if not isinstance(reason, str) or len(reason) > 500:
        raise ArchiveError("Archive reason must be text of at most 500 characters")
    # One lock order for the two local stores. No generated files are moved.
    with jobs_md.board_lock(), run_registry.runs_lock():
        data = run_registry.load()
        record = next((r for r in data["runs"] if r["id"] == run_id and
                       not r.get("deleted_at")), None)
        if record is None:
            raise ArchiveError("unknown run", 404)
        targets = trash.family([r for r in data["runs"] if not r.get("deleted_at")], record)
        if bool(record.get("archived_at")) == archived:
            return {"archived": archived, "board": "unchanged"}
        if any(r.get("phase") not in trash.BUSY_OK for r in targets):
            raise ArchiveError("Cancel the active run before archiving this application", 409)
        seen = state.load()
        before = deepcopy(seen)
        keys = {jobs_md.entry_key(seen, r.get("job_url")) for r in targets if r.get("job_url")}
        keys.discard(None)
        if archived and (trash._sent(record) or any(jobs_md.user_status(seen[k]) == "applied" for k in keys)):
            raise ArchiveError("This application was already sent. Keep its applied status; archive is for applications you decided not to send.", 409)
        stamp = datetime.now().isoformat(timespec="seconds")
        token = uuid.uuid4().hex if archived else record.get("archive_token")
        board = "missing" if not keys else "unchanged"
        for key in keys:
            entry = seen[key]
            if archived:
                entry["application_archive"] = {"token": token, "previous_status": jobs_md.user_status(entry)}
                entry["user_status"] = "no"
                board = "updated"
            else:
                marker = entry.get("application_archive") or {}
                if token and marker.get("token") == token:
                    if jobs_md.user_status(entry) == "no":
                        entry["user_status"] = marker.get("previous_status", "new")
                        board = "restored"
                    entry.pop("application_archive", None)
        for target in targets:
            if archived:
                target.update(archived_at=stamp, archive_reason=reason.strip(), archive_token=token)
            else:
                for field in ("archived_at", "archive_reason", "archive_token"):
                    target.pop(field, None)
        # Roll back the Board if the registry write fails.
        changed = seen != before
        if changed:
            state.save(seen, "application archived" if archived else "application restored")
        try:
            run_registry.store(data)
        except Exception:
            if changed:
                state.save(before, "archive update rolled back")
            raise
    activity.emit("board", "application %s" % ("archived" if archived else "restored"), run_id=run_id)
    return {"archived": archived, "board": board, "ids": [r["id"] for r in targets]}
