#!/usr/bin/env python3
"""Intake filters: postings that are plainly not for you are gated, not scored.

    python3 tools/job_filters.py --apply-existing --dry-run   # what the board would gate
    python3 tools/job_filters.py --apply-existing             # gate them (new/backlog rows only)
    python3 tools/job_filters.py --explain "Staff Software Engineer"

Two layers, both deterministic (keywords only, no model, no token budget):

  title   Runs on the title alone, *before* a LinkedIn `detail` request is
          spent, so a Senior posting costs a search slot at most. Grouped
          (seniority, founding, academic, non-engineering, ...); each group is
          switched on or off in `scrape_config.json`'s `filters` block, and an
          `allow` list of words always lets a title through.
  body    Runs after the posting text is read: an explicitly required PhD, or
          citizenship / security clearance stated as a condition. Only explicit
          wording counts - an unclear permit line never gates.

A filtered posting is not deleted. It lands on the board as `gate` with an
`AUTO-SCREEN:` note naming the rule, so deduplication keeps it from being
fetched again and a wrong call is one click to undo.

Stdlib only, Python 3.9+.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jobs_md  # noqa: E402

CONFIG = jobs_md.ROOT / "job_scraper" / "scrape_config.json"


def _rx(*alternatives):
    return re.compile(r"(?i)(?<![\w-])(?:%s)(?![\w-])" % "|".join(alternatives))


# (key, label, default on, title pattern)
TITLE_GROUPS = (
    ("seniority", "Senior and above", True,
     _rx(r"senior", r"sr\.?", r"(?<!technical )staff", r"principal", r"lead", r"leads", r"head of", r"director",
         r"vp", r"vice president", r"chief", r"cto", r"manager", r"architect", r"distinguished",
         r"teamleiter\w*", r"leiter\w*", r"leitung",
         r"(?:engineer|developer|scientist)\s+(?:iv|v|vi|4|5|6)")),
    ("founding", "Founding roles", True,
     _rx(r"founding", r"founder", r"co-?founder")),
    ("academic", "PhD, postdoc and thesis", True,
     _rx(r"ph\.?d\.?", r"postdoc\w*", r"post-doc\w*", r"doctoral", r"doktorand\w*", r"professor\w*",
         r"lecturer", r"faculty", r"thesis", r"masterarbeit", r"bachelorarbeit", r"abschlussarbeit",
         r"master'?s thesis")),
    ("non_engineering", "Sales, marketing, recruiting, analyst", True,
     _rx(r"sales (?:manager|representative|executive|development\w*|associate)", r"account executive",
         r"account manager", r"marketing (?:manager|specialist|associate|lead|coordinator)",
         r"recruiter", r"recruiting (?:manager|partner|coordinator)", r"talent acquisition",
         r"customer success", r"(?:business|data|financial|bi|operations|reporting) analyst",
         r"business intelligence", r"bi developer")),
    ("qa_test", "QA and test", True,
     _rx(r"qa", r"quality assurance", r"test", r"tester", r"testing", r"sdet")),
    ("embedded", "Embedded, firmware, hardware", True,
     _rx(r"embedded", r"firmware", r"hardware", r"fpga", r"asic", r"electronics?", r"pcb")),
    ("mobile", "iOS and Android", True,
     _rx(r"ios", r"android", r"mobile (?:developer|engineer|app\w*)")),
    ("enterprise_apps", "SAP, Salesforce, ServiceNow", True,
     _rx(r"sap", r"salesforce", r"servicenow", r"dynamics 365")),
    ("security", "Security", True,
     _rx(r"security (?:engineer\w*|analyst|specialist|consultant|architect|researcher|officer)",
         r"(?:application|cloud|network|product|infrastructure|offensive|it|information) security",
         r"appsec", r"cyber ?security (?:engineer\w*|analyst|specialist)", r"penetration", r"pentest\w*",
         r"soc analyst", r"detection (?:and|&) response", r"detection engineer\w*",
         r"security (?:&|and) compliance")),
    ("devops", "DevOps and SRE", False,
     _rx(r"devops", r"sre", r"site reliability", r"devsecops")),
)
GROUP_KEYS = tuple(key for key, _, _, _ in TITLE_GROUPS)

# Body conditions. Deliberately narrow: "PhD or MSc" and "EU citizens or a
# valid permit" must not match.
BODY_RULES = (
    ("phd_required", "PhD required",
     _rx(r"ph\.?d\.? (?:degree )?(?:is )?(?:required|mandatory|a must)",
         r"must (?:have|hold) a ph\.?d",
         r"(?:currently )?(?:enrolled in|pursuing) a ph\.?d(?!\.?\s*(?:\w+\s+){0,2}(?:or|/|,)\s*(?:a\s+)?(?:recently\s+\w+\s+a\s+)?(?:m\.?sc?|master))",
         r"ph\.?d\.? (?:students?|candidates?) only",
         r"open to ph\.?d\.? (?:students?|candidates?) only")),
    ("citizenship", "Citizenship or clearance required",
     _rx(r"must be an? (?:eu|eea|swiss|german|austrian|us|u\.s\.|uk|british) (?:citizen|national)",
         r"(?:eu|eea|swiss|german|austrian|us|u\.s\.|uk|british) citizenship (?:is )?(?:required|mandatory)",
         r"(?:only|exclusively) (?:open to )?(?:eu|eea|swiss|german|us) (?:citizens|nationals)",
         r"(?:active |valid |current )?security clearance (?:is )?(?:required|mandatory)",
         r"(?:must|able to) (?:obtain|hold|pass) (?:a |an )?(?:security clearance|sicherheitsüberprüfung)",
         r"sicherheitsüberprüfung (?:ist )?(?:erforderlich|voraussetzung)",
         r"staatsangehörigkeit (?:ist )?(?:erforderlich|voraussetzung)")),
    # Pay-to-intern programmes (Praktix and the like) sold as internships: the
    # candidate pays the fee. "Relocation fees covered" must not match.
    ("candidate_pays", "You pay a programme fee",
     _rx(r"(?<!no )(?<!without )program(?:me)? fees?",
         r"(?<!no )(?:participation|tuition|enrol?ment|registration|course) fees?",
         r"teilnahmegebühr\w*", r"programmgebühr\w*",
         r"(?:internship|program(?:me)?)\s*[:\-–]\s*(?:€|eur|chf|usd|\$|£)\s?\d")),
)


def settings(cfg=None):
    """The effective filter settings: code defaults under the config's block."""
    block = (cfg or {}).get("filters") if isinstance(cfg, dict) else None
    block = block if isinstance(block, dict) else {}
    groups = {key: on for key, _, on, _ in TITLE_GROUPS}
    for key, value in (block.get("groups") or {}).items():
        if key in groups and isinstance(value, bool):
            groups[key] = value
    allow = [str(w).strip() for w in block.get("allow") or [] if str(w).strip()]
    body = {key: True for key, _, _ in BODY_RULES}
    for key, value in (block.get("body") or {}).items():
        if key in body and isinstance(value, bool):
            body[key] = value
    # Rules you accepted from a reported miss or from "Learn from notes"
    # (tools/board/learn.py): literal, case-insensitive.
    custom_title = [r for r in block.get("custom_title") or []
                    if isinstance(r, dict) and str(r.get("word", "")).strip()]
    custom_body = [r for r in block.get("custom_body") or []
                   if isinstance(r, dict) and str(r.get("phrase", "")).strip()]
    return {"groups": groups, "allow": allow, "body": body,
            "custom_title": custom_title, "custom_body": custom_body,
            "excluded_locations": ((cfg or {}).get("search_preferences") or {}).get("excluded_locations", [])}


def _literal(text):
    """A literal phrase as a regex: case-insensitive, any run of whitespace."""
    words = str(text).split()
    return re.compile(r"(?i)(?<![\w-])%s(?![\w-])" % r"\s+".join(re.escape(w) for w in words))


def location_hit(location, opts):
    """Match explicit excluded place names in listing metadata, never description text."""
    for place in opts.get("excluded_locations") or []:
        if str(place).strip() and _literal(place).search(location or ""):
            return "location", "excluded location", str(place)
    return None


def title_hit(title, opts):
    """(group key, label, matched word) for the first enabled rule, else None."""
    title = title or ""
    if any(re.search(r"(?i)(?<![\w-])%s(?![\w-])" % re.escape(word), title)
           for word in opts["allow"]):
        return None
    for key, label, _, rx in TITLE_GROUPS:
        if not opts["groups"].get(key):
            continue
        match = rx.search(title)
        if match:
            return key, label, match.group(0)
    for rule in opts.get("custom_title") or ():
        match = _literal(rule["word"]).search(title)
        if match:
            return "custom", rule.get("label") or "your rule", match.group(0)
    return None


def body_hit(text, opts):
    """(rule key, label, quote) for the first enabled body rule, else None."""
    if not text:
        return None
    for key, label, rx in BODY_RULES:
        if not opts["body"].get(key):
            continue
        match = rx.search(text)
        if match:
            return key, label, _quote(text, match)
    for rule in opts.get("custom_body") or ():
        match = _literal(rule["phrase"]).search(text)
        if match:
            return "custom", rule.get("label") or "your rule", _quote(text, match)
    return None


def _quote(text, match):
    start, end = max(0, match.start() - 80), min(len(text), match.end() + 80)
    return re.sub(r"\s+", " ", text[start:end]).strip()[:240]


def title_note(hit):
    return "AUTO-SCREEN: filtered - title says '%s' (%s)" % (hit[2], hit[1])


def body_note(hit):
    return "AUTO-SCREEN: %s - '%s'" % (hit[1], hit[2])


_CACHE = {"key": None, "value": None}


def load_settings():
    """Settings from scrape_config.json, re-read only when the file changes."""
    try:
        stat = CONFIG.stat()
        key = (str(CONFIG), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(CONFIG), None, None)
    if _CACHE["key"] != key:
        try:
            value = settings(json.loads(CONFIG.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            value = settings({})
        _CACHE.update(key=key, value=value)
    return _CACHE["value"]


# --------------------------------------------------------------------------
# Applying to rows already on the board

# Statuses the filter may change: the automatic ones. yes / applied / no /
# expired - and any gate - are your decisions or already settled.
OPEN_STATUSES = ("new", "backlog")


def apply_existing(seen, opts, load_body=None):
    """Gate open rows that a rule now matches. Returns [(url, entry, note)]."""
    changed = []
    for url, entry in seen.items():
        if jobs_md.user_status(entry) not in OPEN_STATUSES:
            continue
        hit = title_hit(entry.get("title", ""), opts)
        note = title_note(hit) if hit else None
        if note is None and load_body is not None:
            hit = body_hit(load_body(entry), opts)
            note = body_note(hit) if hit else None
        if note is None:
            continue
        entry["user_status"] = "gate"
        # Never bury a note you wrote: the filter's reason goes in `note`, the
        # automatic field, and `user_note` is left alone.
        entry["note"] = note
        changed.append((url, entry, note))
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply-existing", action="store_true",
                        help="gate new/backlog rows on the board that a rule matches")
    parser.add_argument("--explain", metavar="TITLE", help="which rule a title hits")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args(argv)
    opts = load_settings()

    if args.explain is not None:
        hit = title_hit(args.explain, opts)
        print(title_note(hit) if hit else "passes")
        return 0
    if not args.apply_existing:
        parser.print_help()
        return 0

    import postings  # noqa: E402 - only the board pass needs bodies

    def body(entry):
        try:
            return postings.load(entry)
        except postings.UnsafePath:
            return ""

    with jobs_md.board_lock():
        seen = json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})
        changed = apply_existing(seen, opts, load_body=body)
        counts = {}
        for _, entry, note in changed:
            label = note.split("(")[-1].rstrip(")") if "filtered" in note else note.split(" - ")[0][13:]
            counts[label] = counts.get(label, 0) + 1
        fits = {}
        for _, entry, _ in changed:
            fits[entry.get("fit") or "unranked"] = fits.get(entry.get("fit") or "unranked", 0) + 1
        print("rows gated: %d" % len(changed))
        for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print("  %-40s %d" % (label, n))
        print("by band: %s" % "  ".join("%s=%d" % kv for kv in sorted(fits.items())))
        if args.dry_run:
            for _, entry, note in changed[:0]:
                print(entry.get("title"), note)
            print("dry run - nothing written")
            return 0
        jobs_md.save_seen(seen)
        print("wrote seen_jobs.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
