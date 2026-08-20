#!/usr/bin/env python3
"""Decide whether the browser-header curl retry in 09-web-research.md may run.

The retry exists to get past bot-filtering firewalls on sites whose robots.txt
permits access. It is never used to override a site that has said no.

WebFetch identifies itself as Claude-User and honors robots.txt, so a 403 has
two very different causes: a WAF default on a site whose published policy
allows access, or a site that has actually declined. This tells them apart.

Rules implemented (RFC 9309), deliberately on the cautious side:
  * longest-match wins; on equal specificity Disallow wins
  * a Disallow for either "*" or "Claude-User" blocks the retry
  * blank lines inside a record do not end it (Python's robotparser drops
    rules in that case, which fails open - see tests)
  * 404 means no published policy, which is permission
  * any other failure to read robots.txt leaves permission unconfirmed,
    and the retry does not happen

Usage:  python3 tools/robots_check.py <url>
Exit 0 = the retry may proceed. Exit 1 = do not retry; go to escalation step 3.
"""

import re, sys
from pathlib import Path
from urllib.parse import urlsplit, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from board import fetch_url  # noqa: E402

BROWSER = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36')

def _fetch(url, ua):
    """Fetch a robots.txt through the validated transport.

    This used to shell out to `curl -sS -L --max-redirs 5`. That was adequate
    while this ran once, by hand, against a URL a human had just looked at. It
    is not adequate as the gate on a batch that walks dozens of arbitrary
    careers domains: curl was given no `--proto` restriction, no public-address
    check, and no per-hop validation, so a public domain could answer
    `/robots.txt` with a redirect to `http://169.254.169.254/...` or an RFC1918
    address and this would follow it. The permission check would then be the
    most attacker-reachable request the tool makes - which is the wrong thing
    for a safety gate to be.

    `board/fetch_url.fetch` already solves exactly this: https-only, every hop
    re-resolved and required to be public, the connection pinned to the address
    that was validated, at most 3 redirects, and one absolute deadline for the
    whole operation.

    The `curl` note this replaced recorded that some hosts (jobup.ch) hang
    urllib indefinitely while answering curl in under a second. `fetch_url`
    does not inherit that: it re-applies the remaining budget before every
    blocking read and reads with `read1`, so a dribbling or silent server
    converges on the deadline instead of parking on it.
    """
    try:
        _final, code, body = fetch_url.fetch(
            url, max_redirects=3, timeout=12, max_bytes=512 * 1024)
    except fetch_url.Refused as exc:
        # A policy refusal is not a robots answer. Returning a body here would
        # let a redirect into private space read as "allowed".
        raise RuntimeError('refused: %s' % exc)
    return body, code


def is_robots_body(text):
    """Does this actually look like a robots.txt?

    A misconfigured host can answer /robots.txt with 200 and an HTML error page.
    That body parses to zero rules, and zero rules read as "allowed" - so a
    soft-200 granted permission that was never given. An empty or whitespace-only
    body IS a valid allow-all under RFC 9309 and stays allowed; a non-empty body
    with no recognised directive is treated as unreadable.
    """
    if not text.strip():
        return True
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip().lower()
        if ':' in line and line.split(':', 1)[0].strip() in (
            'user-agent', 'allow', 'disallow', 'sitemap', 'crawl-delay', 'host',
        ):
            return True
    return False

def _groups(text):
    """user-agent -> [(is_allow, pattern)], tolerating blank lines inside a record."""
    out, agents, expect = {}, [], True
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or ':' not in line:
            continue
        field, _, value = line.partition(':')
        field, value = field.strip().lower(), value.strip()
        if field == 'user-agent':
            if not expect:
                agents, expect = [], True
            agents.append(value.lower())
            out.setdefault(value.lower(), [])
        elif field in ('allow', 'disallow') and agents:
            expect = False
            for a in agents:
                out[a].append((field == 'allow', value))
    return out

def _match(pattern, path):
    """RFC 9309 wildcard match; returns match length or -1.

    The pattern is percent-decoded to match the already-decoded path. Without
    this, "Disallow: /foo%20bar" never matched "/foo bar" and the rule was
    silently skipped - a fail-open on any site that encodes its own rules.
    """
    if pattern == '':
        return -1
    pattern = unquote(pattern)
    rx = '^' + ''.join('.*' if c == '*' else ('$' if c == '$' else re.escape(c)) for c in pattern)
    return len(pattern) if re.match(rx, path) else -1

def allowed(text, agent, path):
    g = _groups(text)
    rules = g.get(agent.lower()) or g.get('*') or []
    best_len, best_allow = -1, True
    for is_allow, pat in rules:
        n = _match(pat, path)
        if n > best_len or (n == best_len and n >= 0 and not is_allow):
            best_len, best_allow = n, is_allow      # ties -> Disallow wins (cautious)
    return True if best_len < 0 else best_allow

NO_ROBOTS = 'no robots.txt published'


def gate(url, cache=None):
    """Does robots.txt permit fetching this URL? (exit_code, message).

    `cache` is an optional dict the *caller* owns, holding one robots.txt per
    host for the life of one batch. A backfill walks many rows from a few hosts
    - 36 of one run's 45 candidates were LinkedIn - and asking a host 36 times
    for the same file to be told "no" 36 times is wasteful and rude.

    It is a parameter rather than a module global on purpose: a global would
    leak between unrelated callers and would go stale in a long-lived process
    like the board server, where robots.txt should be re-read. Passing None -
    the default, and what every one-shot caller does - fetches every time,
    exactly as before.

    Only the *file* is cached. The path is evaluated on every call, because
    robots rules are path-specific and caching a verdict would answer the wrong
    question.
    """
    parts = urlsplit(url)
    path = unquote(parts.path) or '/'
    if parts.query:
        path += '?' + parts.query
    robots = f'{parts.scheme}://{parts.netloc}/robots.txt'
    if cache is not None and robots in cache:
        body, last = cache[robots]
        if body is None and last == NO_ROBOTS:
            return 0, 'ALLOWED - ' + NO_ROBOTS
    else:
        body, last = None, 'no attempt'
        for ua in ('Claude-User', BROWSER):
            try:
                text, code = _fetch(robots, ua)
            except Exception as e:
                last = type(e).__name__; continue
            if code == 404:
                if cache is not None:
                    cache[robots] = (None, NO_ROBOTS)
                return 0, 'ALLOWED - ' + NO_ROBOTS
            if code == 200:
                if not is_robots_body(text):
                    last = 'HTTP 200 but the body is not a robots.txt'
                    continue
                body = text; break
            last = 'HTTP %d' % code
        if cache is not None:
            cache[robots] = (body, last)
    if body is None:
        return 1, 'UNCONFIRMED (%s) - do not retry, go to step 3' % last
    for a in ('Claude-User', '*'):
        if not allowed(body, a, path):
            return 1, f'DISALLOWED for {a} - do not retry, go to step 3'
    return 0, 'ALLOWED - robots.txt permits this path'

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('usage: python3 tools/robots_check.py <url>', file=sys.stderr)
        sys.exit(2)
    rc, msg = gate(sys.argv[1])
    print(msg)
    sys.exit(rc)
