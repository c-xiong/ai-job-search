#!/usr/bin/env python3
"""A stand-in for `claude` that behaves like the real CLI where it matters.

The run supervisor cannot be tested against the real binary: a test that spends
money is a test nobody runs. What this fake reproduces is exactly the surface
`tools/board/runs.py` depends on, and nothing else:

* `--output-format stream-json` on stdout - a `system`/`init` event carrying the
  session id, `assistant` events with `tool_use` blocks, and a final `result`
  event with `total_cost_usd` and `usage`;
* **it calls the `PreToolUse` hook itself**, the way the CLI does, and honours
  the verdict, so the guard and the canary are exercised for real rather than
  simulated. `FAKE_MODE=noguard` models a CLI with **no hook installed at all**;
* the files a compliant pass of each stage writes.

It reads the canary path and every target path **out of the prompt**, exactly
as a real run would, so a supervisor that named the wrong file would fail these
tests rather than be rescued by a hardcoded path.

Stage is read from the prompt (`stage: DRAFT`, `Inspect the compiled PDF`, ...).
Failure injection:

* `FAKE_MODE` - `auto` (default), or for every pass: `noguard`, `hang`, `crash`,
  `rate_limit`, `quota`. `FAKE_ONLY=<stage>` limits it to one stage.
* `FAKE_PREPARE` - `ok` (default) | `nothing`
* `FAKE_DRAFT` - `ok` (default) | `cv_then_crash` | `truncate_cover` |
  `conflict` | `nothing` | `badbrief` | `outside` (also tries to write the master)
* `FAKE_REVIEW` - `pass` (default) | `revise_once` | `always_fix` | `blocked` | `bad`
* `FAKE_INSPECT` - `clean` (default) | `fixable` | `fixable_once` | `blocked` | `noread`
  (`FAKE_INSPECT_DOC` names the document an issue is raised on; default `cv`)
* `FAKE_CALLS` - a file that gets one JSON line per invocation (stage + argv flags)
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

RUN_DIR = Path(os.environ.get("JOBFLOW_RUN_DIR", "."))
GUARD = os.environ.get("FAKE_GUARD", "")
PROBE = re.compile(r"`([^`]*\.guard-probe-[0-9a-f]+)`")


def prompt_of(argv):
    return argv[argv.index("-p") + 1] if "-p" in argv else ""


def stage_of(prompt):
    if "Inspect the compiled PDF" in prompt:
        return "inspect"
    if "Apply only the following verified visual-layout repairs" in prompt:
        return "repair"
    match = re.search(r"stage: ([A-Z]+)\.", prompt)
    return match.group(1).lower() if match else "unknown"


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def call_hook(tool, payload):
    """What the CLI does before a guarded tool call."""
    if not GUARD:
        return None
    proc = subprocess.run([sys.executable, GUARD],
                          input=json.dumps({"tool_name": tool, "tool_input": payload}),
                          capture_output=True, text=True)
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


def write_through_guard(path, text):
    """Honour the hook's verdict, as the CLI would."""
    decision = call_hook("Write", {"file_path": str(path)})
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": str(path)}}]}})
    if decision == "deny":
        return False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")
    return True


def read_tool(path, ident):
    emit({"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": ident, "name": "Read", "input": {"file_path": path}}]}})
    emit({"type": "user", "message": {"content": [{
        "type": "tool_result", "tool_use_id": ident, "is_error": False,
        "content": "PDF rendered"}]}})


def target(prompt, label):
    match = re.search(r"^- %s: `([^`]+)`" % re.escape(label), prompt, re.M)
    return match.group(1) if match else None


def finish(session, cost, text="ok", is_error=False, code=0):
    emit({"type": "result", "subtype": "success" if not is_error else "error_during_execution",
          "is_error": is_error, "result": text, "total_cost_usd": cost,
          "session_id": session, "num_turns": 3,
          "usage": {"input_tokens": 1200, "output_tokens": 800,
                    "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 900}})
    return code


def latex(body):
    return "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n" % body


BRIEF = {
    "schema": "jobflow.brief/1", "company": "Acme", "role": "ML Engineer",
    "location": "Zurich, CH", "language": "en", "deadline": None, "sector": "AI",
    "role_type": "Full-time", "contact_person": None, "channel": "portal",
    "hard_conflicts": [], "keywords": ["LLM", "RAG"],
    "requirements": [{"requirement": "LLM systems", "priority": "required",
                      "evidence": "research pipeline", "status": "documented"}],
}


def counter(name):
    path = RUN_DIR / (".fake-%s-count" % name)
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return count


def main():
    argv = sys.argv[1:]
    prompt = prompt_of(argv)
    stage = stage_of(prompt)
    mode = os.environ.get("FAKE_MODE", "auto")
    only = os.environ.get("FAKE_ONLY")
    if only and only != stage:
        mode = "auto"
    global GUARD
    if mode == "noguard":
        GUARD = ""

    session = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "none"
    calls = os.environ.get("FAKE_CALLS")
    if calls:
        with open(calls, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"stage": stage, "resume": "--resume" in argv,
                                     "fork": "--fork-session" in argv,
                                     "session": session,
                                     "run": os.environ.get("JOBFLOW_RUN_ID")}) + "\n")
    emit({"type": "system", "subtype": "init", "session_id": session,
          "tools": ["Read", "Write"]})

    if mode in ("rate_limit", "quota"):
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected",
            "rateLimitType": "out_of_credits" if mode == "quota" else "five_hour"}})
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "api_error_status": 429,
              "result": ("You've hit your session limit" if mode == "quota"
                         else "Rate limited, try again shortly"),
              "total_cost_usd": 0, "session_id": session})
        return 1
    if mode == "crash":
        return finish(session, 0.01, "something went wrong", True, 1)
    if mode == "hang":
        time.sleep(600)
        return 0

    found = PROBE.search(prompt)
    write_through_guard(found.group(1) if found else RUN_DIR / ".guard-probe-missing", "probe")

    if stage == "prepare":
        path = target(prompt, "write posting")
        if os.environ.get("FAKE_PREPARE", "ok") == "ok" and path:
            write_through_guard(path, "# Acme - ML Engineer\n\nWe are looking for an ML "
                                "engineer. Requirements: 3+ years Python, experience with "
                                "LLM systems and RAG, strong communication. You will build "
                                "and evaluate retrieval pipelines with the team.\n" * 3)
        return finish(session, 0.05, "prepared")

    if stage == "draft":
        how = os.environ.get("FAKE_DRAFT", "ok")
        brief_path = target(prompt, "write brief")
        cv = target(prompt, "write CV")
        cover = target(prompt, "write cover letter")
        if how == "outside":
            write_through_guard(os.environ.get("FAKE_MASTER", "cv/my_cv.tex"), latex("x"))
        if brief_path:
            brief = dict(BRIEF)
            if (cover or "with its `letter_plan`" in prompt) and how != "noplan":
                brief["letter_plan"] = {
                    "role_task": "Build and evaluate retrieval pipelines",
                    "role_task_source": "posting.md: Requirements and responsibilities",
                    "primary_evidence": "Contract retrieval agent",
                    "secondary_evidence": "NLP research pipeline",
                    "connection": "Reliable retrieval for useful AI products",
                    "unknowns": [],
                }
            if how == "conflict":
                brief["hard_conflicts"] = ["German C1 is required"]
            if how == "badbrief":
                brief = {"schema": "wrong"}
            write_through_guard(brief_path, json.dumps(brief))
        if how == "conflict" and "proceed despite" not in prompt:
            return finish(session, 0.2, "stopped on a hard conflict")
        if how == "nothing":
            return finish(session, 0.5, "did nothing")
        if cv:
            write_through_guard(cv, latex("Tailored CV for Acme at %s" % time.time()))
        if how == "cv_then_crash":
            return finish(session, 0.6, "budget exhausted mid-draft", True, 1)
        if cover:
            if how == "truncate_cover":
                write_through_guard(cover, "\\documentclass{cover}\n\\begin{document}\nDear")
            else:
                write_through_guard(cover, latex("Dear Hiring Manager, Acme %s" % time.time()))
        return finish(session, 0.87, "drafted")

    if stage == "review":
        how = os.environ.get("FAKE_REVIEW", "pass")
        path = target(prompt, "write review")
        items = re.search(r"Review items: ([^.]+)\.", prompt).group(1).split(", ")
        findings = []
        if how == "always_fix" or (how == "revise_once" and counter("review") == 1):
            doc = "cv" if "cv" in items else items[0]
            findings = [{"doc": doc, "severity": "must_fix", "category": "grounding",
                         "quote": "Tailored", "issue": "metric not in the sources",
                         "fix": "drop the metric"}]
        verdict = "revise" if findings else "pass"
        if how == "blocked":
            verdict = "blocked"
        body = {"schema": "jobflow.review/1", "verdict": verdict, "findings": findings,
                "source_conflicts": ["profile and CV disagree on an end date"]
                if how == "blocked" else [],
                "tailoring_notes": ["led with the RAG evidence"]}
        write_through_guard(path, "{not json" if how == "bad" else json.dumps(body))
        return finish(session, 0.3, "reviewed")

    if stage in ("fix", "revise"):
        for label in ("edit CV", "edit cover letter"):
            path = target(prompt, label)
            if path:
                text = Path(path).read_text(encoding="utf-8")
                write_through_guard(path, text.replace("\\end{document}",
                                                       "Fixed wording %s\n\\end{document}"
                                                       % time.time()))
        return finish(session, 0.2, "fixed")

    if stage == "inspect":
        paths = re.findall(r"^- (?:CV|Cover letter): `([^`]+)`", prompt, re.M)
        how = os.environ.get("FAKE_INSPECT", "clean")
        if how == "fixable_once":
            how = "fixable" if counter("inspect") == 1 else "clean"
        if how != "noread":
            for index, path in enumerate(paths):
                read_tool(path, "read-%d" % index)
        verdict = "clean" if how == "noread" else how
        issues = [] if verdict == "clean" else [{
            "doc": os.environ.get("FAKE_INSPECT_DOC", "cv"), "page": 1,
            "kind": "orphaned heading", "fix_hint": "add needspace"}]
        write_through_guard(RUN_DIR / "inspect.json", json.dumps({
            "schema": "jobflow.inspect/1", "verdict": verdict, "issues": issues}))
        return finish(session, 0.08, "inspected")

    if stage == "repair":
        for path in re.findall(r"^(?:CV|Cover letter): `([^`]+)`", prompt, re.M):
            text = Path(path).read_text(encoding="utf-8")
            if os.environ.get("FAKE_REPAIR") == "truncate":
                write_through_guard(path, text[: len(text) // 2])
                continue
            write_through_guard(path, text.replace(
                "\\end{document}", "\\needspace{5\\baselineskip}\n\\end{document}"))
        return finish(session, 0.08, "repaired")

    return finish(session, 0.01, "unknown stage", True, 1)


if __name__ == "__main__":
    sys.exit(main())
