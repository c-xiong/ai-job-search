#!/usr/bin/env python3
"""Turn a fetched careers page into the posting text, or refuse to.

`tools/board/fetch_url.py` prints the raw HTTP body. Stored as-is that gives
HTML source in the Job pane, and nav/cookie-banner/script wording counted as
posting keywords by the fit scorer. Worst of all, a login wall or a bot check
is a 200 with a body, so without a quality gate it would be recorded as a real
posting and the row would claim full evidence it does not have.

Three extraction paths, best first:

1. JSON-LD `@type: JobPosting` -> `description`. Most ATS and careers pages
   emit it, and it is the publisher's own idea of where the posting starts and
   stops rather than our guess.
2. A container whose id/class names a job description.
3. Whole-page text with chrome removed.

Every path then goes through the same gate, which fails closed: on refusal the
caller writes no sidecar, does not upgrade the row's evidence, and keeps the
reason. A failed extraction must leave a row exactly as it was.

Stdlib only - the repo has no bs4/lxml and must not gain one for this.
"""

import json
import re
from html.parser import HTMLParser

MIN_CHARS = 400
# Above this length a stray "sign in" is almost certainly a line inside a real
# posting rather than the whole page being a wall.
WALL_MAX_CHARS = 1200

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
# Site chrome and anything that is code rather than prose.
SKIP_TAGS = {
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "form", "svg", "iframe", "button", "select", "option", "template",
}
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "ul", "ol", "table",
    "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd",
}
# Tags that also break the line on the way *out*, producing a blank line. List
# items deliberately do not: a ten-line requirements list separated by blank
# lines reads as ten paragraphs in the Job pane.
CLOSING_BREAK_TAGS = {
    "p", "div", "section", "article", "tr", "table", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
}

# id/class/data-* wording that names a job description. Ordered generously -
# the winner is chosen by how much text it holds, not by pattern order, so an
# extra loose pattern costs nothing.
CONTAINER_RE = re.compile(
    r"job[-_ ]?description|job[-_ ]?detail|job[-_ ]?post|job[-_ ]?ad|jobad"
    r"|posting[-_ ]?description|job[-_ ]?section|opportunity[-_ ]?description"
    r"|vacancy[-_ ]?description|description[-_ ]?section|stellenbeschreibung"
    r"|\bdescription\b|\bjobcontent\b|\bposting\b",
    re.I,
)

# Strong, specific evidence that the page is a wall rather than a posting.
# Deliberately not "sign in" or "login" alone: real postings say "sign in to
# apply", and LinkedIn renders that line on genuine job pages.
WALL_RE = re.compile(
    r"verify (?:you are|that you are) (?:a )?human|are you a robot|captcha"
    r"|unusual traffic|enable javascript|javascript is (?:required|disabled)"
    r"|access denied|403 forbidden|page not found|404 error"
    r"|this (?:job|position|posting) is no longer|no longer accepting"
    r"|please log ?in to continue|your session has expired"
    r"|checking your browser|cloudflare|attention required",
    re.I,
)

# Something a job posting says and a nav bar does not. English and German,
# because a DACH board is half German even when the role is English-speaking.
REQUIREMENT_RE = re.compile(
    r"responsibilit|requirement|qualification|what you(?:'| a)?ll|you will"
    r"|we offer|your profile|about the role|your tasks|your role|benefits"
    r"|experience|skills|deine aufgaben|ihre aufgaben|dein profil|ihr profil"
    r"|anforderungen|wir bieten|das bringst du mit|aufgabenbereich",
    re.I,
)


class Extraction(object):
    """The result of one extraction attempt."""

    __slots__ = ("text", "extractor", "ok", "reason")

    def __init__(self, text="", extractor="", ok=False, reason=""):
        self.text = text
        self.extractor = extractor
        self.ok = ok
        self.reason = reason

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Extraction(extractor=%r, ok=%r, chars=%d, reason=%r)" % (
            self.extractor, self.ok, len(self.text), self.reason)


class _Collector(HTMLParser):
    """Flatten HTML to text, remembering which spans were description containers.

    Text is appended to one flat list of chunks. A container of interest records
    the slice of that list it covers, so a candidate is a slice rather than a
    nested buffer - which keeps the bookkeeping honest on the malformed markup
    careers pages routinely serve.
    """

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.chunks = []
        self.candidates = []
        self._depth = 0
        self._skip_depth = None
        self._open = []  # (depth, start_index)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in VOID_TAGS:
            if tag == "br":
                self.chunks.append("\n")
            return
        if self._skip_depth is None and tag in SKIP_TAGS:
            self._skip_depth = self._depth
        if tag in BLOCK_TAGS:
            self.chunks.append("\n")
        if self._skip_depth is None and _names_a_description(attrs):
            self._open.append((self._depth, len(self.chunks)))
        self._depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in VOID_TAGS:
            return
        self._depth = max(0, self._depth - 1)
        if self._skip_depth is not None and self._depth <= self._skip_depth:
            self._skip_depth = None
        while self._open and self._open[-1][0] >= self._depth:
            _, start = self._open.pop()
            self.candidates.append((start, len(self.chunks)))
        if tag.lower() in CLOSING_BREAK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth is not None:
            return
        if data and data.strip():
            self.chunks.append(data)
        elif data:
            self.chunks.append(" ")

    def close(self):
        HTMLParser.close(self)
        while self._open:
            _, start = self._open.pop()
            self.candidates.append((start, len(self.chunks)))

    def page_text(self):
        return _tidy("".join(self.chunks))

    def container_texts(self):
        return [_tidy("".join(self.chunks[start:end])) for start, end in self.candidates]


def _names_a_description(attrs):
    for name, value in attrs or ():
        if not value:
            continue
        if name.lower() in ("id", "class") or name.lower().startswith("data-"):
            if CONTAINER_RE.search(value):
                return True
    return False


def _tidy(text):
    """Collapse runs of spaces and blank lines without losing paragraphs."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_like_html(text):
    if not text:
        return False
    head = text[:4000].lower()
    return bool(re.search(r"<\s*(html|body|div|p|script|meta|section|article)\b", head))


def strip_tags(html):
    """Plain text from an HTML fragment. Used on JSON-LD descriptions too."""
    parser = _Collector()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient; belt and braces
        return _tidy(re.sub(r"<[^>]+>", " ", html or ""))
    return parser.page_text()


def _walk_json_ld(node, found):
    """Collect every JobPosting description in a JSON-LD document."""
    if isinstance(node, list):
        for item in node:
            _walk_json_ld(item, found)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if any(isinstance(t, str) and t.lower() == "jobposting" for t in types):
        description = node.get("description")
        if isinstance(description, str) and description.strip():
            found.append(description)
    for key in ("@graph", "itemListElement", "mainEntity", "item"):
        if key in node:
            _walk_json_ld(node[key], found)


def from_json_ld(html):
    """The JobPosting description a page declares about itself, or ""."""
    found = []
    for match in re.finditer(
        r"<script[^>]+type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
        html or "", re.I | re.S,
    ):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            # Some publishers emit JS-escaped or trailing-comma JSON. One
            # cheap repair, then give up and let the other paths handle it.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except ValueError:
                continue
        _walk_json_ld(data, found)
    if not found:
        return ""
    best = max(found, key=len)
    return strip_tags(best) if looks_like_html(best) else _tidy(best)


def check(text):
    """Gate one candidate body. Returns (ok, reason)."""
    body = (text or "").strip()
    if len(body) < MIN_CHARS:
        return False, "too short (%d chars, need %d)" % (len(body), MIN_CHARS)
    wall = WALL_RE.search(body)
    if wall and len(body) < WALL_MAX_CHARS:
        return False, "looks like a login or bot wall (%r)" % wall.group(0)[:40]
    if not REQUIREMENT_RE.search(body):
        return False, "no requirement-like wording - probably not a posting body"
    return True, ""


def extract(html):
    """Best posting text from a fetched page, gated.

    On refusal `.ok` is False, `.text` is the best candidate found (for
    diagnosis) and `.reason` says why it was rejected. The caller must not
    store a rejected body.
    """
    if not (html or "").strip():
        return Extraction(reason="empty response body")

    if not looks_like_html(html):
        # Already plain text: an inline collector description, or a page served
        # as text/plain. Gate it, but do not run it through the tag stripper.
        body = _tidy(html)
        ok, reason = check(body)
        return Extraction(body, "inline", ok, reason)

    candidates = []

    structured = from_json_ld(html)
    if structured:
        candidates.append(("json-ld", structured))

    parser = _Collector()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover
        parser = None

    if parser is not None:
        container = max(parser.container_texts(), key=len, default="")
        if container:
            candidates.append(("container", container))
        page = parser.page_text()
        if page:
            candidates.append(("fallback", page))
    else:
        candidates.append(("fallback", _tidy(re.sub(r"<[^>]+>", " ", html))))

    # Best path first, and the first one that passes the gate wins. A page that
    # declares a JobPosting is trusted over our container guess even when the
    # guess is longer, because "longer" on a careers page usually means it
    # swallowed the related-jobs rail.
    first_failure = None
    for name, body in candidates:
        ok, reason = check(body)
        if ok:
            return Extraction(body, name, True, "")
        if first_failure is None:
            first_failure = Extraction(body, name, False, reason)

    return first_failure or Extraction(reason="no candidate text found")


def main(argv=None):  # pragma: no cover - thin CLI for spot checks
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python3 tools/posting_text.py <file.html>", file=sys.stderr)
        return 2
    with open(argv[0], "r", encoding="utf-8", errors="replace") as handle:
        result = extract(handle.read())
    print("extractor=%s ok=%s chars=%d reason=%s"
          % (result.extractor, result.ok, len(result.text), result.reason or "-"),
          file=sys.stderr)
    if result.ok:
        sys.stdout.write(result.text)
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
