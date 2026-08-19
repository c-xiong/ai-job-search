"""`tools/board/fetch_url.py` is the only shell command a model run can reach.

Which makes its refusals the interesting part. A posting URL is attacker-supplied
data: it comes off a job board, it can redirect anywhere, and the process running
this script sits inside the owner's network. Every test here is a refusal that
has to keep working.

Nothing here touches the network. `open_connection` is the seam - the same seam
the pinning fix introduced - so the tests can assert *which address* the code
would have connected to, which is the whole point of that fix.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from board import fetch_url  # noqa: E402


def _addrinfo(*addresses):
    return [(2, 1, 6, "", (address, 443)) for address in addresses]


class FakeResponse:
    """A stream, not a buffer: `read(n)` consumes, the way a socket does.

    That distinction is the point of the chunked-read fix - a `read()` that keeps
    handing back the same bytes would make the loop look wrong when it is right.
    """

    def __init__(self, status=200, headers=None, body=b"hello"):
        self.status = status
        self._headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self._body = body
        self._pos = 0

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, n=None):
        end = len(self._body) if n is None else min(len(self._body), self._pos + n)
        chunk = self._body[self._pos:end]
        self._pos = end
        return chunk

    def read1(self, n):
        """One receive: a real socket hands back whatever arrived, not `n`."""
        self.read1_calls = getattr(self, "read1_calls", 0) + 1
        return self.read(min(n, 1024))


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class FakeConnection:
    """Records what the code would have dialled, and answers canned responses.

    It models the one `http.client` behaviour this code has to get right: we send
    `Connection: close`, so `getresponse()` hands the socket to the response and
    sets `connection.sock = None`. A fake that kept `sock` alive would let a
    body-read retiming bug pass unnoticed - which is exactly what happened.
    """

    def __init__(self, log, responses):
        self.log = log
        self.responses = responses
        self.closed = False
        self.sock = FakeSocket()
        self.transport = self.sock

    def request(self, method, target, headers=None):
        self.log.append({"method": method, "target": target,
                         "host": (headers or {}).get("Host")})

    def getresponse(self):
        self.sock = None          # as `Connection: close` makes http.client do
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class ConnectionCase(unittest.TestCase):
    def dial(self, responses, addresses=("93.184.216.34",), url="https://example.com/job",
             **resolve):
        """Patch the seam. Returns (calls, dialled) where dialled is the list of
        (hostname, ip) pairs the code would actually have connected to."""
        calls, dialled = [], []
        self.connections = []

        def opener(parts, ip, timeout):
            dialled.append((parts.hostname, ip))
            conn = FakeConnection(calls, responses)
            self.connections.append(conn)
            return conn

        resolver = resolve.get("resolver") or (lambda *a, **k: _addrinfo(*addresses))
        patches = mock.patch.multiple(fetch_url, open_connection=opener)
        with patches, mock.patch("socket.getaddrinfo", side_effect=resolver):
            self.result = None
            self.error = None
            try:
                self.result = fetch_url.fetch(url)
            except fetch_url.Refused as exc:
                self.error = exc
        return calls, dialled


class SchemeTest(unittest.TestCase):
    def test_only_https(self):
        for url in ("http://example.com", "file:///etc/passwd", "ftp://example.com/x",
                    "data:text/html,hi", "example.com"):
            with self.assertRaises(fetch_url.Refused, msg=url):
                fetch_url.check_url(url)

    def test_credentials_in_the_url_are_refused(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(fetch_url.Refused):
                fetch_url.check_url("https://user:pw@example.com/x")


class AddressPolicyTest(unittest.TestCase):
    def test_private_and_local_addresses_are_refused(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.9", "172.16.4.4",
                        "169.254.169.254", "100.64.0.1", "::1", "fd00::1", "0.0.0.0"):
            with mock.patch("socket.getaddrinfo", return_value=_addrinfo(address)):
                with self.assertRaises(fetch_url.Refused, msg=address):
                    fetch_url.check_public_host("anything.test")

    def test_a_public_address_passes(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            self.assertEqual(fetch_url.check_public_host("example.com"), ["93.184.216.34"])

    def test_one_private_answer_among_several_is_still_a_refusal(self):
        """A name that resolves to both a public and a private address is the
        DNS-rebinding shape; the public answer does not redeem it."""
        with mock.patch("socket.getaddrinfo",
                        return_value=_addrinfo("93.184.216.34", "127.0.0.1")):
            with self.assertRaises(fetch_url.Refused):
                fetch_url.check_public_host("split.test")

    def test_a_name_that_does_not_resolve_is_refused_not_attempted(self):
        import socket
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaises(fetch_url.Refused):
                fetch_url.check_public_host("nowhere.invalid")


class PinningTest(ConnectionCase):
    def test_the_connection_goes_to_the_address_that_was_validated(self):
        """The rebinding fix, stated as a test: the address the policy approved
        is the address dialled, not a second lookup's answer."""
        calls, dialled = self.dial([FakeResponse()], addresses=("93.184.216.34",))
        self.assertEqual(dialled, [("example.com", "93.184.216.34")])
        # TLS still authenticates the hostname, so pinning costs no verification.
        self.assertEqual(calls[0]["host"], "example.com")

    def test_the_pinned_connection_class_keeps_sni_on_the_hostname(self):
        conn = fetch_url.PinnedHTTPSConnection("example.com", 443, "93.184.216.34",
                                               10, mock.Mock())
        self.assertEqual(conn.host, "example.com")
        self.assertEqual(conn.pinned_ip, "93.184.216.34")

        captured = {}

        def create_connection(address, timeout):
            captured["address"] = address
            return mock.sentinel.sock

        conn._context.wrap_socket.side_effect = \
            lambda sock, server_hostname: captured.setdefault("sni", server_hostname)
        with mock.patch("socket.create_connection", side_effect=create_connection):
            conn.connect()
        self.assertEqual(captured["address"], ("93.184.216.34", 443))
        self.assertEqual(captured["sni"], "example.com")

    def test_the_real_context_still_verifies_certificates(self):
        """Pinning the address must not be a back door into a weakened TLS
        setup. This asserts the context `open_connection()` actually builds -
        the previous test's mock context would pass with CERT_NONE."""
        import ssl
        from urllib.parse import urlparse
        conn = fetch_url.open_connection(urlparse("https://example.com/x"),
                                         "93.184.216.34", 10)
        self.assertEqual(conn._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(conn._context.check_hostname)
        self.assertEqual(conn.host, "example.com")
        self.assertEqual(conn.port, 443)
        conn.close()

    def test_an_explicit_port_is_carried_into_the_connection_and_the_host_header(self):
        from urllib.parse import urlparse
        conn = fetch_url.open_connection(urlparse("https://example.com:8443/x"),
                                         "93.184.216.34", 10)
        self.assertEqual(conn.port, 8443)
        conn.close()

        calls, dialled = self.dial([FakeResponse()], url="https://example.com:8443/job")
        self.assertEqual(calls[0]["host"], "example.com:8443")


class RedirectTest(ConnectionCase):
    def test_every_hop_is_revalidated(self):
        """A public URL that 302s to the cloud metadata endpoint must be refused
        at the second hop, not followed because the first one was fine."""
        def resolver(host, *a, **kw):
            return _addrinfo("93.184.216.34" if host == "example.com" else "169.254.169.254")

        calls, dialled = self.dial(
            [FakeResponse(302, {"Location": "https://metadata.test/latest"})],
            resolver=resolver)
        self.assertIsNotNone(self.error)
        self.assertIn("non-public", str(self.error))
        self.assertEqual(dialled, [("example.com", "93.184.216.34")])

    def test_a_redirect_chain_is_bounded(self):
        responses = [FakeResponse(302, {"Location": "https://example.com/hop%d" % n})
                     for n in range(10)]
        calls, dialled = self.dial(responses)
        self.assertIn("redirects", str(self.error))
        self.assertEqual(len(dialled), fetch_url.MAX_REDIRECTS + 1)

    def test_a_redirect_loop_is_caught(self):
        responses = [FakeResponse(302, {"Location": "https://example.com/job"})]
        calls, dialled = self.dial(responses)
        self.assertIn("loop", str(self.error))

    def test_a_redirect_without_a_location_is_refused(self):
        calls, dialled = self.dial([FakeResponse(302, {})])
        self.assertIn("no Location", str(self.error))


class CrossPortRedirectTest(ConnectionCase):
    def test_a_redirect_to_another_port_is_re_validated_and_dialled_there(self):
        calls, dialled = self.dial(
            [FakeResponse(302, {"Location": "https://example.com:8443/moved"}),
             FakeResponse(200, None, b"moved body")])
        self.assertEqual(self.result[2], "moved body")
        self.assertEqual(dialled, [("example.com", "93.184.216.34"),
                                   ("example.com", "93.184.216.34")])
        self.assertEqual([c["host"] for c in calls], ["example.com", "example.com:8443"])

    def test_a_redirect_to_credentials_is_refused(self):
        calls, dialled = self.dial(
            [FakeResponse(302, {"Location": "https://user:pw@example.com/x"})])
        self.assertIn("credentials", str(self.error))

    def test_a_redirect_to_http_is_refused(self):
        calls, dialled = self.dial([FakeResponse(302, {"Location": "http://example.com/x"})])
        self.assertIn("only https", str(self.error))


class BudgetTest(ConnectionCase):
    def test_an_oversized_body_is_refused_rather_than_truncated(self):
        big = b"x" * (fetch_url.MAX_BYTES + 10)
        calls, dialled = self.dial([FakeResponse(200, None, big)])
        self.assertIn("exceeds", str(self.error))

    def test_the_deadline_covers_the_whole_chain_not_each_hop(self):
        """Four slow hops with a per-request timeout is a hundred-second fetch
        inside a run that has its own wall clock."""
        clock = [0.0]

        def opener(parts, ip, timeout):
            clock[0] += 20            # each hop burns 20s of the 25s budget
            return FakeConnection([], [FakeResponse(302, {"Location": "https://example.com/n%d"
                                                          % clock[0]})])

        with mock.patch.object(fetch_url, "open_connection", side_effect=opener), \
                mock.patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")), \
                mock.patch("time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(fetch_url.Refused) as caught:
                fetch_url.fetch("https://example.com/job")
        self.assertIn("gave up after 25s", str(caught.exception))


class DeadlineTest(ConnectionCase):
    def test_the_socket_timeout_is_re_derived_before_every_blocking_read(self):
        """`http.client` sets the socket timeout once, and hands every later
        blocking call a fresh copy of that same interval - so slow headers
        followed by a slow body can spend the budget twice."""
        self.dial([FakeResponse(200, None, b"y" * 200000)])
        # `.transport`, not `.sock`: the connection surrenders `sock` in
        # `getresponse()`, so a retiming that goes through the connection sets
        # nothing at all during the body read.
        self.assertIsNone(self.connections[0].sock)
        timeouts = self.connections[0].transport.timeouts
        self.assertGreater(len(timeouts), 2, "the deadline was applied once, not per read")
        # Monotonically non-increasing: each one is what is left, not a reset.
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))
        self.assertLessEqual(timeouts[0], fetch_url.TIMEOUT)

    def test_a_deadline_that_has_passed_refuses_rather_than_reading(self):
        import time as _time
        with self.assertRaises(fetch_url.Refused) as caught:
            fetch_url._retime(FakeSocket(), _time.monotonic() - 1)
        self.assertIn("gave up after", str(caught.exception))

    def test_the_body_is_read_in_bounded_chunks(self):
        """One `read()` of the whole body would make the deadline unobservable
        until it had already been exceeded."""
        body = b"z" * (fetch_url.CHUNK * 3 + 7)
        self.dial([FakeResponse(200, None, body)])
        self.assertEqual(self.result[2], body.decode())
        self.assertGreaterEqual(len(self.connections[0].transport.timeouts), 4)


class DribbleTest(ConnectionCase):
    def test_the_body_is_read_one_receive_at_a_time(self):
        """`read()` loops internally until it has n bytes, so a server sending a
        byte at a time keeps every receive inside the socket timeout and the
        deadline is never re-checked. `read1` puts the loop back in charge."""
        response = FakeResponse(200, None, b"q" * 5000)
        self.dial([response])
        self.assertEqual(self.result[2], "q" * 5000)
        self.assertGreaterEqual(response.read1_calls, 5,
                                "the body was not read one receive at a time")
        # One deadline application per receive, so a stalled body is noticed.
        self.assertGreaterEqual(len(self.connections[0].transport.timeouts),
                                response.read1_calls)

    def test_a_hard_alarm_bounds_what_the_loop_cannot(self):
        """Header reads happen inside `http.client` with no re-entry point of
        ours, so the graceful checks cannot cover them."""
        import signal
        self.assertTrue(hasattr(signal, "SIGALRM"))
        with self.assertRaises(fetch_url.Refused) as caught:
            fetch_url._expired(signal.SIGALRM, None)
        self.assertIn("hard timeout", str(caught.exception))


class SuccessTest(ConnectionCase):
    def test_a_plain_200_returns_the_body(self):
        self.dial([FakeResponse(200, {"Content-Type": "text/html; charset=utf-8"}, b"posting")])
        url, status, text = self.result
        self.assertEqual((status, text), (200, "posting"))

    def test_a_declared_charset_is_honoured(self):
        self.dial([FakeResponse(200, {"Content-Type": "text/html; charset=iso-8859-1"},
                                b"caf\xe9")])
        self.assertEqual(self.result[2], "café")


if __name__ == "__main__":
    unittest.main()
