"""Offline collection budgets, failure observability and public-feed parsing."""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tools import collectors


def silent(_message):
    pass


class KeywordPlanTest(unittest.TestCase):
    def test_shared_budget_rotates_over_sources_queries_and_pages(self):
        cfg = {"linkedin": [{"q": "Engineer %d" % n, "l": "Remote", "pages": 2} for n in range(9)],
               "freehire": [{"q": "Research %d" % n, "pages": 2} for n in range(5)],
               "keyword_search_budget": 999}
        first = collectors.CollectionSession(cfg, ["linkedin", "freehire"])
        self.assertEqual(len(first.selected), 12)
        self.assertEqual({t["source"] for t in first.selected}, {"linkedin", "freehire"})
        second = collectors.CollectionSession(cfg, ["linkedin", "freehire"], first.persisted_state("2026-10-02"))
        self.assertNotEqual([t["key"] for t in first.selected], [t["key"] for t in second.selected])
        reached = {t["key"] for t in first.selected + second.selected}
        self.assertEqual(len(reached), 24)

    def test_source_subsets_keep_other_query_rotation_and_checkpoints(self):
        cfg = {"linkedin": [{"q": "Engineer %d" % n, "l": "Remote"} for n in range(10)],
               "freehire": [{"q": "Data %d" % n} for n in range(6)]}
        both = collectors.CollectionSession(cfg, ["linkedin", "freehire"])
        both.outcome("freehire", "saved-query", 1, [])
        state = both.persisted_state("2026-10-02")
        single = collectors.CollectionSession(cfg, ["linkedin"], state)
        state = single.persisted_state("2026-10-02")
        ats_only = collectors.CollectionSession(cfg, ["ats"], state)
        state = ats_only.persisted_state("2026-10-02")
        resumed = collectors.CollectionSession(cfg, ["linkedin", "freehire"], state)
        self.assertEqual(resumed.start, both.next_cursor)
        self.assertIn("saved-query", state["queries"])

    def test_collectors_share_twelve_calls_and_preserve_seven_day_window(self):
        cfg = {"linkedin": [{"q": "Engineer %d" % n, "l": "Remote", "pages": 3} for n in range(7)],
               "freehire": [{"q": "Scientist %d" % n, "city": "Berlin"} for n in range(5)]}
        calls = []
        def cli(args, _log):
            calls.append(args)
            return {"results": []}
        with collectors.CollectionSession(cfg, ["linkedin", "freehire"]) as session, patch.object(collectors, "bun", cli):
            collectors.collect_linkedin(cfg, silent)
            collectors.collect_freehire(cfg, silent)
        self.assertEqual(len(calls), 12)
        self.assertTrue(all(args[args.index("--jobage") + 1] == "7" for args in calls))
        self.assertTrue(all(args[args.index("--sort") + 1] == "date" for args in calls if args[0] == collectors.LINKEDIN))
        self.assertGreater(session.summary("linkedin")["deferred_queries"], 0)

    def test_partial_failure_is_not_empty_success_and_does_not_advance_success_checkpoint(self):
        cfg = {"linkedin": [{"q": "Engineer", "l": "Remote", "pages": 2}]}
        good = {"results": [{"id": "9000001", "title": "Engineer", "company": "Example Employer"}]}
        with collectors.CollectionSession(cfg, ["linkedin"]) as session, patch.object(collectors, "bun", side_effect=[good, None]):
            rows = collectors.collect_linkedin(cfg, silent)
        self.assertEqual(len(rows), 1)
        self.assertTrue(session.summary("linkedin")["degraded"])
        state = session.persisted_state("2026-10-02T12:00:00")
        self.assertIn("last_persisted_at", state["queries"][session.selected[0]["key"]])
        self.assertNotIn("last_persisted_at", state["queries"][session.selected[1]["key"]])

    def test_rate_limit_stops_remaining_linkedin_search_pages(self):
        cfg = {"linkedin": [{"q": "Engineer", "l": "Remote", "pages": 3}]}
        def cli(_args, _log):
            collectors.COLLECTION.last_error = "RATE_LIMITED HTTP 429"
            return None
        with collectors.CollectionSession(cfg, ["linkedin"]) as session, patch.object(collectors, "bun", side_effect=cli) as mocked:
            collectors.collect_linkedin(cfg, silent)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(session.summary("linkedin")["deferred_queries"], 2)


class ArbeitnowTest(unittest.TestCase):
    def setUp(self):
        policy = patch.object(collectors, "arbeitnow_access", return_value=(0, "ALLOWED fixture"))
        policy.start()
        self.addCleanup(policy.stop)
    @staticmethod
    def record(slug="example-role"):
        return {"slug": slug, "title": "Data Engineer", "company_name": "Example Employer",
                "location": "Berlin", "url": "https://careers.example.test/jobs/" + slug,
                "created_at": int(datetime.now(timezone.utc).timestamp()),
                "description": "<p>Build pipelines &amp; tools.</p><ul><li>Python</li></ul>", "remote": False}

    @staticmethod
    def response(payload):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _limit): return json.dumps(payload).encode()
        return Response()

    def test_pages_are_bounded_and_full_descriptions_and_posting_dates_are_retained(self):
        cfg = {"arbeitnow": {"enabled": True, "max_pages": 100, "include_titles": ["Engineer"], "locations": ["Berlin"]}}
        payload = {"data": [self.record()], "links": {"next": "https://untrusted.example.test/next"}}
        with collectors.CollectionSession(cfg, ["arbeitnow"]) as session, patch.object(collectors.urllib.request, "urlopen", return_value=self.response(payload)) as mocked:
            found = collectors.collect_arbeitnow(cfg, silent)
        self.assertEqual(mocked.call_count, 3)
        self.assertTrue(all(call.args[0].full_url.startswith(collectors.ARBEITNOW) for call in mocked.call_args_list))
        row = next(iter(found.values()))
        self.assertEqual(row["id"], "arbeitnow:example-role")
        self.assertIn("Build pipelines & tools.", row["description"])
        self.assertNotIn("<p>", row["description"])
        self.assertEqual(len(row["posted"]), 10)
        self.assertFalse(session.summary("arbeitnow")["degraded"])

    def test_partial_api_failure_preserves_first_page(self):
        payload = {"data": [self.record()], "links": {"next": "yes"}}
        cfg = {"arbeitnow": {"enabled": True, "max_pages": 2}}
        with collectors.CollectionSession(cfg, ["arbeitnow"]) as session, patch.object(collectors.urllib.request, "urlopen", side_effect=[self.response(payload), OSError("fixture unavailable")]):
            found = collectors.collect_arbeitnow(cfg, silent)
        self.assertEqual(len(found), 1)
        self.assertTrue(session.summary("arbeitnow")["degraded"])

    def test_unconfirmed_policy_makes_no_api_request(self):
        cfg = {"arbeitnow": {"enabled": True}}
        with collectors.CollectionSession(cfg, ["arbeitnow"]) as session, \
             patch.object(collectors, "arbeitnow_access", return_value=(1, "UNCONFIRMED fixture")), \
             patch.object(collectors.urllib.request, "urlopen", side_effect=AssertionError("policy gate")):
            self.assertEqual(collectors.collect_arbeitnow(cfg, silent), {})
        self.assertTrue(session.summary("arbeitnow")["degraded"])

    def test_remote_flag_does_not_remove_explicit_geographic_restrictions(self):
        restricted = dict(self.record("restricted"), location="United States", remote=True)
        cfg = {"arbeitnow": {"enabled": True, "locations": ["Berlin"], "include_remote": True}}
        with patch.object(collectors.urllib.request, "urlopen", return_value=self.response({"data": [restricted]})):
            self.assertEqual(collectors.collect_arbeitnow(cfg, silent), {})

    def test_unknown_dates_and_locations_are_retained_but_stale_known_dates_are_filtered(self):
        unknown = dict(self.record("unknown"), location="", created_at=None)
        stale = dict(self.record("stale"), created_at=1)
        payload = {"data": [unknown, stale], "links": {"next": None}}
        cfg = {"arbeitnow": {"enabled": True, "locations": ["Berlin"]}}
        with patch.object(collectors.urllib.request, "urlopen", return_value=self.response(payload)):
            found = collectors.collect_arbeitnow(cfg, silent)
        self.assertEqual(len(found), 1)
        self.assertEqual(next(iter(found.values()))["posted"], "")


if __name__ == "__main__":
    unittest.main()
