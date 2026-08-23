import json
import sys
import tempfile
import unittest
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from board import companies, server as board_server  # noqa: E402


class CompaniesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        self.registry = home / "job_scraper" / "companies.json"
        self.registry.parent.mkdir()
        self.registry.write_text(json.dumps({
            "_comment": "keep me", "defaults": {"cadence_days": {"2": 1}},
            "companies": [{"name": "Acme", "domain": "acme.test", "tier": 2,
                           "status": "ambiguous", "route": "ats",
                           "candidates": [{"vendor": "ashby", "token": "acme"}]}]
        }), encoding="utf-8")
        patches = [mock.patch.object(companies, "ROOT", home),
                   mock.patch.object(companies, "REGISTRY", self.registry),
                   mock.patch.object(companies, "LOCK", home / "job_scraper" / ".companies.lock")]
        for patcher in patches:
            patcher.start(); self.addCleanup(patcher.stop)

    def test_listing_uses_string_mtime_and_keeps_raw_fields(self):
        result = companies.listing()
        self.assertIsInstance(result["mtime"], str)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["companies"][0]["candidates"][0]["vendor"], "ashby")
        self.assertEqual(result["companies"][0]["monitoring_status"], "needs_confirmation")
        self.assertFalse(result["companies"][0]["will_be_searched"])

    def test_stale_edit_is_409_and_does_not_change_bytes(self):
        before = self.registry.read_bytes()
        with self.assertRaises(companies.CompanyError) as caught:
            companies.patch_company("acme", {"mtime": "1", "changes": {"countries": ["CH"]}})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_a_successful_ui_write_keeps_the_previous_registry_as_a_backup(self):
        before = json.loads(self.registry.read_text(encoding="utf-8"))
        companies.patch_company("acme", {"mtime": companies.listing()["mtime"],
                                          "changes": {"countries": ["CH"]}})
        backup = self.registry.with_name("companies.backup.json")
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), before)

    def test_ui_cannot_edit_resolver_owned_identity(self):
        with self.assertRaises(companies.CompanyError):
            companies.patch_company("acme", {"mtime": companies.listing()["mtime"],
                                               "changes": {"vendor": "lever"}})

    def test_confirm_must_use_current_candidate(self):
        mtime = companies.listing()["mtime"]
        result = companies.identity("acme", {"mtime": mtime, "decision": "confirm",
                                               "candidate": {"vendor": "ashby", "token": "acme"}})
        row = result["companies"][0]
        self.assertEqual((row["status"], row["identity"]["method"]),
                         ("verified", "human_confirmed"))
        self.assertNotIn("candidates", row)
        self.assertEqual(row["cadence_days"], 1)

    def test_add_normalizes_domain_and_resolves_exact_name_with_six_probes(self):
        seen = []
        def fake_bun(argv, _log):
            seen.append(argv); return {"ok": True}
        with mock.patch.object(companies.collectors, "bun", side_effect=fake_bun):
            result = companies.add({"mtime": companies.listing()["mtime"],
                                    "name": "New Co", "careers_url": "https://www.new.co/work-with-us",
                                    "countries": ["CH", "DE"]})
        row = next(r for r in result["companies"] if r["name"] == "New Co")
        self.assertEqual(row["domain"], "new.co")
        self.assertEqual(row["careers_url"], "https://www.new.co/work-with-us")
        self.assertEqual(row["countries"], ["CH", "DE"])
        self.assertEqual(seen[0][seen[0].index("--company") + 1], "New Co")
        self.assertEqual(seen[0][seen[0].index("--max-probes") + 1], "6")

    def test_add_recognizes_direct_ats_url_without_a_slug_probe(self):
        with mock.patch.object(companies.collectors, "bun") as bun:
            result = companies.add({"mtime": companies.listing()["mtime"],
                                    "name": "Direct Co",
                                    "careers_url": "https://jobs.ashbyhq.com/directco",
                                    "countries": ["DE"]})
        row = next(r for r in result["companies"] if r["name"] == "Direct Co")
        self.assertEqual((row["vendor"], row["token"], row["status"]),
                         ("ashby", "directco", "verified"))
        self.assertEqual(row["identity"]["evidence_kind"], "human_confirmed")
        self.assertNotIn("domain", row)
        bun.assert_not_called()

    def test_country_scope_is_only_switzerland_germany_or_both(self):
        with self.assertRaises(companies.CompanyError):
            companies.add({"mtime": companies.listing()["mtime"], "name": "Nope",
                           "careers_url": "https://nope.test/careers", "countries": ["AT"]})

    def test_changing_careers_url_clears_old_identity_and_fetch_bookkeeping(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        row = data["companies"][0]
        row.update({"careers_url": "https://jobs.ashbyhq.com/old", "status": "verified",
                    "vendor": "ashby", "token": "old", "identity": {"evidence_kind": "human_confirmed"},
                    "last_attempt_at": "2026-08-01T00:00:00Z", "last_success_at": "2026-08-01T00:00:00Z",
                    "last_status": "ok", "stats": {"jobs_seen": 20, "eligible_jobs": 4}})
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        result = companies.patch_company("acme", {
            "mtime": companies.listing()["mtime"],
            "changes": {"careers_url": "https://acme.test/new-careers"}})
        updated = result["companies"][0]
        self.assertEqual(updated["status"], "unresolved")
        self.assertEqual(updated["domain"], "acme.test")
        for field in ("vendor", "token", "identity", "last_attempt_at", "last_success_at", "last_status"):
            self.assertNotIn(field, updated)
        self.assertEqual(updated["stats"]["last_jobs_seen"], None)

    def test_changing_to_direct_smartrecruiters_url_rebuilds_identity_but_policy_is_visible(self):
        result = companies.patch_company("acme", {
            "mtime": companies.listing()["mtime"],
            "changes": {"careers_url": "https://careers.smartrecruiters.com/Acme"}})
        updated = result["companies"][0]
        self.assertEqual((updated["status"], updated["vendor"], updated["token"]),
                         ("verified", "smartrecruiters", "Acme"))
        self.assertEqual(updated["monitoring_status"], "policy_disabled")
        self.assertFalse(updated["will_be_searched"])

    def test_save_and_inspect_rechecks_an_unchanged_unresolved_careers_url(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["companies"][0].update({"status": "unresolved",
                                     "careers_url": "https://acme.test/careers"})
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(companies.collectors, "bun",
                               return_value={"meta": {"status_counts": {"unresolved": 1}}}) as bun:
            companies.patch_company("acme", {
                "mtime": companies.listing()["mtime"], "inspect": True,
                "changes": {"careers_url": "https://acme.test/careers"}})
        self.assertEqual(bun.call_count, 1)
        self.assertIn("resolve", bun.call_args.args[0])

    def test_fetch_uses_real_search_command_and_returns_last_run_counts(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["companies"][0].update({"status": "verified", "vendor": "ashby", "token": "acme",
                                     "identity": {"evidence_kind": "human_confirmed"}})
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        payload = {"meta": {"generated_at": "2026-08-23T10:00:00Z", "companies": [{
            "name": "Acme", "status": "ok", "message": "ok", "jobs_seen": 12,
            "eligible": 3, "requests": 1}]}, "results": [{"title": "Engineer", "url": "https://x"}]}
        with mock.patch.object(companies.collectors, "bun", return_value=payload) as bun:
            result = companies.test_fetch("acme", {"mtime": companies.listing()["mtime"]})
        argv = bun.call_args.args[0]
        self.assertEqual(argv[argv.index("--company") + 1], "Acme")
        self.assertIn("--force-refresh", argv)
        self.assertEqual(result["test_fetch"]["eligible_jobs"], 3)
        self.assertEqual(result["test_fetch"]["sample_jobs"][0]["title"], "Engineer")

    def test_health_check_batches_only_companies_that_will_be_searched(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["companies"] = [
            {"name": "Ready", "tier": 2, "route": "ats", "status": "verified",
             "vendor": "ashby", "token": "ready", "identity": {"evidence_kind": "human_confirmed"}},
            {"name": "Unresolved", "tier": 2, "route": "ats", "status": "unresolved"},
            {"name": "Paused", "tier": 2, "route": "ats", "status": "paused"},
        ]
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        payload = {"meta": {"generated_at": "2026-08-23T10:00:00Z", "companies": [
            {"name": "Ready", "status": "empty", "jobs_seen": 0, "eligible": 0}]}, "results": []}
        with mock.patch.object(companies.collectors, "bun", return_value=payload) as bun:
            result = companies.health_check({"mtime": companies.listing()["mtime"]})
        argv = bun.call_args.args[0]
        self.assertEqual(argv.count("--company"), 1)
        self.assertEqual(argv[argv.index("--company") + 1], "Ready")
        self.assertEqual(result["health_check"], {
            "tested": 1, "succeeded": 1, "failed": 0,
            "generated_at": "2026-08-23T10:00:00Z",
            "reports": payload["meta"]["companies"]})

    def test_resolve_all_batches_only_not_connected_ats_companies(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["companies"].extend([
            {"name": "Find Me", "domain": "find.test", "tier": 3,
             "status": "unresolved", "route": "ats"},
            {"name": "Already Ready", "tier": 3, "status": "verified", "route": "ats"},
            {"name": "Manual Co", "tier": 3, "status": "unresolved", "route": "manual"},
            {"name": "Paused Co", "tier": 3, "status": "paused", "route": "ats"},
        ])
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(companies.collectors, "bun",
                               return_value={"meta": {"status_counts": {"verified": 1}}}) as bun, \
                mock.patch.object(companies.activity, "emit") as emit:
            companies.resolve_all({"mtime": companies.listing()["mtime"]})
        argv = bun.call_args.args[0]
        self.assertEqual(bun.call_count, 1)
        self.assertEqual(argv.count("--company"), 1)
        self.assertEqual(argv[argv.index("--company") + 1], "Find Me")
        self.assertIn("--max-companies", argv)
        messages = [call.args[1] for call in emit.call_args_list]
        self.assertTrue(any("started" in message for message in messages))
        self.assertTrue(any("1 connected" in message for message in messages))

    def test_resolve_all_rejects_a_duplicate_batch_while_one_is_running(self):
        companies.RESOLVE_LOCK.acquire()
        self.addCleanup(companies.RESOLVE_LOCK.release)
        with self.assertRaises(companies.CompanyError) as caught:
            companies.resolve_all({"mtime": companies.listing()["mtime"]})
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("already running", str(caught.exception))


class CompaniesHttpTest(CompaniesTest):
    def setUp(self):
        super().setUp()
        self.server = board_server.ThreadingHTTPServer(("127.0.0.1", 0), board_server.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token=board_server.TOKEN):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        if token is not None:
            url += ("&" if "?" in url else "?") + "t=" + token
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_all_company_routes_require_the_token(self):
        self.assertEqual(self.request("GET", "/api/companies", token=None)[0], 403)
        self.assertEqual(self.request("PATCH", "/api/companies/acme",
                                      {"mtime": "x", "changes": {"countries": ["CH"]}},
                                      token=None)[0], 403)
        self.assertEqual(self.request("POST", "/api/companies/acme/test-fetch",
                                      {"mtime": "x"}, token=None)[0], 403)
        self.assertEqual(self.request("POST", "/api/companies/health-check",
                                      {"mtime": "x"}, token=None)[0], 403)

    def test_get_and_patch_surface_stale_mtime(self):
        status, body = self.request("GET", "/api/companies")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["mtime"], str)
        status, body = self.request("PATCH", "/api/companies/acme",
                                    {"mtime": "stale", "changes": {"countries": ["CH"]}})
        self.assertEqual(status, 409)
        self.assertIn("changed", body["error"])

    def test_resolve_all_http_endpoint_runs_the_batch_resolver(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["companies"].append({"name": "Find Me", "domain": "find.test", "tier": 3,
                                  "status": "unresolved", "route": "ats"})
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        mtime = companies.listing()["mtime"]
        with mock.patch.object(companies.collectors, "bun",
                               return_value={"meta": {"status_counts": {"verified": 1}}}) as bun:
            status, body = self.request("POST", "/api/companies/resolve-all", {"mtime": mtime})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["meta"]["status_counts"]["verified"], 1)
        self.assertEqual(bun.call_count, 1)

    def test_test_fetch_http_endpoint_runs_the_real_company_search(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["companies"][0].update({"status": "verified", "vendor": "ashby", "token": "acme",
                                     "identity": {"evidence_kind": "human_confirmed"}})
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        payload = {"meta": {"companies": [{"name": "Acme", "status": "ok",
                    "jobs_seen": 5, "eligible": 2, "requests": 1}]}, "results": []}
        with mock.patch.object(companies.collectors, "bun", return_value=payload) as bun:
            status, body = self.request("POST", "/api/companies/acme/test-fetch",
                                        {"mtime": companies.listing()["mtime"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["test_fetch"]["jobs_seen"], 5)
        self.assertIn("search", bun.call_args.args[0])

    def test_health_check_http_endpoint_batches_fetchable_companies(self):
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        data["companies"][0].update({"status": "verified", "vendor": "ashby", "token": "acme",
                                     "identity": {"evidence_kind": "human_confirmed"}})
        self.registry.write_text(json.dumps(data), encoding="utf-8")
        payload = {"meta": {"companies": [{"name": "Acme", "status": "empty",
                    "jobs_seen": 0, "eligible": 0, "requests": 1}]}, "results": []}
        with mock.patch.object(companies.collectors, "bun", return_value=payload) as bun:
            status, body = self.request("POST", "/api/companies/health-check",
                                        {"mtime": companies.listing()["mtime"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["health_check"]["succeeded"], 1)
        self.assertEqual(bun.call_count, 1)


if __name__ == "__main__":
    unittest.main()
