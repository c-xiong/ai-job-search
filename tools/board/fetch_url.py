#!/usr/bin/env python3
"""The one command a headless `/apply` run is allowed to shell out to.

    python3 tools/board/fetch_url.py https://example.com/jobs/123

`09-web-research.md`'s 403 fallback tells the workflow to retry a posting with
browser headers when `WebFetch` is refused - most corporate and bank sites reject
WebFetch's user agent while serving the page normally to a browser. In a terminal
that is `curl`. Allowing `curl` inside a model run means allowing an argv, and an
argv is a shell: `curl -o`, `curl --config`, `curl file://`. So the fallback
becomes this script instead, and the run's Bash permission is one fixed shape
with exactly one variable argument (`tools/board/guard_write.py` re-checks it).

The bounds, all of them refusals rather than truncations where truncating would
hide something:

* `https://` only - no `http`, no `file`, no `ftp`, no `data`.
* Every hop's host must resolve to a **public** address, and **the connection is
  then made to the address that was checked**. Resolving, approving, and then
  letting the HTTP library resolve again is the DNS-rebinding hole: the same name
  answers `93.184.216.34` to the check and `127.0.0.1` to the connect, and the
  posting URL becomes a way to make this machine fetch its own board or its
  cloud metadata endpoint. TLS still verifies the certificate against the real
  hostname, so pinning the address costs no authentication.
* At most 3 redirects, each one re-resolved and re-validated.
* 2 MB, and **25 seconds for the whole operation** rather than per hop - four
  slow hops with a per-request timeout is a hundred-second fetch inside a run
  that has its own wall clock.
* The body is printed to stdout as text; nothing is written to disk.

Exit codes: 0 body printed, 2 refused by policy, 3 transport or HTTP error.

Stdlib only, Python 3.9+.
"""

import argparse
import http.client
import ipaddress
import re
import signal
import socket
import ssl
import sys
import time
from urllib.parse import urljoin, urlparse

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT = 25
REDIRECT_CODES = (301, 302, 303, 307, 308)
# The deadline is re-applied between chunks, so a chunk is also the
# granularity at which a slow body is noticed. 16 KB is small enough to
# check often and large enough not to syscall per kilobyte.
CHUNK = 16 * 1024

# The headers a real browser sends. This is the entire point of the script:
# the same URL that returns 403 to a bare user agent returns 200 to this one.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "close",
}

CHARSET = re.compile(r"charset=([\w.-]+)", re.I)


class Refused(Exception):
    """Policy said no. Distinct from a transport failure, and reported as such."""


def _expired(signum, frame):
    raise Refused("hard timeout: the fetch exceeded %ds of wall clock" % (TIMEOUT + 5))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the address we validated, authenticate the name we asked for.

    `socket.create_connection` on the pinned IP is what closes the gap between
    the policy check and the connect; `server_hostname` keeps certificate
    verification pointed at the real hostname, so nothing about TLS is weakened.
    """

    def __init__(self, host, port, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self.pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def check_public_host(host):
    """Every address `host` resolves to must be public. Returns them."""
    if not host:
        raise Refused("no host in URL")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise Refused("cannot resolve %s: %s" % (host, exc))
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise Refused("%s resolves to nothing" % host)
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            raise Refused("%s resolves to an unparseable address %r" % (host, raw))
        if not address.is_global or address.is_multicast:
            raise Refused("%s resolves to the non-public address %s - refusing to fetch "
                          "anything behind this machine's own network boundary"
                          % (host, address))
    return addresses


def check_url(url):
    """Parse, require https, require a public host. Returns (parts, addresses)."""
    parts = urlparse(url)
    if parts.scheme != "https":
        raise Refused("only https:// URLs are fetched, got %r" % (parts.scheme or "no scheme"))
    if parts.username or parts.password:
        raise Refused("credentials in the URL are refused")
    return parts, check_public_host(parts.hostname)


def open_connection(parts, ip, timeout):
    """Overridable seam: the tests replace this rather than the network."""
    return PinnedHTTPSConnection(parts.hostname, parts.port or 443, ip, timeout,
                                 ssl.create_default_context())


def _read1(response, size):
    """One receive at most. Falls back to `read` where `read1` is unavailable."""
    reader = getattr(response, "read1", None)
    return reader(size) if callable(reader) else response.read(size)


def _retime(transport, deadline, timeout=TIMEOUT):
    """Point the socket at the absolute deadline, not at a stale interval.

    `http.client` sets the socket timeout once, from the value the connection was
    created with, and every blocking operation after that gets a *fresh* copy of
    that whole interval - so 24 seconds spent on headers leaves another 25
    available for the body.

    **`transport` is the socket, captured before `getresponse()`, not the
    connection.** We send `Connection: close`, so `getresponse()` hands the
    socket to the response object and sets `connection.sock = None`; retiming
    through the connection during the body read is a guaranteed no-op, not an
    edge case.

    Re-applying the *remaining* budget each time is also what bounds a dribbling
    server. `read()` can perform many receives internally, each one bounded by
    the socket timeout - but that timeout shrinks monotonically toward zero as
    the deadline approaches, so the receives fail progressively faster and the
    next call raises here instead of returning. It converges; a fixed per-receive
    timeout does not.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Refused("gave up after %ds" % timeout)
    if transport is not None and hasattr(transport, "settimeout"):
        try:
            transport.settimeout(remaining)
        except OSError:
            pass
    return remaining


def fetch(url, max_redirects=MAX_REDIRECTS, timeout=TIMEOUT, max_bytes=MAX_BYTES):
    """(final_url, status, text). Raises `Refused`, or a transport error."""
    deadline = time.monotonic() + timeout
    seen = []
    for _ in range(max_redirects + 1):
        if url in seen:
            raise Refused("redirect loop at %s" % url)
        seen.append(url)
        if deadline - time.monotonic() <= 0:
            raise Refused("gave up after %ds across %d hop(s)" % (timeout, len(seen)))

        parts, addresses = check_url(url)

        # Checked again *after* resolving: `getaddrinfo` takes no timeout of its
        # own, so a stalled resolver is time this deadline cannot prevent - only
        # notice. It can still overshoot by one system resolver timeout, which is
        # the honest limit and is why it is checked on both sides.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refused("gave up after %ds - resolving %s took the rest of the budget"
                          % (timeout, parts.hostname))

        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection = open_connection(parts, addresses[0], remaining)
        try:
            connection.request("GET", target, headers=dict(HEADERS, Host=parts.netloc))
            # Captured now. `Connection: close` means `getresponse()` gives the
            # socket to the response and clears it here, so this reference is the
            # only handle on the transport the body is actually read through.
            transport = getattr(connection, "sock", None)
            _retime(transport, deadline, timeout)
            response = connection.getresponse()
            if response.status in REDIRECT_CODES:
                location = response.getheader("Location")
                if not location:
                    raise Refused("HTTP %d with no Location header" % response.status)
                url = urljoin(url, location)
                continue
            # Read in chunks against the same deadline. A single `read()` is
            # bounded by the socket timeout *per packet*, so a server that
            # dribbles one byte at a time under that timeout can hold the
            # connection open indefinitely without ever tripping it.
            chunks, total = [], 0
            while total <= max_bytes:
                if deadline - time.monotonic() <= 0:
                    raise Refused("gave up after %ds while reading the response body"
                                  % timeout)
                # The socket's timeout was computed when the connection opened.
                # Left alone, a slow set of headers followed by a slow body can
                # spend the full budget twice - so it is re-derived from the
                # absolute deadline before every blocking read.
                _retime(transport, deadline, timeout)
                # `read1`, not `read`: `read()` loops internally until it has n
                # bytes, and a server sending one byte at a time keeps every one
                # of those receives inside the socket timeout, so the deadline is
                # never re-checked. `read1` performs at most one receive, which
                # puts the loop - and therefore the deadline - back in charge.
                chunk = _read1(response, min(CHUNK, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            body = b"".join(chunks)
            if len(body) > max_bytes:
                raise Refused("response exceeds %d bytes - refusing to print a truncated "
                              "posting, since a truncated posting drafted from is worse "
                              "than none" % max_bytes)
            match = CHARSET.search(response.getheader("Content-Type") or "")
            charset = match.group(1) if match else "utf-8"
            return url, response.status, body.decode(charset, "replace")
        finally:
            connection.close()
    raise Refused("more than %d redirects" % max_redirects)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fetch one https URL with browser headers. The only command a "
                    "board-driven /apply run may shell out to.")
    parser.add_argument("url")
    args = parser.parse_args(argv)

    # The deadline checks inside `fetch()` bound every loop this code controls.
    # They cannot bound `getresponse()`, which reads headers inside `http.client`
    # with no re-entry point of ours - a server dribbling one header byte per
    # second stays under the socket timeout indefinitely. This script is its own
    # process, so `SIGALRM` is available and is a real ceiling rather than a
    # hopeful one. The grace is for the graceful path to report first.
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _expired)
        signal.alarm(TIMEOUT + 5)

    try:
        final_url, status, text = fetch(args.url)
    except Refused as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print("fetch failed: %s" % exc, file=sys.stderr)
        return 3

    if status >= 400:
        print("HTTP %s from %s" % (status, final_url), file=sys.stderr)
        sys.stdout.write(text)
        return 3

    # The final URL goes to stderr so stdout stays exactly the body: the model
    # is reading a posting, not a report about a posting.
    print("fetched %s (HTTP %s, %d chars)" % (final_url, status, len(text)), file=sys.stderr)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
