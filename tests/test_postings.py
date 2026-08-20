"""Sidecar posting store: purity, path safety, and fingerprint behaviour.

The store exists so posting bodies stay off two hot paths (the full rewrite of
seen_jobs.json on every board click, and the /api/jobs payload). These tests
pin the three properties that make that safe: describe() never writes, a
stored path can never read outside the store, and a fingerprint is stable
across processes.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import postings  # noqa: E402


POSTING_A = (
    "We are looking for an AI Engineer to join our platform team in Zurich. "
    "You will build retrieval augmented generation pipelines with Python and "
    "PyTorch, ship them behind FastAPI services, and evaluate them against "
    "held out data. Requirements include strong software engineering "
    "fundamentals, hands on experience with transformers and embeddings, and "
    "familiarity with Docker and AWS deployment workflows. You will work "
    "closely with researchers to move prototypes into production, own the "
    "evaluation harness, and report honestly on what the system cannot do. "
    "We offer a collaborative environment, a generous learning budget, and "
    "the freedom to choose your own tooling. "
)

# Same posting, a few sentences reworded - what the identical job looks like
# when two portals render it.
POSTING_A_VARIANT = (
    "We are seeking an AI Engineer to join our platform team in Zurich. "
    "You will build retrieval augmented generation pipelines with Python and "
    "PyTorch, ship them behind FastAPI services, and evaluate them against "
    "held out data. Requirements include strong software engineering "
    "fundamentals, hands on experience with transformers and embeddings, and "
    "familiarity with Docker and AWS deployment workflows. You will partner "
    "with researchers to move prototypes into production, own the evaluation "
    "harness, and report honestly on what the system cannot do. "
    "We offer a collaborative team, a generous learning budget, and "
    "the freedom to choose your own tooling. "
)

POSTING_B = (
    "Our accounting department seeks a payroll specialist to administer "
    "monthly salary runs across three cantons. You will reconcile ledgers, "
    "maintain pension contributions, liaise with auditors during the annual "
    "close, and prepare quarterly VAT filings. Candidates should hold a "
    "federal certificate in payroll administration, know Abacus thoroughly, "
    "and communicate comfortably with insurers and tax authorities. "
    "We offer flexible hours and subsidised lunches near the main station. "
)


class ExcerptTest(unittest.TestCase):
    def test_collapses_whitespace_and_truncates_with_an_ellipsis(self):
        text = "Line one.\n\n   Line   two.\t\tLine three." + ("x" * 500)
        result = postings.excerpt(text)
        self.assertNotIn("\n", result)
        self.assertNotIn("  ", result)
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(len(result), postings.EXCERPT_CHARS + 1)

    def test_short_text_is_returned_whole_without_an_ellipsis(self):
        self.assertEqual(postings.excerpt("  a   b  "), "a b")


class NormalizeBodyTest(unittest.TestCase):
    def test_keeps_paragraphs_but_drops_trailing_space_and_crlf(self):
        body = postings.normalize_body("A line   \r\n\r\n\r\n\r\nNext para  \r\n")
        self.assertEqual(body, "A line\n\nNext para")

    def test_empty_input_is_empty_output(self):
        self.assertEqual(postings.normalize_body(""), "")
        self.assertEqual(postings.normalize_body(None), "")


class DescribeTest(unittest.TestCase):
    def test_describe_writes_nothing(self):
        """The dry-run guarantee: merge() may call this on a run that must not
        touch the filesystem."""
        with tempfile.TemporaryDirectory() as tmp:
            before = sorted(os.listdir(tmp))
            postings.describe("https://example.com/job/1", POSTING_A)
            self.assertEqual(sorted(os.listdir(tmp)), before)
            self.assertFalse((Path(tmp) / "postings").exists())

    def test_returns_the_expected_field_set(self):
        fields = postings.describe(
            "https://example.com/job/1", POSTING_A,
            source="backfill", extractor="json-ld",
            final_url="https://example.com/job/1?utm=x")
        self.assertEqual(fields["posting_path"],
                         postings.relpath_for("https://example.com/job/1"))
        self.assertEqual(fields["posting_source"], "backfill")
        self.assertEqual(fields["posting_extractor"], "json-ld")
        self.assertEqual(fields["posting_final_url"], "https://example.com/job/1?utm=x")
        self.assertEqual(fields["posting_chars"], len(postings.normalize_body(POSTING_A)))
        self.assertTrue(fields["posting_fingerprint"])

    def test_empty_body_describes_to_nothing(self):
        self.assertEqual(postings.describe("https://example.com/x", "   \n  "), {})

    def test_final_url_is_omitted_when_not_supplied(self):
        fields = postings.describe("https://example.com/x", POSTING_A)
        self.assertNotIn("posting_final_url", fields)

    def test_path_is_stable_for_a_url_and_differs_between_urls(self):
        self.assertEqual(postings.relpath_for("https://a.test/1"),
                         postings.relpath_for("https://a.test/1"))
        self.assertNotEqual(postings.relpath_for("https://a.test/1"),
                            postings.relpath_for("https://a.test/2"))
        self.assertTrue(postings.relpath_for("https://a.test/1").startswith("postings/"))


class CommitAndLoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_round_trips_through_the_sidecar(self):
        url = "https://example.com/job/42"
        postings.commit(url, POSTING_A, root=self.root)
        entry = postings.describe(url, POSTING_A)
        self.assertEqual(postings.load(entry, root=self.root),
                         postings.normalize_body(POSTING_A))

    def test_commit_is_idempotent_and_leaves_no_temp_files(self):
        url = "https://example.com/job/42"
        postings.commit(url, POSTING_A, root=self.root)
        postings.commit(url, POSTING_A, root=self.root)
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(len(names), 1)
        self.assertFalse(any(n.endswith(".tmp") for n in names))

    def test_commit_all_counts_only_what_it_wrote(self):
        written = postings.commit_all(
            [("https://a.test/1", POSTING_A), ("https://a.test/2", "  ")],
            root=self.root)
        self.assertEqual(written, 1)

    def test_entry_without_a_path_loads_as_empty(self):
        self.assertEqual(postings.load({}, root=self.root), "")
        self.assertEqual(postings.load(None, root=self.root), "")

    def test_missing_file_is_not_an_error(self):
        """A sidecar is a cache; a pruned file must not break a board render."""
        entry = {"posting_path": postings.relpath_for("https://example.com/gone")}
        self.assertEqual(postings.load(entry, root=self.root), "")


class PathSafetyTest(unittest.TestCase):
    """`posting_path` is data read back from disk, so it is untrusted input."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _refuses(self, rel):
        with self.assertRaises(postings.UnsafePath):
            postings.load({"posting_path": rel}, root=self.root)

    def test_absolute_path_is_refused(self):
        self._refuses("/etc/passwd")

    def test_parent_traversal_is_refused(self):
        self._refuses("postings/../../../etc/passwd")
        self._refuses("../secrets.txt")

    def test_nested_subdirectory_is_refused(self):
        self._refuses("postings/nested/deep.txt")

    def test_an_absent_path_reads_as_no_body_rather_than_an_attack(self):
        """Most rows legitimately have no sidecar; that is not a safety event."""
        self.assertEqual(postings.load({"posting_path": ""}, root=self.root), "")
        self.assertEqual(postings.load({"posting_path": None}, root=self.root), "")

    def test_non_string_path_is_refused(self):
        with self.assertRaises(postings.UnsafePath):
            postings.load({"posting_path": 17}, root=self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_out_of_the_store_is_refused(self):
        secret = self.root.parent / "secret.txt"
        secret.write_text("classified", encoding="utf-8")
        self.addCleanup(lambda: secret.unlink(missing_ok=True))
        link = self.root / "escape.txt"
        try:
            os.symlink(str(secret), str(link))
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("symlink creation not permitted")
        self._refuses("postings/escape.txt")

    def test_plain_filename_inside_the_store_is_accepted(self):
        url = "https://example.com/ok"
        postings.commit(url, POSTING_A, root=self.root)
        name = Path(postings.relpath_for(url)).name
        self.assertTrue(postings.load({"posting_path": name}, root=self.root))


class StoreLocationTest(unittest.TestCase):
    """The sidecars follow the state file, not a constant baked in at import.

    This is not hypothetical tidiness: before store_dir() existed, a test that
    redirected `jobs_md.SEEN` to a temp directory and exercised the real commit
    path wrote a posting body into the developer's own `job_scraper/postings/`.
    """

    # Patch the module object `postings` actually holds, not `tools.jobs_md`.
    # The repo is importable both ways - `import jobs_md` with tools/ on the
    # path, and `from tools import jobs_md` - and those are two module objects
    # with two independent `SEEN`s. Patching the wrong one is how a test ends up
    # writing to the real store while believing it was redirected, which is the
    # very failure this class exists to catch.

    def test_the_store_sits_beside_the_active_state_file(self):
        saved = postings.jobs_md.SEEN
        try:
            with tempfile.TemporaryDirectory() as tmp:
                postings.jobs_md.SEEN = Path(tmp) / "seen_jobs.json"
                self.assertEqual(postings.store_dir(), Path(tmp) / "postings")
                postings.commit("https://example.com/job/1", POSTING_A)
                self.assertTrue((Path(tmp) / "postings").is_dir())
        finally:
            postings.jobs_md.SEEN = saved

    def test_redirecting_the_board_does_not_write_into_the_real_repo(self):
        saved = postings.jobs_md.SEEN
        real = saved.parent / "postings"
        before = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                postings.jobs_md.SEEN = Path(tmp) / "seen_jobs.json"
                postings.commit("https://example.com/job/2", POSTING_A)
        finally:
            postings.jobs_md.SEEN = saved
        after = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        self.assertEqual(after, before,
                         "a redirected board wrote into the real posting store")


class FingerprintTest(unittest.TestCase):
    def test_identical_text_matches_exactly(self):
        a = postings.fingerprint(POSTING_A)
        self.assertEqual(postings.fingerprint_similarity(a, a), 1.0)
        self.assertIs(postings.fingerprints_agree(a, a), True)

    def test_the_same_posting_reworded_still_agrees(self):
        a = postings.fingerprint(POSTING_A)
        b = postings.fingerprint(POSTING_A_VARIANT)
        self.assertIs(postings.fingerprints_agree(a, b), True)

    def test_two_different_postings_disagree(self):
        a = postings.fingerprint(POSTING_A)
        b = postings.fingerprint(POSTING_B)
        self.assertIs(postings.fingerprints_agree(a, b), False)

    def test_empty_text_fingerprints_to_nothing_and_has_no_opinion(self):
        self.assertEqual(postings.fingerprint(""), "")
        self.assertIsNone(postings.fingerprints_agree("", postings.fingerprint(POSTING_A)))
        self.assertIsNone(postings.fingerprints_agree(None, None))

    def test_malformed_signatures_have_no_opinion_rather_than_disagreeing(self):
        """None must never be read as "different" - it is "unknown"."""
        self.assertIsNone(postings.fingerprints_agree("abc", "def0"))
        self.assertIsNone(postings.fingerprint_similarity("abcd", "abcdabcd"))

    def test_signature_length_is_bounded(self):
        signature = postings.fingerprint(POSTING_A)
        self.assertEqual(len(signature), postings.MINHASH_N * 4)

    def test_only_the_first_1500_characters_are_fingerprinted(self):
        # The base must already exceed the window and vary across it, or the
        # assertion would hold even if the truncation were removed.
        base = POSTING_A + POSTING_B + POSTING_A
        self.assertGreater(len(base), postings.FINGERPRINT_CHARS)
        tail = base + ("zzz unrelated filler wording " * 400)
        self.assertEqual(postings.fingerprint(base[:postings.FINGERPRINT_CHARS]),
                         postings.fingerprint(tail))

    def test_fingerprint_is_stable_across_processes(self):
        """Guards the docstring's claim: no dependence on PYTHONHASHSEED.

        Python randomises str.__hash__ per process, so a fingerprint built with
        it would silently stop matching yesterday's rows.
        """
        code = (
            "import sys; sys.path.insert(0, %r); import postings; "
            "sys.stdout.write(postings.fingerprint(%r))" % (str(ROOT / "tools"), POSTING_A)
        )
        seeds = []
        for seed in ("0", "1", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run([sys.executable, "-c", code], env=env,
                                 capture_output=True, text=True, check=True)
            seeds.append(out.stdout)
        self.assertEqual(len(set(seeds)), 1)
        self.assertEqual(seeds[0], postings.fingerprint(POSTING_A))


if __name__ == "__main__":
    unittest.main()
