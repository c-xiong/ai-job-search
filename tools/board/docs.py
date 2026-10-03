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
import apply_email  # noqa: E402
import jobs_md  # noqa: E402

from . import activity, cover_blocks, run_guard, run_registry, templates


ROOT = run_registry.ROOT
TRACKER = ROOT / "job_search_tracker.csv"
TRACKER_LOCK = ROOT / ".tracker.lock"
APPLICATIONS = ROOT / "documents" / "applications"
PROFILE = ROOT / ".claude" / "skills" / "job-application-assistant" / "01-candidate-profile.md"
CANONICAL_HEADER = (
    "date,company,sector,role,role_type,channel,status,contact_person,"
    "fit_rating,notes,cv_file,cover_letter_file,source,deadline,portal_url,my_notes,"
    "apply_email"
).split(",")
# The owner's own columns, edited on the Send step and mirrored to Notion:
# where the employer's candidate portal lives, or the address an emailed
# application goes to (prefilled from the posting), and free-text notes. They are
# kept apart from `notes`, which /outcome and /gmail-sync read as a dated log of
# contact with the employer.
OWNER_COLUMNS = {"portal_url": "Application Portal", "apply_email": "Application Email",
                 "my_notes": "My Notes"}
FINAL_STATUSES = {
    "hired", "rejected", "no_response", "offer_declined", "withdrawn",
    "no response", "offer declined",
}
VERIFY_STATES = {"pass", "flag", "fail", "skipped", "unverified"}
# A run does not have to produce both documents: the board can ask for a CV
# only, or a cover letter only, and `scope` on the record says which. Compile,
# snapshot, verification, restore and the tracker row all iterate this instead
# of the hardcoded pair, so a document the run never owned is never compiled,
# never page-checked and never overwritten.
DOC_LABELS = {"cv": "CV", "cover": "cover letter"}
LATEX_ENGINES = {"lualatex", "xelatex", "pdflatex"}
LATEX_CLEAN = (".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".synctex.gz")


# The factual masters. This repository reads them and never writes them: the CV
# master is maintained (and built) in its own repository and reaches this one
# as a symlink, and the cover base holds the rules and evidence bank every
# letter is tailored from.
MASTER_CV_REL = "cv/my_cv.tex"
# One cover base serves every role: the letter's evidence is chosen from the
# posting's tasks, not from the CV's `sde`/`ai` variant.
COVER_BASE_REL = "cover_letters/my_cover.tex"


def _root():
    """The repository root, read at call time so a redirected registry is honoured."""
    return run_registry.ROOT


def master_cv():
    return _root() / MASTER_CV_REL


def cover_base():
    return _root() / COVER_BASE_REL


# A letter fits its one page by cutting words, never by squeezing the layout:
# stretching the page, pulling space back, or shrinking the type or spacing.
COVER_SQUEEZE = re.compile(
    r"\\enlargethispage|\\vspace\*?\{\s*-"
    r"|\\(?:fontsize|small|footnotesize|scriptsize|linespread)(?![A-Za-z])"
    r"|\\(?:setlength|addtolength)\{?\\(?:parskip|baselineskip|textheight|topmargin|itemsep)")

COVER_MAX_WORDS = 380

# Unresolved template slots: `[COMPANY]`, `[ROLE FIT]`, `[CONTACT PERSON OR
# HIRING TEAM]` - searched in the document body and the extracted text - and any
# use of the CV master's `\cvTODO{..}` marker, searched in the whole source
# because the master defines its section bodies as macros in the preamble.
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9 ,/&().'-]{2,}\]")
TODO_USE = re.compile(r"(?<!newcommand\{)(?<!providecommand\{)\\cvTODO\{")


def expected_pages(kind):
    """The exact page count the active template demands - one source of truth."""
    return int(templates.active(kind).get("pages") or templates.STOCK[kind]["pages"])


def protected_paths():
    """Realpaths no pipeline step may ever write: the masters and their targets."""
    paths = set()
    for path in (master_cv(), cover_base()):
        paths.add(os.path.realpath(os.path.abspath(str(path))))
        paths.add(os.path.abspath(str(path)))
    return paths


def assert_writable(path, label):
    """Refuse a write destination that is a master, an alias of one, or a symlink.

    Destinations are resolved first, so `cv/./my_cv.tex`, a second symlink to
    the upstream file, or the upstream path itself are all the same refusal.
    """
    raw = os.path.abspath(str(path))
    real = os.path.realpath(raw)
    if raw in protected_paths() or real in protected_paths():
        raise DocumentError("%s (%s) is a read-only master; refusing to write it"
                            % (label, path))
    if os.path.islink(raw):
        raise DocumentError("%s (%s) is a symlink; refusing to write through it"
                            % (label, path))
    if not _inside(real, _root()):
        raise DocumentError("%s (%s) resolves outside the repository" % (label, path))
    return Path(raw)


def doc_kinds(record):
    """The document kinds one run owns, in display order."""
    scope = (record or {}).get("scope") or "both"
    return ("cv", "cover") if scope == "both" else (scope,)


def doc_phrase(kinds):
    """Name the documents in prose: CV, cover letter, or CV and cover letter."""
    return " and ".join(DOC_LABELS[kind] for kind in kinds)


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
                "name": spec["name"], "source": spec.get("source"),
                "pages": spec.get("pages"), "home": spec.get("home")}
    if len(words) == 2 and engine == "typst" and words[1] == "compile":
        return {"kind": "typst", "engine": "typst", "ext": spec["ext"],
                "name": spec["name"], "source": spec.get("source"),
                "pages": spec.get("pages"), "home": spec.get("home")}
    raise DocumentError("toolchain_unsupported: %s" % command)


def _run(argv, cwd, timeout=120):
    try:
        return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocumentError("%s could not run: %s" % (Path(argv[0]).name, exc))


def compile_one(kind, source, build=None):
    """Compile one exact allowlisted source and return evidence + PDF path.

    The compiler runs from the template's home directory (`cv/`,
    `cover_letters/`), because `cover.cls` and its fonts resolve relative to it,
    and writes into `build` - by default the `build/` folder beside the source.
    An attempt-local source under `documents/runs/` therefore compiles exactly
    as the published copy will, without ever being placed in the live tree.
    """
    source = Path(source)
    if not source.is_absolute():
        source = _root() / source
    if not _inside(source, _root()) or source.is_symlink():
        raise DocumentError("%s source escapes the repository or is a symlink" % kind)
    if os.path.realpath(str(source)) in protected_paths():
        raise DocumentError("%s source is a read-only master; compile a copy" % kind)
    tool = resolve_toolchain(kind)
    if source.suffix != tool["ext"]:
        raise DocumentError("%s source extension %s does not match active template %s"
                            % (kind, source.suffix, tool["ext"]))
    if not source.is_file():
        raise DocumentError("%s source is missing: %s" % (kind, source))

    build = Path(build) if build else source.parent / "build"
    build.mkdir(parents=True, exist_ok=True)
    pdf = build / (source.stem + ".pdf")
    before = pdf.stat().st_mtime_ns if pdf.exists() else None
    if tool["kind"] == "latex":
        home = _root() / (tool.get("home") or source.parent.relative_to(_root()))
        argv = [tool["engine"], "-interaction=nonstopmode",
                "-output-directory=%s" % build, str(source.resolve())]
        cwd = home if home.is_dir() else source.parent
    else:
        argv = ["typst", "compile", str(source), str(pdf)]
        cwd = _root()
    proc = _run(argv, cwd)
    evidence = {"cmd": argv, "exit": proc.returncode,
                "stdout_tail": proc.stdout.splitlines()[-8:],
                "stderr_tail": proc.stderr.splitlines()[-8:],
                "errors": [line for line in proc.stdout.splitlines()
                           if line.startswith("!")][:6],
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
    evidence["pdf"] = str(pdf.relative_to(_root()))
    return pdf, evidence


def pdf_pages(path):
    """Count page objects from the PDF itself, without depending on pdfinfo.

    pdfTeX and XeTeX write PDF 1.5 *object streams*: the page dictionaries are
    zlib-compressed inside a stream, invisible to a byte search. So the search
    runs over the raw file and over every Flate stream that inflates, and the
    page tree's own `/Count` is the fallback.
    """
    import zlib
    data = Path(path).read_bytes()
    chunks = [data]
    for match in re.finditer(rb"stream\r?\n", data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        try:
            chunks.append(zlib.decompressobj().decompress(data[start:end]))
        except zlib.error:
            continue
    count = sum(len(re.findall(rb"/Type\s*/Page\b", chunk)) for chunk in chunks)
    if count <= 0:
        values = [int(v) for chunk in chunks for v in re.findall(
            rb"/Type\s*/Pages\b.{0,300}?/Count\s+(\d+)", chunk, re.DOTALL)]
        count = max(values or [0])
    if count <= 0:
        raise DocumentError("could not determine page count from %s" % path)
    return count


def extract_text(path):
    binary = shutil.which("pdftotext")
    if not binary:
        return None, {"cmd": "pdftotext -layout", "available": False}
    proc = _run([binary, "-layout", str(path), "-"], _root())
    evidence = {"cmd": [binary, "-layout", str(path), "-"], "exit": proc.returncode}
    if proc.returncode != 0:
        return None, evidence
    return proc.stdout, evidence


def _contact_literals(kind=None):
    """The email and phone a document must carry as literal text.

    Read from that document's own source of truth - the CV master for a CV,
    the cover base for a letter - and CLAUDE.md, because the owner's CLAUDE.md
    need not repeat contact details at all (and the two masters may format the
    same phone number differently).
    """
    sources = [_root() / "CLAUDE.md"]
    sources.append(master_cv() if kind == "cv"
                   else cover_base() if kind == "cover" else None)
    emails, phones = [], []
    for path in filter(None, sources):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        text = "\n".join(re.split(r"(?<!\\)%", line, 1)[0] for line in text.splitlines())
        emails += [e for e in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
                   if not e.endswith(("example.com", "example.test"))
                   or path.name == "CLAUDE.md"]
        phones += re.findall(r"\(?\+\d{1,3}\)?(?:[ -]?\d{2,4}){2,5}", text)
    return list(dict.fromkeys(emails[:1] + phones[:1]))


def _literal_present(literal, text):
    """Emails match exactly; phone numbers match on their digits, since
    `(+41) 79 000 12 34` and `+41 79 000 12 34` are the same number."""
    if "@" in literal:
        return literal in text
    digits = re.sub(r"\D", "", literal)
    return bool(digits) and digits in re.sub(r"\D", "", text)


def _check(check_id, label, state, detail, evidence=None):
    if state not in VERIFY_STATES:
        raise ValueError("bad verification state")
    return {"id": check_id, "label": label, "state": state, "detail": detail,
            "evidence": evidence or {}}


def _letter_words(body):
    """Words of body text in a letter: every `\\lettercontent{..}` after the
    salutation plus every list, with LaTeX commands removed. An estimate, good
    to a few words."""
    blocks, start = [], 0
    while True:
        at = body.find("\\lettercontent{", start)
        if at < 0:
            break
        depth, end = 0, at + len("\\lettercontent")
        for end in range(end, len(body)):
            depth += {"{": 1, "}": -1}.get(body[end], 0)
            if depth == 0:
                break
        blocks.append(body[at + len("\\lettercontent{"):end])
        start = end + 1
    lists = re.findall(r"\\begin\{(?:itemize|enumerate)\}(?:\[[^\]]*\])?(.*?)"
                       r"\\end\{(?:itemize|enumerate)\}", body, re.S)
    text = " ".join(blocks[1:] + lists)
    text = re.sub(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?", " ", text)
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'.,%/-]*", text))


def check_pdf(kind, pdf, source, keywords, compile_evidence):
    """Mechanical checks for one built document. Returns `(state, checks)`.

    Everything here is a property of *this* document: CV keyword coverage is
    measured on the CV's own text layer, never on a letter that happens to
    mention the term. A check that could not run is `skipped`/`unverified`,
    never silently a pass.
    """
    checks = []
    expected = expected_pages(kind)
    pages = pdf_pages(pdf)
    checks.append(_check(
        kind + "_page_count",
        "%s is exactly %d page%s" % (DOC_LABELS[kind].capitalize(), expected,
                                    "" if expected == 1 else "s"),
        "pass" if pages == expected else "fail",
        "%s, %d page%s" % (compile_evidence.get("toolchain", "?"), pages,
                          "" if pages == 1 else "s"),
        dict(compile_evidence, pages=pages)))
    try:
        source_text = Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        source_text = ""
    code = "\n".join(re.split(r"(?<!\\)%", line, 1)[0]
                     for line in source_text.splitlines())
    body = code.split("\\begin{document}", 1)[-1]
    slots = sorted(set(PLACEHOLDER.findall(body)))
    if TODO_USE.search(code):
        slots.append("\\cvTODO{..}")
    if kind == "cover":
        try:
            base_text = cover_base().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            base_text = ""
        fixed = cover_blocks.validate_fixed_blocks(source_text, base_text)
        if fixed["enabled"]:
            checks.append(_check(
                "cover_fixed_blocks", "Selected approved letter paragraphs are unchanged",
                "fail" if fixed["issues"] else "pass",
                "; ".join(fixed["issues"]) if fixed["issues"] else
                "approved blocks match the canonical base", fixed))
        layout = cover_blocks.validate_layout(source_text, base_text)
        if layout["enabled"]:
            checks.append(_check(
                "cover_layout", "Letter preserves the approved base layout",
                "fail" if layout["issues"] else "pass",
                "; ".join(layout["issues"]) if layout["issues"] else
                "preamble, geometry and fonts match the canonical base", layout))
        squeeze = sorted(set(m.group(0) for m in COVER_SQUEEZE.finditer(body)))
        checks.append(_check("cover_no_squeeze",
                             "Letter fits one page without layout squeezing",
                             "fail" if squeeze else "pass",
                             "cut words instead of: " + ", ".join(squeeze) if squeeze
                             else "no page-stretching or type-shrinking commands"))
        words = _letter_words(body)
        checks.append(_check("cover_words",
                             "Letter body is at most %d words" % COVER_MAX_WORDS,
                             "fail" if words > COVER_MAX_WORDS else "pass",
                             "about %d words of body text" % words))
    text, evidence = extract_text(pdf)
    if text is not None:
        slots = sorted(set(slots) | set(PLACEHOLDER.findall(text)))
    checks.append(_check(kind + "_placeholders", "No unresolved template placeholders",
                         "fail" if slots else "pass",
                         "unresolved: " + ", ".join(slots[:8]) if slots
                         else "no [SLOT] or \\cvTODO markers remain"))
    if text is None:
        for suffix, label in (
                ("text_layer", "Text layer is extractable"),
                ("contact_literals", "Contact details survived extraction"),
                ("reading_order", "Extracted reading order is usable")):
            checks.append(_check(kind + "_" + suffix, label, "skipped",
                                 "pdftotext is unavailable or failed", evidence))
    else:
        clean = bool(text.strip()) and "\ufffd" not in text and "(cid:" not in text
        checks.append(_check(kind + "_text_layer", "Text layer is extractable",
                             "pass" if clean else "fail",
                             "%d extracted characters" % len(text.strip()), evidence))
        literals = _contact_literals(kind)
        missing = [literal for literal in literals if not _literal_present(literal, text)]
        checks.append(_check(kind + "_contact_literals",
                             "Contact details survived extraction",
                             ("pass" if not missing else "flag") if literals else "skipped",
                             ("all configured literals found" if not missing
                              else "missing: " + ", ".join(missing)) if literals
                             else "no contact literals are configured in CLAUDE.md",
                             evidence))
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        order_ok = len(lines) >= 3 and max((len(line) for line in lines), default=0) < 500
        checks.append(_check(kind + "_reading_order",
                             "Extracted reading order is usable",
                             "pass" if order_ok else "flag",
                             "%d non-empty lines" % len(lines), evidence))
    coverage = None
    if kind == "cv":
        if not keywords:
            checks.append(_check("cv_keywords", "CV covers the targeted posting keywords",
                                 "unverified", "no keyword list was recorded for this run"))
        elif text is None:
            checks.append(_check("cv_keywords", "CV covers the targeted posting keywords",
                                 "skipped", "pdftotext is unavailable; coverage not measured"))
        else:
            folded = text.casefold()
            covered = [k for k in keywords if k.casefold() in folded]
            absent = [k for k in keywords if k.casefold() not in folded]
            coverage = {"covered": covered, "absent": absent, "source": "cv"}
            # The CV is the untailored master variant, so an absent term is a
            # screening note for the owner (shown under Screening), not a defect.
            checks.append(_check("cv_keywords", "Posting terms screened against the CV",
                                 "pass",
                                 "%d/%d in the CV text layer%s"
                                 % (len(covered), len(keywords),
                                    "; absent: " + ", ".join(absent[:8]) if absent else ""),
                                 coverage))
    order = {"fail": 4, "unverified": 3, "flag": 2, "skipped": 1, "pass": 0}
    worst = max((c["state"] for c in checks), key=lambda st: order[st])
    # Only failures block; a skipped extraction or a flagged keyword is shown,
    # not hidden behind a green summary.
    state = "fail" if worst == "fail" else ("flag" if worst != "pass" else "pass")
    return state, checks, coverage


def build_verify(record, pdfs, compile_evidence, keywords, sources=None):
    """Machine-produce verify.json. Visual checks await evidence from inspection."""
    checks = []
    coverage = {"covered": [], "absent": list(keywords or []), "source": "cv"}
    kinds = doc_kinds(record)
    for kind in kinds:
        source = (sources or {}).get(kind) or _root() / record["targets"][kind]
        _state, doc_checks, doc_cover = check_pdf(kind, pdfs[kind], source, keywords,
                                                  compile_evidence[kind])
        checks += doc_checks
        if doc_cover is not None:
            coverage = doc_cover
    checks.append(_check("visual_layout", "Visual layout was inspected", "unverified",
                         "No inspection has yet proved it read the compiled %s"
                         % doc_phrase(kinds)))
    verify = {"schema": "jobflow.verify/1", "run_id": record["id"],
              "checks": checks, "keywords": coverage}
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


def judge_inspection(pdfs, payload, normal_success, stream_path, stream_offset=0):
    """Per-document visual verdicts, and whether the inspection is proven.

    Proven means: the inspection file is valid, the pass ended normally, and
    the transcript shows a *successful* Read of every exact PDF path - the very
    files whose hashes the verdict will be bound to. A reader that looked at a
    different (or older) PDF, or claimed success without reading, proves
    nothing and every document stays `unverified`.
    """
    problems = validate_inspect(payload)
    evidence = read_evidence(stream_path, pdfs, stream_offset)
    wanted = {str(Path(path).resolve()) for path in pdfs.values()}
    proven = not problems and normal_success and evidence == wanted
    verdicts = {}
    issues = payload.get("issues", []) if isinstance(payload, dict) and not problems else []
    for kind in pdfs:
        mine = [issue for issue in issues if issue.get("doc") == kind]
        if not proven:
            missing = problems or (["normal completion missing"] if not normal_success
                                   else ["exact PDF Read results not found"])
            verdicts[kind] = ("unverified", "Inspection evidence missing: "
                              + "; ".join(missing), mine)
        elif payload["verdict"] == "blocked" and (mine or not issues):
            verdicts[kind] = ("fail", "%d blocking visual issue(s)" % len(mine), mine)
        elif mine:
            verdicts[kind] = ("flag", "%d fixable visual issue(s)" % len(mine), mine)
        else:
            verdicts[kind] = ("pass", "the exact PDF was read and no issue was found", [])
    return proven, verdicts, {"read": sorted(evidence), "expected": sorted(wanted)}


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
                                       else ["exact PDF Read results not found"])
                check.update(state="unverified",
                             detail="Inspection evidence missing: " + "; ".join(missing),
                             evidence={"read": sorted(evidence), "expected": sorted(wanted)})
            elif inspect_payload["verdict"] == "clean":
                check.update(state="pass",
                             detail="Pass C read %d PDF(s) and found no issues" % len(wanted),
                             evidence={"inspect": inspect_payload, "read": sorted(evidence)})
            else:
                check.update(state="fail" if inspect_payload["verdict"] == "blocked" else "flag",
                             detail="%d visual issue(s): %s"
                             % (len(issues), inspect_payload["verdict"]),
                             evidence={"inspect": inspect_payload, "read": sorted(evidence)})
    jobs_md.write_json_atomic(verify_path, verify)
    return verify, proven


def compile_record(record):
    """Compile this run's targets, verify them and snapshot the version."""
    run_id = record["id"]
    if not (run_registry.run_dir(run_id) / "verify_request.json").exists():
        return recompile_record(record)
    request_path = run_registry.run_dir(run_id) / "verify_request.json"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DocumentError("verify_request.json cannot be read: %s" % exc)
    keywords = request.get("keywords")
    if not isinstance(keywords, list) or not all(isinstance(k, str) and k for k in keywords):
        raise DocumentError("verify_request.json has no valid keywords")
    return _compile_with(record, keywords)


def run_keywords(run_id):
    """The CV keyword list a run targeted: brief.json, else the legacy file."""
    directory = run_registry.run_dir(run_id)
    for name in ("brief.json", "verify_request.json"):
        try:
            payload = json.loads((directory / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        keywords = payload.get("keywords") if isinstance(payload, dict) else None
        if isinstance(keywords, list) and all(isinstance(k, str) and k for k in keywords):
            return keywords
    return []


def recompile_record(record):
    """Recompile a finished application's live sources without a model call."""
    return _compile_with(record, run_keywords(record["id"]))


def _compile_with(record, keywords):
    run_id = record["id"]

    pdfs, evidence = {}, {}
    for kind in doc_kinds(record):
        assert_writable(_root() / record["targets"][kind], "live %s source" % kind)
        pdfs[kind], evidence[kind] = compile_one(kind, record["targets"][kind])
        activity.emit("latex", "%s compiled %s" % (run_id, kind),
                      cmd=" ".join(evidence[kind]["cmd"]), exit_code=0, run_id=run_id)
    verify = build_verify(record, pdfs, evidence, keywords)
    snapshot = snapshot_record(record, pdfs)
    return pdfs, verify, snapshot


def require_cover_page_limit(pdf):
    """The rendered cover must fit before any finished copy is replaced."""
    pages, expected = pdf_pages(pdf), expected_pages("cover")
    if pages != expected:
        raise DocumentError("cover letter has %d pages; exactly %d required. "
                            "Draft kept; shorten optional evidence or tailored prose "
                            "and rebuild before publishing." % (pages, expected))


def snapshot_record(record, pdfs):
    if "cover" in pdfs:
        require_cover_page_limit(pdfs["cover"])
    target = run_registry.run_dir(record["id"])
    target.mkdir(parents=True, exist_ok=True)
    artefacts = {}
    for kind in doc_kinds(record):
        source = _root() / record["targets"][kind]
        source_copy = target / ("%s_source%s" % (kind, source.suffix))
        pdf_copy = target / ("%s.pdf" % kind)
        shutil.copy2(source, source_copy)
        shutil.copy2(pdfs[kind], pdf_copy)
        artefacts[kind + "_source"] = str(source_copy.relative_to(_root()))
        artefacts[kind + "_pdf"] = str(pdf_copy.relative_to(_root()))
    return artefacts


def _atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".publish-%d-%d"
                            % (os.getpid(), threading.get_ident()))
    try:
        shutil.copyfile(str(source), str(temp))
        os.replace(str(temp), str(target))
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


SUBMIT_LABELS = {"cv": "CV", "cover": "Cover_Letter"}


def candidate_name():
    """The owner's name from CLAUDE.md's `**Name:**` line, or None."""
    try:
        text = (_root() / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"\*\*Name:\*\*\s*([^\n\[]+)", text)
    return match.group(1).strip() if match and match.group(1).strip() else None


def submission_copy(record, kind, pdf):
    """The copy you submit: named for the recipient, filed by application.

    `documents/applications/<company>_<role>/<First>_<Last>_CV.pdf` - the folder
    says which job it is for, the CV's name says only whose document it is, and
    the letter's also names the company it is addressed to
    (`<First>_<Last>_Cover_Letter_<Company>.pdf`). Refreshed whenever its bytes
    differ from `pdf`.
    """
    if kind == "cover":
        require_cover_page_limit(pdf)
    parts = [candidate_name(), SUBMIT_LABELS[kind]]
    if kind == "cover":
        parts.append(record.get("company"))
    filename = "_".join(stem for stem in (re.sub(r"[^\w-]+", "_", p or "").strip("_")
                                          for p in parts) if stem) + ".pdf"
    target = assert_writable(_root() / "documents" / "applications" / record["slug"] / filename,
                             "submission %s" % kind)
    from . import checkpoint
    if checkpoint.sha256(target) != checkpoint.sha256(pdf):
        _atomic_copy(Path(pdf), target)
    return target


def publish_document(record, kind, source, pdf):
    """Place one checked attempt-local version onto its live paths, atomically.

    Idempotent: when the live files already hold exactly these bytes nothing is
    written. The destination is resolved and refused if it is a master, an
    alias of one, or a symlink - the model never writes live files at all, and
    this is the one supervisor path that does.
    """
    from . import checkpoint
    if kind == "cover":
        require_cover_page_limit(pdf)
    target = assert_writable(_root() / record["targets"][kind], "live %s source" % kind)
    try:
        run_guard.reject_symlinks(target, "live %s source" % kind)
    except run_guard.PreflightError as exc:
        raise DocumentError(str(exc))
    pdf_target = assert_writable(target.parent / "build" / (target.stem + ".pdf"),
                                 "live %s PDF" % kind)
    source_sha, pdf_sha = checkpoint.sha256(source), checkpoint.sha256(pdf)
    if not source_sha or not pdf_sha:
        raise DocumentError("%s cannot be published: its checked files are missing" % kind)
    written = []
    if checkpoint.sha256(target) != source_sha:
        _atomic_copy(source, target)
        written.append(str(target.relative_to(_root())))
    if checkpoint.sha256(pdf_target) != pdf_sha:
        _atomic_copy(pdf, pdf_target)
        written.append(str(pdf_target.relative_to(_root())))
    # The version snapshot restore reads, in the layout older runs used.
    directory = run_registry.run_dir(record["id"])
    _atomic_copy(source, directory / ("%s_source%s" % (kind, Path(source).suffix)))
    _atomic_copy(pdf, directory / ("%s.pdf" % kind))
    submission = submission_copy(record, kind, pdf)
    return {"target": str(target.relative_to(_root())),
            "pdf_target": str(pdf_target.relative_to(_root())),
            "submission": os.path.relpath(str(submission), str(_root())),
            "source_sha256": source_sha, "pdf_sha256": pdf_sha,
            "written": written}


def restore_record(version, current):
    """Restore one immutable snapshot onto the live sources, then compile.

    Every source the snapshot holds is replaced as one document transaction. If
    compilation fails, the previous bytes are put back before the error reaches
    the HTTP caller. A CV-only or cover-only version restores only its own
    document and leaves the other one alone.
    """
    version_dir = run_registry.run_dir(version["id"]).resolve()
    if version.get("slug") != current.get("slug"):
        raise DocumentError("version belongs to a different application")
    sources, backups = {}, {}
    for kind in doc_kinds(version):
        saved = version_dir / ("%s_source%s" %
                               (kind, Path(current["targets"][kind]).suffix))
        try:
            saved.resolve().relative_to(version_dir)
        except ValueError:
            raise DocumentError("snapshot path escaped its run")
        if not saved.is_file():
            raise DocumentError("snapshot is missing %s source" % kind)
        live = _root() / current["targets"][kind]
        assert_writable(live, "live %s source" % kind)
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
        # Compile the union: the documents the application already had, plus
        # the one this snapshot just put back. Narrowing to the version's own
        # scope would leave a live document uncompiled and its verification
        # stale; narrowing to the current run's scope would make restoring a
        # document it never had a silent no-op.
        kinds = [kind for kind in ("cv", "cover")
                 if kind in set(doc_kinds(version)) | set(doc_kinds(current))]
        scope = "both" if len(kinds) == 2 else kinds[0]
        return compile_record(dict(current, scope=scope))
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


def merge_tracker(record, brief=None):
    """Merge a drafted application without moving an open row backwards.

    Safe to repeat: a row that already carries exactly this run's document
    paths as a `drafted` row dated today is left untouched, so a publication
    retried after an interruption neither duplicates the row nor stacks
    `redrafted` markers. Never writes any status beyond `drafted`.

    `fit_rating` comes only from a real fit evaluation. The default pipeline
    does not score, and an absent score is written as an empty cell - never as
    0, and never by blanking a rating an earlier evaluation stored.
    """
    fit = record.get("fit") or {}
    meta = dict(brief or {})
    meta.update({k: v for k, v in fit.items() if v not in (None, "")})
    today = date.today().isoformat()
    values = {
        "date": today, "company": record["company"], "sector": meta.get("sector") or "",
        "role": record["role"], "role_type": meta.get("role_type") or "",
        "channel": meta.get("channel") or "", "status": "drafted",
        "contact_person": meta.get("contact_person") or "",
        "fit_rating": fit.get("overall", ""), "notes": "",
        "cv_file": "", "cover_letter_file": "",
        "source": record["job_url"], "deadline": meta.get("deadline") or "",
        "apply_email": _posting_email(record),
    }
    kinds = doc_kinds(record)
    columns = {"cv": "cv_file", "cover": "cover_letter_file"}
    for kind in kinds:
        values[columns[kind]] = record["targets"][kind]
    with _tracker_lock():
        rows = []
        header = list(CANONICAL_HEADER)
        if TRACKER.exists():
            with open(TRACKER, newline="", encoding="utf-8") as handle:
                raw = list(csv.reader(handle))
            if raw:
                header = raw[0]
                rows = raw[1:]
                header, rows = _upgrade_header(header, rows)
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
            same = all(row[indexes[columns[kind]]] == values[columns[kind]] for kind in kinds)
            if same and old_status == "drafted" and row[indexes["date"]] == today:
                return "unchanged"
            for key in [columns[kind] for kind in kinds] + ["fit_rating", "source"]:
                if key == "fit_rating" and values[key] in ("", None):
                    continue
                row[indexes[key]] = str(values[key])
            if values["deadline"]:
                row[indexes["deadline"]] = values["deadline"]
            # Prefill only: an address the owner typed or Notion holds wins.
            if values["apply_email"] and not row[indexes["apply_email"]].strip():
                row[indexes["apply_email"]] = values["apply_email"]
            note = row[indexes["notes"]].strip()
            row[indexes["notes"]] = note + ("; " if note else "") + "redrafted"
            if old_status == "drafted":
                row[indexes["date"]] = today
            action = "updated"
        else:
            rows.append([str(values.get(name, "")) for name in header])
            action = "appended"
        _write_tracker(header, rows)
    return action


def _posting_email(record):
    """The posting's application address, or "" - read from the run's posting.md."""
    try:
        text = (run_registry.run_dir(record["id"]) / "posting.md").read_text(encoding="utf-8")
    except (KeyError, OSError):
        return ""
    return apply_email.best(text)


def _upgrade_header(header, rows):
    """Append any canonical column an older tracker lacks, and pad every row.

    Columns are only ever added at the end, so a tracker written before
    `deadline` or the owner columns existed keeps every value where it was.
    """
    header = list(header) + [name for name in CANONICAL_HEADER if name not in header]
    return header, [row + [""] * (len(header) - len(row)) for row in rows]


def _write_tracker(header, rows):
    """Atomically replace the tracker. Callers hold `_tracker_lock()`."""
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


def _tracker_key(company, role):
    return ((company or "").casefold().strip(), (role or "").casefold().strip())


def tracker_rows():
    """{(company, role) folded: row dict} from the tracker; last row wins."""
    try:
        with open(TRACKER, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return {}
    return {_tracker_key(r.get("company"), r.get("role")): r for r in rows}


def tracker_statuses():
    """{(company, role) folded: (status, date)} from the tracker; last row wins."""
    return {key: ((r.get("status") or "").strip(), (r.get("date") or "").strip())
            for key, r in tracker_rows().items()}


def save_owner_fields(record, values, mark_applied=False):
    """Write the owner columns (`OWNER_COLUMNS`) on this application's row.

    `values` holds only the columns to change. Returns "updated", "unchanged"
    or "missing" (no tracker row for this company and role yet).
    """
    with _tracker_lock():
        if not TRACKER.exists():
            return "missing"
        with open(TRACKER, newline="", encoding="utf-8") as handle:
            raw = list(csv.reader(handle))
        if not raw:
            return "missing"
        header, rows = _upgrade_header(raw[0], raw[1:])
        ix = {name: index for index, name in enumerate(header)}
        key = _tracker_key(record["company"], record["role"])
        match = next((row for row in reversed(rows)
                      if _tracker_key(row[ix["company"]], row[ix["role"]]) == key), None)
        if match is None:
            return "missing"
        changed = False
        for name, value in values.items():
            if name in OWNER_COLUMNS and match[ix[name]] != value:
                match[ix[name]], changed = value, True
        if mark_applied and match[ix["status"]].strip() == "drafted":
            match[ix["status"]], match[ix["date"]] = "applied", date.today().isoformat()
            changed = True
        if not changed:
            return "unchanged"
        _write_tracker(header, rows)
    return "updated"


def mark_applied(record):
    """Move this application's tracker row from `drafted` to `applied`, dated today.

    Only a `drafted` row moves - a status Notion or /outcome already advanced is
    left alone. Returns "updated", "unchanged" or "missing".
    """
    with _tracker_lock():
        if not TRACKER.exists():
            return "missing"
        with open(TRACKER, newline="", encoding="utf-8") as handle:
            raw = list(csv.reader(handle))
        if not raw:
            return "missing"
        header, rows = raw[0], raw[1:]
        ix = {name: index for index, name in enumerate(header)}
        rows = [row + [""] * (len(header) - len(row)) for row in rows]
        key = _tracker_key(record["company"], record["role"])
        match = next((row for row in reversed(rows)
                      if _tracker_key(row[ix["company"]], row[ix["role"]]) == key), None)
        if match is None:
            return "missing"
        if match[ix["status"]].strip() != "drafted":
            return "unchanged"
        match[ix["status"]], match[ix["date"]] = "applied", date.today().isoformat()
        _write_tracker(header, rows)
    return "updated"


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
    return str(target.relative_to(_root()))
