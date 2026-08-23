"""Proving the write boundary before a run is allowed to cost anything.

`guard_write.py` is the hook the CLI calls. This module is the half that runs on
the board's side: it builds the per-run allowlist, and it refuses to start a run
until the guard has been shown to work. The distinction matters because the CLI
fails *open* on a malformed `--settings` file - measured on 2.1.159, the run
proceeds with no hook at all and stops only at the dollar cap.

Four checks, and a run starts only when all four pass. They run before **every**
spawn, not once at admission: a settings file that was valid when pass A started
can be edited, moved or broken during the minutes a human spends looking at the
approval card, and pass B is the expensive one.

1. the settings file parses and declares exactly the matcher and command the
   guard was written for;
2. the guard binary is executed for real, four times, and must allow what should
   be allowed and deny what should not - including the canary path;
3. the run's target paths contain no symlink, so an allowlisted `cv/main_x.tex`
   cannot be a link pointing at `CLAUDE.md`;
4. once running, a per-pass canary write must be refused. That is the positive
   signal: "nothing tried to write" and "nothing was watching" are otherwise the
   same silence.

Stdlib only, Python 3.9+.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

from . import activity, run_registry  # noqa: E402

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "run-settings.json"
GUARD = HERE / "guard_write.py"

EXPECTED_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash"


class PreflightError(Exception):
    """The guard could not be proven installed. No run starts on a maybe."""


# ---------------------------------------------------------------- preflight

def validate_settings(path=None):
    """Parse and shape-check the settings file the CLI is about to be handed.

    The CLI accepts a malformed `--settings` file without complaint and runs
    anyway, so "the file exists" proves nothing. Checked here: it parses, it
    declares exactly one `PreToolUse` matcher, the matcher is the one the guard
    was written for, and the command it runs is this repo's `guard_write.py`.
    """
    path = Path(path or SETTINGS)
    if not path.exists():
        raise PreflightError("%s is missing" % path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PreflightError("%s is not valid JSON (%s) - the CLI would accept this "
                             "silently and run with no guard" % (path, exc))
    try:
        entries = data["hooks"]["PreToolUse"]
        entry = entries[0]
        matcher = entry["matcher"]
        command = entry["hooks"][0]["command"]
        kind = entry["hooks"][0]["type"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PreflightError("%s has no usable hooks.PreToolUse entry (%s)" % (path, exc))
    if len(entries) != 1:
        raise PreflightError("%s declares %d PreToolUse entries; the guard expects exactly 1"
                             % (path, len(entries)))
    if matcher != EXPECTED_MATCHER:
        raise PreflightError("%s matcher is %r, expected %r - a narrower matcher silently "
                             "leaves a tool unguarded" % (path, matcher, EXPECTED_MATCHER))
    if kind != "command":
        raise PreflightError("%s hook type is %r, expected 'command'" % (path, kind))
    if "tools/board/guard_write.py" not in command:
        raise PreflightError("%s runs %r, which is not this repo's guard" % (path, command))
    if not GUARD.exists():
        raise PreflightError("%s is missing" % GUARD)
    return True


def probe_guard():
    """Run the guard for real, allowed and denied. A guard that cannot say no is
    not installed, and a guard that cannot say yes fails every run."""
    with tempfile.TemporaryDirectory() as tmp:
        allowed_dir = Path(tmp) / "run"
        allowed_dir.mkdir()
        probe = allowed_dir / ".guard-probe-preflight"
        allowlist = Path(tmp) / "allowlist.json"
        allowlist.write_text(json.dumps({
            "dirs": [str(allowed_dir.resolve())],
            "files": [],
            "deny": [str(probe.resolve())],
        }), encoding="utf-8")
        env = dict(os.environ, JOBFLOW_ALLOWLIST=str(allowlist))
        env.pop("JOBFLOW_HOOK_LOG", None)

        def verdict(payload):
            proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                                  capture_output=True, text=True, env=env, timeout=30)
            if proc.returncode != 0:
                raise PreflightError("guard exited %d: %s" % (proc.returncode, proc.stderr[:400]))
            if not proc.stdout.strip():
                return None
            try:
                body = json.loads(proc.stdout)
            except ValueError:
                raise PreflightError("guard printed non-JSON: %r" % proc.stdout[:200])
            return body.get("hookSpecificOutput", {}).get("permissionDecision")

        checks = (
            ("a path inside the run directory", "allow",
             {"tool_name": "Write", "tool_input": {"file_path": str(allowed_dir / "fit.json")}}),
            ("a write outside the allowlist", "deny",
             {"tool_name": "Write", "tool_input": {"file_path": str(Path(tmp) / "elsewhere.tex")}}),
            ("the canary path", "deny",
             {"tool_name": "Write", "tool_input": {"file_path": str(probe)}}),
            ("a chained shell command", "deny",
             {"tool_name": "Bash", "tool_input": {
                 "command": "python3 tools/board/fetch_url.py https://example.com; rm -rf /"}}),
        )
        for label, expected, payload in checks:
            actual = verdict(payload)
            if actual != expected:
                raise PreflightError("the guard answered %r for %s, expected %r - the write "
                                     "boundary is not enforcing" % (actual, label, expected))
    return True


def preflight(claude_bin=None):
    """Every check, timed onto the activity strip. Raises `PreflightError`."""
    import shutil

    with activity.Timer() as timer:
        binary = claude_bin or run_registry.config()["claude_bin"]
        if not (shutil.which(binary) or Path(binary).exists()):
            raise PreflightError("%r is not on PATH. Install the Claude Code CLI, or set "
                                 "claude_bin in job_scraper/board_config.json." % binary)
        validate_settings()
        probe_guard()
    activity.emit("claude", "preflight ok - settings shape verified, guard denied a write "
                            "outside the allowlist and allowed one inside", ms=timer.ms)
    return True


# ---------------------------------------------------------------- allowlist

def reject_symlinks(path, label, base=None):
    """Refuse a path that is, or sits under, a symlink **inside the repo**.

    The allowlist stores realpaths, and so does the guard - which is what makes
    `..` traversal pointless. It also means a *pre-existing* symlink at an
    allowlisted target silently authorises its destination: make
    `cv/main_acme.tex` a link to `CLAUDE.md` and an allowlisted CV write
    overwrites the profile. Checked here, before the run starts, because
    afterwards the guard has no way to tell the two apart.

    Only components at or below `base` are checked. Above it is the machine's own
    filesystem layout, which no model run can influence and which is routinely
    symlinked - `/var` -> `/private/var` on macOS would otherwise make every path
    on the system unallowlistable.
    """
    base = Path(base or run_registry.ROOT)
    path = Path(path)
    try:
        relative = path.relative_to(base)
    except ValueError:
        candidates = [path]
    else:
        candidates = [base.joinpath(*relative.parts[:n + 1])
                      for n in range(len(relative.parts))]
    for part in candidates:
        try:
            is_link = part.is_symlink()
        except OSError:
            continue
        if is_link:
            raise PreflightError(
                "%s (%s) is or sits under a symlink (%s); refusing to allowlist a path "
                "whose real destination is somewhere else" % (label, path, part))
    return True


def probe_name(nonce):
    """The canary file for one pass. Per-pass, so pass B cannot inherit pass A's
    proof that the guard was working twenty minutes ago."""
    return ".guard-probe-%s" % nonce


def new_nonce():
    return uuid.uuid4().hex[:8]


def write_allowlist(run_id, targets=(), nonce=""):
    """The per-run write boundary, as exact realpaths.

    Pass A gets the run directory only; pass B additionally gets the two target
    documents. `deny` names the files the supervisor writes into a directory the
    model can otherwise write: the canary must stay refusable, and the transcript
    must stay the supervisor's account of the run.
    """
    directory = run_registry.state_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    run_dir = run_registry.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    reject_symlinks(run_dir, "run directory")
    for target in targets:
        reject_symlinks(target, "target document")

    path = directory / "allowlist.json"
    deny = [str((run_dir / "stream.jsonl").resolve())]
    if nonce:
        deny.append(str((run_dir / probe_name(nonce)).resolve()))
    body = {
        "run_id": run_id,
        "dirs": [str(run_dir.resolve())],
        "files": sorted({str(Path(t).resolve()) for t in targets}),
        "deny": deny,
    }
    jobs_md.write_json_atomic(path, body)
    return path


# ------------------------------------------------------- the fit.json schema

FIT_GATES = ("PASS", "FLAG", "FAIL")
FIT_VERDICTS = ("strong", "good", "moderate", "weak", "poor")
FIT_SCORES = ("technical", "experience", "behavioural", "career")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_fit(payload):
    """[] when `payload` is a valid `jobflow.fit/1` document, else the reasons.

    "Valid JSON with arbitrary keys" is not a contract: the tracker row, the
    approval card and `/upskill`'s arithmetic all read specific fields, and a
    missing one has to fail the run rather than reach the UI as a blank.
    """
    problems = []
    if not isinstance(payload, dict):
        return ["fit.json is not a JSON object"]
    if payload.get("schema") != "jobflow.fit/1":
        problems.append("schema is %r, expected 'jobflow.fit/1'" % (payload.get("schema"),))

    for key in ("company", "role"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            problems.append("%s must be a non-empty string" % key)
    if not isinstance(payload.get("location"), str):
        problems.append("location must be a string")

    for key in ("language_gate", "location_gate"):
        if payload.get(key) not in FIT_GATES:
            problems.append("%s must be one of %s" % (key, list(FIT_GATES)))
    if not isinstance(payload.get("language_note"), str):
        problems.append("language_note must be a string")

    scores = payload.get("scores")
    if not isinstance(scores, dict):
        problems.append("scores must be an object")
    else:
        for key in FIT_SCORES:
            if not _number(scores.get(key)) or not 0 <= scores[key] <= 100:
                problems.append("scores.%s must be a number 0-100" % key)

    if not _number(payload.get("overall")) or not 0 <= payload["overall"] <= 100:
        problems.append("overall must be a number 0-100")
    if payload.get("verdict") not in FIT_VERDICTS:
        problems.append("verdict must be one of %s" % list(FIT_VERDICTS))

    for key in ("matches", "gaps"):
        value = payload.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            problems.append("%s must be a list of strings" % key)

    # Nullable, but never *absent*: a posting that does not state a sector must
    # produce an empty tracker cell, and an explicit null is the model saying so.
    # A missing key is the model forgetting, and the two must not look alike.
    for key in ("deadline", "sector", "role_type", "contact_person", "channel"):
        if key not in payload:
            problems.append("%s is required (use null when the posting does not state it)" % key)
        elif payload[key] is not None and not isinstance(payload[key], str):
            problems.append("%s must be a string or null" % key)
    if payload.get("deadline") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(payload["deadline"])):
        problems.append("deadline must be YYYY-MM-DD or null")
    if payload.get("channel") not in (None, "portal", "online"):
        problems.append("channel must be 'portal', 'online' or null")
    if not isinstance(payload.get("posting_chars"), int) or isinstance(
            payload.get("posting_chars"), bool):
        problems.append("posting_chars must be an integer")
    return problems


def validate_drafts(payload, targets, kinds=("cv", "cover")):
    """[] when `drafts.json` names exactly this run's allowlisted targets.

    The supervisor compiles what this file points at, so "some path the model
    wrote" is not good enough: it has to be the paths the guard authorised, or
    the run failed and is not allowed to look like it succeeded. `kinds` is the
    run's scope - a CV-only run is judged on `cv_source` alone, and whatever it
    says about the document it was not allowed to write is ignored.
    """
    problems = []
    if not isinstance(payload, dict):
        return ["drafts.json is not a JSON object"]
    if payload.get("schema") != "jobflow.drafts/1":
        problems.append("schema is %r, expected 'jobflow.drafts/1'" % (payload.get("schema"),))
    keys = {"cv": "cv_source", "cover": "cover_source"}
    for kind in kinds:
        key = keys[kind]
        expected, actual = targets[kind], payload.get(key)
        if actual != expected:
            problems.append("%s is %r, expected %r - the run may only write the paths "
                            "it was given" % (key, actual, expected))
    return problems
