import unittest

from tools import jobs_md


class JobIds(unittest.TestCase):
    def test_assign_fills_only_missing_and_keeps_existing(self):
        seen = {"a": {"job_id": "J-AAAAAA"}, "b": {}, "c": {}}
        self.assertEqual(jobs_md.assign_job_ids(seen), 2)
        self.assertEqual(seen["a"]["job_id"], "J-AAAAAA")
        ids = [e["job_id"] for e in seen.values()]
        self.assertEqual(len(set(ids)), 3)
        for job_id in ids:
            self.assertRegex(job_id, jobs_md.JOB_ID_PATTERN)
        self.assertEqual(jobs_md.assign_job_ids(seen), 0)

    def test_find_job_by_id_case_insensitive_and_by_url(self):
        url = "https://example.com/jobs/1"
        seen = {url: {"job_id": "J-7K3M9Q", "url": url}}
        self.assertEqual(jobs_md.find_job(seen, "j-7k3m9q")[0], url)
        self.assertEqual(jobs_md.find_job(seen, url)[0], url)
        self.assertEqual(jobs_md.find_job(seen, "J-0000ZZ"), (None, None))


if __name__ == "__main__":
    unittest.main()
