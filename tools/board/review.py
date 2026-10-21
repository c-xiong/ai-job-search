"""The owner's review of a run's compiled PDFs: automatic checks, screening, reveal.

By default no model checks the PDFs (`automated_review: false`). The board's
automatic checks decide whether a finished run is ready to send; the owner
looks at the PDFs without ticking anything.

Stdlib only, Python 3.9+.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

from . import checkpoint, docs, run_registry

# The model-produced checks. Hidden, and ignored for readiness, when
# `automated_review` is off: they would only ever read "not run".
_MODEL_CHECKS = ("content_", "visual_", "consistency")


class ReviewError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _pdf(record, kind):
    relative = (record.get("artefacts") or {}).get(kind + "_pdf")
    if not relative:
        return None
    path = (run_registry.ROOT / relative).resolve()
    try:
        path.relative_to(run_registry.run_dir(record["id"]).resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def _brief(record):
    try:
        payload = json.loads((run_registry.run_dir(record["id"]) / "brief.json")
                             .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _shown_checks(checks):
    if run_registry.config().get("automated_review", False):
        return list(checks or [])
    return [c for c in checks or []
            if not str(c.get("id", "")).startswith(_MODEL_CHECKS)
            and c.get("id") != "visual_layout"
            and not str(c.get("id", "")).endswith("_mechanical")]


def summary(record):
    """How the automatic checks on a finished run came out: `flagged` counts
    the ones that did not pass, which is what keeps it in "to review"."""
    try:
        payload = json.loads((run_registry.run_dir(record["id"]) / "verify.json")
                             .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"total": 0, "flagged": 0, "measured": False}
    checks = _shown_checks(payload.get("checks") if isinstance(payload, dict) else None)
    return {"total": len(checks), "measured": True,
            "flagged": sum(1 for c in checks if c.get("state") != "pass")}


def view(record, verify):
    """verify.json as the Preview screen shows it."""
    verify = dict(verify or {})
    verify["checks"] = _shown_checks(verify.get("checks"))
    brief = _brief(record) or {}
    order = {"gap": 0, "adjacent": 1}
    requirements = sorted(
        ({k: item.get(k) for k in ("requirement", "priority", "status", "evidence")}
         for item in brief.get("requirements") or [] if isinstance(item, dict)),
        key=lambda item: (order.get(item.get("status"), 2),
                          item.get("priority") != "required"))
    verify["screening"] = {
        "requirements": requirements,
        "conflicts": brief.get("hard_conflicts") or [],
        "facts": {k: brief.get(k) for k in ("location", "language", "role_type",
                                            "deadline", "contact_person")
                  if brief.get(k)},
    } if brief else None
    verify["variant"] = "ai" if record.get("resolved_base_cv") in ("ai", "ml") else "sde"
    targets = {kind: reveal_target(record, kind) for kind in docs.doc_kinds(record)}
    verify["files"] = {kind: _rel(path)
                       for kind, path in targets.items() if path is not None}
    return verify


def reveal_target(record, kind):
    """The file to show in Finder: the live PDF in this repository when it
    holds exactly this version, otherwise the run's own snapshot."""
    snapshot = _pdf(record, kind)
    target = (record.get("targets") or {}).get(kind)
    if target:
        live = run_registry.ROOT / Path(target).parent / "build" / (Path(target).stem + ".pdf")
        if live.is_file() and (snapshot is None or
                               checkpoint.sha256(live) == checkpoint.sha256(snapshot)):
            return live
    return snapshot


def reveal(record, kind):
    if kind not in docs.doc_kinds(record):
        raise ReviewError("this run has no %s" % kind)
    path = reveal_target(record, kind)
    if path is None:
        raise ReviewError("there is no compiled %s yet" % docs.DOC_LABELS[kind], 404)
    # Show the submission copy of exactly the version on screen: refreshed from
    # it first, so a recompile, restore or older version never reveals a stale file.
    try:
        path = docs.submission_copy(record, kind, path)
    except docs.DocumentError as exc:
        raise ReviewError(str(exc), 500)
    if sys.platform == "darwin":
        argv = ["open", "-R", str(path)]
    elif sys.platform.startswith("linux"):
        argv = ["xdg-open", str(path.parent)]
    else:
        raise ReviewError("revealing a file is supported on macOS and Linux only", 501)
    try:
        subprocess.run(argv, check=True, timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewError("could not open the file manager: %s" % exc, 500)
    return _rel(path)


def _rel(path):
    return str(Path(path).resolve().relative_to(run_registry.ROOT.resolve()))
