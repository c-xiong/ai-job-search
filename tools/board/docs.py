"""Supervisor-owned document compilation, verification and publication.

Model output names source paths only. Commands come from the same managed
template blocks /apply reads, and only explicitly supported toolchains execute.
This module never invokes a shell.
"""

import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

from . import activity, run_guard, run_registry, templates


ROOT = run_registry.ROOT
TRACKER = ROOT / "job_search_tracker.csv"
TRACKER_LOCK = ROOT / ".tracker.lock"
APPLICATIONS = ROOT / "documents" / "applications"
PROFILE = ROOT / ".claude" / "skills" / "job-application-assistant" / "01-candidate-profile.md"
CANONICAL_HEADER = (
    "date,company,sector,role,role_type,channel,status,contact_person,"
    "fit_rating,notes,cv_file,cover_letter_file,source,deadline"
).split(",")
FINAL_STATUSES = {
    "hired", "rejected", "no_response", "offer_declined", "withdrawn",
    "no response", "offer declined",
}
VERIFY_STATES = {"pass", "flag", "fail", "skipped", "unverified"}
LATEX_ENGINES = {"lualatex", "xelatex", "pdflatex"}
LATEX_CLEAN = (".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".synctex.gz")


class DocumentError(RuntimeError):
    pass


class DocumentBusy(DocumentError):
    pass


_DOC_LOCAL = {}
_DOC_LOCAL_GUARD = threading.Lock()


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


@contextmanager
def document_lock(slug, blocking=True):
    """Serialize every writer of one CV/cover pair, across threads/processes."""
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,120}$", slug or ""):
        raise DocumentError("invalid document-set slug")
    with _DOC_LOCAL_GUARD:
        local = _DOC_LOCAL.setdefault(slug, threading.Lock())
    if not local.acquire(blocking=blocking):
        raise DocumentBusy("the document set is busy")
    handle = None
    try:
        path = run_registry.RUN_STATE / ("document-%s.lock" % slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")
        if fcntl is not None:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), flags)
            except OSError:
                raise DocumentBusy("the document set is busy")
        yield
    finally:
        if handle is not None:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
        local.release()


def resolve_toolchain(kind):
    """Return a strict, supervisor-owned compile specification."""
    spec = templates.active(kind)
    command = spec.get("compile")
    if not command:
        raise DocumentError("%s template has no compile command" % kind)
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise DocumentError("%s compile command is invalid: %s" % (kind, exc))
    if not words:
        raise DocumentError("%s template has an empty compile command" % kind)
    engine = Path(words[0]).name
    if engine in LATEX_ENGINES:
        allowed = {"-interaction=nonstopmode"}
        extras = [word for word in words[1:] if word not in allowed]
        if extras:
            raise DocumentError("%s compile command has unsupported arguments: %s"
                                % (kind, " ".join(extras)))
        return {"kind": "latex", "engine": engine, "ext": spec["ext"],
                "name": spec["name"], "source": spec.get("source")}
    if len(words) == 2 and engine == "typst" and words[1] == "compile":
        return {"kind": "typst", "engine": "typst", "ext": spec["ext"],
                "name": spec["name"], "source": spec.get("source")}
    raise DocumentError("toolchain_unsupported: %s" % command)


def _run(argv, cwd, timeout=120):
    try:
        return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocumentError("%s could not run: %s" % (Path(argv[0]).name, exc))


def compile_one(kind, source):
    """Compile one exact allowlisted source and return evidence + PDF path."""
    source = Path(source)
    if not source.is_absolute():
        source = ROOT / source
    if not _inside(source, ROOT) or source.is_symlink():
        raise DocumentError("%s source escapes the repository or is a symlink" % kind)
    tool = resolve_toolchain(kind)
    if source.suffix != tool["ext"]:
        raise DocumentError("%s source extension %s does not match active template %s"
                            % (kind, source.suffix, tool["ext"]))
    if not source.is_file():
        raise DocumentError("%s source is missing: %s" % (kind, source))

    build = source.parent / "build"
    build.mkdir(parents=True, exist_ok=True)
    pdf = build / (source.stem + ".pdf")
    before = pdf.stat().st_mtime_ns if pdf.exists() else None
    if tool["kind"] == "latex":
        argv = [tool["engine"], "-interaction=nonstopmode",
                "-output-directory=%s" % build, source.name]
        cwd = source.parent
    else:
        argv = ["typst", "compile", str(source), str(pdf)]
        cwd = ROOT
    proc = _run(argv, cwd)
    evidence = {"cmd": argv, "exit": proc.returncode,
                "stdout_tail": proc.stdout.splitlines()[-8:],
                "stderr_tail": proc.stderr.splitlines()[-8:],
                "toolchain": tool["kind"]}
    if proc.returncode != 0:
        raise DocumentError("%s compile failed (exit %d): %s"
                            % (kind, proc.returncode,
                               " / ".join(evidence["stderr_tail"][-3:]
                                        or evidence["stdout_tail"][-3:])))
    if not pdf.is_file() or not pdf.stat().st_size:
        raise DocumentError("%s compile reported success but produced no PDF" % kind)
    if before is not None and pdf.stat().st_mtime_ns == before:
        raise DocumentError("%s compile did not refresh its PDF" % kind)
    if tool["kind"] == "latex":
        for suffix in LATEX_CLEAN:
            candidate = build / (source.stem + suffix)
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
    evidence["pdf"] = str(pdf.relative_to(ROOT))
    return pdf, evidence


def pdf_pages(path):
    """Count page objects from the PDF itself, without depending on pdfinfo."""
    data = Path(path).read_bytes()
    count = len(re.findall(rb"/Type\s*/Page\b", data))
    if count <= 0:
        values = [int(v) for v in re.findall(
            rb"/Type\s*/Pages\b.{0,300}?/Count\s+(\d+)", data, re.DOTALL)]
        count = max(values or [0])
    if count <= 0:
        raise DocumentError("could not determine page count from %s" % path)
    return count


def extract_text(path):
    binary = shutil.which("pdftotext")
    if not binary:
        return None, {"cmd": "pdftotext -layout", "available": False}
    proc = _run([binary, "-layout", str(path), "-"], ROOT)
    evidence = {"cmd": [binary, "-layout", str(path), "-"], "exit": proc.returncode}
    if proc.returncode != 0:
        return None, evidence
    return proc.stdout, evidence


def _contact_literals():
    try:
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return []
    emails = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    phones = re.findall(r"\+\d[\d ()-]{7,}\d", text)
    return list(dict.fromkeys(emails[:1] + phones[:1]))


def _check(check_id, label, state, detail, evidence=None):
    if state not in VERIFY_STATES:
        raise ValueError("bad verification state")
    return {"id": check_id, "label": label, "state": state, "detail": detail,
            "evidence": evidence or {}}


def build_verify(record, pdfs, compile_evidence, keywords):
    """Machine-produce verify.json. Visual checks await evidence from pass C."""
    checks = []
    extracted = {}
    for kind, expected in (("cv", 2), ("cover", 1)):
        pdf = pdfs[kind]
        pages = pdf_pages(pdf)
        checks.append(_check(
            kind + "_page_count",
            ("CV is exactly 2 pages" if kind == "cv" else "Cover letter is exactly 1 page"),
            "pass" if pages == expected else "fail",
            "%s, %d page%s" % (compile_evidence[kind]["toolchain"], pages,
                              "" if pages == 1 else "s"),
            dict(compile_evidence[kind], pages=pages)))
        text, evidence = extract_text(pdf)
        extracted[kind] = text
        if text is None:
            for suffix, label in (
                    ("text_layer", "Text layer is extractable"),
                    ("contact_literals", "Contact details survived extraction"),
                    ("reading_order", "Extracted reading order is usable")):
                checks.append(_check(kind + "_" + suffix, label, "skipped",
                                     "pdftotext is unavailable or failed", evidence))
        else:
            clean = bool(text.strip()) and "\ufffd" not in text
            checks.append(_check(kind + "_text_layer", "Text layer is extractable",
                                 "pass" if clean else "fail",
                                 "%d extracted characters" % len(text.strip()), evidence))
            literals = _contact_literals()
            missing = [literal for literal in literals if literal not in text]
            checks.append(_check(kind + "_contact_literals",
                                 "Contact details survived extraction",
                                 "pass" if not missing else "flag",
                                 "all configured literals found" if not missing
                                 else "missing: " + ", ".join(missing), evidence))
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            order_ok = len(lines) >= 3 and max((len(line) for line in lines), default=0) < 500
            checks.append(_check(kind + "_reading_order",
                                 "Extracted reading order is usable",
                                 "pass" if order_ok else "flag",
                                 "%d non-empty lines" % len(lines), evidence))

    combined = "\n".join(value or "" for value in extracted.values()).casefold()
    covered, absent = [], []
    for keyword in keywords:
        (covered if keyword.casefold() in combined else absent).append(keyword)
    checks.append(_check("visual_layout", "Visual layout was inspected", "unverified",
                         "Pass C has not yet proved it read both PDFs"))
    verify = {"schema": "jobflow.verify/1", "run_id": record["id"],
              "checks": checks,
              "keywords": {"covered": covered, "absent": absent, "source": "posting"}}
    jobs_md.write_json_atomic(run_registry.run_dir(record["id"]) / "verify.json", verify)
    return verify


def validate_inspect(payload):
    problems = []
    if not isinstance(payload, dict):
        return ["inspect.json is not an object"]
    if payload.get("schema") != "jobflow.inspect/1":
        problems.append("schema must be jobflow.inspect/1")
    if payload.get("verdict") not in ("clean", "fixable", "blocked"):
        problems.append("verdict must be clean, fixable or blocked")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        problems.append("issues must be a list")
    else:
        for index, issue in enumerate(issues):
            if not isinstance(issue, dict):
                problems.append("issues[%d] is not an object" % index)
                continue
            if issue.get("doc") not in ("cv", "cover"):
                problems.append("issues[%d].doc must be cv or cover" % index)
            if not isinstance(issue.get("page"), int) or issue["page"] < 1:
                problems.append("issues[%d].page must be a positive integer" % index)
            for key in ("kind", "fix_hint"):
                if not isinstance(issue.get(key), str):
                    problems.append("issues[%d].%s must be a string" % (index, key))
    return problems


def read_evidence(stream_path, pdfs, offset=0):
    """Return exact PDF paths successfully returned by Read tool results."""
    wanted = {str(Path(path).resolve()) for path in pdfs.values()}
    tool_ids = {}
    successful = set()
    try:
        with open(stream_path, encoding="utf-8") as handle:
            handle.seek(offset)
            lines = handle.read().splitlines()
    except OSError:
        return successful
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        content = (event.get("message") or {}).get("content") or []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Read":
                raw = (block.get("input") or {}).get("file_path")
                if raw:
                    tool_ids[block.get("id")] = str(Path(raw).resolve())
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                target = tool_ids.get(block.get("tool_use_id"))
                if target in wanted:
                    successful.add(target)
    return successful


def apply_inspection(record, pdfs, inspect_payload, normal_success, stream_path,
                     stream_offset=0):
    verify_path = run_registry.run_dir(record["id"]) / "verify.json"
    verify = json.loads(verify_path.read_text(encoding="utf-8"))
    problems = validate_inspect(inspect_payload)
    evidence = read_evidence(stream_path, pdfs, stream_offset)
    wanted = {str(Path(path).resolve()) for path in pdfs.values()}
    proven = not problems and normal_success and evidence == wanted
    issues = inspect_payload.get("issues", []) if isinstance(inspect_payload, dict) else []
    for check in verify["checks"]:
        if check["id"] == "visual_layout":
            if not proven:
                missing = problems or (["normal completion missing"] if not normal_success
                                       else ["both exact PDF Read results not found"])
                check.update(state="unverified",
                             detail="Inspection evidence missing: " + "; ".join(missing),
                             evidence={"read": sorted(evidence), "expected": sorted(wanted)})
            elif inspect_payload["verdict"] == "clean":
                check.update(state="pass", detail="Pass C read both PDFs and found no issues",
                             evidence={"inspect": inspect_payload, "read": sorted(evidence)})
            else:
                check.update(state="fail" if inspect_payload["verdict"] == "blocked" else "flag",
                             detail="%d visual issue(s): %s"
                             % (len(issues), inspect_payload["verdict"]),
                             evidence={"inspect": inspect_payload, "read": sorted(evidence)})
    jobs_md.write_json_atomic(verify_path, verify)
    return verify, proven


def compile_record(record):
    """Compile both targets, create machine verification and snapshot version."""
    run_id = record["id"]
    request_path = run_registry.run_dir(run_id) / "verify_request.json"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DocumentError("verify_request.json cannot be read: %s" % exc)
    keywords = request.get("keywords")
    if not isinstance(keywords, list) or not all(isinstance(k, str) and k for k in keywords):
        raise DocumentError("verify_request.json has no valid keywords")

    pdfs, evidence = {}, {}
    for kind in ("cv", "cover"):
        pdfs[kind], evidence[kind] = compile_one(kind, record["targets"][kind])
        activity.emit("latex", "%s compiled %s" % (run_id, kind),
                      cmd=" ".join(evidence[kind]["cmd"]), exit_code=0, run_id=run_id)
    verify = build_verify(record, pdfs, evidence, keywords)
    snapshot = snapshot_record(record, pdfs)
    return pdfs, verify, snapshot


def snapshot_record(record, pdfs):
    target = run_registry.run_dir(record["id"])
    target.mkdir(parents=True, exist_ok=True)
    artefacts = {}
    for kind in ("cv", "cover"):
        source = ROOT / record["targets"][kind]
        source_copy = target / ("%s_source%s" % (kind, source.suffix))
        pdf_copy = target / ("%s.pdf" % kind)
        shutil.copy2(source, source_copy)
        shutil.copy2(pdfs[kind], pdf_copy)
        artefacts[kind + "_source"] = str(source_copy.relative_to(ROOT))
        artefacts[kind + "_pdf"] = str(pdf_copy.relative_to(ROOT))
    return artefacts


def restore_record(version, current):
    """Restore one immutable snapshot into the only live source pair, then compile.

    Both sources are replaced as one document transaction. If compilation fails,
    the previous bytes are put back before the error reaches the HTTP caller.
    """
    version_dir = run_registry.run_dir(version["id"]).resolve()
    if version.get("slug") != current.get("slug"):
        raise DocumentError("version belongs to a different application")
    sources, backups = {}, {}
    for kind in ("cv", "cover"):
        saved = version_dir / ("%s_source%s" %
                               (kind, Path(current["targets"][kind]).suffix))
        try:
            saved.resolve().relative_to(version_dir)
        except ValueError:
            raise DocumentError("snapshot path escaped its run")
        if not saved.is_file():
            raise DocumentError("snapshot is missing %s source" % kind)
        live = ROOT / current["targets"][kind]
        reject_symlinks = getattr(run_guard, "reject_symlinks", None)
        if reject_symlinks:
            reject_symlinks(live, "live %s source" % kind)
        sources[kind] = (saved, live)
        backups[kind] = live.read_bytes() if live.is_file() else None
    try:
        for saved, live in sources.values():
            live.parent.mkdir(parents=True, exist_ok=True)
            temp = live.with_name(live.name + ".restore-%d" % os.getpid())
            shutil.copyfile(saved, temp)
            os.replace(temp, live)
        return compile_record(current)
    except Exception:
        for kind, (_saved, live) in sources.items():
            old = backups[kind]
            if old is None:
                try:
                    live.unlink()
                except FileNotFoundError:
                    pass
            else:
                temp = live.with_name(live.name + ".rollback-%d" % os.getpid())
                temp.write_bytes(old)
                os.replace(temp, live)
        raise


def standing_preferences():
    """Read only the managed preference block; text outside it is never exposed."""
    try:
        text = PROFILE.read_text(encoding="utf-8")
    except OSError:
        return {"preferences": [], "available": False}
    begin, end = "JOBFLOW-PREFS:BEGIN", "JOBFLOW-PREFS:END"
    if text.count(begin) != 1 or text.count(end) != 1:
        return {"preferences": [], "available": False,
                "error": "managed preference block is missing or ambiguous"}
    block = text.split(begin, 1)[1].split(end, 1)[0]
    prefs = [line.strip()[2:].strip() for line in block.splitlines()
             if line.strip().startswith("- ") and line.strip()[2:].strip()]
    return {"preferences": prefs, "available": True,
            "source": str(PROFILE.relative_to(ROOT))}


@contextmanager
def _tracker_lock():
    TRACKER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = open(TRACKER_LOCK, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def merge_tracker(record):
    """Merge a drafted application without moving an open row backwards."""
    fit = record.get("fit") or {}
    today = date.today().isoformat()
    values = {
        "date": today, "company": record["company"], "sector": fit.get("sector") or "",
        "role": record["role"], "role_type": fit.get("role_type") or "",
        "channel": fit.get("channel") or "", "status": "drafted",
        "contact_person": fit.get("contact_person") or "",
        "fit_rating": fit.get("overall", ""), "notes": "",
        "cv_file": record["targets"]["cv"], "cover_letter_file": record["targets"]["cover"],
        "source": record["job_url"], "deadline": fit.get("deadline") or "",
    }
    with _tracker_lock():
        rows = []
        header = list(CANONICAL_HEADER)
        if TRACKER.exists():
            with open(TRACKER, newline="", encoding="utf-8") as handle:
                raw = list(csv.reader(handle))
            if raw:
                header = raw[0]
                rows = raw[1:]
                if not header or header[-1] != "deadline":
                    header = header + ["deadline"]
                    rows = [row + [""] for row in rows]
        indexes = {name: index for index, name in enumerate(header)}
        missing = [name for name in CANONICAL_HEADER if name not in indexes]
        if missing:
            raise DocumentError("tracker header is missing columns: " + ", ".join(missing))
        width = len(header)
        rows = [row + [""] * (width - len(row)) for row in rows]
        matches = [index for index, row in enumerate(rows)
                   if row[indexes["company"]].casefold() == record["company"].casefold()
                   and row[indexes["role"]].casefold() == record["role"].casefold()]
        open_matches = [index for index in matches
                        if rows[index][indexes["status"]].strip().casefold()
                        not in FINAL_STATUSES]
        if open_matches:
            row = rows[open_matches[-1]]
            old_status = row[indexes["status"]]
            for key in ("cv_file", "cover_letter_file", "fit_rating", "source"):
                row[indexes[key]] = str(values[key])
            if values["deadline"]:
                row[indexes["deadline"]] = values["deadline"]
            note = row[indexes["notes"]].strip()
            row[indexes["notes"]] = note + ("; " if note else "") + "redrafted"
            if old_status == "drafted":
                row[indexes["date"]] = today
            action = "updated"
        else:
            rows.append([str(values.get(name, "")) for name in header])
            action = "appended"
        TRACKER.parent.mkdir(parents=True, exist_ok=True)
        temp = TRACKER.with_name(TRACKER.name + ".tmp-%d-%d"
                                 % (os.getpid(), threading.get_ident()))
        try:
            with open(temp, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(header)
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, TRACKER)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
    return action


def archive_posting(record):
    source = run_registry.run_dir(record["id"]) / "posting.md"
    if not source.is_file() or not source.stat().st_size:
        raise DocumentError("posting.md is missing")
    target_dir = APPLICATIONS / record["slug"]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "job_posting.md"
    if not target.exists():
        temp = target.with_name(target.name + ".tmp-%d" % os.getpid())
        shutil.copyfile(source, temp)
        os.replace(temp, target)
    return str(target.relative_to(ROOT))
