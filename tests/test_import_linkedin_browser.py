"""Authenticated LinkedIn browser imports use the shared board state safely."""

import json
import tempfile
import unittest
from pathlib import Path

from tools import import_linkedin_browser as linkedin_import

jobs_md = linkedin_import.jobs_md


def browser_job(**overrides):
    row = {
        "title": "Machine Learning Engineer",
        "company": "Example AG",
        "location": "Zurich, Switzerland",
        "url": "https://ch.linkedin.com/jobs/view/machine-learning-engineer-at-example-4451224579?trk=jobs",
        "posted": "2026-08-19",
        "deadline": None,
        "description": "Build production machine-learning systems.",
    }
    row.update(overrides)
    return row


class LinkedInBrowserImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = (jobs_md.SEEN, jobs_md.MD, jobs_md.CSV_ACTIVE, jobs_md.CSV_EXCLUDED)
        jobs_md.SEEN = root / "seen_jobs.json"
        jobs_md.MD = root / "jobs.md"
        jobs_md.CSV_ACTIVE = root / "jobs_active.csv"
        jobs_md.CSV_EXCLUDED = root / "jobs_excluded.csv"

        def restore():
            jobs_md.SEEN, jobs_md.MD, jobs_md.CSV_ACTIVE, jobs_md.CSV_EXCLUDED = self.paths

        self.addCleanup(restore)

    def test_new_job_is_canonicalized_and_persisted_without_exports(self):
        rows, duplicates = linkedin_import.normalize_rows({"jobs": [browser_job()]})
        self.assertEqual(duplicates, 0)
        seen = {}
        stats = linkedin_import.import_rows(seen, rows, "2026-08-20")
        linkedin_import.persist(seen)

        key = "https://www.linkedin.com/jobs/view/4451224579"
        self.assertEqual(stats["added"], 1)
        self.assertEqual(seen[key]["portal"], "linkedin-browser")
        self.assertEqual(seen[key]["sources"], [{
            "portal": "linkedin-browser",
            "url": key,
            "id": "4451224579",
            "first_seen": "2026-08-20",
        }])
        stored = json.loads(jobs_md.SEEN.read_text(encoding="utf-8"))["seen"]
        self.assertIn(key, stored)
        self.assertFalse(jobs_md.MD.exists())
        self.assertFalse(jobs_md.CSV_ACTIVE.exists())
        self.assertFalse(jobs_md.CSV_EXCLUDED.exists())

    def test_reimport_preserves_user_and_rank_fields(self):
        rows, _ = linkedin_import.normalize_rows([browser_job()])
        seen = {}
        linkedin_import.import_rows(seen, rows, "2026-08-20")
        key = "https://www.linkedin.com/jobs/view/4451224579"
        seen[key]["user_status"] = "star"
        seen[key]["user_note"] = "apply this week"
        seen[key]["rank_score"] = 91

        stats = linkedin_import.import_rows(seen, rows, "2026-08-21")

        self.assertEqual(stats["already_known"], 1)
        self.assertEqual(seen[key]["user_status"], "star")
        self.assertEqual(seen[key]["user_note"], "apply this week")
        self.assertEqual(seen[key]["rank_score"], 91)
        self.assertEqual(seen[key]["last_seen_at"], "2026-08-21")
        self.assertEqual(len(seen[key]["sources"]), 1)

    def test_first_party_ats_url_is_the_key_and_linkedin_is_provenance(self):
        rows, _ = linkedin_import.normalize_rows([browser_job(
            url="https://boards.greenhouse.io/example/jobs/42?gh_src=linkedin",
            linkedin_url="https://www.linkedin.com/jobs/view/4451224579?trk=jobs",
        )])
        self.assertEqual(rows[0]["ats_vendor"], "greenhouse")
        self.assertEqual(rows[0]["ats_id"], "greenhouse:example:42")

        seen = {}
        stats = linkedin_import.import_rows(seen, rows, "2026-08-20")

        key = "https://job-boards.greenhouse.io/example/jobs/42"
        self.assertEqual(stats["added"], 1)
        self.assertEqual(list(seen), [key])
        self.assertEqual(seen[key]["primary_source"], "ats-search")
        self.assertEqual(seen[key]["ats_vendor"], "greenhouse")
        self.assertEqual(
            {source["portal"] for source in seen[key]["sources"]},
            {"ats-search", "linkedin-browser"},
        )
        ats_source = next(source for source in seen[key]["sources"]
                          if source["portal"] == "ats-search")
        self.assertEqual(ats_source["ats_vendor"], "greenhouse")

    def test_company_url_preserves_meaningful_query_and_drops_tracking(self):
        rows, _ = linkedin_import.normalize_rows([browser_job(
            url="https://careers.example.com/apply?jobId=42&utm_source=linkedin",
            linkedin_url="https://www.linkedin.com/jobs/view/4451224579",
        )])
        self.assertEqual(rows[0]["url"], "https://careers.example.com/apply?jobId=42")
        self.assertEqual(rows[0]["ats_vendor"], "")

        seen = {}
        linkedin_import.import_rows(seen, rows, "2026-08-20")
        key = "https://careers.example.com/apply?jobId=42"
        self.assertIn(key, seen)
        self.assertEqual(seen[key]["primary_source"], "company-careers")
        self.assertIn("linkedin-browser", {source["portal"] for source in seen[key]["sources"]})

    def test_common_ats_host_is_identified_without_a_supported_composite_id(self):
        rows, _ = linkedin_import.normalize_rows([browser_job(
            url="https://example.wd3.myworkdayjobs.com/en-US/careers/job/Zurich/ML_R123",
            linkedin_url="https://www.linkedin.com/jobs/view/4451224579",
        )])
        self.assertEqual(rows[0]["ats_vendor"], "workday")
        self.assertEqual(rows[0]["ats_id"], "")

    def test_matching_first_party_job_collapses_without_rekeying(self):
        ats_key = "https://job-boards.greenhouse.io/example/jobs/42"
        seen = {ats_key: {
            "title": "Machine Learning Engineer",
            "company": "Example AG",
            "location": "Zurich, Switzerland",
            "url": ats_key,
            "first_seen": "2026-08-18",
            "posted": "2026-08-18",
            "portal": "ats-search",
            "primary_source": "ats-search",
            "user_status": "yes",
            "user_note": "strong lead",
        }}
        rows, _ = linkedin_import.normalize_rows([browser_job()])

        stats = linkedin_import.import_rows(seen, rows, "2026-08-20")

        self.assertEqual(stats["collapsed"], 1)
        self.assertEqual(list(seen), [ats_key])
        self.assertEqual(seen[ats_key]["user_note"], "strong lead")
        self.assertIn("linkedin-browser", {source["portal"] for source in seen[ats_key]["sources"]})

    def test_duplicate_ids_in_one_payload_are_imported_once(self):
        rows, duplicates = linkedin_import.normalize_rows([
            browser_job(),
            browser_job(url="https://www.linkedin.com/jobs/view/4451224579"),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(duplicates, 1)

    def test_invalid_job_rejects_the_whole_payload(self):
        payload = [browser_job(), browser_job(url="https://www.linkedin.com/jobs/search/")]
        with self.assertRaises(linkedin_import.InputError):
            linkedin_import.normalize_rows(payload)

    def test_check_triages_cards_without_writing(self):
        seen = {
            # Seen through public linkedin-search under a slugged locale URL.
            "https://www.linkedin.com/jobs/view/4451224579": {
                "title": "Machine Learning Engineer", "company": "Example AG",
                "url": "https://ch.linkedin.com/jobs/view/ml-engineer-at-example-4451224579"},
            "https://job-boards.greenhouse.io/other/jobs/1": {
                "title": "Data Scientist", "company": "Other", "sources": []},
        }
        cards = {"jobs": [
            {"title": "Machine Learning Engineer", "company": "Example AG",
             "linkedin_url": "https://www.linkedin.com/jobs/view/4451224579/?trk=x"},
            {"title": "Data Scientist", "company": "Other",
             "linkedin_url": "https://www.linkedin.com/jobs/view/5000000001"},
            {"title": "NLP Engineer", "company": "New Co",
             "linkedin_url": "https://www.linkedin.com/jobs/view/5000000002"},
            {"title": "NLP Engineer", "company": "New Co",
             "linkedin_url": "https://ch.linkedin.com/jobs/view/nlp-engineer-5000000002"},
        ]}
        result = linkedin_import.check_cards(seen, cards, aliases={})
        self.assertEqual([c["title"] for c in result["known"]], ["Machine Learning Engineer"])
        self.assertEqual(result["likely_known"][0]["matches"],
                         "https://job-boards.greenhouse.io/other/jobs/1")
        self.assertEqual([c["linkedin_url"] for c in result["new"]],
                         ["https://www.linkedin.com/jobs/view/5000000002"])
        self.assertFalse(jobs_md.SEEN.exists())


if __name__ == "__main__":
    unittest.main()
