"""Durable, file-based checkpoints for one application attempt.

A run used to be recoverable only through its Claude session: a failure after
the CV was written but before the letter was finished meant starting again from
the posting. This module is the file-side record that makes `Continue` work
without any provider session at all.

One attempt owns one directory, `documents/runs/<run id>/`, and inside it:

    posting.md            the complete posting, saved once (untrusted data)
    brief.json            requirement -> evidence mapping, keywords, conflicts
    inputs/master_cv.tex  private snapshot of the factual master CV
    work/cv.tex           attempt-local CV source (never the live file)
    work/cover.tex        attempt-local cover-letter source
    work/build/*.pdf      the PDFs built from those exact sources
    checkpoint.json       this module's manifest (`jobflow.checkpoint/1`)

The manifest is written **by the supervisor, after it validated an artifact**,
never because a model said it succeeded. Every check it records carries the
hashes of the inputs it was made against, so a check is reusable exactly when
those inputs are byte-identical, and silently stale never.

Stdlib only, Python 3.9+.
"""

import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import run_guard, run_registry

SCHEMA = "jobflow.checkpoint/1"
# Bumped whenever a stage's rules change in a way that makes an older check's
# verdict incomparable. Recorded on the manifest, not folded into check inputs:
# a rules change is reported, while content/PDF hashes decide validity.
RULES_VERSION = "pipeline/2"

KINDS = ("cv", "cover")
STATES = ("pass", "flag", "fail", "skipped", "unverified")
# What a document "is", in the four words the owner sees. File existence or a
# successful compilation alone never reaches `verified`.
DOC_STATES = ("missing", "draft saved", "content checked", "PDF built", "verified")

# A LaTeX document that stops before `\end{document}` was truncated - by a
# timeout, a budget cap, or a crashed write - and must never be adopted.
END_DOCUMENT = re.compile(r"\\end\{document\}\s*(%[^\n]*\s*)*\Z")


def now():
    return datetime.now().isoformat(timespec="seconds")


_HASHES = {}


def sha256(path):
    """Hex digest of one file, or None when it is absent/unreadable.

    Memoised on (path, size, mtime_ns, inode): the board recomputes plans on
    every poll, and re-reading unchanged PDFs each time is pure waste. Any
    rewrite changes at least one of those four.
    """
    try:
        info = os.stat(str(path))
        key = (os.path.realpath(str(path)), info.st_size, info.st_mtime_ns, info.st_ino)
        cached = _HASHES.get(key)
        if cached:
            return cached
        digest = hashlib.sha256()
        with open(str(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        if len(_HASHES) > 4096:
            _HASHES.clear()
        _HASHES[key] = digest.hexdigest()
        return _HASHES[key]
    except OSError:
        return None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_json(value):
    return sha256_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False)
                        .encode("utf-8"))


def rel(path):
    """Repository-relative string for a path under ROOT, else the absolute one."""
    path = Path(path)
    try:
        return str(path.resolve().relative_to(run_registry.ROOT.resolve()))
    except ValueError:
        return str(path)


def absolute(value):
    path = Path(value)
    return path if path.is_absolute() else run_registry.ROOT / path


# ------------------------------------------------------------------ layout

def run_path(run_id, *parts):
    return run_registry.run_dir(run_id).joinpath(*parts)


def manifest_path(run_id):
    return run_path(run_id, "checkpoint.json")


def work_source(run_id, kind, ext=".tex"):
    return run_path(run_id, "work", "%s%s" % (kind, ext))


def work_pdf(run_id, kind):
    return run_path(run_id, "work", "build", "%s.pdf" % kind)


def complete_latex(path):
    """(complete, reason). A partial write must never become a draft."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, "unreadable: %s" % exc
    if not text.strip():
        return False, "empty file"
    if "\\documentclass" not in text:
        return False, "no \\documentclass - not a complete LaTeX document"
    if not END_DOCUMENT.search(text.rstrip()):
        return False, "does not end with \\end{document} - truncated or incomplete"
    return True, ""


def complete_source(path):
    """Complete-document test for the active toolchain's source."""
    if Path(path).suffix == ".tex":
        return complete_latex(path)
    try:
        return (bool(Path(path).read_text(encoding="utf-8").strip()),
                "empty file")
    except (OSError, UnicodeDecodeError) as exc:
        return False, "unreadable: %s" % exc


# ---------------------------------------------------------------- manifest

def new(record):
    return {
        "schema": SCHEMA,
        "rules_version": RULES_VERSION,
        "application_id": record.get("application_id") or record["id"],
        "attempt_id": record["id"],
        "attempt": int(record.get("attempt") or 1),
        "source_attempt": record.get("continue_of") or record.get("parent"),
        "scope": record.get("scope") or "both",
        "created_at": now(),
        "updated_at": now(),
        "inputs": {},
        "docs": {},
        "checks": {},
        "publication": {},
        "pending": [],
        "issues": [],
        "failure": None,
        "adopted": [],
    }


def load(run_id):
    """(manifest, problem). A malformed manifest is reported, never trusted.

    It is also never overwritten in place: `quarantine()` moves it aside first,
    so the evidence of what went wrong survives the recovery that follows.
    """
    path = manifest_path(run_id)
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, "checkpoint.json is unreadable: %s" % exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None, "checkpoint.json is not a %s manifest" % SCHEMA
    for key, kind in (("docs", dict), ("checks", dict), ("inputs", dict),
                      ("publication", dict)):
        if not isinstance(data.get(key), kind):
            return None, "checkpoint.json has no valid %r section" % key
    return data, None


def quarantine(run_id):
    path = manifest_path(run_id)
    if path.exists():
        target = path.with_name("checkpoint.bad-%s.json"
                                % datetime.now().strftime("%Y%m%d%H%M%S%f"))
        os.replace(str(path), str(target))
        return target
    return None


def save(run_id, manifest):
    import jobs_md
    manifest["updated_at"] = now()
    jobs_md.write_json_atomic(manifest_path(run_id), manifest)
    return manifest


# ------------------------------------------------------------------- checks

def record_check(manifest, check_id, state, inputs, detail="", evidence=None, **extra):
    if state not in STATES:
        raise ValueError("bad check state %r" % state)
    entry = {"state": state, "inputs": dict(inputs), "detail": detail,
             "evidence": evidence or {}, "at": now()}
    entry.update(extra)
    manifest["checks"][check_id] = entry
    return entry


def check_valid(manifest, check_id, inputs):
    """The stored check, when it was made against exactly `inputs`, else None."""
    entry = (manifest or {}).get("checks", {}).get(check_id)
    if not entry or entry.get("inputs") != dict(inputs):
        return None
    if any(value is None for value in inputs.values()):
        return None
    return entry


def invalidate(manifest, *check_ids):
    for check_id in check_ids:
        manifest["checks"].pop(check_id, None)


# --------------------------------------------------------------- documents

def record_doc(manifest, kind, source, origin, seed_sha=None):
    """Record one attempt-local source as a saved (unchecked) draft."""
    entry = manifest["docs"].setdefault(kind, {})
    entry.update({"source": {"path": rel(source), "sha256": sha256(source)},
                  "origin": origin, "saved_at": now()})
    if seed_sha is not None:
        entry["seed_sha256"] = seed_sha
    entry.pop("pdf", None)
    return entry


def record_pdf(manifest, kind, pdf, pages):
    entry = manifest["docs"].setdefault(kind, {})
    entry["pdf"] = {"path": rel(pdf), "sha256": sha256(pdf), "pages": pages,
                    "built_from": (entry.get("source") or {}).get("sha256")}
    return entry


def current_source_sha(manifest, kind):
    """The recorded source hash - only if the file on disk still matches it."""
    entry = (manifest.get("docs") or {}).get(kind) or {}
    source = entry.get("source") or {}
    if not source.get("path") or not source.get("sha256"):
        return None
    actual = sha256(absolute(source["path"]))
    return actual if actual == source["sha256"] else None


def current_pdf_sha(manifest, kind):
    entry = (manifest.get("docs") or {}).get(kind) or {}
    pdf = entry.get("pdf") or {}
    source_sha = current_source_sha(manifest, kind)
    if not pdf.get("path") or not source_sha or pdf.get("built_from") != source_sha:
        return None
    actual = sha256(absolute(pdf["path"]))
    return actual if actual == pdf.get("sha256") else None


# ------------------------------------------------------ check input bindings

def content_inputs(manifest, kind):
    inputs = {"source": current_source_sha(manifest, kind),
              "posting": (manifest.get("inputs", {}).get("posting") or {}).get("sha256"),
              "evidence": (manifest.get("inputs", {}).get("evidence") or {}).get(kind)}
    if kind == "cover":
        inputs["letter_plan"] = sha256_json((manifest.get("brief") or {}).get("letter_plan"))
    return inputs


def consistency_inputs(manifest):
    return {"cv": current_source_sha(manifest, "cv"),
            "cover": current_source_sha(manifest, "cover")}


def build_inputs(manifest, kind, toolchain):
    inputs = {"source": current_source_sha(manifest, kind),
              "toolchain": sha256_json(toolchain)}
    if kind == "cover" and toolchain.get("kind") == "latex":
        inputs["cover_class"] = sha256(run_registry.ROOT / (
            toolchain.get("home") or "cover_letters") / "cover.cls") or "absent"
    return inputs


def mechanical_inputs(manifest, kind):
    inputs = {"pdf": current_pdf_sha(manifest, kind),
              "keywords": sha256_json(manifest.get("keywords") or [])}
    if kind == "cover":
        # Fixed prose is checked against the live canonical library, even if
        # the compiled PDF has not changed since the previous measurement.
        from . import cover_blocks, docs
        inputs.update(source=current_source_sha(manifest, kind),
                      cover_base=sha256(docs.cover_base()),
                      cover_policy=sha256_json({
                          "max_words": docs.COVER_MAX_WORDS,
                          "validator": sha256(Path(cover_blocks.__file__)),
                          "pdf_checks": sha256(Path(docs.__file__)),
                      }))
    return inputs


def visual_inputs(manifest, kind):
    return {"pdf": current_pdf_sha(manifest, kind)}


def doc_state(manifest, kind, toolchain, inspection_enabled=True, review_enabled=True):
    """One of DOC_STATES, derived from hashes rather than stored as a label."""
    if current_source_sha(manifest, kind) is None:
        return "missing"
    content = check_valid(manifest, "content_" + kind, content_inputs(manifest, kind))
    if review_enabled and (not content or content["state"] not in ("pass", "flag")):
        return "draft saved"
    build = check_valid(manifest, "build_" + kind, build_inputs(manifest, kind, toolchain))
    if not build or build["state"] != "pass" or current_pdf_sha(manifest, kind) is None:
        return "content checked"
    mechanical = check_valid(manifest, "mechanical_" + kind,
                             mechanical_inputs(manifest, kind))
    visual = check_valid(manifest, "visual_" + kind, visual_inputs(manifest, kind))
    if not mechanical or mechanical["state"] == "fail":
        return "PDF built"
    # With inspection switched off nobody looked at the page, so the document
    # stays "PDF built" - a disabled check is shown as missing, not as a pass.
    if not visual or visual["state"] != "pass":
        return "PDF built"
    return "verified"


def plan(manifest, kinds, toolchains, inspection_enabled=True, review_enabled=True):
    """The ordered list of work still missing or invalid: `[(stage, [kinds])]`.

    The supervisor executes only the first step and then plans again, so this
    has to be exact about the first step and honest about the rest. A document
    whose content is not yet checked is "dirty": whatever follows for it is
    pending even if an older build happens to still exist.

    Mechanical stages (`build`, `mechanical`, `publish`) never need a model.

    With `review_enabled` off (the default, `automated_review: false`) the owner
    checks the PDFs by hand: content review and inspection are skipped, but
    cover page overflow still requires a bounded repair and a fresh build.
    """
    inspection_enabled = inspection_enabled and review_enabled
    posting = manifest.get("inputs", {}).get("posting") or {}
    if not posting.get("sha256") or sha256(absolute(posting.get("path") or "")) \
            != posting["sha256"]:
        later = ["draft"] + (["review"] if review_enabled else []) + ["build", "mechanical"] + \
            (["inspect"] if inspection_enabled else []) + ["publish"]
        return [("prepare", list(kinds))] + [(stage, list(kinds)) for stage in later]
    steps = []
    missing = [k for k in kinds if current_source_sha(manifest, k) is None]
    # The draft pass also writes the requirement brief, which the screening
    # needs even when the only document is the (never drafted) CV.
    letter_plan_missing = "cover" in kinds and run_guard.validate_letter_plan(
        (manifest.get("brief") or {}).get("letter_plan"))
    if missing or not manifest.get("brief") or letter_plan_missing:
        steps.append(("draft", missing))
    content = {k: check_valid(manifest, "content_" + k, content_inputs(manifest, k))
               for k in kinds}
    consistency = (check_valid(manifest, "consistency", consistency_inputs(manifest))
                   if len(kinds) == 2 else None)
    if not review_enabled:
        content = {k: {"state": "manual"} for k in kinds}
        consistency = {"state": "manual"} if len(kinds) == 2 else None
    # A recorded *failed* verdict is still a verdict: the next work is to act on
    # its findings, not to ask the same question of the same bytes again.
    needs_fix = [k for k in kinds if content[k] and content[k]["state"] == "fail"]
    if consistency and consistency["state"] == "fail":
        needs_fix = list(kinds)
    if needs_fix:
        steps.append(("fix", needs_fix))
    dirty = [k for k in kinds if k in missing or k in needs_fix or not content[k]]
    review = list(dirty)
    if len(kinds) == 2 and (dirty or not consistency):
        review.append("consistency")
    if review and review_enabled:
        steps.append(("review", review))
    unbuilt = [k for k in kinds if k in dirty or not check_valid(
        manifest, "build_" + k, build_inputs(manifest, k, toolchains[k]))
        or current_pdf_sha(manifest, k) is None]
    if unbuilt:
        steps.append(("build", unbuilt))
    mechanical = {k: check_valid(manifest, "mechanical_" + k, mechanical_inputs(manifest, k))
                  for k in kinds}
    unchecked = [k for k in kinds if k in unbuilt or not mechanical[k] or (
        not review_enabled and k == "cover" and any(
            c.get("id") == "cover_page_count" and c.get("state") == "fail"
            for c in (mechanical[k].get("evidence") or {}).get("checks", [])))]
    if unchecked:
        steps.append(("mechanical", unchecked))
    visual = {k: check_valid(manifest, "visual_" + k, visual_inputs(manifest, k))
              for k in kinds}
    needs_repair = [] if not review_enabled else [k for k in kinds if k not in unbuilt and (
        (mechanical[k] and mechanical[k]["state"] == "fail") or
        (inspection_enabled and visual[k] and visual[k]["state"] in ("flag", "fail")))]
    if needs_repair:
        steps.append(("repair", needs_repair))
    if inspection_enabled:
        # `unverified` means nobody proved they looked - that is not an answer.
        uninspected = [k for k in kinds if k in unbuilt or k in needs_repair
                       or not visual[k] or visual[k]["state"] == "unverified"]
        if uninspected:
            steps.append(("inspect", uninspected))
    unpublished = [k for k in kinds if k in unbuilt or k in needs_repair
                   or not published(manifest, k)]
    if unpublished or not manifest.get("publication", {}).get("recorded"):
        steps.append(("publish", unpublished))
    return steps


def pending_findings(manifest, kinds):
    """Must-fix review findings recorded against the current bytes."""
    found = []
    for check_id in ["content_" + k for k in kinds] + ["consistency"]:
        entry = manifest.get("checks", {}).get(check_id) or {}
        if entry.get("state") == "fail":
            found += [f for f in (entry.get("evidence") or {}).get("findings") or []
                      if f.get("severity") == "must_fix"]
    return found


def pending_repairs(manifest, kinds):
    """Layout issues recorded against the current PDFs, as repair instructions."""
    issues = []
    for kind in kinds:
        visual = manifest.get("checks", {}).get("visual_" + kind) or {}
        if visual.get("state") in ("flag", "fail"):
            issues += (visual.get("evidence") or {}).get("issues") or []
        mechanical = manifest.get("checks", {}).get("mechanical_" + kind) or {}
        if mechanical.get("state") == "fail":
            issues += [{"doc": kind, "page": 1, "kind": c.get("label", ""),
                        "fix_hint": c.get("detail", "")}
                       for c in (mechanical.get("evidence") or {}).get("checks") or []
                       if c.get("state") == "fail"]
    return issues


def published(manifest, kind):
    entry = manifest.get("publication", {}).get(kind) or {}
    return (entry.get("source_sha256") and
            entry.get("source_sha256") == current_source_sha(manifest, kind) and
            entry.get("pdf_sha256") == current_pdf_sha(manifest, kind))


MODEL_STAGES = ("prepare", "draft", "fix", "review", "repair", "inspect")


def needs_model(steps):
    return any(stage in MODEL_STAGES for stage, _kinds in steps)


def summary(manifest, kinds, toolchains, inspection_enabled=True, review_enabled=True):
    """What the board shows on a card: documents, checks, what remains."""
    if not manifest:
        return None
    docs = {}
    for kind in kinds:
        entry = manifest.get("docs", {}).get(kind) or {}
        docs[kind] = {"state": doc_state(manifest, kind, toolchains[kind],
                                         inspection_enabled, review_enabled),
                      "origin": entry.get("origin"),
                      "pdf": (entry.get("pdf") or {}).get("path")
                      if current_pdf_sha(manifest, kind) else None}
    checks = {}
    for check_id, entry in manifest.get("checks", {}).items():
        kind = check_id.rsplit("_", 1)[-1]
        if kind in KINDS and kind not in kinds:
            continue
        inputs = _inputs_for(manifest, check_id, toolchains)
        valid = inputs is not None and check_valid(manifest, check_id, inputs) is not None
        checks[check_id] = {"state": entry.get("state") if valid else "stale",
                            "detail": entry.get("detail", "")}
    steps = plan(manifest, kinds, toolchains, inspection_enabled, review_enabled)
    return {"docs": docs, "checks": checks,
            "pending": [stage for stage, _k in steps],
            "issues": manifest.get("issues", [])[-8:],
            "conflicts": (manifest.get("brief") or {}).get("hard_conflicts") or [],
            "failure": manifest.get("failure"),
            "rules_version": manifest.get("rules_version")}


def _inputs_for(manifest, check_id, toolchains):
    if check_id == "consistency":
        return consistency_inputs(manifest)
    prefix, _sep, kind = check_id.rpartition("_")
    if kind not in KINDS:
        return None
    if prefix == "content":
        return content_inputs(manifest, kind)
    if prefix == "build":
        return build_inputs(manifest, kind, toolchains.get(kind))
    if prefix == "mechanical":
        return mechanical_inputs(manifest, kind)
    if prefix == "visual":
        return visual_inputs(manifest, kind)
    return None


# ------------------------------------------------------------------ adoption

def hook_approved_writes(run_id):
    """Realpaths the guard approved a write to, across every pass of a run."""
    approved = set()
    directory = run_registry.state_dir(run_id)
    for log in sorted(directory.glob("hook-*.jsonl")) if directory.is_dir() else ():
        try:
            lines = log.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("decision") == "allow" and entry.get("tool") in (
                    "Write", "Edit", "MultiEdit", "NotebookEdit"):
                target = entry.get("resolved") or entry.get("target")
                if target:
                    approved.add(os.path.realpath(str(target)))
    return approved


def _copy_into(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".adopt-%d" % os.getpid())
    shutil.copyfile(str(source), str(temp))
    os.replace(str(temp), str(target))
    return target


def adopt_from_checkpoint(new_manifest, new_id, source_manifest, source_id, kinds, ext):
    """Copy every artifact the source attempt had *validated* into this attempt.

    Byte copies keep their hashes, so every check bound to them stays valid;
    anything whose bytes no longer match what the source recorded is refused.
    Returns a list of (what, outcome) lines for the audit trail.
    """
    log = []
    src_inputs = source_manifest.get("inputs", {})
    posting = src_inputs.get("posting") or {}
    if posting.get("sha256") and sha256(absolute(posting.get("path", ""))) == posting["sha256"]:
        target = _copy_into(absolute(posting["path"]), run_path(new_id, "posting.md"))
        new_manifest["inputs"]["posting"] = {"path": rel(target), "sha256": sha256(target),
                                             "origin": "adopted:%s" % source_id}
        log.append(("posting", "adopted"))
    for key in ("brief", "cv_variant"):
        if source_manifest.get(key):
            new_manifest[key] = source_manifest[key]
    if source_manifest.get("keywords") is not None:
        new_manifest["keywords"] = source_manifest["keywords"]
    brief = run_path(source_id, "brief.json")
    if brief.is_file():
        _copy_into(brief, run_path(new_id, "brief.json"))
    for name in ("master_cv.tex",):
        snap = run_path(source_id, "inputs", name)
        if snap.is_file():
            _copy_into(snap, run_path(new_id, "inputs", name))
    for kind in kinds:
        entry = source_manifest.get("docs", {}).get(kind)
        sha = current_source_sha(source_manifest, kind) if entry else None
        if not sha:
            if entry:
                log.append((kind, "not adopted: its bytes no longer match the checkpoint"))
            continue
        target = _copy_into(absolute(entry["source"]["path"]), work_source(new_id, kind, ext))
        record_doc(new_manifest, kind, target, "adopted:%s" % source_id,
                   entry.get("seed_sha256"))
        if entry.get("reviewed_copy"):
            new_manifest["docs"][kind]["reviewed_copy"] = entry["reviewed_copy"]
        pdf_sha = current_pdf_sha(source_manifest, kind)
        if pdf_sha:
            pdf = _copy_into(absolute(entry["pdf"]["path"]), work_pdf(new_id, kind))
            record_pdf(new_manifest, kind, pdf, entry["pdf"].get("pages"))
        log.append((kind, "adopted draft%s" % (" and PDF" if pdf_sha else "")))
    # Checks are copied as they are: each is bound to input hashes, and the
    # copies above are byte-identical, so a check survives exactly when the
    # thing it checked did. Evidence hashes are re-derived by the caller.
    for check_id, entry in (source_manifest.get("checks", {}).items() if kinds else ()):
        new_manifest["checks"][check_id] = dict(entry, adopted_from=source_id)
    for kind in kinds:
        pub = source_manifest.get("publication", {}).get(kind)
        if pub:
            new_manifest["publication"][kind] = dict(pub)
    new_manifest["adopted"].append({"from": source_id, "at": now(),
                                    "items": ["%s: %s" % item for item in log]})
    return log


def adopt_legacy(new_manifest, new_id, record, kinds, ext):
    """Recover what a pre-checkpoint run left behind, conservatively.

    Order of trust: the run's own snapshots first; then a live target file only
    when this run's guard log shows an approved write to it *and* its mtime
    falls inside the run's own lifetime. A live file at the target path is not,
    on its own, evidence that this run wrote it. Nothing is ever rewritten or
    deleted; whatever cannot be attributed is left intact and explained.
    """
    log = []
    run_id = record["id"]
    posting = run_path(run_id, "posting.md")
    if posting.is_file() and posting.stat().st_size:
        target = _copy_into(posting, run_path(new_id, "posting.md"))
        new_manifest["inputs"]["posting"] = {"path": rel(target), "sha256": sha256(target),
                                             "origin": "legacy:%s" % run_id}
        log.append(("posting", "adopted from the run's saved posting.md"))
    else:
        log.append(("posting", "not recoverable: the run saved no posting.md"))

    request = run_path(run_id, "verify_request.json")
    try:
        keywords = json.loads(request.read_text(encoding="utf-8")).get("keywords")
        if isinstance(keywords, list) and all(isinstance(k, str) for k in keywords):
            new_manifest["keywords"] = keywords
    except (OSError, ValueError, AttributeError):
        pass

    approved = hook_approved_writes(run_id)
    started = _epoch(record.get("started_at"))
    ended = _epoch(record.get("ended_at"))
    # Attempts that continue this very run are not "later writers": they are
    # the adopters, and they write only their own run directory.
    later = [r for r in run_registry.load()["runs"]
             if r.get("slug") == record.get("slug") and r["id"] not in (run_id, new_id)
             and r.get("continue_of") != run_id
             and (r.get("started_at") or "") > (record.get("started_at") or "")
             and r.get("kind") in ("apply", "revise", "redraft")]
    for kind in kinds:
        snapshot = run_path(run_id, "%s_source%s" % (kind, ext))
        live_rel = (record.get("targets") or {}).get(kind)
        live = run_registry.ROOT / live_rel if live_rel else None
        chosen, why = None, ""
        if snapshot.is_file():
            chosen, why = snapshot, "the run's own source snapshot"
        elif live is not None and live.is_file():
            real = os.path.realpath(str(live))
            mtime = live.stat().st_mtime
            if live.is_symlink():
                why = "live file is a symlink; refusing to attribute it"
            elif real not in approved:
                why = ("no guard record shows this run writing %s; a file at the "
                       "target path is not evidence of ownership" % live_rel)
            elif started is None or mtime < started - 1:
                why = "%s is older than the run itself" % live_rel
            elif ended is not None and mtime > ended + 120:
                why = "%s changed after the run ended" % live_rel
            elif later:
                why = ("a later run for the same application (%s) may have written %s"
                       % (later[0]["id"], live_rel))
            else:
                chosen, why = live, "a live target this run's guard log shows it writing"
        else:
            why = "no draft was left behind"
        if chosen is None:
            log.append((kind, "not adopted: %s" % why))
            new_manifest["issues"].append("%s draft from %s not adopted: %s"
                                          % (kind, run_id, why))
            continue
        complete, reason = complete_source(chosen)
        if not complete:
            log.append((kind, "not adopted (%s): %s" % (why, reason)))
            new_manifest["issues"].append("%s draft from %s is incomplete (%s); left intact"
                                          % (kind, run_id, reason))
            continue
        target = _copy_into(chosen, work_source(new_id, kind, ext))
        record_doc(new_manifest, kind, target, "legacy:%s" % run_id)
        log.append((kind, "adopted as an unchecked draft from %s" % why))
    new_manifest["adopted"].append({"from": run_id, "legacy": True, "at": now(),
                                    "items": ["%s: %s" % item for item in log]})
    return log


def _epoch(stamp):
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def layout_only_change(before_text, after_text):
    """True when a repair only added/removed layout commands or trimmed lines.

    A visual repair that inserts `\\needspace`, nudges spacing or removes a
    line cannot introduce an unsupported claim, so the content check it
    followed may be carried over. Any *added* prose line invalidates it.
    """
    import difflib
    layout = re.compile(
        r"^\s*(%.*|\\(needspace|enlargethispage|vspace\*?|vfill|newpage|clearpage|"
        r"pagebreak|nopagebreak|linebreak|smallskip|medskip|bigskip|noindent|"
        r"setlength|addtolength|looseness|raggedbottom|par)\b[^%]*|\s*)$")
    for line in difflib.ndiff(before_text.splitlines(), after_text.splitlines()):
        if line.startswith("+ ") and not layout.match(line[2:]):
            return False
    return True
