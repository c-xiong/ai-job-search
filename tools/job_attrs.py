#!/usr/bin/env python3
"""Structured job attributes, read from a row's title, location and posting text.

    python3 tools/job_attrs.py --backfill --dry-run   # what the high/medium rows would get
    python3 tools/job_attrs.py --backfill             # write them, then rescore those rows
    python3 tools/job_attrs.py --explain <url>        # one row's detected attributes

Deterministic and free: keywords only, no model, no token budget. A field the
text does not settle stays "unknown" rather than being guessed; the board lets
you set any field by hand, and a hand-set value always wins over detection
(`attrs_manual` is never rewritten by this module).

Fields (all on `entry["attrs"]`, overrides on `entry["attrs_manual"]`):

    job_type      list of "full_time" | "intern" | "working_student"; German
                  postings often offer intern and working student in one ad,
                  which is simply both values
    work_mode     "onsite" | "hybrid" | "remote" | "unknown"
    remote_scope  for remote rows: "local" (restricted to CH/DE/AT) |
                  "region" (Europe/EMEA) | "anywhere" | "unknown"
    country       "CH" | "DE" | "AT" | "EU" | "other" | "unknown"
    language      "en" | "de_plus" | "de_required" | "de_posting" | "unknown"
    visa          "offered" | "none" | "permit_required" | "citizenship" | "unknown"
    timing        a short quote of the stated duration, start or weekly hours
                  for an intern/working-student row, else ""

`fit_score.py` reads the effective attributes (`effective()`) to apply its
remote and German-working-student deductions; nothing here changes a band.

Stdlib only, Python 3.9+.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collectors  # noqa: E402
import jobs_md  # noqa: E402
import postings  # noqa: E402

ATTRS_VERSION = 1

JOB_TYPES = ("full_time", "intern", "working_student")
CHOICES = {
    "work_mode": ("onsite", "hybrid", "remote", "unknown"),
    "remote_scope": ("local", "region", "anywhere", "unknown"),
    "country": ("CH", "DE", "AT", "EU", "other", "unknown"),
    "language": ("en", "de_plus", "de_required", "de_posting", "unknown"),
    "visa": ("offered", "none", "permit_required", "citizenship", "unknown"),
}

# Bands the backfill touches: the user reads high and medium rows; low rows get
# their attributes when a collect next rescores them.
BACKFILL_BANDS = ("high", "medium")


def _rx(*alternatives):
    return re.compile(r"(?i)(?<![\w-])(?:%s)(?![\w-])" % "|".join(alternatives))


# --------------------------------------------------------------------------
# Job type

_WORKING_STUDENT = _rx(r"working[ -]students?", r"werkstudent\w*", r"werkstudierende\w*",
                       r"studentische[rn]? (?:hilfskraft|mitarbeit\w*|aushilfe)",
                       r"student assistant", r"student job", r"part[- ]time student")
_INTERN = _rx(r"intern", r"interns", r"internships?", r"praktik\w*", r"pflichtpraktik\w*",
              r"stagiaire", r"summer student", r"co-?op")
# Body phrases strong enough to type a row whose title says nothing.
_BODY_WORKING_STUDENT = _rx(r"as a working student", r"working student position",
                            r"werkstudententätigkeit", r"als werkstudent\w*",
                            r"werkstudentenstelle")
_BODY_INTERN = _rx(r"this internship", r"the internship", r"internship position",
                   r"duration of the internship", r"als praktikant\w*", r"das praktikum",
                   r"your internship")


def job_type(title, body):
    """The list of types; full-time when nothing student-shaped is stated."""
    found = []
    if _INTERN.search(title):
        found.append("intern")
    if _WORKING_STUDENT.search(title):
        found.append("working_student")
    if not found and body:
        if _BODY_INTERN.search(body):
            found.append("intern")
        if _BODY_WORKING_STUDENT.search(body):
            found.append("working_student")
    return [t for t in JOB_TYPES if t in found] or ["full_time"]


# --------------------------------------------------------------------------
# Country

_COUNTRY_WORDS = (
    # Germany first: "Baden-Württemberg" and "Baden-Baden" are not Baden AG.
    ("DE", ("germany", "deutschland", "baden-württemberg", "baden-wuerttemberg",
            "baden-baden", "bavaria", "bayern", "berlin", "munich", "münchen", "muenchen",
            "hamburg", "frankfurt", "stuttgart", "cologne", "köln", "koeln", "düsseldorf",
            "dusseldorf", "duesseldorf", "leipzig", "dresden", "nuremberg", "nürnberg",
            "nuernberg", "hannover", "hanover", "bremen", "essen", "dortmund", "bochum",
            "duisburg", "karlsruhe", "mannheim", "heidelberg", "bonn", "münster", "muenster",
            "augsburg", "aachen", "freiburg", "darmstadt", "wiesbaden", "mainz", "potsdam",
            "ulm", "regensburg", "ingolstadt", "erlangen", "wolfsburg", "braunschweig",
            "kiel", "lübeck", "rostock", "jena", "göttingen", "tübingen", "tuebingen",
            "konstanz", "metzingen", "leonberg", "böblingen", "sindelfingen", "walldorf",
            "garching", "heilbronn", "kassel", "saarbrücken", "hesse", "hessen",
            "north rhine-westphalia", "nordrhein-westfalen", "saxony", "sachsen")),
    ("AT", ("austria", "österreich", "oesterreich", "vienna", "wien", "graz", "linz",
            "salzburg", "innsbruck", "klagenfurt")),
    ("CH", ("switzerland", "schweiz", "suisse", "svizzera", "zurich", "zürich", "zuerich",
            "zug", "baden", "winterthur", "lucerne", "luzern", "basel", "bern", "berne",
            "geneva", "genève", "geneve", "genf", "lausanne", "st. gallen", "st gallen",
            "sankt gallen", "lugano", "neuchâtel", "neuchatel", "fribourg", "sion", "chur",
            "biel", "bienne", "thun", "solothurn", "olten", "aarau", "schaffhausen",
            "kreuzlingen", "thurgau", "aargau", "vaud", "ticino", "valais", "villigen",
            "rotkreuz", "schlieren", "dübendorf", "duebendorf", "wallisellen", "kloten",
            "opfikon", "glattbrugg", "baar", "cham", "pfäffikon", "rapperswil", "zh")),
    ("EU", ("netherlands", "amsterdam", "rotterdam", "utrecht", "eindhoven", "the hague",
            "randstad", "france", "paris", "lyon", "toulouse", "grenoble", "spain", "madrid",
            "barcelona", "valencia", "portugal", "lisbon", "porto", "italy", "milan", "milano",
            "rome", "turin", "torino", "belgium", "brussels", "antwerp", "ghent",
            "luxembourg", "ireland", "dublin", "cork", "denmark", "copenhagen", "aarhus",
            "sweden", "stockholm", "gothenburg", "malmö", "finland", "helsinki", "espoo",
            "poland", "warsaw", "krakow", "kraków", "wroclaw", "wrocław", "gdansk",
            "czech", "czechia", "prague", "brno", "slovakia", "bratislava", "hungary",
            "budapest", "romania", "bucharest", "cluj", "bulgaria", "sofia", "greece",
            "athens", "croatia", "zagreb", "slovenia", "ljubljana", "estonia", "tallinn",
            "latvia", "riga", "lithuania", "vilnius", "cyprus", "malta")),
    ("other", ("united kingdom", "uk", "england", "scotland", "london", "manchester",
               "edinburgh", "cambridge, uk", "oxford", "united states", "usa",
               "new york", "nyc", "san francisco", "seattle", "boston", "austin", "california",
               "canada", "toronto", "vancouver", "montreal", "norway", "oslo", "serbia",
               "belgrade", "ukraine", "kyiv", "turkey", "istanbul", "israel", "tel aviv",
               "india", "bangalore", "bengaluru", "singapore", "china", "shanghai", "beijing",
               "hong kong", "japan", "tokyo", "australia", "sydney", "dubai", "brazil")),
)
_COUNTRY_RE = [(code, _rx(*(re.escape(w) for w in words))) for code, words in _COUNTRY_WORDS]
# A bare ISO code after a comma, as ATS feeds write them ("Geneva, GENEVA, ch").
_ISO_TAIL = re.compile(r"(?i),\s*(ch|de|at)\s*$")


def country(location, title=""):
    """CH/DE/AT/EU/other from the location, then the title; unknown if neither says."""
    for text in (location or "", title or ""):
        tail = _ISO_TAIL.search(text)
        if tail:
            return tail.group(1).upper()
        for code, rx in _COUNTRY_RE:
            if rx.search(text):
                return code
    return "unknown"


# --------------------------------------------------------------------------
# Work mode and remote scope

_LOC_REMOTE = _rx(r"remote", r"home ?office", r"work from home", r"anywhere", r"homeoffice")
_LOC_HYBRID = _rx(r"hybrid")
_LOC_ONSITE = _rx(r"on-?site", r"in[- ]office")
_FULL_REMOTE = _rx(r"fully remote", r"full(?:y)?[- ]remote", r"100\s?% remote", r"remote[- ]first",
                   r"remote[- ]only", r"work from anywhere", r"remote position", r"remote role",
                   r"this is a remote", r"100\s?% (?:home ?office|homeoffice)",
                   r"vollständig remote", r"komplett remote",
                   r"(?:role|position|job) is (?:fully )?remote", r"join our remote team",
                   r"or remotely (?:in|from)")
_HYBRID = _rx(r"hybrid", r"\d (?:days?|tage) (?:a|per|pro) (?:week|woche) (?:in|im) (?:the )?office",
              r"(?:days?|tage) (?:in|im) (?:the |our )?office", r"mix of remote and office",
              r"partly remote", r"partial(?:ly)? remote", r"occasional(?:ly)? remote",
              r"teilweise (?:remote|homeoffice|mobil)", r"remote work (?:options?|possible|days?)",
              r"home ?office (?:options?|days?|possible|möglich|regelung)", r"mobiles? arbeiten",
              r"mobile work(?:ing)?", r"flexible (?:remote|home ?office|working (?:model|from home))",
              r"work[- ]from[- ]home (?:days?|options?|when needed)", r"option to work from home",
              r"\d+ days? (?:remote|from home|wfh)", r"remote[- ]friendly",
              r"days? (?:a|per) week (?:on-?site|in (?:the |our )?office)")
_ONSITE = _rx(r"on-?site role", r"fully on-?site", r"on-?site position", r"in[- ]office role",
              r"5 days (?:a|per) week in (?:the|our) office", r"office-first", r"office first",
              r"vor ort", r"work on-?site", r"on-?site in [A-Z]\w+", r"location:? on-?site")
_SCOPE_ANYWHERE = _rx(r"anywhere", r"worldwide", r"globally", r"any (?:country|location)")
_SCOPE_REGION = _rx(r"europe", r"european", r"emea", r"eu", r"cet", r"cest",
                    r"european time ?zones?", r"dach")


# A "remote" phrase followed by a time limit is a perk, not the work mode:
# "work from anywhere (6 weeks/year)", "100% remote during pregnancy".
_PERK_TAIL = re.compile(r"(?i)^.{0,60}?\b(?:weeks?|months?|days?)\b.{0,20}?\b(?:a|per|each|/)\s*year\b"
                        r"|^.{0,40}?\b(?:during|option during|for up to)\b")
_SCOPE_LOCAL = _rx(r"(?:in|from|within|across) (?:germany|switzerland|austria|deutschland|der schweiz)",
                   r"based in (?:germany|switzerland|austria)",
                   r"(?:germany|switzerland|austria)[- ]based")


def _full_remote(body):
    """The first fully-remote statement that is not a perk, or None."""
    for match in _FULL_REMOTE.finditer(body):
        if not _PERK_TAIL.search(body[match.end():match.end() + 80]):
            return match
    return None


_ANYWHERE_LOCAL = _rx(r"anywhere (?:in|within|across) (?:germany|switzerland|austria|deutschland|der schweiz)")


def _scope(text):
    if _ANYWHERE_LOCAL.search(text):
        return "local"
    if _SCOPE_ANYWHERE.search(text) or re.search(r"(?i)eligible countries", text):
        return "anywhere"
    if _SCOPE_REGION.search(text):
        return "region"
    if _SCOPE_LOCAL.search(text):
        return "local"
    return None


def work_mode(title, location, body):
    """(mode, remote_scope). Location and title tags beat the body's wording."""
    head = "%s %s" % (title or "", location or "")
    body = body or ""
    window = head
    if _LOC_HYBRID.search(head):
        mode = "hybrid"
    elif _LOC_ONSITE.search(head):
        mode = "onsite"
    elif _LOC_REMOTE.search(head):
        mode = "remote"
    else:
        match = _full_remote(body) if not _HYBRID.search(body) else None
        if match:
            mode = "remote"
            window = body[max(0, match.start() - 150):match.end() + 150]
        elif _HYBRID.search(body):
            mode = "hybrid"
        elif _ONSITE.search(body):
            mode = "onsite"
        else:
            mode = "unknown"
    if mode != "remote":
        return mode, ""
    scope = _scope(window)
    if scope:
        return mode, scope
    # Remote with no stated scope but a DACH location: hiring is normally
    # restricted to that country.
    if country(location) in ("CH", "DE", "AT"):
        return mode, "local"
    return mode, "unknown"


# --------------------------------------------------------------------------
# Language and visa

def language(title, body, posting_language):
    """From the German screen (collectors) and the posting's own language."""
    if not (body or "").strip():
        return "unknown"
    verdict, _ = collectors.german_hit(body)
    # Stated outright, "German is a plus" outranks what the posting is written in.
    if posting_language == "de" and collectors.german_optional(body):
        return "de_plus"
    if verdict == "hard":
        return "de_required"
    if posting_language == "de":
        return "de_posting"
    if verdict == "soft":
        return "de_plus"
    return "en"


_VISA_CITIZEN = _rx(r"(?:eu|eea|efta|swiss|german|us|u\.s\.) (?:citizens?|citizenship|nationals?|nationality)",
                    r"citizenship (?:is )?required", r"security clearance", r"sicherheitsüberprüfung",
                    r"must be a citizen")
_VISA_NONE = _rx(r"(?:no|not|cannot|can ?not|unable to|do not|don't|does not|doesn't|will not|won't)"
                 r" (?:offer |provide |be able to )?(?:visa )?sponsor(?:ship)?\w*",
                 r"without (?:visa )?sponsorship", r"sponsorship (?:is )?not (?:available|possible|offered)",
                 r"keine (?:visa|arbeitserlaubnis)\w*")
_VISA_PERMIT = _rx(r"(?:valid|existing|current) (?:swiss |eu |german )?(?:work|working) (?:permit|authori[sz]ation)",
                   r"(?:right|eligib\w+|authori[sz]ed) to work in (?:switzerland|germany|the eu|austria|europe)",
                   r"gültige arbeitserlaubnis", r"arbeitsbewilligung")
_VISA_OFFERED = _rx(r"visa sponsorship (?:is )?(?:available|provided|offered|possible)",
                    r"(?:we|will|can) sponsor", r"(?:offer|provide) visa", r"visa support",
                    r"relocation (?:and|&) visa", r"sponsor (?:your |a )?(?:work )?visa")


def visa(body):
    """Only explicit wording counts; silence is "unknown", never a no."""
    if not (body or "").strip():
        return "unknown"
    for value, rx in (("citizenship", _VISA_CITIZEN), ("none", _VISA_NONE),
                      ("offered", _VISA_OFFERED), ("permit_required", _VISA_PERMIT)):
        if rx.search(body):
            return value
    return "unknown"


# --------------------------------------------------------------------------
# Timing (intern / working student)

_MONTHS = (r"jan(?:uary|uar)?|feb(?:ruary|ruar)?|m(?:ar(?:ch)?|ärz|aerz)|apr(?:il)?|ma[iy]|"
           r"jun[ei]?|jul[iy]?|aug(?:ust)?|sep(?:t(?:ember)?)?|o[ck]t(?:ober)?|nov(?:ember)?|"
           r"de[cz](?:ember)?")
_TIMING = (
    re.compile(r"(?i)\b\d{1,2}\s*(?:[-–]|to|bis)\s*\d{1,2}\s*(?:months|monate[n]?)\b"),
    re.compile(r"(?i)\b\d{1,2}\s*(?:months|monate[n]?)\b"),
    re.compile(r"(?i)\b(?:start(?:ing)?(?: date)?|beginn|ab|from|as of)\s*:?\s*(?:%s)\.?\s*\d{4}\b" % _MONTHS),
    re.compile(r"(?i)\b(?:%s)\.?\s*(?:20\d\d)\b" % _MONTHS),
    re.compile(r"(?i)\b\d{1,2}\s*(?:[-–]|to|bis)\s*\d{1,2}\s*(?:h|hrs|hours|stunden|std\.?)\s*(?:/|per|a|pro|in der)\s*(?:week|woche)\b"),
    re.compile(r"(?i)\b(?:as soon as possible|asap|ab sofort|immediately)\b"),
)


def timing(body, types):
    """Stated duration / start / hours, joined; empty for a full-time row."""
    if not body or not ({"intern", "working_student"} & set(types)):
        return ""
    found = []
    for rx in _TIMING:
        match = rx.search(body)
        if match:
            text = re.sub(r"\s+", " ", match.group(0)).strip()
            if not any(text.lower() in seen.lower() for seen in found):
                found.append(text)
    return " · ".join(found[:3])


# --------------------------------------------------------------------------
# Entry-level API


def detect(title, location, body, posting_language=None):
    """Every attribute for one posting."""
    title, location, body = title or "", location or "", body or ""
    if posting_language is None:
        import fit_score  # local: fit_score imports this module
        posting_language = fit_score.posting_language(title, body)[0]
    types = job_type(title, body)
    mode, scope = work_mode(title, location, body)
    return {
        "job_type": types,
        "work_mode": mode,
        "remote_scope": scope,
        "country": country(location, title),
        "language": language(title, body, posting_language),
        "visa": visa(body),
        "timing": timing(body, types),
    }


def effective(entry):
    """Detected attributes with the hand-set ones laid over them, or None."""
    auto = entry.get("attrs")
    manual = entry.get("attrs_manual") or {}
    if not isinstance(auto, dict) and not manual:
        return None
    merged = dict(auto or {})
    merged.update(manual)
    if merged.get("work_mode") != "remote":
        merged["remote_scope"] = ""
    elif not merged.get("remote_scope"):
        merged["remote_scope"] = "unknown"
    return merged


def apply(entry, body=None, posting_language=None):
    """Write `attrs` onto an entry from its stored body (or the one passed)."""
    if body is None:
        try:
            body = postings.load(entry)
        except postings.UnsafePath:
            body = ""
    entry["attrs"] = detect(entry.get("title", ""), entry.get("location", ""), body,
                            posting_language)
    entry["attrs_version"] = ATTRS_VERSION
    return entry


def clean_manual(raw):
    """Validate a hand-set override from the board. Returns (dict, error)."""
    if not isinstance(raw, dict):
        return None, "attrs must be an object"
    out = {}
    for key, value in raw.items():
        if key == "job_type":
            if (not isinstance(value, list) or not value
                    or any(v not in JOB_TYPES for v in value)):
                return None, "job_type must be a non-empty list of %s" % ", ".join(JOB_TYPES)
            out[key] = [t for t in JOB_TYPES if t in value]
        elif key in CHOICES:
            if value not in CHOICES[key]:
                return None, "%s must be one of %s" % (key, ", ".join(CHOICES[key]))
            out[key] = value
        else:
            return None, "unknown attribute %r" % key
    return out, None


# --------------------------------------------------------------------------
# CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backfill", action="store_true",
                        help="detect attributes for the high/medium rows and rescore them")
    parser.add_argument("--all", action="store_true", help="with --backfill, every band")
    parser.add_argument("--explain", metavar="URL", help="show one row's detected attributes")
    parser.add_argument("--dry-run", action="store_true", help="compute and report, write nothing")
    args = parser.parse_args(argv)

    import fit_score  # local: fit_score imports this module

    with jobs_md.board_lock():
        seen = {}
        if jobs_md.SEEN.exists():
            seen = json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})

        if args.explain:
            entry = seen.get(jobs_md.canonical_url(args.explain))
            if entry is None:
                print("no row for %s" % args.explain, file=sys.stderr)
                return 1
            body = postings.load(entry)
            print(json.dumps({"title": entry.get("title"), "location": entry.get("location"),
                              "detected": detect(entry.get("title"), entry.get("location"), body),
                              "manual": entry.get("attrs_manual") or {}},
                             indent=2, ensure_ascii=False))
            return 0

        if not args.backfill:
            parser.print_help()
            return 0

        try:
            ctx = fit_score.Context()
        except fit_score.ConfigError as exc:
            print("job_attrs: %s" % exc, file=sys.stderr)
            return 2

        bands = None if args.all else BACKFILL_BANDS
        counts, moved, touched = {}, {}, 0
        for entry in seen.values():
            if bands and (entry.get("fit") or "").lower() not in bands:
                continue
            before = entry.get("fit")
            fit_score.apply_to(entry, ctx, detect_attrs=True)
            touched += 1
            attrs = effective(entry) or {}
            for key in ("work_mode", "country"):
                counts.setdefault(key, {}).setdefault(attrs.get(key), 0)
                counts[key][attrs.get(key)] += 1
            label = "+".join(attrs.get("job_type") or [])
            counts.setdefault("job_type", {}).setdefault(label, 0)
            counts["job_type"][label] += 1
            if entry.get("fit") != before:
                change = "%s->%s" % (before, entry.get("fit"))
                moved[change] = moved.get(change, 0) + 1

        print("rows given attributes: %d" % touched)
        for key, values in counts.items():
            print("  %-9s %s" % (key, "  ".join("%s=%d" % kv for kv in
                                               sorted(values.items(), key=lambda kv: -kv[1]))))
        print("band changes: %s" % ("  ".join("%s=%d" % kv for kv in sorted(moved.items()))
                                    or "none"))
        if args.dry_run:
            print("dry run - nothing written")
            return 0
        jobs_md.save_seen(seen)
        print("wrote seen_jobs.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
