"""The on-demand orchestrator and the board route that triggers it.

What these pin: a second click cannot double the request volume, LinkedIn's
per-run description budget actually bounds it, and an unauthenticated POST
cannot make this machine crawl five vendors.
"""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tools import collectors, fetch_jobs
from tools.board import server as board_server, state as board_state


def silent(_message):
    pass


# Every fetch in this module stubs `collect`; the detection step that runs before
# it must be stubbed too, or a test would run `resolve --due` against the real
# registry and the network. Tests that are about detection install their own.
_REAL_DETECT = fetch_jobs.ats_fetch.detect


def setUpModule():
    fetch_jobs.ats_fetch.detect = lambda _log, **_kw: {"checked": 0, "found": [], "ask": []}


def tearDownModule():
    fetch_jobs.ats_fetch.detect = _REAL_DETECT


class RunLockTest(unittest.TestCase):
    """Plan §2: a second click while a run is in flight is refused, not doubled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_path = Path(self.tmp.name) / ".fetch.lock"

    def test_a_second_lock_is_refused_while_the_first_is_held(self):
        with fetch_jobs.RunLock(self.lock_path):
            with self.assertRaises(fetch_jobs.AlreadyRunning):
                with fetch_jobs.RunLock(self.lock_path):
                    self.fail("two runs held the lock at once")

    def test_the_lock_is_released_even_when_the_run_raises(self):
        with self.assertRaises(RuntimeError):
            with fetch_jobs.RunLock(self.lock_path):
                raise RuntimeError("boom")
        # The property is "acquirable again", not "the file is gone" - with flock
        # the file is the anchor, and the lock is the flock on it.
        with fetch_jobs.RunLock(self.lock_path):
            pass

    def test_a_lock_file_left_behind_by_a_dead_process_does_not_wedge_the_button(self):
        # A killed run must not block every future click. The kernel drops its
        # flock when the process dies, so the leftover file is just a file.
        self.lock_path.write_text(json.dumps(
            {"pid": 999999, "started_at": "2020-01-01T00:00:00"}), encoding="utf-8")
        with fetch_jobs.RunLock(self.lock_path):
            self.assertEqual(json.loads(self.lock_path.read_text())["pid"], os.getpid())

    def test_a_lock_held_by_another_live_process_is_respected(self):
        """The race the previous design could not close: two processes, one lock."""
        import subprocess
        import sys as _sys
        holder = subprocess.Popen(
            [_sys.executable, "-c",
             "import fcntl,sys,time\n"
             "h=open(%r,'a+')\n"
             "fcntl.flock(h.fileno(), fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "time.sleep(30)\n" % str(self.lock_path)],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with self.assertRaises(fetch_jobs.AlreadyRunning):
                with fetch_jobs.RunLock(self.lock_path):
                    self.fail("two processes held the run lock at once")
        finally:
            holder.kill()
            holder.wait(timeout=10)
        # And once the holder is gone - killed, not shut down cleanly - the lock
        # is immediately available again. No staleness threshold to wait out.
        with fetch_jobs.RunLock(self.lock_path):
            pass


class ExplicitBoundsTest(unittest.TestCase):
    """An explicit bound beats config. The other order is a trap."""

    def test_an_explicit_value_wins_over_the_config_block(self):
        cfg = {"max_companies": 8}
        self.assertEqual(fetch_jobs._bound(1, cfg, "max_companies", 8), 1)

    def test_config_is_used_only_when_nothing_was_supplied(self):
        self.assertEqual(fetch_jobs._bound(None, {"max_companies": 12}, "max_companies", 8), 12)
        self.assertEqual(fetch_jobs._bound(None, {}, "max_companies", 8), 8)
        self.assertEqual(fetch_jobs._bound(None, {"max_companies": "many"}, "max_companies", 8), 8)

    def test_core_bounds_cannot_be_bypassed_from_terminal_or_config(self):
        self.assertEqual(fetch_jobs._bound(9999, {}, "max_companies", 8, 1, 20), 20)
        self.assertEqual(fetch_jobs._bound(None, {"max_new_jobs": 9999},
                                           "max_new_jobs", 40, 1, 200), 200)
        self.assertEqual(fetch_jobs._bound(0, {}, "max_companies", 8, 1, 20), 1)

    def test_the_ui_asking_for_one_company_gets_one(self):
        asked = {}
        real_collect = fetch_jobs.ats_fetch.collect

        def collect(_log, **kw):
            asked.update(kw)
            return [], {"companies": [], "requests": 0}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        jm = fetch_jobs.jobs_md
        saved = (jm.SEEN, jm.MD, jm.CSV_ACTIVE, jm.CSV_EXCLUDED,
                 fetch_jobs.CONFIG, fetch_jobs.LOG, fetch_jobs.LOCK, fetch_jobs.STATUS)
        jm.SEEN, jm.MD = root / "seen.json", root / "jobs.md"
        jm.CSV_ACTIVE, jm.CSV_EXCLUDED = root / "a.csv", root / "e.csv"
        fetch_jobs.CONFIG = root / "cfg.json"
        fetch_jobs.LOG, fetch_jobs.LOCK = root / "log", root / ".lock"
        fetch_jobs.STATUS = root / "status.json"
        fetch_jobs.CONFIG.write_text(json.dumps({"ats": {"max_companies": 8, "max_new_jobs": 40}}),
                                     encoding="utf-8")

        def restore():
            (jm.SEEN, jm.MD, jm.CSV_ACTIVE, jm.CSV_EXCLUDED, fetch_jobs.CONFIG,
             fetch_jobs.LOG, fetch_jobs.LOCK, fetch_jobs.STATUS) = saved
        self.addCleanup(restore)

        fetch_jobs.ats_fetch.collect = collect
        try:
            fetch_jobs.fetch(["ats"], max_companies=1, max_new_jobs=2, dry_run=True)
        finally:
            fetch_jobs.ats_fetch.collect = real_collect
        self.assertEqual(asked["max_companies"], 1)
        self.assertEqual(asked["max_new_jobs"], 2)


class BoardLockTest(unittest.TestCase):
    """The board lock has to hold across processes, not just across threads."""

    def test_the_lock_is_visible_to_another_process(self):
        import subprocess
        import sys as _sys
        jm = fetch_jobs.jobs_md
        with tempfile.TemporaryDirectory() as tmp:
            saved = jm.SEEN
            jm.SEEN = Path(tmp) / "seen_jobs.json"
            try:
                probe = (
                    "import fcntl,sys\n"
                    "h=open(%r,'a+')\n"
                    "try:\n"
                    "    fcntl.flock(h.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
                    "    print('free')\n"
                    "except OSError:\n"
                    "    print('held')\n" % str(jm.board_lock_path())
                )
                with jm.board_lock():
                    out = subprocess.run([_sys.executable, "-c", probe],
                                         capture_output=True, text=True, timeout=20)
                    self.assertEqual(out.stdout.strip(), "held",
                                     "another process could write while the lock was held")
                out = subprocess.run([_sys.executable, "-c", probe],
                                     capture_output=True, text=True, timeout=20)
                self.assertEqual(out.stdout.strip(), "free", "the lock was not released")
            finally:
                jm.SEEN = saved

    def test_the_lock_is_reentrant_within_one_process(self):
        jm = fetch_jobs.jobs_md
        with tempfile.TemporaryDirectory() as tmp:
            saved = jm.SEEN
            jm.SEEN = Path(tmp) / "seen_jobs.json"
            try:
                with jm.board_lock():
                    with jm.board_lock():
                        pass
            finally:
                jm.SEEN = saved


class DetailBudgetTest(unittest.TestCase):
    """Plan §2: the per-click LinkedIn cap is what actually bounds the burst."""

    def test_the_budget_stops_after_its_limit_and_counts_what_it_deferred(self):
        budget = collectors.Budget(2)
        self.assertTrue(budget.take())
        self.assertTrue(budget.take())
        self.assertFalse(budget.take())
        self.assertFalse(budget.take())
        self.assertEqual(budget.used, 2)
        self.assertEqual(budget.deferred, 2)

    def test_a_deferred_posting_is_stored_without_a_description_and_says_so(self):
        budget = collectors.Budget(0)
        status, note, text = collectors.screen(
            {"portal": "linkedin-search", "id": "123"}, silent, budget)
        self.assertEqual(status, "new")
        self.assertIn("detail budget", note)
        self.assertEqual(budget.deferred, 1)
        self.assertEqual(text, "", "a posting that was never fetched has no body to store")

    def test_a_record_that_already_has_its_text_costs_no_request(self):
        budget = collectors.Budget(0)
        status, _note, text = collectors.screen(
            {"portal": "freehire-search", "id": "x", "description": "Python and Go."},
            silent, budget)
        self.assertEqual(status, "new")
        self.assertEqual(budget.used, 0)
        self.assertEqual(budget.deferred, 0, "an inline description is not a deferred fetch")
        self.assertEqual(text, "Python and Go.",
                         "the inline body is handed back so merge can store it")

    def test_the_ceiling_cannot_be_raised_from_config(self):
        budget = fetch_jobs._detail_budget({"linkedin_max_detail_fetches": 500}, None)
        self.assertEqual(budget.limit, fetch_jobs.LINKEDIN_DETAIL_CEILING)
        self.assertEqual(fetch_jobs._detail_budget({}, 500).limit, fetch_jobs.LINKEDIN_DETAIL_CEILING)
        self.assertEqual(fetch_jobs._detail_budget({}, None).limit, fetch_jobs.LINKEDIN_DETAIL_DEFAULT)

    def test_a_nonsense_config_value_falls_back_to_the_default(self):
        self.assertEqual(fetch_jobs._detail_budget({"linkedin_max_detail_fetches": "lots"}, None).limit,
                         fetch_jobs.LINKEDIN_DETAIL_DEFAULT)


class HardenedCliRunnerTest(unittest.TestCase):
    """§10.3: a non-zero exit must not throw away a payload the CLI did print."""

    def test_stdout_is_kept_when_the_exit_code_is_non_zero(self):
        payload = {"meta": {"companies": [{"name": "X", "status": "not_found"}]}, "results": []}

        class Proc:
            returncode = 1
            stdout = json.dumps(payload)
            stderr = '{"error":"all companies failed","code":"ALL_COMPANIES_FAILED"}'

        original = collectors.subprocess.run
        collectors.subprocess.run = lambda *a, **k: Proc()
        try:
            got = collectors.bun(["x"], silent)
        finally:
            collectors.subprocess.run = original
        self.assertEqual(got, payload)

    def test_unparseable_stdout_is_none_rather_than_a_crash(self):
        class Proc:
            returncode = 0
            stdout = "<html>not json</html>"
            stderr = ""

        original = collectors.subprocess.run
        collectors.subprocess.run = lambda *a, **k: Proc()
        try:
            self.assertIsNone(collectors.bun(["x"], silent))
        finally:
            collectors.subprocess.run = original


class BoardFetchRouteTest(unittest.TestCase):
    """Plan test 28: the trigger is token-protected and single-flight."""

    @classmethod
    def setUpClass(cls):
        cls.server = board_server.ThreadingHTTPServer(("127.0.0.1", 0), board_server.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        board_server.FETCH.update({"running": False, "log": [], "sources": [],
                                 "error": None, "finished_at": None})
        # No test in this class may start a real collection: it would hit five
        # vendors and write to the developer's own job board. The worker is
        # replaced rather than `threading.Thread` - the server under test is a
        # ThreadingHTTPServer, so stubbing Thread itself deadlocks the requests.
        self.started = []
        started = self.started
        real_worker = board_server.fetch_worker

        def recording_worker(*args):
            started.append(args)
            with board_server.FETCH_LOCK:
                board_server.FETCH["running"] = False

        board_server.fetch_worker = recording_worker
        self.addCleanup(lambda: setattr(board_server, "fetch_worker", real_worker))
        self.addCleanup(lambda: board_server.FETCH.update({"running": False}))

    def post(self, path, body=None, token=board_server.TOKEN):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        if token is not None:
            url += "?t=" + token
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_an_unauthenticated_fetch_is_refused(self):
        # Without this, any web page you happen to have open could POST to
        # localhost and make your machine crawl five vendors.
        status, body = self.post("/api/fetch", {"sources": ["ats"]}, token=None)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        self.assertFalse(board_server.FETCH["running"])

    def test_a_wrong_token_is_refused(self):
        status, _body = self.post("/api/fetch", {"sources": ["ats"]}, token="not-the-token")
        self.assertEqual(status, 403)
        self.assertFalse(board_server.FETCH["running"])

    def test_a_concurrent_fetch_is_refused_rather_than_doubled(self):
        board_server.FETCH["running"] = True
        status, body = self.post("/api/fetch", {"sources": ["ats"]})
        self.assertEqual(status, 409)
        self.assertIn("already running", body["error"])

    def test_an_unknown_source_is_rejected(self):
        status, body = self.post("/api/fetch", {"sources": ["ats", "monster"]})
        self.assertEqual(status, 400)
        self.assertIn("sources must be", body["error"])
        self.assertFalse(board_server.FETCH["running"])

    def test_an_empty_source_list_is_rejected(self):
        # `[]` is falsy, so a plain `or SOURCES` turns "I deselected everything"
        # into "fetch everything" - the opposite of what was asked.
        status, body = self.post("/api/fetch", {"sources": []})
        self.assertEqual(status, 400)
        self.assertIn("at least one source", body["error"])
        self.assertEqual(self.started, [])

    def test_the_bounds_are_clamped_server_side(self):
        status, body = self.post("/api/fetch", {"sources": ["ats"], "max_companies": 9999,
                                                "linkedin_detail_fetches": 9999})
        self.assertEqual(status, 202)
        self.assertEqual(body["max_companies"], 20)
        self.assertEqual(body["linkedin_detail_fetches"], fetch_jobs.LINKEDIN_DETAIL_CEILING)
        self.assertEqual(self.started[0][0], ["ats"])  # (sources, max_companies, ...)

    def test_an_absent_source_list_means_every_source(self):
        status, body = self.post("/api/fetch", {})
        self.assertEqual(status, 202)
        self.assertEqual(body["sources"], list(fetch_jobs.SOURCES))

    def get(self, path, token=board_server.TOKEN):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        if token is not None:
            url += "?t=" + token
        try:
            with urllib.request.urlopen(url, timeout=5) as res:
                return res.status, res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_the_page_ships_the_fetch_control(self):
        status, html = self.get("/")
        self.assertEqual(status, 200)
        for marker in ('id="fetch"', 'id="s-ats"', 'id="s-freehire"', 'id="s-linkedin"',
                       'id="degraded"', 'src="/static/app.js"'):
            self.assertIn(marker, html, marker)
        self.assertNotIn("__TOKEN__", html)

    def test_the_script_is_served_and_still_drives_the_fetch_endpoints(self):
        """The controls moved to a file; the wiring has to have moved with them.

        Before M1 the page was one constant, so asserting `/api/fetch/status`
        appeared in the HTML covered both the markup and the polling loop. Now
        they are two files, and checking only the markup would pass while the
        button did nothing at all.
        """
        status, js = self.get("/static/app.js")
        self.assertEqual(status, 200)
        for marker in ("/api/fetch/status", "/api/fetch?t=", "/api/jobs?t=",
                       "/api/update?t=", "/api/activity?t="):
            self.assertIn(marker, js, marker)

    def test_static_assets_do_not_leak_the_source_tree(self):
        for path in ("/static/../server.py", "/static/nope.css"):
            status, _body = self.get(path)
            self.assertEqual(status, 404, path)

    def test_the_page_itself_needs_the_token(self):
        status, _body = self.get("/", token=None)
        self.assertEqual(status, 403)

    def test_the_status_endpoint_needs_the_token_too(self):
        url = "http://127.0.0.1:%d/api/fetch/status" % self.port
        try:
            with urllib.request.urlopen(url, timeout=5) as res:
                self.fail("status endpoint answered without a token: %s" % res.status)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 403)


class BoardTokenTest(unittest.TestCase):
    """The token has to outlive the process that minted it.

    It used to be `secrets.token_urlsafe(16)` at import time, so every restart
    invalidated every open tab - and the page did not say so, because a 403's
    body is valid JSON and the client read it as "no runs". The fix is a longer
    lived secret, not a weaker one, so these pin both halves: the same token
    comes back after a restart, and it is never readable by anyone else.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = board_server.TOKEN_FILE
        self.addCleanup(lambda: setattr(board_server, "TOKEN_FILE", self.saved))
        board_server.TOKEN_FILE = Path(self.tmp.name) / ".board-token"

    def test_a_restart_reuses_the_token_so_open_tabs_keep_working(self):
        first = board_server.load_token()
        self.assertEqual(first, board_server.load_token())
        self.assertTrue(board_server.TOKEN_FILE.exists())

    def test_the_token_file_is_not_readable_by_anyone_else(self):
        board_server.load_token()
        self.assertEqual(board_server.TOKEN_FILE.stat().st_mode & 0o077, 0)

    def test_new_token_rotates_and_persists_the_new_one(self):
        first = board_server.load_token()
        rotated = board_server.load_token(new=True)
        self.assertNotEqual(first, rotated)
        self.assertEqual(rotated, board_server.load_token())

    def test_a_damaged_token_file_is_replaced_rather_than_trusted(self):
        for junk in ("", "   \n", "short", "has spaces in it", "x" * 400):
            board_server.TOKEN_FILE.write_text(junk, encoding="utf-8")
            token = board_server.load_token()
            self.assertNotEqual(token, junk.strip())
            self.assertGreaterEqual(len(token), 16, junk)

    def test_an_unwritable_token_file_does_not_stop_the_board(self):
        """A board with no place to save its token still has to serve.

        Degrading to a per-process token costs you the open tabs; refusing to
        start costs you the board.
        """
        board_server.TOKEN_FILE = Path(self.tmp.name) / "missing" / "sub" / "x"
        board_server.TOKEN_FILE.parent.parent.mkdir()
        board_server.TOKEN_FILE.parent.write_text("not a directory", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            self.assertGreaterEqual(len(board_server.load_token()), 16)
        self.assertIn("stop working when it restarts", warning.getvalue())

    def test_the_client_shows_a_stale_link_instead_of_an_empty_board(self):
        """The bug this whole change exists for: 403 must not render as "no runs"."""
        js = (Path(board_server.STATIC) / "app.js").read_text(encoding="utf-8")
        html = (Path(board_server.STATIC) / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="stale"', html)
        self.assertIn("function checkAuth(response)", js)
        self.assertIn("response.status===403", js)
        # Every polling site has to consult it - one that does not is one that
        # silently empties the view it owns.
        for guarded in ('const response=await fetch("/api/runs?t="+T);if(!checkAuth(response)',
                        'const response=await fetch(url);if(!checkAuth(response)',
                        'const response=await fetch("/api/fetch/status?t="+T);if(!checkAuth(response)',
                        'const response=await fetch("/api/jobs?t="+T);checkAuth(response)'):
            self.assertIn(guarded, js, guarded)


class ActivitySeedTest(unittest.TestCase):
    """A restarted board shows the history that is on disk beside it.

    The ring is per process and used to start empty, so two boards - or one
    board either side of a restart - disagreed about what had happened, with
    `activity.jsonl` holding the answer the whole time.
    """

    def setUp(self):
        from tools.board import activity
        self.activity = activity
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = activity.LOG
        self.addCleanup(lambda: setattr(activity, "LOG", saved))
        self.addCleanup(activity.reset_for_tests)
        activity.LOG = Path(self.tmp.name) / "activity.jsonl"
        activity.reset_for_tests()

    def write(self, count, start=900):
        with open(self.activity.LOG, "w", encoding="utf-8") as handle:
            handle.write("{ this line was torn by a crash\n")
            for i in range(count):
                handle.write(json.dumps({
                    "epoch": "older", "seq": start + i, "source": "collect",
                    "level": "info", "ts": "2026-09-18T10:00:00",
                    "msg": "earlier event %d" % i}) + "\n")

    def test_prior_events_are_seeded_and_renumbered_into_this_epoch(self):
        """Their own seq numbers cannot be trusted: `since()` filters on seq, and
        a client polling past a stale counter would never see a live event."""
        self.write(3)
        self.assertEqual(self.activity.seed(), 3)
        self.activity.emit("server", "board listening")
        snapshot = self.activity.since()
        self.assertEqual([e["seq"] for e in snapshot["events"]], [1, 2, 3, 4])
        self.assertEqual(snapshot["counts"]["all"], 4)
        self.assertEqual([e.get("prior") for e in snapshot["events"]],
                         [True, True, True, None])
        live = self.activity.since(seq=3, epoch=snapshot["epoch"])
        self.assertEqual([e["msg"] for e in live["events"]], ["board listening"])

    def test_seeding_is_bounded_and_survives_a_missing_or_torn_log(self):
        self.write(self.activity.RING + 50)
        self.assertEqual(self.activity.seed(), self.activity.RING)
        self.activity.reset_for_tests()
        self.activity.LOG = Path(self.tmp.name) / "not-there.jsonl"
        self.assertEqual(self.activity.seed(), 0)

    def test_a_ring_that_already_has_events_is_never_seeded_twice(self):
        self.write(3)
        self.activity.emit("server", "board listening")
        self.assertEqual(self.activity.seed(), 0)


class LostUpdateTest(unittest.TestCase):
    """A status set while a fetch is running must survive the fetch.

    The board serves `/api/update` from the same process that runs the fetch in a
    background thread. If the fetch loads `seen_jobs.json` when it starts and
    writes that snapshot back a minute later, every status set in between is
    silently reverted - and "nothing already collected is ever overwritten" would
    be false for the one field you care about most.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        jm = fetch_jobs.jobs_md
        saved = (jm.SEEN, jm.MD, jm.CSV_ACTIVE, jm.CSV_EXCLUDED,
                 fetch_jobs.CONFIG, fetch_jobs.LOG, fetch_jobs.LOCK, fetch_jobs.STATUS)
        jm.SEEN, jm.MD = root / "seen_jobs.json", root / "jobs.md"
        jm.CSV_ACTIVE, jm.CSV_EXCLUDED = root / "a.csv", root / "e.csv"
        fetch_jobs.CONFIG = root / "scrape_config.json"
        fetch_jobs.LOG = root / "scrape.log"
        fetch_jobs.LOCK = root / ".fetch.lock"
        fetch_jobs.STATUS = root / "fetch_status.json"

        def restore():
            (jm.SEEN, jm.MD, jm.CSV_ACTIVE, jm.CSV_EXCLUDED, fetch_jobs.CONFIG,
             fetch_jobs.LOG, fetch_jobs.LOCK, fetch_jobs.STATUS) = saved
        self.addCleanup(restore)

        self.existing = "https://www.linkedin.com/jobs/view/4451224579"
        jm.save_seen({self.existing: {
            "title": "ML Engineer", "company": "Parloa", "location": "Berlin",
            "url": self.existing, "first_seen": "2026-08-10", "posted": "2026-08-10",
            "deadline": None, "fit": "", "status": "new", "portal": "linkedin-search",
            "user_status": "new", "user_note": "", "note": "",
        }})
        fetch_jobs.CONFIG.write_text(json.dumps({"ats": {"max_companies": 1, "max_new_jobs": 5}}),
                                     encoding="utf-8")

    def test_a_status_set_mid_run_is_not_reverted_by_the_fetch(self):
        jm = fetch_jobs.jobs_md
        existing = self.existing

        def slow_collect(_log, **_kw):
            # Stands in for the network half. While it runs, the user presses `s`
            # on a row: the board writes straight to seen_jobs.json.
            seen = json.loads(jm.SEEN.read_text(encoding="utf-8"))["seen"]
            seen[existing]["user_status"] = "star"
            seen[existing]["user_note"] = "worth applying"
            jm.save_seen(seen)
            return ([{"id": "greenhouse:parloa:1", "title": "Backend Engineer",
                      "company": "Parloa", "registry_company": "Parloa", "location": "Berlin",
                      "posted": "2026-08-17", "deadline": None,
                      "url": "https://job-boards.greenhouse.io/parloa/jobs/1",
                      "description": "Python.", "prefit_score": 55, "prefit_reasons": ["title"]}],
                    {"companies": [{"name": "Parloa", "status": "ok"}], "requests": 1})

        real_collect = fetch_jobs.ats_fetch.collect
        fetch_jobs.ats_fetch.collect = slow_collect
        try:
            summaries, _lines = fetch_jobs.fetch(["ats"])
        finally:
            fetch_jobs.ats_fetch.collect = real_collect

        seen = json.loads(jm.SEEN.read_text(encoding="utf-8"))["seen"]
        self.assertEqual(seen[existing]["user_status"], "star",
                         "the fetch reverted a status set while it was running")
        self.assertEqual(seen[existing]["user_note"], "worth applying")
        self.assertIn("https://job-boards.greenhouse.io/parloa/jobs/1", seen,
                      "and the new row still landed")
        self.assertEqual(summaries[0]["added"], 1)
        self.assertFalse(jm.MD.exists())
        self.assertFalse(jm.CSV_ACTIVE.exists())
        self.assertFalse(jm.CSV_EXCLUDED.exists())

    def test_the_run_lock_is_released_after_a_normal_run(self):
        def collect(_log, **_kw):
            return [], {"companies": [], "requests": 0}

        real_collect = fetch_jobs.ats_fetch.collect
        fetch_jobs.ats_fetch.collect = collect
        try:
            fetch_jobs.fetch(["ats"])
            fetch_jobs.fetch(["ats"])  # a second run must not be refused
        finally:
            fetch_jobs.ats_fetch.collect = real_collect
        self.assertFalse(fetch_jobs.LOCK.exists())


class DetectionStepTest(unittest.TestCase):
    """COMPANIES_PLAN §3.1: detection is retried before the ATS search, bounded,
    skipped by a dry run, and its finds are reported."""

    # A fetch writes the board state, its status file and its log: every one of
    # them must point into a temporary directory, never at the real board.
    setUp = LostUpdateTest.setUp

    def test_the_cli_call_is_the_bounded_retry_queue(self):
        calls = []

        def runner(args):
            calls.append(args)
            return {"results": [{"name": "Acme", "status": "verified"},
                                {"name": "Beta", "status": "ambiguous"},
                                {"name": "Gamma", "status": "unresolved"}]}

        result = _REAL_DETECT(silent, per_fetch=5, runner=runner)
        self.assertEqual(result, {"checked": 3, "found": ["Acme"], "ask": ["Beta"]})
        args = calls[0]
        self.assertIn("--due", args)
        self.assertEqual(args[args.index("--max-companies") + 1], "5")
        self.assertEqual(_REAL_DETECT(silent, per_fetch=0, runner=runner)["checked"], 0)
        self.assertEqual(len(calls), 1, "per_fetch 0 makes no call at all")

    def test_a_fetch_detects_first_and_a_dry_run_does_not(self):
        order = []
        real_collect = fetch_jobs.ats_fetch.collect
        fetch_jobs.ats_fetch.detect = lambda _log, **_kw: (
            order.append("detect") or {"checked": 1, "found": ["Acme"], "ask": []})
        fetch_jobs.ats_fetch.collect = lambda _log, **_kw: (
            order.append("collect") or ([], {"companies": [], "requests": 0}))
        try:
            summaries, _ = fetch_jobs.fetch(["ats"], dry_run=True)
            self.assertEqual(order, ["collect"])
            order.clear()
            summaries, _ = fetch_jobs.fetch(["ats"])
            self.assertEqual(order, ["detect", "collect"])
            self.assertEqual(summaries[0]["boards_found"], ["Acme"])
        finally:
            fetch_jobs.ats_fetch.collect = real_collect
            setUpModule()


class ArrivalStampTest(unittest.TestCase):
    """A fetch marks what it brought in, so the board can show you only that.

    `first_seen` is a date, and two fetches on one afternoon are one date. The
    run stamp is what separates them - it has to be identical across every
    source of one run, different between runs, and never rewritten on a row
    that was already there.
    """

    def _sandbox(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        jm, af = fetch_jobs.jobs_md, fetch_jobs.ats_fetch
        saved = (jm.SEEN, jm.MD, jm.CSV_ACTIVE, jm.CSV_EXCLUDED, fetch_jobs.CONFIG,
                 fetch_jobs.LOG, fetch_jobs.LOCK, fetch_jobs.STATUS, af.REGISTRY)
        jm.SEEN, jm.MD = root / "seen.json", root / "jobs.md"
        jm.CSV_ACTIVE, jm.CSV_EXCLUDED = root / "a.csv", root / "e.csv"
        fetch_jobs.CONFIG, fetch_jobs.LOG = root / "cfg.json", root / "log"
        fetch_jobs.LOCK, fetch_jobs.STATUS = root / ".lock", root / "status.json"
        af.REGISTRY = root / "companies.json"

        def restore():
            (jm.SEEN, jm.MD, jm.CSV_ACTIVE, jm.CSV_EXCLUDED, fetch_jobs.CONFIG,
             fetch_jobs.LOG, fetch_jobs.LOCK, fetch_jobs.STATUS, af.REGISTRY) = saved
        self.addCleanup(restore)
        return root

    def _clock(self):
        """A run stamp that moves on every call, so "one run, one stamp" is a
        claim about the code rather than about how fast the test machine is.
        Two real fetches are minutes apart; two in one test are microseconds."""
        ticks = iter("2026-09-18T10:0%d:00" % n for n in range(9))
        real = fetch_jobs.ats_fetch.run_stamp
        fetch_jobs.ats_fetch.run_stamp = lambda: next(ticks)
        self.addCleanup(lambda: setattr(fetch_jobs.ats_fetch, "run_stamp", real))

    @staticmethod
    def _run(rows):
        """One fetch over both an ATS source and a plain one, with stubbed collectors.

        Patched on `fetch_jobs`'s own module references: `tools/` is on the path
        twice over, so `tools.collectors` and the `collectors` this orchestrator
        imported are two module objects and patching the wrong one silently runs
        the real collector.
        """
        source = fetch_jobs.collectors
        real_collect, real_freehire = fetch_jobs.ats_fetch.collect, source.collect_freehire
        fetch_jobs.ats_fetch.collect = lambda _log, **_kw: (
            [r for r in rows if r["portal"] == "ats"], {"companies": [], "requests": 0})
        # The plain sources hand back a dict keyed by URL, already carrying the
        # listing's description - which is also what keeps the screen local.
        source.collect_freehire = lambda _cfg, _log: {
            r["url"]: dict(r, description="We are hiring an engineer. English speaking team.")
            for r in rows if r["portal"] == "freehire"}
        try:
            fetch_jobs.fetch(["ats", "freehire"])
        finally:
            fetch_jobs.ats_fetch.collect = real_collect
            source.collect_freehire = real_freehire

    @staticmethod
    def _row(n, portal):
        # Distinct titles and companies: two sources returning the same job in
        # one run is one row by design (§11.1), which would hide the thing these
        # tests are about.
        return {"url": "https://example.test/%s/%d" % (portal, n), "id": str(n),
                "title": "Machine Learning Engineer %d" % n, "company": "Example %d AG" % n,
                "location": "Zurich, Switzerland", "posted": "2026-01-0%d" % n,
                "portal": portal, "status": "new", "note": ""}

    def test_one_run_is_one_stamp_across_every_source(self):
        self._sandbox();self._clock()
        self._run([self._row(1, "ats"), self._row(2, "freehire")])
        seen = fetch_jobs.load_seen()
        stamps = {entry["first_seen_at"] for entry in seen.values()}
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(stamps), 1, "two sources in one fetch are still one batch")
        status = json.loads(fetch_jobs.STATUS.read_text(encoding="utf-8"))
        # The board reads this to decide what "latest fetch" means, so it has to
        # be the same value the rows carry - and it has to survive the run.
        self.assertEqual(status["started_at"], stamps.pop())
        self.assertTrue(status["finished_at"])

    def test_a_later_run_is_a_different_batch_and_never_restamps_the_old_one(self):
        self._sandbox();self._clock()
        self._run([self._row(1, "ats")])
        first = fetch_jobs.load_seen()["https://example.test/ats/1"]["first_seen_at"]
        # Same row again plus a new one: the fetcher re-sees rows constantly, and
        # a re-sighting that moved the arrival stamp would make every old row
        # look like it arrived today.
        self._run([self._row(1, "ats"), self._row(2, "ats")])
        seen = fetch_jobs.load_seen()
        self.assertEqual(seen["https://example.test/ats/1"]["first_seen_at"], first)
        self.assertNotEqual(seen["https://example.test/ats/2"]["first_seen_at"], first)
        status = json.loads(fetch_jobs.STATUS.read_text(encoding="utf-8"))
        self.assertEqual(status["started_at"],
                         seen["https://example.test/ats/2"]["first_seen_at"])

    def test_the_last_fetch_is_still_known_after_the_server_restarts(self):
        """The in-process copy starts empty; the file is what remembers.

        Without this, restarting the board would silently redefine "latest
        fetch" as "nothing", and the arrival dots would all go out.
        """
        root = self._sandbox()
        # The board reads its own `fetch_jobs`: `tools/` is on the path twice,
        # so the orchestrator this test drives and the one the server imported
        # are two module objects with two STATUS paths.
        served = board_server.fetch_jobs
        saved_path, saved_state = served.STATUS, dict(board_server.FETCH)
        served.STATUS = root / "status.json"
        served.STATUS.write_text(json.dumps({
            "running": False, "started_at": "2026-09-18T10:00:00",
            "finished_at": "2026-09-18T10:04:00", "log": [], "sources": [],
        }), encoding="utf-8")
        board_server.FETCH.update({"running": False, "log": [], "sources": [],
                                   "error": None, "finished_at": None})

        def restore():
            served.STATUS = saved_path
            board_server.FETCH.update(saved_state)
        self.addCleanup(restore)

        status = board_server.fetch_status()
        self.assertEqual(status["started_at"], "2026-09-18T10:00:00")
        self.assertEqual(status["finished_at"], "2026-09-18T10:04:00")

    def test_the_payload_carries_arrival_and_falls_back_for_older_rows(self):
        rows = board_state._shape([
            ("https://example.test/new", {"first_seen": "2026-09-01",
                                          "first_seen_at": "2026-09-01T14:30:00"}),
            ("https://example.test/old", {"first_seen": "2026-08-20"}),
        ])
        by_url = {row["url"]: row for row in rows}
        self.assertEqual(by_url["https://example.test/new"]["first_seen_at"],
                         "2026-09-01T14:30:00")
        # A row collected before stamping existed still sorts and filters, on the
        # date it does have. Both are ISO-8601, and a date is a prefix of a
        # timestamp, so one string comparison covers the mixed board.
        self.assertEqual(by_url["https://example.test/old"]["first_seen_at"], "2026-08-20")


class PrimaryUrlTest(unittest.TestCase):
    """The link you click is the first-party one, without re-keying the row."""

    def test_the_preferred_source_url_wins(self):
        entry = {
            "url": "https://www.linkedin.com/jobs/view/4451224579",
            "primary_source": "ats-search",
            "sources": [
                {"portal": "linkedin-search", "url": "https://www.linkedin.com/jobs/view/4451224579"},
                {"portal": "ats-search", "url": "https://jobs.ashbyhq.com/deepjudge/41c7"},
            ],
        }
        self.assertEqual(board_state.primary_url(entry), "https://jobs.ashbyhq.com/deepjudge/41c7")
        # And the row's own key is untouched - nothing is ever re-keyed.
        self.assertEqual(entry["url"], "https://www.linkedin.com/jobs/view/4451224579")

    def test_a_row_with_no_source_history_falls_back_to_its_url(self):
        self.assertEqual(board_state.primary_url({"url": "https://example.test/1"}),
                         "https://example.test/1")

    def test_a_primary_source_with_no_matching_entry_falls_back(self):
        entry = {"url": "https://example.test/1", "primary_source": "ats-search", "sources": []}
        self.assertEqual(board_state.primary_url(entry), "https://example.test/1")


if __name__ == "__main__":
    unittest.main()
