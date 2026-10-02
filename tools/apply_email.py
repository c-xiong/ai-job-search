#!/usr/bin/env python3
"""Find the address a posting asks you to apply to by email.

Some postings have no candidate portal: "Please send CV and motivation letter to
careers@acme.ch". That address is the application channel, so the board shows
it beside the posting and the tracker keeps it in `apply_email`, the way it
keeps `portal_url` for portal applications.

Not every address in a posting is that channel. A data-protection officer, a
no-reply sender or a press desk is never where a CV goes, so those are dropped,
and of what remains the address whose surrounding sentence talks about applying
ranks first. An address named in the posting but in no apply sentence is still
returned (a small company's only contact is usually where applications go), only
ranked after the ones that are.

Stdlib only.
"""

import re

# "name [at] company [dot] ch" and its variants, folded back before matching.
_AT = re.compile(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\})\s*", re.IGNORECASE)
_DOT = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\})\s*", re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})(?![\w-])")

# Local parts and words around an address that mean it is not an application inbox.
_NOT_APPLY_LOCAL = re.compile(
    r"^(?:no-?reply|do-?not-?reply|privacy|datenschutz|dpo|data-?protection|gdpr|"
    r"press|presse|media|medien|marketing|sales|verkauf|support|abuse|webmaster|"
    r"unsubscribe|billing|invoice|rechnung|accounting|info-?sec|security)\b",
    re.IGNORECASE)
_NOT_APPLY_CONTEXT = re.compile(
    r"data protection|privacy|datenschutz|gdpr|dsgvo|unsubscribe|your data|"
    r"fraud|scam|phishing|accommodation|adjustment|disabilit|anything you need|"
    r"recruit(?:ment|ing)? agenc|personalvermittl|press enquir|media enquir",
    re.IGNORECASE)
# The sentence asks for the application itself to go to this address.
_APPLY_CONTEXT = re.compile(
    r"\b(?:send|submit|forward|e-?mail|mail)\b[^.]{0,80}\b(?:cv|resume|résumé|application|"
    r"documents|dossier|motivation|cover letter)|"
    r"\b(?:cv|resume|application|dossier|motivation letter|cover letter)\b[^.]{0,60}\bto\s*$|"
    r"how to apply|apply (?:by|via|per) e-?mail|\bapply\b[^.]{0,30}\b(?:to|at)\s*$|"
    r"bewerb|bewirb|unterlagen|lebenslauf|dossier de candidature|candidature",
    re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")

# How much text either side of an address counts as its sentence.
CONTEXT_CHARS = 140
MAX_RESULTS = 3


def _clean(text):
    """Markup gone and obfuscated addresses folded back, so one regex finds them."""
    text = _TAG.sub(" ", text).replace("&nbsp;", " ").replace("\\", "")
    text = re.sub(r"\*{1,2}|_{2}", "", text)
    return _DOT.sub(".", _AT.sub("@", text))


def _context(text, start, end):
    """The sentence an address sits in, trimmed to CONTEXT_CHARS each side."""
    left = text[max(0, start - CONTEXT_CHARS):start]
    right = text[end:end + CONTEXT_CHARS]
    # Cut at the nearest sentence boundary so the snippet reads as one clause.
    cut = max(left.rfind(". "), left.rfind("\n"), left.rfind("! "), left.rfind("? "))
    if cut >= 0:
        left = left[cut + 1:]
    stop = min([i for i in (right.find(". "), right.find("\n")) if i >= 0] or [len(right)])
    right = right[:stop + 1] if stop < len(right) else right
    return re.sub(r"\s+", " ", (left + text[start:end] + right)).strip()


def extract(text):
    """[{email, context, apply}] in rank order, at most MAX_RESULTS.

    `apply` is True when the address's own sentence asks for the application to
    be sent there; False is a named contact (a recruiter, "questions to ...").
    """
    text = _clean(text or "")
    found = {}
    for match in _EMAIL.finditer(text):
        email = match.group(1).rstrip(".")
        key = email.casefold()
        if key in found:
            continue
        local = email.split("@", 1)[0]
        if _NOT_APPLY_LOCAL.match(local):
            continue
        start = match.start(1)
        context = _context(text, start, start + len(email))
        if _NOT_APPLY_CONTEXT.search(context):
            continue
        # "Send your CV to" reads left of the address; "Bewerbung an" can be either side.
        before = re.sub(r"\s+", " ", text[max(0, start - CONTEXT_CHARS):start])
        found[key] = {"email": email, "context": context,
                      "apply": bool(_APPLY_CONTEXT.search(before)
                                    or _APPLY_CONTEXT.search(context)),
                      "order": len(found)}
    ranked = sorted(found.values(), key=lambda item: (not item["apply"], item["order"]))
    return [{k: v for k, v in item.items() if k != "order"} for item in ranked[:MAX_RESULTS]]


def best(text):
    """The single most likely application address, or ""."""
    hits = extract(text)
    return hits[0]["email"] if hits and hits[0]["apply"] else ""


if __name__ == "__main__":  # pragma: no cover - spot checks
    import json
    import sys
    print(json.dumps(extract(sys.stdin.read()), indent=2, ensure_ascii=False))
