#!/usr/bin/env python3
"""Report what the candidate profile claims that the master CV does not.

    python3 tools/profile_drift.py            # human-readable report
    python3 tools/profile_drift.py --json     # machine-readable, for a hook

`/apply`'s Factual Grounding Audit treats three files as sources of truth and
calls a claim grounded if *any* of them supports it. That is the right rule for
the audit - it stops a draft inventing things - but it has a blind spot that is
structural rather than accidental: a fact present in one source and missing from
another is an **absence**, not a contradiction, so it can never be reported. The
consequence is that deleting a project from the CV is invisible. The profile goes
on asserting it, and the next fit evaluation cites it in detail at a company that
will never see it on the CV.

This is the missing half. It does not judge, rewrite, or grade anything: it lists
the headline claims in the profile that no longer appear in the master CV, so the
divergence is something you look at rather than something you discover in a run.

Absence is not automatically wrong. A project can be deliberately off a one-page
CV and still be real evidence for an interview - which is why this prints a
report and exits 0 by default, and only fails with `--strict` if you want to wire
it into a hook.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / ".claude" / "skills" / "job-application-assistant" / "01-candidate-profile.md"
CV = ROOT / "cv" / "my_cv.tex"

# The sections whose bold headline entries are claims a CV would carry. Skills,
# languages and references are deliberately excluded: a skills list and a CV's
# skills line are worded differently on purpose, and comparing them produces
# noise that trains you to ignore the report.
CLAIM_SECTIONS = ("Education", "Professional Experience", "Independent Projects",
                  "Publications", "Awards")

# `- **Name of the thing** (dates) - stack`, and `### Role - Employer (dates)`.
BULLET_CLAIM = re.compile(r"^\s*-\s+\*\*(?P<claim>[^*]+)\*\*")
HEADING_CLAIM = re.compile(r"^###\s+(?P<claim>.+?)\s*$")


def sections(markdown):
    """`{heading: [line, ...]}` for the profile's `##` sections."""
    found, current = {}, None
    for line in markdown.splitlines():
        heading = re.match(r"^##\s+(?!#)(?P<name>.+?)\s*$", line)
        if heading:
            current = heading.group("name").strip()
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return found


def claims(markdown):
    """Headline claims, as (section, claim) pairs, in document order."""
    out = []
    for name, lines in sections(markdown).items():
        if name not in CLAIM_SECTIONS:
            continue
        for line in lines:
            match = BULLET_CLAIM.match(line) or HEADING_CLAIM.match(line)
            if not match:
                continue
            claim = match.group("claim").strip().rstrip(":")
            # "Sentinel: An Agentic Code Review System" -> the name is
            # what a CV would repeat; the subtitle usually is not.
            out.append((name, claim))
    return out


def _searchable(latex):
    """The CV's prose, with LaTeX markup flattened into something comparable."""
    text = re.sub(r"%.*", " ", latex)                       # comments
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)             # control sequences
    text = re.sub(r"[{}$&~^\\]", " ", text)                 # syntax
    text = re.sub(r"[^0-9a-zA-Z]+", " ", text)
    return " " + text.lower().strip() + " "


def _keys(claim):
    """The distinctive words of a claim, for a substring test that survives
    rewording. A CV writes "Annotator" where the profile writes "Annotator
    (ACL 2025)", so the parenthetical and the stack list are dropped and what
    is left has to appear."""
    head = re.split(r"[(\u2013\u2014]| - ", claim)[0]
    words = re.findall(r"[0-9a-zA-Z]+", head.lower())
    return [w for w in words if len(w) > 2]


def drift(profile_text, cv_text):
    haystack = _searchable(cv_text)
    missing = []
    for section, claim in claims(profile_text):
        keys = _keys(claim)
        if not keys:
            continue
        hits = sum(1 for key in keys if " " + key in haystack or key in haystack)
        # Most of the distinctive words have to land. One shared word ("Engineer")
        # is not evidence that the CV carries the entry.
        if hits < max(1, (len(keys) + 1) // 2):
            missing.append({"section": section, "claim": claim,
                            "matched": hits, "of": len(keys)})
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when anything has drifted (for a hook)")
    parser.add_argument("--profile", type=Path, default=PROFILE)
    parser.add_argument("--cv", type=Path, default=CV)
    args = parser.parse_args(argv)

    for path, what in ((args.profile, "candidate profile"), (args.cv, "master CV")):
        if not path.exists():
            print("no %s at %s - nothing to compare" % (what, path), file=sys.stderr)
            return 2

    missing = drift(args.profile.read_text(encoding="utf-8"),
                    args.cv.read_text(encoding="utf-8"))

    if args.json:
        print(json.dumps({"missing_from_cv": missing}, ensure_ascii=False, indent=2))
        return 1 if (missing and args.strict) else 0

    if not missing:
        print("No drift: every headline claim in the profile appears in the master CV.")
        return 0
    try:
        shown = args.cv.relative_to(ROOT)
    except ValueError:      # --cv given a path outside the repo
        shown = args.cv
    print("In the profile, not found in %s:\n" % shown)
    for item in missing:
        print("  [%s] %s" % (item["section"], item["claim"]))
    print("\n%d entr%s. Each is either a deliberate omission from a one-page CV or a"
          "\ndeletion the profile never heard about - the second kind keeps turning up"
          "\nin fit evaluations for jobs whose CV will not mention it."
          % (len(missing), "y" if len(missing) == 1 else "ies"))
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
