#!/usr/bin/env python3
"""The deterministic fit score: a reading-order prior, computed in code.

    python3 tools/fit_score.py --recompute --dry-run   # histogram, write nothing
    python3 tools/fit_score.py --recompute             # rescore every row
    python3 tools/fit_score.py --explain <url>         # why one row scored that

The board's Fit column has always been empty because nothing ever filled it:
every collector hardcodes `"fit": ""`, and `/rank` writes `rank_score` without
touching the band. This module fills it for every row from every source,
without an LLM and without a token budget, so the board can be read top-down.

**What this is.** A triage prior, in the same sense as `score.ts`'s `prefit`: it
decides reading order, it never drops a posting, and a number never appears
without the reasons that produced it. `/rank` remains the authority and
overwrites the band when it runs.

**What it is not.** A capability assessment. Two of its five components - the
graded role family and the company affinity you set by hand - are pure
preference, and together they outweigh skills and location. That is deliberate:
how much you want a job is a real input to which job you read first.

Two rules shape the weights (both spelled out in fit_profile.example.json):

  P1  A missing skill is not a missing qualification. The skills component only
      ever adds, and saturates fast. Only an explicitly excluded stack counts
      against a posting, and it gates rather than deducting.
  P2  Preference is a first-class signal.

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
import posting_text  # noqa: E402
import postings  # noqa: E402
from board import fetch_url  # noqa: E402

ROOT = jobs_md.ROOT
PROFILE = ROOT / "job_scraper" / "fit_profile.json"
PROFILE_EXAMPLE = ROOT / "job_scraper" / "fit_profile.example.json"
AFFINITY = ROOT / "job_scraper" / "company_affinity.json"
AFFINITY_EXAMPLE = ROOT / "job_scraper" / "company_affinity.example.json"
REGISTRY = ROOT / "job_scraper" / "companies.json"

# Bumped whenever the formula changes, so `--recompute --stale` can find the
# rows that were scored under the old one. It is not a schema version: the
# fields are additive and readers tolerate their absence as always.
FIT_VERSION = 5   # 2: posting language, German, gaps; 3: gaps inform only, experience gate; 4: fit_losses; 5: optional company, fallback/compound roles, founding, synonyms

BANDS = ("high", "medium", "low")
# The ceiling each band puts on a row's sort priority. Owned by jobs_md, which
# does the sorting - stated in one place so the score written here and the
# order the board renders can never disagree.
BAND_CEILING = jobs_md.BAND_CEILING

MAX_REASONS = 4
# A shortfall smaller than this is rounding (Zug vs Zurich), not a reason.
MIN_LOSS = 2


class ConfigError(ValueError):
    """A profile or affinity file that cannot be scored with."""


# --------------------------------------------------------------------------
# Matching primitives


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _term_re(term):
    """Word-boundary-safe match, the same shape score.ts:41 already proved.

    `\\b` is wrong here: half these terms end in punctuation (`next.js`, `c#`,
    `sr.`) where `\\b` behaves in ways nobody remembers correctly. Bracketing on
    "not alphanumeric" keeps `ml` out of `html` and still matches `c#`.
    """
    return re.compile(
        r"(?:^|[^a-z0-9])" + re.escape(term.lower()) + r"(?:[^a-z0-9]|$)", re.I)


_TERM_CACHE = {}


def matches_term(text, term):
    if not text or not term:
        return False
    pattern = _TERM_CACHE.get(term)
    if pattern is None:
        pattern = _TERM_CACHE[term] = _term_re(term)
    return bool(pattern.search(text))


def first_match(text, terms):
    for term in terms or ():
        if matches_term(text, term):
            return term
    return None


def all_matches(text, terms):
    return [term for term in terms or () if matches_term(text, term)]


# --------------------------------------------------------------------------
# Configuration


def _strip_comments(node):
    """Drop the `_comment` / `_readme` keys the config files document themselves with."""
    if isinstance(node, dict):
        return {k: _strip_comments(v) for k, v in node.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(node, list):
        return [_strip_comments(v) for v in node]
    return node


def _read_json(path):
    try:
        return _strip_comments(json.loads(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc))


def load_profile(path=None):
    """The scoring profile, private copy preferred.

    Falling back to the tracked example is the documented policy for a fresh
    clone: the public fork ships only `*.example.json`, and a scorer that
    refused to run without a private file would make the board unusable until
    someone copied it. The example is a working template, so the fallback
    degrades to "generic defaults", never to "no score at all".
    """
    if path is not None:
        return validate_profile(_read_json(Path(path)))
    source = PROFILE if PROFILE.exists() else PROFILE_EXAMPLE
    return validate_profile(_read_json(source))


def validate_profile(profile):
    for key in ("roles", "seniority", "skills", "geo", "weights"):
        if key not in profile:
            raise ConfigError("fit profile is missing the %r block" % key)
    weights = profile["weights"]
    maxima = weights.get("max") or {}
    expected = {"role", "seniority", "skills", "company", "location"}
    if set(maxima) != expected:
        raise ConfigError("weights.max must name exactly %s, got %s"
                          % (sorted(expected), sorted(maxima)))
    total = sum(maxima.values())
    if total != 100:
        raise ConfigError("weights.max must sum to 100, got %d - otherwise the "
                          "band thresholds mean something different per profile" % total)
    bands = weights.get("bands") or {}
    if not {"high", "medium"} <= set(bands):
        raise ConfigError("weights.bands needs `high` and `medium`")
    if bands["high"] <= bands["medium"]:
        raise ConfigError("weights.bands.high must be above .medium")
    geo = profile["geo"]
    for bucket in geo.get("order") or ():
        if bucket not in geo:
            raise ConfigError("geo.order names %r, which has no term list" % bucket)
        if bucket not in (geo.get("scores") or {}):
            raise ConfigError("geo.order names %r, which has no score" % bucket)
    for name, family in (profile["roles"].get("families") or {}).items():
        if "score" not in family or "terms" not in family:
            raise ConfigError("role family %r needs both `score` and `terms`" % name)
    return profile


def load_affinity(path=None):
    if path is not None:
        return _read_json(Path(path))
    source = AFFINITY if AFFINITY.exists() else AFFINITY_EXAMPLE
    if not source.exists():
        return {"default": 2, "companies": {}}
    return _read_json(source)


def load_tiers(path=None):
    """{normalized company name -> registry tier}, for seeding affinity."""
    source = Path(path) if path else REGISTRY
    if not source.exists():
        return {}
    try:
        registry = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for company in registry.get("companies", []) or []:
        name = company.get("name")
        tier = company.get("tier")
        if name and isinstance(tier, int):
            out[_company_key(name)] = tier
            for alias in company.get("aliases") or ():
                out[_company_key(alias)] = tier
    return out


# --------------------------------------------------------------------------
# Component 1: role family


def role_family(title, body, profile):
    """(score, family, reason). Title evidence wins outright.

    A title that names any family settles it - a Data Scientist posting whose
    body is full of LLM wording is still a Data Scientist role, and letting the
    body promote it would quietly undo the whole point of grading families.
    Only a title that names nothing lets the description speak, and then at a
    discount.

    Families listed under `cap_families` behave differently: they *cap* rather
    than compete. A word like "Consultant" or "Solutions Engineer" describes the
    shape of the job, and the shape outranks the subject matter - an "Agentic AI
    Consultant" is a consulting role that happens to be about agents, so it must
    not inherit the AI-engineer score just because the highest match wins.
    """
    roles = profile["roles"]
    families = roles.get("families") or {}
    capping = set(roles.get("cap_families") or ())
    # A catch-all ("engineer", "developer") is only the answer when no named
    # family speaks - otherwise it outscores a weaker named one (generic 14 beat
    # data engineer 12) and swallows titles that do name a family.
    fallback = set(roles.get("fallback_families") or ())
    factor = roles.get("description_factor", 0.6)

    ceiling, cap_term, cap_name = None, None, None
    for name in capping:
        family = families.get(name)
        if not family:
            continue
        term = first_match(title, family["terms"])
        if term is not None and (ceiling is None or family["score"] < ceiling):
            ceiling, cap_term, cap_name = family["score"], term, name

    def _finish(points, name, reason):
        if ceiling is not None and points > ceiling:
            return ceiling, cap_name, '%s, but the title says "%s"' % (reason, cap_term)
        return points, name, reason

    # A capping title ("Forward Deployed Engineer") is itself a named family,
    # so it outranks the catch-all its "engineer" would otherwise fall into.
    tiers = [[n for n in families if n not in capping and n not in fallback]]
    if ceiling is None:
        tiers.append([n for n in families if n in fallback])
    best = (0, None, None)
    for tier in tiers:
        for name in tier:
            family = families[name]
            term = first_match(title, family["terms"]) or _compound_match(title, family)
            if term and family["score"] > best[0]:
                best = (family["score"], name, term)
        if best[1]:
            break
    if best[1]:
        return _finish(best[0], best[1], 'title matches "%s"' % best[2])
    if ceiling is not None:
        return ceiling, cap_name, 'title says "%s"' % cap_term

    best = (0, None, None)
    for name, family in families.items():
        if name in capping:
            continue
        term = first_match(body, family["terms"])
        if term:
            scaled = int(round(family["score"] * factor))
            if scaled > best[0]:
                best = (scaled, name, term)
    if best[1]:
        return _finish(best[0], best[1],
                       'posting body mentions "%s" (title is generic)' % best[2])

    return 0, None, "no engineering or AI role wording found"


def _compound_match(title, family):
    """A title naming one word from every `compound` group, e.g. AI + engineer.

    "AI Product Engineer", "AI Agent Engineer" and "KI-Entwickler" name the
    subject and the shape in separate words, so no fixed phrase list keeps up.
    Bare "ai" is safe here only because a second group has to match too.
    """
    groups = family.get("compound") or ()
    if not groups:
        return None
    found = [first_match(title, group) for group in groups]
    return " + ".join(found) if all(found) else None


# --------------------------------------------------------------------------
# Component 2: seniority

_YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:[-–—]\s*(\d{1,2})\s*)?\+?\s*(?:years?|yrs?|jahren?|jahre)",
    re.I)


def _years_required(body, profile):
    """The lowest marker-adjacent years-of-experience bar the body states.

    Only counted near an experience marker, which is what keeps "founded 10
    years ago" and "10 years of company history" out of it. Where a posting
    states several bars, the lowest is the one it will actually accept.
    """
    window = profile["seniority"].get("marker_window", 60)
    markers = profile["seniority"].get("experience_markers") or []
    found = []
    for match in _YEARS_RE.finditer(body or ""):
        start, end = match.span()
        context = body[max(0, start - window):min(len(body), end + window)]
        if first_match(context, markers):
            found.append(int(match.group(1)))
    return min(found) if found else None


def _level_for_years(years, profile):
    for band in profile["seniority"].get("years_bands") or ():
        if years <= band["max_years"]:
            return band["level"]
    return "lead"


TITLE_LADDER = ("lead", "senior", "three_plus", "founding", "one_two", "entry")


def seniority(title, body, profile):
    """(score, level, reason). Title reads the full ladder; the body does not.

    "Senior" in a job title is a requirement. "Senior" in a description is
    usually furniture - "reporting to the Head of AI", "you will work with
    senior engineers" - so from the body only the years regex and explicit
    entry markers count. When both halves speak, the lower score wins.
    """
    block = profile["seniority"]
    scores = block["scores"]
    candidates = []

    # Title: the whole ladder, most senior wins. "founding" sits between: a
    # founding role expects ownership beyond a new grad's, but states no bar
    # that rules one out, so it deducts without gating.
    for level in block.get("title_ladder") or TITLE_LADDER:
        term = first_match(title, block.get(level) or [])
        if term:
            candidates.append((scores.get(level, scores["none"]), level,
                               'title says "%s"' % term))
            break

    # Body: years, plus explicit entry wording. Nothing else.
    years = _years_required(body, profile)
    if years is not None:
        level = _level_for_years(years, profile)
        candidates.append((scores[level], level, "posting asks for %d+ years" % years))
    else:
        term = first_match(body, block.get("entry") or [])
        if term:
            candidates.append((scores["entry"], "entry", 'posting says "%s"' % term))

    if not candidates:
        return scores["none"], "none", "no seniority requirement stated"
    return min(candidates, key=lambda c: c[0])


# --------------------------------------------------------------------------
# Component 3: skills


def skill_overlap(body, profile, maximum):
    """(score, hits, reason). Additive, saturating, never negative - P1."""
    block = profile["skills"]
    saturation = max(1, block.get("saturation", 10))
    weights = (("primary", 3), ("secondary", 2), ("domain", 1))
    # Spellings of one skill ("llm", "llms", "large language model") count once.
    canon = {}
    for group in block.get("synonyms") or ():
        for term in group:
            canon[term.lower()] = group[0].lower()
    hits, seen, points = [], set(), 0
    for tier, weight in weights:
        for term in all_matches(body, block.get(tier) or []):
            key = canon.get(term.lower(), term.lower())
            if key in seen:
                continue
            seen.add(key)
            hits.append(term)
            points += weight
    score = min(maximum, int(round(maximum * points / float(saturation))))
    if not hits:
        # Not a penalty and it must not read like one: an absent keyword is an
        # absent keyword, and the interview is where a learnable gap is settled.
        return 0, [], "no profile skills named in the posting"
    return score, hits, "skills matched: %s" % ", ".join(hits[:3])


def excluded_stack(title, body, profile):
    """The excluded stack this role is *built on*, or None.

    A passing mention is nothing - plenty of good roles touch a legacy system.
    The gate needs the stack to be the role's own: named in the title, or
    inside a must-have line. Migration and interoperability wording is an
    explicit counter-signal, because "C# to Java migration" is a Java job.
    """
    block = profile["skills"]
    excluded = block.get("exclude") or []
    counters = block.get("exclude_counter_markers") or []

    term = first_match(title, excluded)
    if term and not first_match(title, counters):
        return term

    for region in _must_have_regions(body, profile):
        term = first_match(region, excluded)
        if term and not first_match(region, counters):
            return term
    return None


def _must_have_regions(body, profile):
    """The text blocks a requirements marker introduces.

    A requirements *section*, not a requirements *line*: postings put
    "Requirements:" on its own line and the bullets underneath it, so a
    line-by-line search finds the header and the stack in different lines and
    never fires. The window is the block a marker introduces.
    """
    block = profile["skills"]
    window = block.get("must_have_window", 400)
    body = body or ""
    lowered = body.lower()
    for marker in block.get("must_have_markers") or []:
        for match in _TERM_CACHE.setdefault(marker, _term_re(marker)).finditer(lowered):
            yield body[match.start():match.start() + window]


def must_have_gaps(body, profile):
    """(penalty, gaps, reason). Requirements outside your profile, named not punished.

    `skills.gaps` is a short list you curate of things you have not done (a
    domain, a regulated environment, a language you never used). Most postings
    list them beside the core stack as wishes, and no employer expects one hire
    to bring all of them, so by default they only *inform*: the reason names
    them and `gap_penalty` is 0. Raising it makes them deduct, capped at
    `gap_max` - P1 holds unless you opt out of it.
    """
    block = profile["skills"]
    terms = block.get("gaps") or []
    if not terms or not (body or "").strip():
        return 0, [], None
    gaps = []
    for region in _must_have_regions(body, profile):
        for term in all_matches(region, terms):
            if term not in gaps:
                gaps.append(term)
    if not gaps:
        return 0, [], None
    named = ", ".join(gaps[:3])
    penalty = min(block.get("gap_max", 0), block.get("gap_penalty", 0) * len(gaps))
    if not penalty:
        return 0, gaps, "also asks for %s (learnable, not scored)" % named
    return penalty, gaps, "asks for %s - not in your profile" % named


# --------------------------------------------------------------------------
# Posting language
#
# A posting written in German assumes you read and work in German, whether or
# not it states a level - and the candidate cannot. Measured on the stored
# postings the ratio below is sharply bimodal (under 0.1 or over 0.9), so the
# thresholds are not delicate; between them sits the bilingual posting.

_DE_WORDS = frozenset(
    "der die das und ist nicht mit für auf ein eine einer einen den dem des sie "
    "wir ihr ihre ihren bei zu von im auch als oder sowie werden wird unsere "
    "unser unseren du deine dich dir uns sind über zur zum diese dieser kannst "
    "bist hast".split())
_EN_WORDS = frozenset(
    "the and to of for with you we our is are on as be will your this that or "
    "from at have can who what us an by".split())
# A title alone is enough when it is a German job title: no English posting
# calls the role "Entwickler" or "Mitarbeiter".
_DE_TITLE = re.compile(
    r"entwickler|ingenieur|mitarbeiter|sachbearbeiter|informatiker|"
    r"praktikant|werkstudent|fachkraft|spezialist|referent|wissenschaftliche|"
    r"leiter(?:in)?\b|berater|softwareentwicklung|:in\b|\*in\b", re.I)
GERMAN_SHARE = 0.6
MIXED_SHARE = 0.3
MIN_LANGUAGE_WORDS = 12


def posting_language(title, body):
    """("de" | "mixed" | "en" | None, reason). None when there is too little text."""
    words = re.findall(r"[a-zäöüß]+", (body or "").lower())
    de = sum(1 for w in words if w in _DE_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    if de + en >= MIN_LANGUAGE_WORDS:
        share = de / float(de + en)
        if share >= GERMAN_SHARE:
            return "de", "posting is written in German"
        if share >= MIXED_SHARE:
            return "mixed", "posting is partly written in German"
        return "en", None
    match = _DE_TITLE.search(title or "")
    if match:
        return "de", 'German job title ("%s")' % match.group(0)
    return None, None


# --------------------------------------------------------------------------
# Component 4: company affinity


def _company_key(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _match_company(name, ratings):
    """Exact-ish first, then the repo's existing fuzzy company matcher."""
    key = _company_key(name)
    if not key:
        return None
    by_key = {_company_key(k): k for k in ratings}
    if key in by_key:
        return by_key[key]
    try:
        sys.path.insert(0, str(ROOT))
        import salary_lookup  # noqa: E402
    except ImportError:  # pragma: no cover - the module ships with the repo
        return None
    best, best_score = None, 0
    for candidate in ratings:
        try:
            score = salary_lookup.match_score(name, candidate)
        except Exception:  # pragma: no cover - never let matching break scoring
            continue
        if score > best_score:
            best, best_score = candidate, score
    return (best, best_score) if best else None


def company_affinity(name, profile, affinity, tiers, maximum):
    """(score, reason). Your rating wins; a registry tier seeds; else default.

    "Unrated" therefore does not mean one fixed number - a tier-1 company with
    no rating seeds to 4, not to the neutral default.
    """
    if not maximum:
        # Company switched off in weights.max: it neither scores nor explains.
        return 0, None
    ratings = affinity.get("companies") or {}
    threshold = (profile.get("company") or {}).get("match_threshold", 85)

    matched = _match_company(name, ratings)
    if isinstance(matched, tuple):
        matched, score = matched
        if score < threshold:
            matched = None
    if matched is not None:
        value = ratings[matched]
        reason = "you rated %s %d/5" % (matched, value)
        return int(round(maximum * value / 5.0)), reason

    tier = tiers.get(_company_key(name))
    if tier is not None:
        seeds = (profile.get("company") or {}).get("tier_seed") or {}
        value = seeds.get(str(tier), seeds.get(tier, 2))
        return int(round(maximum * value / 5.0)), "tier %s company (unrated)" % tier

    default = affinity.get("default", 2)
    # Unrated is the neutral default and says nothing about this posting, so it
    # gives no reason - it would only push a real one out of the four shown.
    return int(round(maximum * default / 5.0)), None


# --------------------------------------------------------------------------
# Component 5: location


def location_score(text, profile):
    """(score, bucket, reason). An unresolved location is neutral, never zero."""
    geo = profile["geo"]
    scores = geo.get("scores") or {}
    if not _norm(text):
        return scores.get("unknown", 8), "unknown", "no location given"
    for bucket in geo.get("order") or ():
        term = first_match(text, geo.get(bucket) or [])
        if term:
            return scores.get(bucket, 0), bucket, "location: %s" % term
    return scores.get("unknown", 8), "unknown", "location not recognised: %s" % _norm(text)[:40]


# --------------------------------------------------------------------------
# The score


class Context(object):
    """Everything the scorer needs, loaded once for a whole run."""

    __slots__ = ("profile", "affinity", "tiers")

    def __init__(self, profile=None, affinity=None, tiers=None):
        self.profile = profile if profile is not None else load_profile()
        self.affinity = affinity if affinity is not None else load_affinity()
        self.tiers = tiers if tiers is not None else load_tiers()


def score(title, company, location, body, ctx, german_hard=False):
    """The five components, the gates, the band, and the sort ceiling.

    Returns the `fit_*` fields, ready to be merged onto an entry.
    """
    profile = ctx.profile
    maxima = profile["weights"]["max"]
    bands = profile["weights"]["bands"]
    body = body or ""
    title = title or ""
    evidence = "full" if body.strip() else "title-only"

    role_pts, family, role_reason = role_family(title, body, profile)
    sen_pts, level, sen_reason = seniority(title, body, profile)
    skill_pts, hits, skill_reason = (
        skill_overlap(body, profile, maxima["skills"]) if body.strip()
        else (0, [], "no posting text stored - skills not assessed"))
    comp_pts, comp_reason = company_affinity(
        company, profile, ctx.affinity, ctx.tiers, maxima["company"])
    loc_pts, bucket, loc_reason = location_score(location, profile)
    gap_pts, gaps, gap_reason = must_have_gaps(body, profile)
    language, language_reason = posting_language(title, body)
    german, german_quote = collectors.german_hit(body) if body.strip() else (None, "")

    role_pts = min(role_pts, maxima["role"])
    sen_pts = min(sen_pts, maxima["seniority"])
    loc_pts = min(loc_pts, maxima["location"])

    parts = {"role": role_pts, "seniority": sen_pts, "skills": skill_pts,
             "company": comp_pts, "location": loc_pts}
    if gap_pts:
        parts["gaps"] = -gap_pts
    raw = max(0, sum(parts.values()))

    band = "high" if raw >= bands["high"] else "medium" if raw >= bands["medium"] else "low"
    reasons = [role_reason, sen_reason,
               skill_reason if body.strip() else None, gap_reason,
               comp_reason, loc_reason]
    if german == "soft" and language == "en":
        reasons.insert(0, "German mentioned as a plus")
    reasons = [r for r in reasons if r]

    # Gates. Each can only lower the band, the number is always preserved, and
    # the reason goes to the front - a band never changes silently.
    gates = []
    if german_hard or german == "hard":
        gates.append(("low", "German stated as a job condition"))
    elif language == "de":
        gates.append(("low", language_reason))
    if language == "mixed":
        gates.append(("medium", language_reason))
    if role_pts == 0:
        gates.append(("low", "not an engineering or AI role"))
    # Experience is the other hard line besides language: a bar you cannot
    # clear is not a stretch application. Which levels count is the profile's.
    if level in (profile["seniority"].get("gate_levels") or ()):
        gates.append(("low", "%s - beyond your experience" % sen_reason))
    blocked = excluded_stack(title, body, profile)
    if blocked:
        gates.append(("low", 'built on "%s", which you have excluded' % blocked))
    if evidence == "title-only":
        gates.append(("medium", "scored from the title alone - no posting text stored"))

    for ceiling, reason in gates:
        if BANDS.index(ceiling) > BANDS.index(band):
            band = ceiling
        reasons.insert(0, reason)

    # Where the number fell short of 100, biggest shortfall first, so a low or
    # medium row explains itself without opening the posting. Gates lead: they
    # cost no points but they are why the band reads lower than the number.
    shortfalls = [
        ("role", role_reason),
        ("seniority", sen_reason),
        ("skills", "only %d profile skill%s named (%s)" % (
            len(hits), "" if len(hits) == 1 else "s", ", ".join(hits[:3]))
         if hits else skill_reason),
        ("company", comp_reason or (maxima["company"] and "company unrated (neutral default)")),
        ("location", loc_reason),
    ]
    losses = []
    for key, reason in shortfalls:
        lost = maxima[key] - parts[key]
        if lost >= MIN_LOSS and reason:
            losses.append((lost, "-%d %s: %s" % (lost, key, reason)))
    if gap_pts:
        losses.append((gap_pts, "-%d gaps: %s" % (gap_pts, gap_reason)))
    losses.sort(key=lambda item: -item[0])
    losses = ["band capped at %s: %s" % (ceiling, reason) for ceiling, reason in gates
              ] + [text for _, text in losses]

    return {
        "fit": band,
        "fit_score": raw,
        "fit_priority_score": min(raw, BAND_CEILING[band]),
        "fit_parts": parts,
        "fit_reasons": reasons[:MAX_REASONS],
        "fit_losses": losses,
        "fit_source": "deterministic",
        "fit_evidence": evidence,
        "fit_version": FIT_VERSION,
        "fit_family": family,
        "fit_seniority": level,
        "fit_language": language,
    }


def score_entry(entry, ctx, body=None):
    """Score one board row. Reads its stored posting body unless one is passed."""
    if body is None:
        try:
            body = postings.load(entry)
        except postings.UnsafePath:
            body = ""
    german = (entry.get("user_status") == "gate"
              and "German" in (entry.get("note") or ""))
    return score(entry.get("title", ""), entry.get("company", ""),
                 entry.get("location", ""), body, ctx, german_hard=german)


def apply_to(entry, ctx, body=None):
    """Write the deterministic fields onto an entry, respecting /rank.

    `/rank` is the authority: once it has judged a row, a recompute may refresh
    the number and the parts but must not move the band it displays, or the
    next collect would quietly undo an LLM assessment that cost real tokens.
    """
    fields = score_entry(entry, ctx, body)
    if entry.get("fit_source") == "ranked":
        fields.pop("fit", None)
        fields.pop("fit_source", None)
        # The displayed band still governs the sort, so the ceiling has to come
        # from the band that is actually on the row.
        fields["fit_priority_score"] = min(
            fields["fit_score"], BAND_CEILING.get(entry.get("fit"), 100))
        fields.pop("fit_reasons", None)
    entry.update(fields)
    return entry


# --------------------------------------------------------------------------
# CLI


# Backfill eligibility is deliberately NOT the display band.
#
# Gating fetches on "band >= medium" is circular: a generic Backend Engineer row
# scores below the line *because* it has no body, so it never earns the body
# that would name React, TypeScript and Python and rescue it. The predicate has
# to run on what the title alone can establish - that this is a target role at
# all, and that it is not already hopeless - and then let the evidence decide.
BACKFILL_FLOOR = 40


def backfill_candidates(seen, ctx, floor=BACKFILL_FLOOR):
    """(url, entry) pairs worth spending a request on, best first."""
    out = []
    for url, entry in seen.items():
        if entry.get("posting_path") or entry.get("user_status") in ("no", "expired"):
            continue
        fields = score_entry(entry, ctx, body="")
        if fields["fit_parts"]["role"] <= 0 or fields["fit_score"] < floor:
            continue
        out.append((fields["fit_score"], url, entry))
    out.sort(key=lambda item: -item[0])
    return [(url, entry) for _score, url, entry in out]


def backfill(seen, ctx, log=print, limit=25, dry_run=False, fetcher=None):
    """Fetch, extract and store the bodies of rows that would benefit most.

    Every fetch is gated by robots.txt first and refused if it does not clearly
    allow the path. A page that fails extraction - a login wall, a bot check, a
    body too short to be a posting - stores nothing and leaves the row exactly
    as it was, rather than recording a non-posting as full evidence.
    """
    # One robots.txt per host for the whole run. Most of a batch is a handful of
    # hosts, and re-asking each of them per row is wasted requests on someone
    # else's server.
    fetcher = fetcher or _fetcher_with_cache({})
    candidates = backfill_candidates(seen, ctx)
    log("%d rows have no stored posting and are worth fetching (limit %d)"
        % (len(candidates), limit))
    stats = {"fetched": 0, "stored": 0, "refused": 0, "rejected": 0, "failed": 0}
    pending = []
    for url, entry in candidates[:limit]:
        target = jobs_md.primary_url(entry) or url
        if dry_run:
            log("  would fetch %s" % target[:100])
            stats["fetched"] += 1
            continue
        try:
            html, reason = fetcher(target)
        except Exception as exc:  # pragma: no cover - transport variety
            stats["failed"] += 1
            log("  ! %s: %s" % (target[:70], exc))
            continue
        if html is None:
            stats["refused"] += 1
            log("  - %s: %s" % (target[:70], reason))
            continue
        stats["fetched"] += 1
        result = posting_text.extract(html)
        if not result.ok:
            stats["rejected"] += 1
            log("  - %s: %s" % (target[:70], result.reason))
            continue
        entry.update(postings.describe(
            url, result.text, source="backfill",
            extractor=result.extractor, final_url=target))
        pending.append((url, result.text))
        apply_to(entry, ctx, body=result.text)
        stats["stored"] += 1
        log("  + %s (%s, %d chars) -> %s"
            % (target[:60], result.extractor, len(result.text), entry["fit"]))
    return stats, pending


def _fetcher_with_cache(robots_cache):
    """A fetcher that gates on robots.txt, reusing one file per host per run."""
    import robots_check

    def fetch(url):
        """(html, reason). `html` is None when policy or transport said no."""
        code, message = robots_check.gate(url, robots_cache)
        if code != 0:
            return None, "robots: %s" % message
        try:
            _final, status, body = fetch_url.fetch(url)
        except fetch_url.Refused as exc:
            return None, "refused: %s" % exc
        if status >= 400:
            return None, "HTTP %s" % status
        return body, ""

    return fetch


def histogram(seen):
    counts = {"high": 0, "medium": 0, "low": 0, "unscored": 0}
    for entry in seen.values():
        counts[entry.get("fit") or "unscored"] = counts.get(entry.get("fit") or "unscored", 0) + 1
    return counts


def _fmt(counts):
    return " ".join("%s=%d" % (k, counts.get(k, 0))
                    for k in ("high", "medium", "low", "unscored"))


def recompute(seen, ctx, only_stale=False):
    """Rescore rows in place. Returns (touched, skipped_ranked)."""
    touched = ranked = 0
    for entry in seen.values():
        if only_stale and entry.get("fit_version") == FIT_VERSION:
            continue
        if entry.get("fit_source") == "ranked":
            ranked += 1
        apply_to(entry, ctx)
        touched += 1
    return touched, ranked


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recompute", action="store_true", help="rescore rows in the board")
    parser.add_argument("--stale", action="store_true",
                        help="with --recompute, only rows scored under an older formula")
    parser.add_argument("--explain", metavar="URL", help="show one row's components")
    parser.add_argument("--backfill-postings", action="store_true",
                        help="fetch the missing posting bodies worth having")
    parser.add_argument("--max-fetches", type=int, default=25,
                        help="how many pages one backfill run may request")
    parser.add_argument("--dry-run", action="store_true", help="compute and report, write nothing")
    args = parser.parse_args(argv)

    try:
        ctx = Context()
    except ConfigError as exc:
        print("fit_score: %s" % exc, file=sys.stderr)
        return 2

    with jobs_md.board_lock():
        seen = {}
        if jobs_md.SEEN.exists():
            seen = json.loads(jobs_md.SEEN.read_text(encoding="utf-8")).get("seen", {})

        if args.explain:
            key = jobs_md.canonical_url(args.explain)
            entry = seen.get(key)
            if entry is None:
                print("no row for %s" % key, file=sys.stderr)
                return 1
            fields = score_entry(entry, ctx)
            print(json.dumps({"title": entry.get("title"), "company": entry.get("company"),
                              **fields}, indent=2, ensure_ascii=False))
            return 0

        if args.backfill_postings:
            stats, pending = backfill(seen, ctx, limit=max(0, args.max_fetches),
                                      dry_run=args.dry_run)
            print("fetched %(fetched)d, stored %(stored)d, refused %(refused)d, "
                  "rejected %(rejected)d, failed %(failed)d" % stats)
            print("after:  %s" % _fmt(histogram(seen)))
            if args.dry_run:
                print("dry run - nothing written")
                return 0
            # Bodies before state, as everywhere else: an orphan sidecar is
            # inert, a saved posting_path with no file behind it is a row that
            # claims a posting it cannot show.
            postings.commit_all(pending)
            jobs_md.save_seen(seen)
            print("wrote %d posting bodies and seen_jobs.json" % len(pending))
            return 0

        if not args.recompute:
            parser.print_help()
            return 0

        before = histogram(seen)
        touched, ranked = recompute(seen, ctx, only_stale=args.stale)
        after = histogram(seen)
        print("rows: %d   rescored: %d   ranked rows whose band was preserved: %d"
              % (len(seen), touched, ranked))
        print("before: %s" % _fmt(before))
        print("after:  %s" % _fmt(after))

        evidence = {}
        for entry in seen.values():
            key = entry.get("fit_evidence") or "none"
            evidence[key] = evidence.get(key, 0) + 1
        print("evidence: %s" % " ".join("%s=%d" % kv for kv in sorted(evidence.items())))

        if args.dry_run:
            print("dry run - nothing written")
            return 0
        jobs_md.save_seen(seen)
        print("wrote seen_jobs.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
