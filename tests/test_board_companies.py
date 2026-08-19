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
        self.assertEqual(result["companies"][0]["candidates"][0]["vendor"], "ashby")

    def test_stale_edit_is_409_and_does_not_change_bytes(self):
        before = self.registry.read_bytes()
        with self.assertRaises(companies.CompanyError) as caught:
            companies.patch_company("acme", {"mtime": "1", "changes": {"note": "x"}})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.registry.read_bytes(), before)

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
                                    "name": "New Co", "website": "https://www.new.co/jobs", "tier": 3})
        row = next(r for r in result["companies"] if r["name"] == "New Co")
        self.assertEqual(row["domain"], "new.co")
        self.assertEqual(seen[0][seen[0].index("--company") + 1], "New Co")
        self.assertEqual(seen[0][seen[0].index("--max-probes") + 1], "6")


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
                                      {"mtime": "x", "changes": {"note": "x"}},
                                      token=None)[0], 403)

    def test_get_and_patch_surface_stale_mtime(self):
        status, body = self.request("GET", "/api/companies")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["mtime"], str)
        status, body = self.request("PATCH", "/api/companies/acme",
                                    {"mtime": "stale", "changes": {"note": "x"}})
        self.assertEqual(status, 409)
        self.assertIn("changed", body["error"])


if __name__ == "__main__":
    unittest.main()
