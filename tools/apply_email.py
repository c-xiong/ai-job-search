#!/usr/bin/env python3
"""Find the address a posting asks you to apply to by email.

Some postings have no candidate portal: "Please send CV and motivation letter to
careers@acme.ch". That address is the application channel, so the board shows
it beside the posting and the tracker keeps it in `apply_email`, the way it
keeps `portal_url` for portal applications.

Not every address in a posting is that channel. A data-protection officer, a
no-reply sender or a press desk is never where a CV goes, so those are dropped,
and of what remains the address whose surrounding sentence talks about applying
ranks first. Other addresses are returned as contacts, never assumed to accept applications.

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
_HELP_CONTEXT = re.compile(
    r"already (?:applied|submitted)|submission (?:error|problem)|need help|"
    r"help.*error|technical (?:support|problem)|hilfe|fehler|bereits.*beworb", re.IGNORECASE)
_QUESTIONS = re.compile(r"questions?|enquir|learn more|fragen|auskunft", re.IGNORECASE)
_NEGATED = re.compile(r"(?:do not|don.t|not accept|no applications|cannot accept)[^.]*|keine.*bewerb", re.IGNORECASE)


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
    """[{email, context, apply}] in rank order.

    `apply` is True when the address's own sentence asks for the application to
    be sent there; False is a named contact (a recruiter, "questions to ...").
    """
    text = _clean(text or "")
    found = {}
    for match in _EMAIL.finditer(text):
        email = match.group(1).rstrip(".")
        key = email.casefold()
        local = email.split("@", 1)[0]
        if _NOT_APPLY_LOCAL.match(local):
            continue
        start = match.start(1)
        context = _context(text, start, start + len(email))
        if _NOT_APPLY_CONTEXT.search(context):
            continue
        # Only this address's clause may establish its purpose. An application
        # sentence elsewhere on the page is not evidence for this recipient.
        help_contact = bool(_HELP_CONTEXT.search(context))
        nearby = text[max(0, start - 220):start]
        prior_addresses = list(_EMAIL.finditer(nearby))
        if prior_addresses:
            nearby = nearby[prior_addresses[-1].end():]
        question_contact = bool(_QUESTIONS.search(context) or
                                (not _APPLY_CONTEXT.search(context) and _QUESTIONS.search(nearby)))
        if question_contact and not _QUESTIONS.search(context):
            context = re.sub(r"\s+", " ", nearby + context).strip()
        is_apply = bool(_APPLY_CONTEXT.search(context) and not
                        (help_contact or question_contact or _NEGATED.search(context)))
        purpose = "application" if is_apply else ("support" if help_contact else
                  "questions" if question_contact else "unknown")
        item = {"email": email, "context": context, "apply": is_apply,
                "purpose": purpose, "order": len(found)}
        if key not in found or (is_apply and not found[key]["apply"]):
            found[key] = item
    ranked = sorted(found.values(), key=lambda item: (not item["apply"], item["order"]))
    return [{k: v for k, v in item.items() if k != "order"} for item in ranked]


def guidance(hits):
    """Conservative recipient recommendation from explicit posting evidence."""
    applications = [h for h in hits if h["apply"]]
    recipients = [h["email"] for h in applications]
    if not recipients:
        return {"mode": "unconfirmed", "recipients": [], "message":
                "No email application instructions found. Open the posting to use its application link or confirm how to apply. Do not send your CV to the contacts below without confirmation."}
    if len(recipients) == 1:
        return {"mode": "email", "recipients": recipients,
                "message": "Send your application to " + recipients[0] + ". Other contacts are not application recipients."}
    contexts = {h["context"] for h in applications}
    # Multiple independent instructions, alternatives and CC clauses must not
    # silently turn into a blanket To list.
    together = (len(contexts) == 1 and
                re.search(r"\b(?:and|both|und)\b", applications[0]["context"], re.I) and
                not re.search(r"\b(?:or|oder|cc|copy)\b", applications[0]["context"], re.I))
    if together:
        return {"mode": "email", "recipients": recipients,
                "message": "Send to both application recipients: " + ", ".join(recipients) + "."}
    return {"mode": "choose", "recipients": [], "message":
            "The posting names multiple application addresses, but does not clearly ask you to email all of them. Check the source instructions and choose the intended recipient before drafting."}


def best(text):
    """The unambiguous application recipient list, or an empty string."""
    return ", ".join(guidance(extract(text))["recipients"])


if __name__ == "__main__":  # pragma: no cover - spot checks
    import json
    import sys
    print(json.dumps(extract(sys.stdin.read()), indent=2, ensure_ascii=False))
