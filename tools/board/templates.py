"""Which template is active, read from the same file `/apply` reads.

`/add-template` writes an `ACTIVE-TEMPLATE` managed block at the head of
`05-cv-templates.md` and `06-cover-letter-templates.md`. `/apply` resolves
`<CV_EXT>`/`<CV_COMPILE>` from it and is explicitly forbidden from falling back
to lualatex/xelatex when a custom command is declared.

The supervisor has to resolve the same two values, for two different reasons and
at two different times:

* **the extension**, before pass A, because the guard allowlist names the two
  target files by exact path and a `.typ` target allowlisted as `.tex` means the
  run's first real write is refused;
* **the compile command**, in M3, because a supervisor that hardcodes LaTeX
  compiles the right file with the wrong tool - silently, since a missing binary
  and a wrong binary fail differently.

The block is read from disk rather than taken from model output. An argv that
came out of a model is an argv you are about to `exec`.

Stdlib only, Python 3.9+.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_md  # noqa: E402

SKILL = jobs_md.ROOT / ".claude" / "skills" / "job-application-assistant"

GUIDANCE = {
    "cv": SKILL / "05-cv-templates.md",
    "cover": SKILL / "06-cover-letter-templates.md",
}

# What `/apply` Step 5a uses when no managed block is present. Kept here so the
# stock path and the custom path go through one resolver, not two.
STOCK = {
    "cv": {"ext": ".tex", "compile": "lualatex -interaction=nonstopmode", "name": "default"},
    "cover": {"ext": ".tex", "compile": "xelatex -interaction=nonstopmode", "name": "default"},
}

BLOCK = re.compile(r"<!--\s*BEGIN ACTIVE-TEMPLATE.*?-->(.*?)<!--\s*END ACTIVE-TEMPLATE\s*-->",
                   re.DOTALL)
FIELD = re.compile(r"^>\s*-\s*\*\*(?P<label>[^*]+?):\*\*\s*(?P<value>.*)$", re.MULTILINE)
BACKTICKED = re.compile(r"`([^`]*)`")


def _first_backticked(value, fallback=None):
    match = BACKTICKED.search(value or "")
    return match.group(1).strip() if match else fallback


def active(kind):
    """`{name, ext, compile, source}` for `kind` in ('cv', 'cover')."""
    if kind not in GUIDANCE:
        raise ValueError("kind must be 'cv' or 'cover', got %r" % (kind,))
    stock = dict(STOCK[kind], source=None)
    path = GUIDANCE[kind]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return stock
    match = BLOCK.search(text)
    if not match:
        return stock
    body = match.group(1)

    fields = {}
    for field in FIELD.finditer(body):
        fields[field.group("label").strip().lower()] = field.group("value").strip()

    override = re.search(r"Active template override:\s*`([^`]+)`", body)
    name = override.group(1).strip() if override else "custom"
    ext = _first_backticked(fields.get("source extension", ""))
    command = _first_backticked(fields.get("compile command", ""))

    # A block that exists but does not declare an extension is a broken
    # registration, not a licence to guess: `/apply` would resolve `<CV_EXT>` to
    # nothing either. Report what was found and let the caller refuse.
    return {
        "name": name or "custom",
        "ext": ext or None,
        "compile": command or None,
        "source": str(path.relative_to(jobs_md.ROOT)),
    }


def resolve_extensions():
    """`(cv_ext, cover_ext, problems)` - problems is empty when both resolved."""
    problems = []
    exts = {}
    for kind in ("cv", "cover"):
        spec = active(kind)
        if spec["ext"] is None:
            problems.append(
                "%s declares an ACTIVE-TEMPLATE block without a `Source extension:` "
                "line - fix it with /add-template before starting a run" % spec["source"])
            exts[kind] = STOCK[kind]["ext"]
        else:
            exts[kind] = spec["ext"]
    return exts["cv"], exts["cover"], problems
