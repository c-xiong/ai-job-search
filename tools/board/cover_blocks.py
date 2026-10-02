"""Validate approved cover-letter prose without asking a model to judge it.

The canonical base opts in with COVER_LIBRARY_V1 and stores plain-text blocks
in comments. A generated letter selects blocks with USE_COVER_TEXT markers.
Only whitespace may change. Legacy/custom bases without the declaration keep
their existing behaviour. This is a checker for the base's supported LaTeX
shape, not a general-purpose TeX parser.
"""

import re


_ID = r"[a-z][a-z0-9_-]*"
_DECLARATION = re.compile(r"^\s*%\s*COVER_LIBRARY_V1\s*$", re.M)
_LIBRARY_START = re.compile(r"^\s*%\s*COVER_TEXT (" + _ID + r") (opening|highlight|research)\s*$")
_EXCLUSIVE = re.compile(r"^\s*%\s*COVER_EXCLUSIVE\s+(.+?)\s*$")
_USE_START = re.compile(r"^\s*%\s*USE_COVER_TEXT (" + _ID + r")\s*$")
_FIXED_TOKEN = re.compile(r"\\fixedcoverblock\{(\d+)\}")


def _normalise(text):
    return " ".join(text.split())


def _without_comments(text):
    return "\n".join(re.split(r"(?<!\\)%", line, 1)[0] for line in text.splitlines())


def _library(base, issues):
    blocks, current, lines = {}, None, []
    for line in base.splitlines():
        comment = re.sub(r"^\s*%\s?", "", line).strip()
        start = _LIBRARY_START.fullmatch(line)
        if start:
            if current:
                issues.append("nested COVER_TEXT block: " + current[0])
            current, lines = start.groups(), []
        elif comment == "END_COVER_TEXT":
            if not current:
                issues.append("END_COVER_TEXT without a start")
                continue
            ident, kind = current
            if ident in blocks:
                issues.append("duplicate library ID: " + ident)
            text = _normalise(" ".join(lines))
            if not text:
                issues.append("empty library text: " + ident)
            blocks[ident] = (kind, text)
            current = None
        elif comment.startswith("COVER_TEXT"):
            issues.append("invalid COVER_TEXT declaration")
        elif current:
            if not re.match(r"^\s*%", line):
                issues.append("library text must be commented: " + current[0])
            lines.append(comment)
    if current:
        issues.append("unterminated COVER_TEXT block: " + current[0])
    if not blocks:
        issues.append("declared library has no text blocks")
    return blocks


def _exclusive_groups(base, library, issues):
    """Read optional groups of alternatives that describe the same experience."""
    groups = []
    for line in base.splitlines():
        match = _EXCLUSIVE.fullmatch(line)
        if not match:
            if re.match(r"^\s*%\s*COVER_EXCLUSIVE\b", line):
                issues.append("invalid COVER_EXCLUSIVE declaration")
            continue
        ids = match.group(1).split()
        if len(ids) < 2 or len(set(ids)) != len(ids) or any(
                not re.fullmatch(_ID, ident) for ident in ids):
            issues.append("invalid COVER_EXCLUSIVE declaration")
        elif any(ident not in library for ident in ids):
            issues.append("unknown library ID in COVER_EXCLUSIVE: " + " ".join(ids))
        else:
            groups.append(ids)
    return groups


def _selected(source, issues):
    blocks, current, lines, offset = [], None, [], 0
    in_document = False
    for line in source.splitlines(keepends=True):
        code = re.split(r"(?<!\\)%", line, 1)[0]
        if r"\begin{document}" in code:
            in_document = True
        if r"\end{document}" in code:
            in_document = False
        if not in_document:
            offset += len(line)
            continue
        start = _USE_START.fullmatch(line.rstrip("\r\n"))
        comment = re.sub(r"^\s*%\s?", "", line).strip()
        if start:
            if current:
                issues.append("nested USE_COVER_TEXT block: " + current[0])
            current, lines = (start.group(1), offset), []
        elif comment == "END_USE_COVER_TEXT":
            if not current:
                issues.append("END_USE_COVER_TEXT without a start")
            else:
                ident, begin = current
                blocks.append((ident, "".join(lines), begin, offset + len(line)))
                current = None
        elif comment.startswith("USE_COVER_TEXT"):
            issues.append("invalid USE_COVER_TEXT declaration")
        elif current:
            lines.append(line)
        offset += len(line)
    if current:
        issues.append("unterminated USE_COVER_TEXT block: " + current[0])
    return blocks


def _commands(text, name, arguments=1):
    """Yield spans and balanced arguments for a supported literal command."""
    for match in re.finditer(r"\\" + name + r"(?![A-Za-z])", text):
        at, values = match.end(), []
        for _ in range(arguments):
            while at < len(text) and text[at].isspace():
                at += 1
            if at == len(text) or text[at] != "{":
                break
            start, depth = at + 1, 1
            at += 1
            while at < len(text) and depth:
                if text[at] == "\\":
                    at += 2  # Escaped braces do not open or close arguments.
                    continue
                depth += {"{": 1, "}": -1}.get(text[at], 0)
                at += 1
            if depth:
                break
            values.append(text[start:at - 1])
        if len(values) == arguments:
            yield match.start(), at, values


def _shape(source, selected, kinds, issues):
    # Replace whole marked blocks before stripping comments. Comments alone
    # cannot satisfy the text check or count as selected visible paragraphs.
    masked, last = [], 0
    for index, (_ident, _text, start, end) in enumerate(selected):
        masked.extend((source[last:start], "\\fixedcoverblock{%d}\n" % index))
        last = end
    masked.append(source[last:])
    code = _without_comments("".join(masked))
    begin, end = r"\begin{document}", r"\end{document}"
    if code.count(begin) != 1 or code.count(end) != 1:
        issues.append("fixed letter requires one document environment")
        return
    body = code.split(begin, 1)[1].split(end, 1)[0]
    tokens = list(_FIXED_TOKEN.finditer(body))
    if len(tokens) != len(selected):
        issues.append("selected blocks must appear inside the document body")
    starts = list(re.finditer(r"\\begin\{itemize\}(?:\[[^\]]*\])?", body))
    ends = list(re.finditer(r"\\end\{itemize\}", body))
    if len(starts) != 1 or len(ends) != 1 or starts[0].end() > ends[0].start():
        issues.append("fixed letter requires exactly one itemize list")
    else:
        for token in tokens:
            index = int(token.group(1))
            inside = starts[0].end() <= token.start() < ends[0].start()
            if index >= len(kinds) or inside != (kinds[index] == "highlight"):
                issues.append("only selected highlights may appear inside the itemize list")
    if re.search(r"\\item(?![A-Za-z])", body):
        issues.append("every list item must use an approved text block")

    paragraphs = list(_commands(body, "lettercontent"))
    if len(paragraphs) != 3:
        issues.append("exactly the salutation, role motivation and closing must be unmarked lettercontent")
    elif tokens:
        if (paragraphs[0][1] > tokens[0].start()
                or paragraphs[-1][0] < tokens[-1].end()):
            issues.append("salutation must precede, and closing must follow, fixed blocks")
        if not re.match(r"Dear\b", paragraphs[0][2][0].strip()):
            issues.append("the first unmarked lettercontent must be the salutation")
        if paragraphs[1][1] > tokens[0].start():
            issues.append("role motivation must precede the fixed opening")
        motivation = paragraphs[1][2][0].strip()
        if not motivation:
            issues.append("role motivation must be non-empty")
        if re.search(r"\[[A-Z][A-Z0-9 _.,/&()'-]*\]", motivation):
            issues.append("role motivation must not contain unresolved placeholders")
        if starts and ends and any(starts[0].end() <= start < ends[0].start()
                                   for start, _finish, _values in paragraphs):
            issues.append("custom paragraphs must remain outside the highlight list")

    # Consume the known formal header/footer and layout commands. Anything
    # else left in the body would be new, unmarked prose or an unsupported
    # wrapper rather than one of the three permitted custom paragraphs.
    spans = [(start, finish) for start, finish, _values in paragraphs]
    for name, nargs in (("senderblock", 2), ("recipientblock", 1),
                        ("currentdate", 1), ("subjectline", 1),
                        ("closing", 1), ("signature", 1), ("vspace", 1)):
        commands = list(_commands(body, name, nargs))
        if name != "vspace" and len(commands) > 1:
            issues.append("duplicate formal letter command: " + name)
        for start, finish, _ in commands:
            if len(paragraphs) == 3 and name != "vspace":
                if name in ("closing", "signature"):
                    correctly_placed = start >= paragraphs[-1][1]
                else:
                    correctly_placed = finish <= paragraphs[0][0]
                if not correctly_placed:
                    issues.append("formal letter command outside its header/footer: " + name)
            spans.append((start, finish))
    spans.extend((m.start(), m.end()) for m in starts + ends + tokens)
    chars = list(body)
    for start, finish in spans:
        chars[start:finish] = " " * (finish - start)
    residual = re.sub(r"\\(?:raggedright|letterbodyfont|par|noindent)(?![A-Za-z])", "", "".join(chars))
    if re.sub(r"[\s{}]", "", residual):
        issues.append("unmarked text or unsupported commands remain in the letter body")


def validate_fixed_blocks(source_text, base_text):
    """Return enabled/selected/issues; no declaration means a legacy opt-out."""
    result = {"enabled": bool(_DECLARATION.search(base_text)), "selected": [], "issues": []}
    if not result["enabled"]:
        return result
    issues = result["issues"]
    library = _library(base_text, issues)
    exclusive_groups = _exclusive_groups(base_text, library, issues)
    selected = _selected(source_text, issues)
    kinds, seen = [], set()
    for ident, text, _start, _end in selected:
        result["selected"].append(ident)
        if ident in seen:
            issues.append("duplicate selected ID: " + ident)
        seen.add(ident)
        if ident not in library:
            issues.append("unknown selected ID: " + ident)
            kinds.append("unknown")
            continue
        kind, expected = library[ident]
        kinds.append(kind)
        code = _without_comments(text).strip()
        pattern = (r"\\item(?![A-Za-z])\s+(.+)" if kind == "highlight"
                   else r"\\lettercontent\s*\{([^{}]*)\}")
        match = re.fullmatch(pattern, code, re.S)
        if not match:
            issues.append("invalid %s wrapper: %s" % (kind, ident))
        elif _normalise(match.group(1)) != expected:
            issues.append("approved text was changed: " + ident)
    if kinds.count("opening") != 1 or not 2 <= kinds.count("highlight") <= 3 or kinds.count("research") > 1:
        issues.append("select one opening, two or three highlights, and at most one research sentence")
    order = {"opening": 0, "highlight": 1, "research": 2, "unknown": 3}
    if kinds != sorted(kinds, key=order.get):
        issues.append("fixed blocks must follow opening, highlights, research order")
    for group in exclusive_groups:
        together = [ident for ident in group if ident in seen]
        if len(together) > 1:
            issues.append("mutually exclusive blocks selected: " + ", ".join(together))
    _shape(source_text, selected, kinds, issues)
    return result


def validate_layout(source_text, base_text):
    """Preserve the opted-in base's preamble, including geometry and fonts.

    The stock fixed library declares its approved geometry explicitly. Older
    bases without that declaration retain their previous layout behaviour.
    Comparing the supported preamble avoids pretending to interpret arbitrary
    TeX while also detecting added font or margin overrides.
    """
    base = _without_comments(base_text)
    geometries = list(_commands(base, "geometry"))
    result = {"enabled": bool(_DECLARATION.search(base_text) and geometries), "issues": []}
    if not result["enabled"]:
        return result
    source = _without_comments(source_text)
    begin = r"\begin{document}"
    if len(geometries) != 1 or begin not in base:
        result["issues"].append("canonical cover base requires one declared geometry and a document")
        return result
    # Whitespace and comments may vary, but every executable preamble command
    # must stay intact. Layout is maintained in the base, never per employer.
    base_preamble = re.sub(r"\s+", "", base.split(begin, 1)[0])
    source_preamble = re.sub(r"\s+", "", source.split(begin, 1)[0])
    if source_preamble != base_preamble:
        result["issues"].append(
            "approved cover preamble changed; preserve base geometry, fonts and layout commands")
    body = source.split(begin, 1)[-1]
    if re.search(r"\\(?:geometry|newgeometry|restoregeometry|loadgeometry)(?![A-Za-z])", body):
        result["issues"].append("cover geometry must not be overridden in the document body")
    base_body = base.split(begin, 1)[1]
    list_start = re.compile(r"\\begin\{itemize\}(?:\[([^\]]*)\])?")
    source_lists, base_lists = list(list_start.finditer(body)), list(list_start.finditer(base_body))
    list_options = lambda matches: [re.sub(r"\s+", "", match.group(1) or "")
                                    for match in matches]
    if list_options(source_lists) != list_options(base_lists):
        result["issues"].append("itemize options must match the approved base spacing")
    spacing = lambda text: [re.sub(r"\s+", "", text[start:end])
                            for start, end, _ in _commands(text, r"vspace\*?")]
    if spacing(body) != spacing(base_body):
        result["issues"].append("vertical spacing commands must match the approved base")
    # The class supplies body typography through lettercontent. Lists live
    # outside that macro, so their explicit group/font wrapper is essential.
    list_end = re.compile(r"\\end\{itemize\}")
    ends = list(list_end.finditer(body))
    if len(source_lists) != 1 or len(ends) != 1 or not (
            re.search(r"\{\s*\\raggedright\s*\\letterbodyfont\s*$", body[:source_lists[0].start()])
            and re.match(r"\s*\\par\s*\}", body[ends[0].end():])):
        result["issues"].append("itemize must retain the approved raggedright/letterbodyfont group")
    return result
