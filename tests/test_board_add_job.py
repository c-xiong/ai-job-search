"""Add job (DESIGN.md §17): a hand-picked posting reaches the board without a model."""

import json
import tempfile
import unittest
from pathlib import Path

from tools.board import activity, add_job

jobs_md = add_job.jobs_md
postings = add_job.postings

BODY = ("About the role\nYou will build retrieval pipelines and agent tooling in Python.\n\n"
        "Requirements\n- 2+ years of experience with Python and PyTorch\n"
        "- Strong software engineering fundamentals\n- Fluent English\n") * 3
SEARCH_URL = ("https://www.linkedin.com/jobs/search-results/?currentJobId=4000000001"
              "&eBP=NOT_ELIGIBLE_FOR_CHARGING&keywords=Software%20Engineer&geoId=100000000")
ASHBY_APPLY = ("https://jobs.ashbyhq.com/example/0a1b2c3d-0000-4000-8000-000000000001"
               "/application?utm_source=abc")
ASHBY = "https://jobs.ashbyhq.com/example/0a1b2c3d-0000-4000-8000-000000000001"


class AddJobTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        saved = (jobs_md.SEEN, postings.ROOT, activity.LOG,
                 add_job.resolve_linkedin, add_job.resolve_ats, add_job.resolve_page)

        def restore():
            (jobs_md.SEEN, postings.ROOT, activity.LOG, add_job.resolve_linkedin,
             add_job.resolve_ats, add_job.resolve_page) = saved

        self.addCleanup(restore)
        jobs_md.SEEN = root / "seen_jobs.json"
        postings.ROOT = root
        activity.LOG = root / "activity.jsonl"
        self.calls = []
        self.linkedin = {"title": "AI Agent Engineer (f/m/d)", "company": "Example Robotics",
                         "location": "Berlin, Germany", "description": BODY}
        self.ats = {"title": "AI Agent Engineer (f/m/d)", "company": "", "location": "Berlin",
                    "posted": "2026-07-20", "description": BODY, "url": ASHBY}
        add_job.resolve_linkedin = lambda job_id: self.calls.append(("li", job_id)) or dict(self.linkedin)
        add_job.resolve_ats = lambda url: self.calls.append(("ats", url)) or dict(self.ats)
        add_job.resolve_page = lambda url: self.calls.append(("page", url)) or {}

    def seen(self):
        return json.loads(jobs_md.SEEN.read_text(encoding="utf-8"))["seen"]

    def test_linkedin_id_from_every_address_form(self):
        self.assertEqual(add_job.linkedin_id(SEARCH_URL), "4000000001")
        self.assertEqual(add_job.linkedin_id(
            "https://ch.linkedin.com/jobs/view/ai-engineer-at-foo-4451224579?trk=x"), "4451224579")
        self.assertEqual(add_job.linkedin_id(
            "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4000000002"),
            "4000000002")
        self.assertIsNone(add_job.linkedin_id("https://www.linkedin.com/jobs/search/?keywords=x"))

    def test_search_page_is_resolved_by_id_and_added_as_yes(self):
        code, body = add_job.add({"job_url": SEARCH_URL}, today="2026-09-28")
        self.assertEqual(code, 200, body)
        self.assertEqual(self.calls, [("li", "4000000001")])
        key = "https://www.linkedin.com/jobs/view/4000000001"
        self.assertEqual((body["outcome"], body["url"], body["status"]), ("added", key, "yes"))
        entry = self.seen()[key]
        self.assertEqual(entry["company"], "Example Robotics")
        self.assertEqual(entry["portal"], "manual")
        self.assertTrue((postings.ROOT / entry["posting_path"]).exists())

    def test_company_link_leads_and_linkedin_fills_the_company_name(self):
        code, body = add_job.add({"job_url": SEARCH_URL, "apply_url": ASHBY_APPLY},
                                 today="2026-09-28")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["url"], ASHBY)
        entry = self.seen()[ASHBY]
        self.assertEqual(entry["company"], "Example Robotics")
        portals = {s["portal"] for s in entry["sources"]}
        self.assertIn("manual", portals)
        self.assertIn("https://www.linkedin.com/jobs/view/4000000001",
                      {s["url"] for s in entry["sources"]})

    def test_company_link_added_later_joins_the_linkedin_row(self):
        add_job.add({"job_url": SEARCH_URL}, today="2026-09-28")
        self.ats["company"] = "Example Robotics GmbH"
        code, body = add_job.add({"job_url": SEARCH_URL, "apply_url": ASHBY_APPLY},
                                 today="2026-09-29")
        self.assertEqual((code, body["outcome"]), (200, "already_known"), body)
        seen = self.seen()
        self.assertEqual(list(seen), ["https://www.linkedin.com/jobs/view/4000000001"])
        entry = seen[body["url"]]
        self.assertEqual(entry["company"], "Example Robotics")
        self.assertEqual(jobs_md.primary_url(entry), ASHBY)

    def test_non_job_linkedin_page_is_refused_before_any_fetch(self):
        code, body = add_job.add({"job_url": "https://www.linkedin.com/feed/"})
        self.assertEqual(code, 400)
        self.assertEqual(self.calls, [])

    def test_linkedin_in_the_company_field_is_refused(self):
        code, _ = add_job.add({"job_url": SEARCH_URL,
                               "apply_url": "https://www.linkedin.com/jobs/view/4000000001"})
        self.assertEqual(code, 400)

    def test_failed_fetch_asks_for_details_then_uses_them(self):
        self.linkedin = {}
        code, body = add_job.add({"job_url": SEARCH_URL})
        self.assertEqual(code, 422)
        self.assertEqual(body["need"], ["title", "company", "description"])
        self.assertFalse(jobs_md.SEEN.exists())
        code, body = add_job.add({"job_url": SEARCH_URL, "title": "AI Agent Engineer",
                                  "company": "Example Robotics", "description": BODY},
                                 today="2026-09-28")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["has_description"])

    def test_typed_values_never_overwrite_fetched_ones(self):
        code, body = add_job.add({"job_url": SEARCH_URL, "title": "Something else"},
                                 today="2026-09-28")
        self.assertEqual(body["title"], "AI Agent Engineer (f/m/d)")

    def test_no_description_only_when_allowed(self):
        self.linkedin = dict(self.linkedin, description="")
        code, body = add_job.add({"job_url": SEARCH_URL})
        self.assertEqual((code, body["need"]), (422, ["description"]))
        code, body = add_job.add({"job_url": SEARCH_URL, "allow_no_description": True},
                                 today="2026-09-28")
        self.assertEqual(code, 200, body)
        self.assertFalse(body["has_description"])

    def test_existing_decision_is_not_overwritten(self):
        add_job.add({"job_url": SEARCH_URL}, today="2026-09-28")
        seen = self.seen()
        seen["https://www.linkedin.com/jobs/view/4000000001"]["user_status"] = "applied"
        jobs_md.save_seen(seen)
        code, body = add_job.add({"job_url": SEARCH_URL}, today="2026-09-29")
        self.assertEqual((code, body["outcome"], body["status"]), (200, "already_known", "applied"))


if __name__ == "__main__":
    unittest.main()
