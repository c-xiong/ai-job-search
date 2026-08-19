#!/usr/bin/env python3
"""`PreToolUse` hook: the write boundary for a headless `/apply` run.

A board-driven run is `claude -p --permission-mode acceptEdits`. Under that mode
every `Write` and `Edit` is auto-approved, so *this file is the only thing*
standing between a model run and the rest of the repository. It is passed to the
CLI through `tools/board/run-settings.json` and reads its per-run allowlist from
`$JOBFLOW_ALLOWLIST`.

Three decisions, in this order:

1. **Writes** are matched against **exact realpaths** from the allowlist, not
   against directory prefixes. A run may write its own run directory, plus the
   two target documents the supervisor computed for *this* application. A
   previous application's `cover_*.tex`, `cv/main_example.tex`, and every other
   run's snapshot are all outside it - which is what keeps "restore v1" from
   being destroyed by the run producing v2.
2. **Bash** is one fixed argv shape with one variable argument:
   `python3 tools/board/fetch_url.py <https url>`. Anything else - a flag, a
   pipe, a second command, a different script - is refused. The CLI's
   `--allowedTools` already narrows this; the hook re-validates because a
   matcher is a string match and this is a `shlex` parse.
3. Everything else the hook sees is left to the CLI's own permission handling.
   Reads stay wide on purpose: WebSearch and WebFetch are how the reviewer step
   does company research.

Every decision is appended to `$JOBFLOW_HOOK_LOG`. That log is what makes the
guard *observable*: the supervisor's canary write (`.guard-probe`) has to show
up there as a denial before the run is allowed to continue, because "the hook
did not fire because nothing tried to write" and "the hook is not installed"
are otherwise the same silence.

Stdlib only, Python 3.9+.
"""

import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
FETCH_SCRIPT = "tools/board/fetch_url.py"

# RFC 3986's allowed URL characters, minus the three that mean something to a
# shell even inside double quotes: `$`, a backquote, and a backslash. `&`, `?`,
# `=`, `;` and `%` are here because real posting URLs need them - but only inside
# a quoted argument, which is what `_tokenize` below is for.
URL_CHARS = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!&'()*+,;=%-]+$")


def _log(entry):
    """Append one decision. Never raises: a logger that breaks the run is worse."""
    path = os.environ.get("JOBFLOW_HOOK_LOG")
    if not path:
        return
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass


def load_allowlist(path=None):
    """`{files, dirs, deny, error}` - all realpaths. A missing file denies everything."""
    path = path or os.environ.get("JOBFLOW_ALLOWLIST")
    if not path:
        return {"files": [], "dirs": [], "deny": [], "error": "JOBFLOW_ALLOWLIST is not set"}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"files": [], "dirs": [], "deny": [], "error": "unreadable allowlist: %s" % exc}
    if not isinstance(data, dict):
        return {"files": [], "dirs": [], "deny": [], "error": "allowlist is not an object"}
    return {
        "files": [str(x) for x in data.get("files") or []],
        "dirs": [str(x) for x in data.get("dirs") or []],
        "deny": [str(x) for x in data.get("deny") or []],
        "error": None,
    }


def _resolved(raw):
    """Realpath without requiring the file to exist - a Write usually creates it."""
    try:
        return str(Path(os.path.realpath(os.path.abspath(str(raw)))))
    except (OSError, ValueError):
        return None


def _under(target, directory):
    try:
        Path(target).relative_to(Path(directory))
        return True
    except ValueError:
        return False


def check_write(raw_path, allowlist):
    """(allowed, reason) for one write target."""
    if allowlist.get("error"):
        return False, "guard cannot read its allowlist (%s)" % allowlist["error"]
    if not raw_path:
        return False, "the tool call carries no path"
    target = _resolved(raw_path)
    if target is None:
        return False, "path does not resolve: %r" % (raw_path,)
    if target in allowlist["deny"]:
        return False, "%s is supervisor-owned and never model-writable" % raw_path
    if target in allowlist["files"]:
        return True, "target document for this run"
    for directory in allowlist["dirs"]:
        if _under(target, directory):
            return True, "inside this run's own directory"
    return False, ("%s is outside this run's allowlist. This run may write only its own "
                   "run directory and the two target documents named in the prompt."
                   % raw_path)


def _tokenize(command):
    """Split the way a shell does, with operators as their own tokens.

    `shlex.split` is not enough, and the difference is a hole rather than a nit:
    it does not treat `;` `&` `|` as operators, so
    `fetch_url.py https://x.test/a;id` comes back as **three** tokens, looks like
    the permitted shape, and runs `id`. `punctuation_chars=True` makes the shell's
    operators tokens in their own right, so that command lands as five and is
    refused - while a properly quoted `"https://x.test/a?b=1&c=2"` stays one
    token and is allowed, because quoting is exactly what makes it one argument
    to the shell too.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # `shlex` treats `#` as starting a comment; **Bash does not**, mid-word. So
    # `.../a#;id` lexed with comments enabled is three tokens ending at the `#`,
    # looks like the permitted shape, and the shell then runs `id`. Turning
    # comments off is what makes the tokenizer model the shell rather than a
    # config-file syntax; a genuine URL fragment like `.../a#top` still lands as
    # one token and is still allowed.
    lexer.commenters = ""
    return list(lexer)


def check_bash(command):
    """(allowed, reason) for one Bash command.

    The check is on the **parsed argv**, not on a banned-character list: exactly
    three tokens, the first two fixed, and the third a plain https URL. What is
    refused inside that third token is the small set of characters a shell
    expands even inside double quotes - `$`, a backquote, a backslash - because
    those would make the argv something other than what it reads as.
    """
    if not command or not str(command).strip():
        return False, "empty command"
    command = str(command)
    if "\n" in command or "\r" in command:
        return False, "a newline makes this more than one command"
    try:
        argv = _tokenize(command)
    except ValueError as exc:
        return False, "command does not parse: %s" % exc
    if len(argv) != 3 or argv[0] != "python3" or argv[1] != FETCH_SCRIPT:
        return False, ("the only permitted command is: python3 %s <https url> - one "
                       "command, three words, and a URL containing & or ; must be quoted "
                       "(got %d token(s))" % (FETCH_SCRIPT, len(argv)))
    if not URL_CHARS.match(argv[2]):
        return False, ("%r is not a plain https:// URL; shell-expandable characters are "
                       "refused because they make the argv something other than it reads as"
                       % argv[2][:120])
    return True, "supervisor-owned fetch shim"


def decide(payload, allowlist):
    """(decision, reason) where decision is 'allow', 'deny' or None (defer to the CLI)."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    if tool in WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        allowed, reason = check_write(raw, allowlist)
        return ("allow" if allowed else "deny"), reason
    if tool == "Bash":
        allowed, reason = check_bash(tool_input.get("command"))
        return ("allow" if allowed else "deny"), reason
    return None, "not a guarded tool"


def main(argv=None):
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    allowlist = load_allowlist()
    decision, reason = decide(payload, allowlist)
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    target = (tool_input.get("file_path") or tool_input.get("notebook_path")
              or tool_input.get("command"))

    # Both the path as asked for and the path the decision was actually made on.
    # The supervisor reads `resolved` when it checks which writes this pass was
    # allowed; comparing raw paths would fail on a relative one, since the hook's
    # cwd and the supervisor's are not guaranteed to be the same directory.
    resolved = _resolved(target) if payload.get("tool_name") in WRITE_TOOLS else None
    _log({"tool": payload.get("tool_name"), "target": str(target)[:500],
          "resolved": resolved,
          "decision": decision or "defer", "reason": reason,
          "run_id": os.environ.get("JOBFLOW_RUN_ID"),
          # The canary carries a per-pass nonce (`.guard-probe-<nonce>`), so the
          # match is on the prefix. The supervisor checks the exact nonce; this
          # flag is what makes the log readable by a human.
          "probe": bool(target and Path(str(target)).name.startswith(".guard-probe"))})

    if decision is None:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": "jobflow guard: " + reason,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
