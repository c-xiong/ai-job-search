"""The on-demand orchestrator and the board route that triggers it.

What these pin: a second click cannot double the request volume, LinkedIn's
per-run description budget actually bounds it, and an unauthenticated POST
cannot make this machine crawl five vendors.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tools import collectors, fetch_jobs, jobs_board


def silent(_message):
    pass


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
        status, note = collectors.screen(
            {"portal": "linkedin-search", "id": "123"}, silent, budget)
        self.assertEqual(status, "new")
        self.assertIn("detail budget", note)
        self.assertEqual(budget.deferred, 1)

    def test_a_record_that_already_has_its_text_costs_no_request(self):
        budget = collectors.Budget(0)
        status, _note = collectors.screen(
            {"portal": "freehire-search", "id": "x", "description": "Python and Go."},
            silent, budget)
        self.assertEqual(status, "new")
        self.assertEqual(budget.used, 0)
        self.assertEqual(budget.deferred, 0, "an inline description is not a deferred fetch")

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
        cls.server = jobs_board.ThreadingHTTPServer(("127.0.0.1", 0), jobs_board.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        jobs_board.FETCH.update({"running": False, "log": [], "sources": [],
                                 "error": None, "finished_at": None})
        # No test in this class may start a real collection: it would hit five
        # vendors and write to the developer's own job board. The worker is
        # replaced rather than `threading.Thread` - the server under test is a
        # ThreadingHTTPServer, so stubbing Thread itself deadlocks the requests.
        self.started = []
        started = self.started
        real_worker = jobs_board.fetch_worker

        def recording_worker(*args):
            started.append(args)
            with jobs_board.FETCH_LOCK:
                jobs_board.FETCH["running"] = False

        jobs_board.fetch_worker = recording_worker
        self.addCleanup(lambda: setattr(jobs_board, "fetch_worker", real_worker))
        self.addCleanup(lambda: jobs_board.FETCH.update({"running": False}))

    def post(self, path, body=None, token=jobs_board.TOKEN):
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
        self.assertFalse(jobs_board.FETCH["running"])

    def test_a_wrong_token_is_refused(self):
        status, _body = self.post("/api/fetch", {"sources": ["ats"]}, token="not-the-token")
        self.assertEqual(status, 403)
        self.assertFalse(jobs_board.FETCH["running"])

    def test_a_concurrent_fetch_is_refused_rather_than_doubled(self):
        jobs_board.FETCH["running"] = True
        status, body = self.post("/api/fetch", {"sources": ["ats"]})
        self.assertEqual(status, 409)
        self.assertIn("already running", body["error"])

    def test_an_unknown_source_is_rejected(self):
        status, body = self.post("/api/fetch", {"sources": ["ats", "monster"]})
        self.assertEqual(status, 400)
        self.assertIn("sources must be", body["error"])
        self.assertFalse(jobs_board.FETCH["running"])

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

    def get(self, path, token=jobs_board.TOKEN):
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
                       "/api/fetch/status", 'id="degraded"'):
            self.assertIn(marker, html, marker)
        self.assertNotIn("__TOKEN__", html)

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
        self.assertEqual(jobs_board.primary_url(entry), "https://jobs.ashbyhq.com/deepjudge/41c7")
        # And the row's own key is untouched - nothing is ever re-keyed.
        self.assertEqual(entry["url"], "https://www.linkedin.com/jobs/view/4451224579")

    def test_a_row_with_no_source_history_falls_back_to_its_url(self):
        self.assertEqual(jobs_board.primary_url({"url": "https://example.test/1"}),
                         "https://example.test/1")

    def test_a_primary_source_with_no_matching_entry_falls_back(self):
        entry = {"url": "https://example.test/1", "primary_source": "ats-search", "sources": []}
        self.assertEqual(jobs_board.primary_url(entry), "https://example.test/1")


if __name__ == "__main__":
    unittest.main()
