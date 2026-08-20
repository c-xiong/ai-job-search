"""Enrichment of rows already on the board, and the dry-run write barrier.

merge() reaches an existing row two ways - under its own key, and as a
collapsed cross-source duplicate - and both used to record the sighting and
drop the posting body it carried. Every row on the board is an existing row on
the next run, so that path is not an edge case: it is the common one.

The second half pins the barrier that makes storing bodies safe at all. A
`--dry-run` executes merge() in full and only skips `save_seen()`, so anything
that writes from inside merge() litters a run that is supposed to touch
nothing.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools import ats_fetch, jobs_md, postings
from tests.test_ats_fetch import linkedin_entry, row, silent


BODY = ("Build LLM and RAG systems in PyTorch with evaluation harnesses. "
        "You will own the retrieval pipeline end to end, ship it behind "
        "FastAPI, and report honestly on what it cannot do yet.")
LONGER_BODY = BODY + (" Requirements: Python, Docker, AWS, and a habit of "
                      "writing the test before the fix. We offer a learning "
                      "budget and unhurried code review.")


class ExactKeyEnrichmentTest(unittest.TestCase):
    """The path every one of the board's existing rows takes on the next run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._registry = ats_fetch.REGISTRY
        ats_fetch.REGISTRY = Path(self.tmp.name) / "companies.json"
        self.addCleanup(lambda: setattr(ats_fetch, "REGISTRY", self._registry))

    def test_a_row_with_no_body_gains_one_when_seen_again(self):
        seen = {}
        ats_fetch.merge(seen, [row(description="")], "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        self.assertNotIn("posting_path", seen[key])

        pending = []
        stats = ats_fetch.merge(seen, [row(description=BODY)], "2026-08-21", silent,
                                pending=pending)
        self.assertEqual(stats["already_known"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(seen[key]["posting_path"], postings.relpath_for(key))
        self.assertEqual(seen[key]["posting_chars"], len(BODY))
        self.assertTrue(seen[key]["posting_fingerprint"])
        self.assertEqual(pending, [(key, BODY)])

    def test_enrichment_does_not_disturb_what_you_or_rank_own(self):
        seen = {}
        ats_fetch.merge(seen, [row(description="")], "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        seen[key].update({"user_status": "star", "user_note": "referral via Anna",
                          "rank_score": 82, "rank_verdict": "strong fit",
                          "strengths": ["NLP"], "gaps": ["German"]})
        before = {f: json.dumps(seen[key].get(f), sort_keys=True)
                  for f in ats_fetch.PROTECTED}

        ats_fetch.merge(seen, [row(description=BODY, title="Renamed Role")],
                        "2026-08-21", silent, pending=[])

        for field in ats_fetch.PROTECTED:
            self.assertEqual(json.dumps(seen[key].get(field), sort_keys=True), before[field],
                             "enrichment moved %r, which it does not own" % field)
        self.assertTrue(seen[key]["posting_path"], "the body still landed")

    def test_a_better_body_replaces_a_worse_one(self):
        seen = {}
        ats_fetch.merge(seen, [dict(row(description=BODY), posting_extractor="fallback")],
                        "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        self.assertEqual(seen[key]["posting_extractor"], "fallback")

        ats_fetch.merge(seen, [dict(row(description=BODY), posting_extractor="json-ld")],
                        "2026-08-21", silent, pending=[])
        self.assertEqual(seen[key]["posting_extractor"], "json-ld")

    def test_a_worse_body_does_not_replace_a_better_one(self):
        seen = {}
        ats_fetch.merge(seen, [dict(row(description=LONGER_BODY), posting_extractor="inline")],
                        "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"

        ats_fetch.merge(seen, [dict(row(description=BODY), posting_extractor="fallback")],
                        "2026-08-21", silent, pending=[])
        self.assertEqual(seen[key]["posting_extractor"], "inline")
        self.assertEqual(seen[key]["posting_chars"], len(LONGER_BODY))

    def test_a_longer_body_from_the_same_source_wins(self):
        """A LinkedIn detail page over the truncated snippet that preceded it."""
        seen = {}
        ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"

        ats_fetch.merge(seen, [row(description=LONGER_BODY)], "2026-08-21", silent, pending=[])
        self.assertEqual(seen[key]["posting_chars"], len(LONGER_BODY))

    def test_an_empty_description_never_clears_a_stored_body(self):
        seen = {}
        ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"

        ats_fetch.merge(seen, [row(description="")], "2026-08-21", silent, pending=[])
        self.assertEqual(seen[key]["posting_chars"], len(BODY))


class CollapsedDuplicateEnrichmentTest(unittest.TestCase):
    """The case worth having: the ATS copy arrives with the body LinkedIn lacked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._registry = ats_fetch.REGISTRY
        ats_fetch.REGISTRY = Path(self.tmp.name) / "companies.json"
        self.addCleanup(lambda: setattr(ats_fetch, "REGISTRY", self._registry))

    def test_the_surviving_row_takes_the_body(self):
        key = "https://www.linkedin.com/jobs/view/4451224579"
        seen = {key: linkedin_entry(description="")}
        self.assertNotIn("posting_path", seen[key])

        pending = []
        stats = ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent,
                                pending=pending)
        self.assertEqual(stats["collapsed"], 1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[key]["posting_chars"], len(BODY))

    def test_the_body_is_filed_under_the_surviving_key_not_the_sighting(self):
        """The key never moves (§11.5), so neither may the sidecar behind it."""
        key = "https://www.linkedin.com/jobs/view/4451224579"
        seen = {key: linkedin_entry(description="")}
        pending = []
        ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent, pending=pending)

        self.assertEqual(seen[key]["posting_path"], postings.relpath_for(key))
        self.assertNotEqual(seen[key]["posting_path"],
                            postings.relpath_for("https://job-boards.greenhouse.io/parloa/jobs/1"))
        self.assertEqual([url for url, _ in pending], [key])

    def test_a_collapse_still_changes_nothing_you_or_rank_own(self):
        key = "https://www.linkedin.com/jobs/view/4451224579"
        seen = {key: linkedin_entry(description="")}
        before = {f: json.dumps(seen[key].get(f), sort_keys=True)
                  for f in ats_fetch.PROTECTED}

        ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent, pending=[])

        for field in ats_fetch.PROTECTED:
            self.assertEqual(json.dumps(seen[key].get(field), sort_keys=True), before[field],
                             "a collapse moved %r, which it does not own" % field)


class ProtectedFieldGuardTest(unittest.TestCase):
    """The allow-list used to be a docstring promise. Now it is enforced."""

    def test_a_write_into_a_protected_field_raises_rather_than_persisting(self):
        entry = linkedin_entry(description="")
        original = ats_fetch.append_source

        def sabotage(target, record):
            target["user_status"] = "no"
            return original(target, record)

        ats_fetch.append_source = sabotage
        try:
            with self.assertRaises(ats_fetch.ProtectedFieldWritten) as caught:
                ats_fetch.enrich_existing(entry, row(), entry["url"], "2026-08-20",
                                          "ats-search")
        finally:
            ats_fetch.append_source = original
        self.assertIn("user_status", str(caught.exception))

    def test_every_protected_field_is_outside_the_append_allowlist(self):
        overlap = set(ats_fetch.PROTECTED) & set(ats_fetch.APPENDABLE)
        self.assertEqual(overlap, set(),
                         "a field cannot be both appendable and protected")

    def test_the_posting_fields_are_all_appendable(self):
        for field in postings.ENTRY_FIELDS:
            self.assertIn(field, ats_fetch.APPENDABLE)


class DryRunWriteBarrierTest(unittest.TestCase):
    """merge() must not touch the filesystem; only the caller commits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._registry = ats_fetch.REGISTRY
        ats_fetch.REGISTRY = Path(self.tmp.name) / "companies.json"
        self.addCleanup(lambda: setattr(ats_fetch, "REGISTRY", self._registry))

    @staticmethod
    def _tree(root):
        return {str(p.relative_to(root)): p.stat().st_mtime_ns
                for p in sorted(Path(root).rglob("*")) if p.is_file()}

    def test_merging_writes_no_files_at_all(self):
        store = Path(self.tmp.name) / "postings"
        store.mkdir()
        (store / "sentinel.txt").write_text("untouched", encoding="utf-8")
        before = self._tree(self.tmp.name)

        seen = {}
        pending = []
        ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent, pending=pending)
        ats_fetch.merge(seen, [row(description=LONGER_BODY)], "2026-08-21", silent,
                        pending=pending)

        self.assertEqual(self._tree(self.tmp.name), before,
                         "merge() wrote to disk; a --dry-run would leave files behind")
        self.assertTrue(pending, "the bodies were still collected for the caller to commit")

    def test_the_row_describes_its_body_before_any_file_exists(self):
        """describe() is what makes the deferral possible: fields without I/O."""
        seen = {}
        ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent, pending=[])
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        self.assertTrue(seen[key]["posting_path"])
        self.assertFalse((Path(self.tmp.name) / seen[key]["posting_path"]).exists())

    def test_committing_the_pending_batch_makes_the_body_readable(self):
        store = Path(self.tmp.name) / "postings"
        seen = {}
        pending = []
        ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent, pending=pending)
        postings.commit_all(pending, root=store)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        self.assertEqual(postings.load(seen[key], root=store), BODY)

    def test_merge_without_a_pending_list_still_records_the_fields(self):
        """A caller that does not want the bodies must not crash on None."""
        seen = {}
        ats_fetch.merge(seen, [row(description=BODY)], "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        self.assertTrue(seen[key]["posting_excerpt"])


if __name__ == "__main__":
    unittest.main()
