"""The owner's review of a run's compiled PDFs: checklist, marks, screening, reveal.

By default no model checks the PDFs (`automated_review: false`); the owner does,
from a short checklist on the Preview screen. A mark is bound to the exact PDF
bytes it was made against, so a recompile, restore or revision shows the item
unchecked again rather than carrying an old tick onto new pages.

Stdlib only, Python 3.9+.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

from . import checkpoint, docs, run_registry

# (id, doc, label). `doc` is the document whose PDF the mark is bound to;
# "both" binds to both PDFs.
CHECKLIST = (
    ("cv_layout", "cv", "Exactly one page; no heading split from its bullets"),
    ("cv_variant", "cv", "Right variant and title line for this role"),
    ("cv_facts", "cv", "Dates, titles and contact details are correct"),
    ("cover_layout", "cover", "One page; the signature fits with the body"),
    ("cover_addressee", "cover", "Employer block, addressee and role subject are right"),
    ("cover_claims", "cover", "Every claim is true; company facts are verified"),
    ("cover_language", "cover", "Reads well; no filler, spelling or grammar errors"),
    ("consistency", "both", "CV and letter agree on every shared fact"),
)
CHECK_IDS = {item[0] for item in CHECKLIST}
# The model-produced checks that manual review replaces. Hidden from the rail
# when `automated_review` is off: they would only ever read "not run".
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


def _binding(record, doc):
    kinds = docs.doc_kinds(record)
    wanted = kinds if doc == "both" else [doc]
    shas = []
    for kind in wanted:
        path = _pdf(record, kind)
        if path is None:
            return None
        shas.append(checkpoint.sha256(path))
    return ":".join(shas)


def _marks_path(record):
    return run_registry.run_dir(record["id"]) / "marks.json"


def _load_marks(record):
    try:
        payload = json.loads(_marks_path(record).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload.get("marks") or {} if isinstance(payload, dict) else {}


def checklist(record):
    """The items that apply to this run's documents, with their current marks."""
    kinds = docs.doc_kinds(record)
    marks = _load_marks(record)
    out = []
    for check_id, doc, label in CHECKLIST:
        if doc == "both" and len(kinds) < 2 or doc != "both" and doc not in kinds:
            continue
        mark = marks.get(check_id) or {}
        binding = _binding(record, doc)
        out.append({"id": check_id, "doc": doc, "label": label,
                    "done": bool(binding and mark.get("pdf") == binding),
                    "stale": bool(mark and binding and mark.get("pdf") != binding)})
    return out


def mark(record, check_id, done):
    if check_id not in CHECK_IDS:
        raise ReviewError("unknown checklist item")
    doc = next(item[1] for item in CHECKLIST if item[0] == check_id)
    binding = _binding(record, doc)
    if binding is None:
        raise ReviewError("there is no compiled PDF to check yet", 409)
    marks = _load_marks(record)
    if done:
        marks[check_id] = {"pdf": binding,
                           "at": datetime.now().isoformat(timespec="seconds")}
    else:
        marks.pop(check_id, None)
    jobs_md.write_json_atomic(_marks_path(record), {"schema": "jobflow.marks/1",
                                                    "marks": marks})
    return checklist(record)


def _brief(record):
    try:
        payload = json.loads((run_registry.run_dir(record["id"]) / "brief.json")
                             .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def view(record, verify):
    """verify.json as the Preview screen shows it."""
    verify = dict(verify or {})
    if not run_registry.config().get("automated_review", False):
        verify["checks"] = [c for c in verify.get("checks") or []
                            if not str(c.get("id", "")).startswith(_MODEL_CHECKS)
                            and c.get("id") != "visual_layout"
                            and not str(c.get("id", "")).endswith("_mechanical")]
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
    verify["manual"] = checklist(record)
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
