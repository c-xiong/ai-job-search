#!/usr/bin/env python3
"""A stand-in for `claude` that behaves like the real CLI where it matters.

The run supervisor cannot be tested against the real binary: a test that spends
money is a test nobody runs. What this fake reproduces is exactly the surface
`tools/board/runs.py` depends on, and nothing else:

* `--output-format stream-json` on stdout - a `system`/`init` event carrying the
  session id, `assistant` events with `tool_use` blocks, and a final `result`
  event with `total_cost_usd`;
* **it calls the `PreToolUse` hook itself**, the way the CLI does, and honours
  the verdict, so the guard and the canary are exercised for real rather than
  simulated. `FAKE_MODE=noguard` models a CLI with **no hook installed at all** -
  every write succeeds, including the canary - which is precisely the failure the
  canary exists to catch;
* the files a compliant run writes into `$JOBFLOW_RUN_DIR`.

It reads the canary path **out of the prompt**, exactly as a real run would, so
a supervisor that stopped minting a fresh nonce per pass would fail these tests
rather than quietly reusing an old one.

`FAKE_MODE=auto` picks pass A or pass B from the prompt. The others are failure
injections: `noguard`, `badfit`, `nofit`, `noposting`, `nowrite`, `hang`,
`crash`, `rate_limit`. `FAKE_SKIP=drafts|verify` omits one pass-B contract file, and
`FAKE_SCOPE=ignore` makes pass B write both documents whatever scope it was
given.

The real CLI's behaviour is pinned separately by `tests/test_live_cli_contract.py`,
which runs against the installed binary and is skipped unless asked for.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

RUN_DIR = Path(os.environ.get("JOBFLOW_RUN_DIR", "."))
GUARD = os.environ.get("FAKE_GUARD", "")
# `noguard` is a CLI with no hook installed at all - not one that skips the
# canary and then politely asks permission for everything else. Modelling it as
# the latter is how a test can pass while the real failure goes uncaught.
if os.environ.get("FAKE_MODE") == "noguard":
    GUARD = ""

PROBE = re.compile(r"`([^`]*\.guard-probe-[0-9a-f]+)`")


def mode(argv):
    """`auto` reads the prompt, the way the real workflow does."""
    chosen = os.environ.get("FAKE_MODE", "auto")
    if chosen != "auto":
        return chosen
    prompt = prompt_of(argv)
    if "Inspect the compiled PDF" in prompt:
        return "inspect"
    if "Apply only the following verified visual-layout repairs" in prompt:
        return "repair"
    return "draft" if ("Continue from Step 2" in prompt or
                       "Resume this application" in prompt) else "fit"


def prompt_of(argv):
    return argv[argv.index("-p") + 1] if "-p" in argv else ""


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


FIT = {
    "schema": "jobflow.fit/1", "company": "Acme", "role": "ML Engineer",
    "location": "Zurich, CH", "deadline": None,
    "language_gate": "PASS", "language_note": "English posting", "location_gate": "PASS",
    "scores": {"technical": 80, "experience": 72, "behavioural": 77, "career": 85},
    "overall": 78, "verdict": "good", "matches": ["LLM systems"], "gaps": ["Rust"],
    "sector": "AI", "role_type": "Full-time", "contact_person": None,
    "channel": "portal", "posting_chars": 4321,
}


def main():
    argv = sys.argv[1:]
    chosen = mode(argv)
    prompt = prompt_of(argv)

    session = "fake-session"
    for flag in ("--session-id", "--resume"):
        if flag in argv:
            session = argv[argv.index(flag) + 1]
    if "--fork-session" in argv:
        session = str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": session,
          "tools": ["Read", "Write"]})

    if chosen == "rate_limit":
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "rateLimitType": "out_of_credits"}})
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "api_error_status": 429, "result": "You've hit your session limit",
              "total_cost_usd": 0, "session_id": session})
        return 1
    if chosen == "crash":
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "result": "something went wrong", "total_cost_usd": 0.01,
              "session_id": session})
        return 1
    if chosen == "hang":
        time.sleep(600)
        return 0

    # Every real run's first instruction: a write the allowlist excludes. The
    # path is read from the prompt, so a stale nonce would not be refused. With
    # no hook installed (`noguard`) the write simply succeeds, which is exactly
    # the state the canary exists to detect.
    found = PROBE.search(prompt)
    write_through_guard(found.group(1) if found else RUN_DIR / ".guard-probe-missing",
                        "probe")

    if chosen == "inspect":
        paths = re.findall(r"- (?:CV|Cover(?: letter)?): `([^`]+)`", prompt)
        if os.environ.get("FAKE_INSPECT") == "noread":
            paths = []
        for index, path in enumerate(paths):
            ident = "read-%d" % index
            emit({"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": ident, "name": "Read",
                "input": {"file_path": path}}]}})
            emit({"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": ident, "is_error": False,
                "content": "PDF rendered"}]}})
        verdict = os.environ.get("FAKE_INSPECT", "clean")
        if verdict == "noread":
            verdict = "clean"
        issues = [] if verdict == "clean" else [{
            "doc": "cv", "page": 2, "kind": "orphaned heading",
            "fix_hint": "add needspace"}]
        write_through_guard(RUN_DIR / "inspect.json", json.dumps({
            "schema": "jobflow.inspect/1", "verdict": verdict, "issues": issues}))
        emit({"type": "result", "subtype": "success", "is_error": False,
              "result": "inspected", "total_cost_usd": 0.08, "session_id": session})
        return 0

    if chosen == "repair":
        for path in re.findall(r"^(?:CV|Cover letter): `([^`]+)`", prompt, re.M):
            write_through_guard(path, "%% repaired at %s\n" % time.time())
        emit({"type": "result", "subtype": "success", "is_error": False,
              "result": "repaired", "total_cost_usd": 0.08, "session_id": session})
        return 0

    if chosen == "draft":
        cv = os.environ["JOBFLOW_CV_TARGET"]
        cover = os.environ["JOBFLOW_COVER_TARGET"]
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Task", "input": {"description": "reviewer"}}]}})
        selected = (cv, cover)
        if "Scope: cv." in prompt:
            selected = (cv,)
        elif "Scope: cover." in prompt:
            selected = (cover,)
        # A model that ignores the stated scope and writes the other document
        # anyway: the run must be stopped by the write allowlist, not by the
        # model's good manners.
        if os.environ.get("FAKE_SCOPE") == "ignore":
            selected = (cv, cover)
        for path in selected:
            if not write_through_guard(path, "%% draft for %s at %s\n" % (path, time.time())):
                emit({"type": "result", "subtype": "success", "is_error": True,
                      "result": "guard refused %s" % path, "total_cost_usd": 0.5,
                      "session_id": session})
                return 1
        if os.environ.get("FAKE_SKIP") != "drafts":
            write_through_guard(RUN_DIR / "drafts.json", json.dumps(
                {"schema": "jobflow.drafts/1", "cv_source": cv, "cover_source": cover}))
        if os.environ.get("FAKE_SKIP") != "verify":
            write_through_guard(RUN_DIR / "verify_request.json", json.dumps(
                {"schema": "jobflow.verify/1", "keywords": ["LLM", "RAG"]}))
        emit({"type": "result", "subtype": "success", "is_error": False,
              "result": "drafted", "total_cost_usd": 0.87, "session_id": session})
        return 0

    if chosen == "nowrite":
        # Pass B that produces nothing but claims success - the shape that must
        # not be rescued by last month's files sitting at the target paths.
        emit({"type": "result", "subtype": "success", "is_error": False,
              "result": "did nothing", "total_cost_usd": 0.87, "session_id": session})
        return 0

    if chosen == "badfit":
        write_through_guard(RUN_DIR / "fit.json", json.dumps({"schema": "wrong", "company": "Acme"}))
    elif chosen != "nofit":
        write_through_guard(RUN_DIR / "fit.json", json.dumps(FIT))
    if chosen != "noposting":
        write_through_guard(RUN_DIR / "posting.md", "# Acme - ML Engineer\n\nthe posting.\n")
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": "evaluated", "total_cost_usd": 0.12, "session_id": session})
    return 0


if __name__ == "__main__":
    sys.exit(main())
