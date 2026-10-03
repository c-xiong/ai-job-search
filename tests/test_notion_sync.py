"""Notion sync: field ownership, derived fields, and the tracker cache.

The HTTP layer is replaced by an in-memory fake; nothing reaches the network.
"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from board import docs, notion, run_registry  # noqa: E402


def page(pid, edited="2026-09-20T10:00:00.000Z", **props):
    kinds = {"Position Title": "title", "Company": "rich_text", "Next Action": "rich_text",
             "Job Posting URL": "url", "Application Date": "date",
             "First Response Date": "date", "Follow-up Date": "date",
             "Application Portal": "url", "My Notes": "rich_text"}
    out = {}
    for name, value in props.items():
        name = name.replace("_", " ")
        kind = kinds.get(name, "select")
        if kind in ("title", "rich_text"):
            out[name] = {"type": kind, kind: [{"plain_text": value}]}
        elif kind == "url":
            out[name] = {"type": "url", "url": value}
        elif kind == "date":
            out[name] = {"type": "date", "date": {"start": value}}
        else:
            out[name] = {"type": "select", "select": {"name": value}}
    return {"id": pid, "last_edited_time": edited, "properties": out}


class FakeNotion:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, cfg, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/query"):
            return {"results": self.pages, "has_more": False}
        if path == "/file_uploads":
            return {"id": "up%d" % len(self.calls)}
        return {}


class NotionSyncTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "job_scraper").mkdir()
        (home / "job_scraper" / "notion_sync.json").write_text(
            json.dumps({"token": "t", "data_source_id": "ds"}))
        for target, name, value in ((run_registry, "ROOT", home),
                                    (docs, "TRACKER", home / "job_search_tracker.csv"),
                                    (docs, "TRACKER_LOCK", home / ".tracker.lock")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tracker = home / "job_search_tracker.csv"

    def fake(self, pages):
        fake = FakeNotion(pages)
        patcher = mock.patch.object(notion, "_call", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def write_tracker(self, *rows):
        with open(self.tracker, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(docs.CANONICAL_HEADER)
            for row in rows:
                writer.writerow([row.get(c, "") for c in docs.CANONICAL_HEADER])

    def read_tracker(self):
        with open(self.tracker, newline="") as handle:
            return list(csv.DictReader(handle))

    def test_unconfigured_is_a_no_op(self):
        (run_registry.ROOT / "job_scraper" / "notion_sync.json").unlink()
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertIsNone(notion.push({"job_url": "u", "company": "c", "role": "r"}))
            self.assertIsNone(notion.pull())

    def test_push_creates_row_with_identity_fields(self):
        fake = self.fake([])
        record = {"job_url": "https://jobs.lever.co/acme/1?lever-source=x",
                  "company": "Acme", "role": "ML Engineer",
                  "targets": {"cv": "cv/main_acme_ml.tex"}}
        self.assertEqual(notion.push(record, {"location": "Zürich, Switzerland",
                                              "primary_source": "lever"}), "created")
        props = fake.calls[-1][2]["properties"]
        self.assertEqual(props["Country"], {"select": {"name": "🇨🇭 CH"}})
        self.assertEqual(props["Source"], {"select": {"name": "Company Website"}})
        self.assertEqual(props["Stage"], {"select": {"name": "Interested"}})
        self.assertIn("main_acme_ml.tex", props["Next Action"]["rich_text"][0]["text"]["content"])

    def test_push_never_overwrites_owner_fields(self):
        existing = page("p1", Position_Title="ML Engineer", Company="Acme",
                        Job_Posting_URL="https://jobs.lever.co/acme/1", Stage="Applied",
                        Country="🇩🇪 DE")
        fake = self.fake([existing])
        record = {"job_url": "https://www.jobs.lever.co/acme/1/", "company": "Acme",
                  "role": "ML Engineer", "targets": {}}
        self.assertEqual(notion.push(record, {"location": "Zurich"}), "updated")
        props = fake.calls[-1][2]["properties"]
        self.assertNotIn("Stage", props)
        self.assertNotIn("Country", props)
        self.assertNotIn("Next Action", props)       # already past Interested
        self.assertEqual(props["Active Status"], {"select": {"name": "Active"}})

    def test_pull_derives_dates_and_updates_tracker(self):
        applied = page("p1", edited="2026-09-21T08:00:00.000Z", Position_Title="ML Engineer",
                       Company="Acme", Job_Posting_URL="https://jobs.lever.co/acme/1",
                       Stage="Applied", Next_Action=notion.AUTO_PREFIX + ": cv.pdf")
        onsite = page("p2", Position_Title="SWE", Company="Beta", Stage="Onsite",
                      Application_Date="2026-09-01", Furthest_Stage="OA")
        idle = page("p3", Position_Title="X", Company="Gamma", Stage="Interested")
        fake = self.fake([applied, onsite, idle])
        self.write_tracker({"date": "2026-09-19", "company": "Acme", "role": "ML Engineer",
                            "status": "drafted", "cv_file": "cv/main_acme.tex",
                            "source": "https://jobs.lever.co/acme/1"})
        result = notion.pull()
        self.assertEqual(result, {"pages": 3, "derived": 2, "documents": 0, "tracker": 2})
        patches = {path: body["properties"] for method, path, body in fake.calls
                   if method == "PATCH"}
        p1 = patches["/pages/p1"]
        self.assertEqual(p1["Application Date"], {"date": {"start": "2026-09-21"}})
        self.assertEqual(p1["Follow-up Date"], {"date": {"start": "2026-10-01"}})
        self.assertEqual(p1["Next Action"], {"rich_text": []})
        p2 = patches["/pages/p2"]
        self.assertEqual(p2["Furthest Stage"], {"select": {"name": "Onsite"}})
        self.assertIn("First Response Date", p2)
        self.assertNotIn("Application Date", p2)
        rows = {r["company"]: r for r in self.read_tracker()}
        self.assertEqual((rows["Acme"]["status"], rows["Acme"]["date"]),
                         ("applied", "2026-09-21"))
        self.assertEqual(rows["Acme"]["cv_file"], "cv/main_acme.tex")
        self.assertEqual(rows["Beta"]["status"], "interview")
        self.assertNotIn("Gamma", rows)

    def test_pull_is_idempotent(self):
        done = page("p1", Position_Title="SWE", Company="Beta", Stage="Rejected",
                    Application_Date="2026-09-01", First_Response_Date="2026-09-10",
                    Furthest_Stage="Screened Out", Outcome_Reason="Experience level")
        self.fake([done])
        self.assertEqual(notion.pull()["tracker"], 1)
        self.assertEqual(notion.pull(), {"pages": 1, "derived": 0, "documents": 0, "tracker": 0})

    def test_pull_attaches_pdfs_once_and_replaces_rebuilt_ones(self):
        build = run_registry.ROOT / "cv" / "build"
        build.mkdir(parents=True)
        (build / "main_acme.pdf").write_bytes(b"%PDF-1 v1")
        row = page("p1", Position_Title="ML Engineer", Company="Acme", Stage="Interested",
                   Job_Posting_URL="https://jobs.lever.co/acme/1")
        fake = self.fake([row])
        self.write_tracker({"date": "2026-09-19", "company": "Acme", "role": "ML Engineer",
                            "status": "drafted", "cv_file": "cv/main_acme.tex",
                            "cover_letter_file": "cover_letters/never_built.tex",
                            "source": "https://jobs.lever.co/acme/1"})
        sent = []
        with mock.patch.object(notion, "_schema_ready", set()), \
                mock.patch.object(notion, "_upload",
                                  lambda cfg, path, name: sent.append(name) or "up1"):
            self.assertEqual(notion.pull()["documents"], 1)
            schema = [b for m, p, b in fake.calls if m == "PATCH" and "data_sources" in p]
            self.assertEqual(schema, [{"properties": {"CV": {"files": {}},
                                                      "Cover Letter": {"files": {}}}}])
            attach = [b for m, p, b in fake.calls if p == "/pages/p1"][-1]["properties"]
            self.assertEqual(list(attach), ["CV"])
            self.assertEqual(attach["CV"]["files"][0]["file_upload"], {"id": "up1"})
            row["properties"]["CV"] = {"type": "files", "files": [{"name": sent[0]}]}
            self.assertEqual(notion.pull()["documents"], 0)      # unchanged PDF
            (build / "main_acme.pdf").write_bytes(b"%PDF-1 v2")
            self.assertEqual(notion.pull()["documents"], 1)      # rebuilt PDF
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0], sent[1])
        self.assertTrue(sent[0].startswith("main_acme."))

    def test_mark_applied_moves_stage_forward_only(self):
        row = page("p1", Position_Title="ML Engineer", Company="Acme", Stage="Interested",
                   Job_Posting_URL="https://jobs.lever.co/acme/1",
                   Next_Action=notion.AUTO_PREFIX + ": cv.pdf")
        fake = self.fake([row])
        record = {"job_url": "https://jobs.lever.co/acme/1", "company": "Acme",
                  "role": "ML Engineer", "targets": {}}
        self.assertEqual(notion.mark_applied(record), "Applied")
        props = fake.calls[-1][2]["properties"]
        today = notion.date.today()
        self.assertEqual(props["Stage"], {"select": {"name": "Applied"}})
        self.assertEqual(props["Application Date"], {"date": {"start": today.isoformat()}})
        self.assertEqual(props["Follow-up Date"]["date"]["start"],
                         (today + notion.timedelta(days=notion.FOLLOW_UP_DAYS)).isoformat())
        self.assertEqual(props["Next Action"], {"rich_text": []})
        moved = page("p2", Position_Title="SWE", Company="Beta", Stage="Onsite")
        fake = self.fake([moved])
        self.assertEqual(notion.mark_applied({"job_url": "", "company": "Beta",
                                              "role": "SWE"}), "Onsite")
        self.assertFalse([c for c in fake.calls if c[0] == "PATCH"])

    def test_tracker_mark_applied_moves_only_drafted_rows(self):
        self.write_tracker({"date": "2026-09-19", "company": "Acme", "role": "ML Engineer",
                            "status": "drafted"},
                           {"date": "2026-09-01", "company": "Beta", "role": "SWE",
                            "status": "interview"})
        self.assertEqual(docs.mark_applied({"company": "acme", "role": "ML engineer"}),
                         "updated")
        self.assertEqual(docs.mark_applied({"company": "Beta", "role": "SWE"}), "unchanged")
        self.assertEqual(docs.mark_applied({"company": "Nope", "role": "X"}), "missing")
        rows = {r["company"]: r for r in self.read_tracker()}
        self.assertEqual((rows["Acme"]["status"], rows["Acme"]["date"]),
                         ("applied", notion.date.today().isoformat()))
        self.assertEqual(rows["Beta"]["status"], "interview")
        self.assertEqual(docs.tracker_statuses()[("acme", "ml engineer")][0], "applied")


    def test_save_owner_fields_writes_notion_then_tracker(self):
        row = page("p1", Position_Title="ML Engineer", Company="Acme", Stage="Applied",
                   Job_Posting_URL="https://jobs.lever.co/acme/1")
        fake = self.fake([row])
        self.write_tracker({"date": "2026-09-19", "company": "Acme", "role": "ML Engineer",
                            "status": "applied"})
        record = {"job_url": "https://jobs.lever.co/acme/1", "company": "Acme",
                  "role": "ML Engineer", "targets": {}}
        values = {"portal_url": "https://acme.wd3.myworkdayjobs.com/", "my_notes": "login: me"}
        with mock.patch.object(notion, "_schema_ready", set()):
            self.assertEqual(notion.save_owner_fields(record, values), "updated")
        schema = [b for m, p, b in fake.calls if m == "PATCH" and "data_sources" in p]
        self.assertEqual(schema, [{"properties": {"Application Portal": {"url": {}},
                                                  "Application Email": {"email": {}},
                                                  "My Notes": {"rich_text": {}}}}])
        props = fake.calls[-1][2]["properties"]
        self.assertEqual(props["Application Portal"],
                         {"url": "https://acme.wd3.myworkdayjobs.com/"})
        self.assertEqual(props["My Notes"], {"rich_text": [{"text": {"content": "login: me"}}]})
        self.assertEqual(docs.save_owner_fields(record, values), "updated")
        self.assertEqual(docs.save_owner_fields(record, values), "unchanged")
        self.assertEqual(self.read_tracker()[0]["portal_url"],
                         "https://acme.wd3.myworkdayjobs.com/")
        with mock.patch.object(notion, "_schema_ready", {("ds", "owner")}):
            notion.save_owner_fields(record, {"portal_url": "", "my_notes": ""})
        props = fake.calls[-1][2]["properties"]
        self.assertEqual(props, {"Application Portal": {"url": None},
                                 "My Notes": {"rich_text": []}})

    def test_owner_save_combines_application_status_and_details(self):
        record = {"job_url": "https://example.test/jobs/1", "company": "Example",
                  "role": "Engineer", "targets": {}}
        values = {"apply_email": "careers@example.test", "my_notes": "Reference 42"}
        for stage, status in (("Interested", "drafted"), ("Onsite", "interview")):
            with self.subTest(stage=stage):
                row = page("p1", Position_Title="Engineer", Company="Example", Stage=stage,
                           Job_Posting_URL=record["job_url"])
                fake = self.fake([row])
                self.write_tracker({"date": "2026-09-19", "company": "Example",
                                    "role": "Engineer", "status": status})
                notion.save_owner_fields(record, values, mark_applied=True)
                patches = [body["properties"] for method, path, body in fake.calls
                           if method == "PATCH" and path == "/pages/p1"]
                self.assertEqual(len(patches), 1)
                self.assertEqual(patches[0]["Application Email"], {"email": values["apply_email"]})
                if status == "drafted":
                    self.assertEqual(patches[0]["Stage"], {"select": {"name": "Applied"}})
                    self.assertIn("Follow-up Date", patches[0])
                else:
                    self.assertNotIn("Stage", patches[0])
                with mock.patch.object(docs, "_write_tracker", wraps=docs._write_tracker) as write:
                    docs.save_owner_fields(record, values, mark_applied=True)
                    self.assertEqual(write.call_count, 1)
                saved = self.read_tracker()[0]
                self.assertEqual(saved["status"], "applied" if status == "drafted" else status)
                self.assertEqual(saved["apply_email"], values["apply_email"])
                self.assertEqual(saved["my_notes"], values["my_notes"])
                self.assertEqual(saved["date"], notion.date.today().isoformat()
                                 if status == "drafted" else "2026-09-19")

    def test_pull_mirrors_owner_fields_and_upgrades_old_header(self):
        legacy = docs.CANONICAL_HEADER[:14]
        with open(self.tracker, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(legacy)
            writer.writerow(["2026-09-19", "Acme", "", "ML Engineer", "", "", "drafted"]
                            + [""] * 7)
            writer.writerow(["2026-09-19", "Beta", "", "SWE", "", "", "drafted"] + [""] * 7)
        acme = page("p1", Position_Title="ML Engineer", Company="Acme", Stage="Interested",
                    Application_Portal="https://acme.example/portal", My_Notes="ref 42")
        beta = page("p2", Position_Title="SWE", Company="Beta", Stage="Interested")
        self.fake([acme, beta])
        self.assertEqual(notion.pull()["tracker"], 2)
        rows = {r["company"]: r for r in self.read_tracker()}
        self.assertEqual(list(rows["Acme"])[-3:], ["portal_url", "my_notes", "apply_email"])
        self.assertEqual((rows["Acme"]["portal_url"], rows["Acme"]["my_notes"]),
                         ("https://acme.example/portal", "ref 42"))
        self.assertEqual(rows["Beta"]["portal_url"], "")
        self.assertEqual(notion.pull()["tracker"], 0)

    def test_pull_fills_empty_notion_owner_fields_from_tracker(self):
        row = page("p1", Position_Title="ML Engineer", Company="Acme", Stage="Interested")
        fake = self.fake([row])
        self.write_tracker({"date": "2026-09-19", "company": "Acme", "role": "ML Engineer",
                            "status": "drafted", "cv_file": "cv/never_built.tex",
                            "portal_url": "https://acme.example/portal"})
        with mock.patch.object(notion, "_schema_ready", {("ds", "owner")}):
            self.assertEqual(notion.pull()["documents"], 1)
        props = [b for m, p, b in fake.calls if p == "/pages/p1"][-1]["properties"]
        self.assertEqual(props, {"Application Portal": {"url": "https://acme.example/portal"}})
        self.assertEqual(self.read_tracker()[0]["portal_url"], "https://acme.example/portal")

    def test_application_email_round_trips_as_an_email_property(self):
        row = page("p1", Position_Title="ML Engineer", Company="Acme", Stage="Interested",
                   Job_Posting_URL="https://jobs.lever.co/acme/1")
        row["properties"]["Application Email"] = {"type": "email", "email": "jobs@acme.ch"}
        fake = self.fake([row])
        self.write_tracker({"date": "2026-09-19", "company": "Acme", "role": "ML Engineer",
                            "status": "drafted"})
        notion.pull()
        self.assertEqual(self.read_tracker()[0]["apply_email"], "jobs@acme.ch")
        record = {"job_url": "https://jobs.lever.co/acme/1", "company": "Acme",
                  "role": "ML Engineer", "targets": {}}
        with mock.patch.object(notion, "_schema_ready", {("ds", "owner")}):
            notion.save_owner_fields(record, {"apply_email": ""})
        self.assertEqual(fake.calls[-1][2]["properties"], {"Application Email": {"email": None}})

    def test_owner_values_accepts_web_links_only(self):
        from board import server
        self.assertEqual(server.run_route("/api/runs/r-20260928-101010-acme-abc123/owner"),
                         ("r-20260928-101010-acme-abc123", "owner"))
        self.assertIn("owner", server.RUN_POST_ACTIONS)   # else the POST is a 403
        self.assertEqual(server.owner_values({"portal_url": " https://x.com/a ",
                                              "my_notes": "hi  "}),
                         ({"portal_url": "https://x.com/a", "my_notes": "hi"}, None))
        self.assertEqual(server.owner_values({"portal_url": ""}), ({"portal_url": ""}, None))
        self.assertEqual(server.owner_values({"apply_email": " jobs@acme.ch "}),
                         ({"apply_email": "jobs@acme.ch"}, None))
        for bad in ({"portal_url": "javascript:alert(1)"}, {"my_notes": 3}, {},
                    {"my_notes": "x" * 2001}, {"apply_email": "not an address"}):
            values, error = server.owner_values(bad)
            self.assertIsNone(values)
            self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
