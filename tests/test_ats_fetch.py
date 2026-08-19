"""The ATS merge: URL identity, conservative dedup, and the append-only invariant.

These are the guards on the one thing the whole design refuses to get wrong -
"nothing already collected is ever overwritten" - plus the URL canonicalization
that decides whether two spellings of a posting are the same row at all.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tools import ats_fetch, jobs_md


class CanonicalUrlTest(unittest.TestCase):
    """Plan test 19: every vendor spelling of one posting collapses onto one key."""

    def test_greenhouse_hosts_fold_together(self):
        one = jobs_md.canonical_url("https://job-boards.eu.greenhouse.io/parloa/jobs/4635890101")
        for spelling in (
            "https://boards.greenhouse.io/parloa/jobs/4635890101",
            "https://job-boards.greenhouse.io/parloa/jobs/4635890101",
            "https://job-boards.eu.greenhouse.io/parloa/jobs/4635890101?gh_src=abc123",
        ):
            self.assertEqual(jobs_md.canonical_url(spelling), one, spelling)

    def test_tracking_parameters_are_stripped(self):
        base = "https://jobs.ashbyhq.com/deepjudge/41c79388-fad4-4286-948c-b279c35c568d"
        self.assertEqual(jobs_md.canonical_url(base + "?utm_source=linkedin&utm_medium=x"), base)
        self.assertEqual(jobs_md.canonical_url(base + "/application"), base)

    def test_every_vendor_has_a_rule(self):
        cases = {
            "https://parloa.jobs.personio.de/job/2246041?language=de":
                "https://parloa.jobs.personio.de/job/2246041",
            "https://jobs.lever.co/sonarsource/54743786-1ad7-4be7-bcbe-67f660e9c381/apply":
                "https://jobs.lever.co/sonarsource/54743786-1ad7-4be7-bcbe-67f660e9c381",
            "https://jobs.smartrecruiters.com/nexthink/744000144073609?utm_campaign=z":
                "https://jobs.smartrecruiters.com/nexthink/744000144073609",
        }
        for raw, expected in cases.items():
            self.assertEqual(jobs_md.canonical_url(raw), expected, raw)

    def test_the_existing_portals_still_work(self):
        self.assertEqual(
            jobs_md.canonical_url("https://ch.linkedin.com/jobs/view/ai-engineer-at-foo-4451224579"),
            "https://www.linkedin.com/jobs/view/4451224579")
        self.assertEqual(jobs_md.canonical_url("https://freehire.me/jobs/golang-zensar-2bxu6dxm?x=1"),
                         "https://freehire.me/jobs/golang-zensar-2bxu6dxm")

    def test_a_composite_id_can_be_recovered_from_a_posting_url(self):
        cases = {
            "https://job-boards.greenhouse.io/parloa/jobs/4635890101": "greenhouse:parloa:4635890101",
            "https://jobs.ashbyhq.com/deepjudge/41c79388-fad4-4286-948c-b279c35c568d":
                "ashby:deepjudge:41c79388-fad4-4286-948c-b279c35c568d",
            "https://merantix.jobs.personio.de/job/2246041": "personio:merantix:2246041",
            "https://jobs.lever.co/sonarsource/54743786-1ad7-4be7-bcbe-67f660e9c381":
                "lever:sonarsource:54743786-1ad7-4be7-bcbe-67f660e9c381",
            "https://jobs.smartrecruiters.com/nexthink/744000144073609":
                "smartrecruiters:nexthink:744000144073609",
        }
        for url, expected in cases.items():
            self.assertEqual(jobs_md.composite_id_for_url(url), expected, url)
        self.assertIsNone(jobs_md.composite_id_for_url("https://www.linkedin.com/jobs/view/1"))
        self.assertIsNone(jobs_md.composite_id_for_url(""))

    def test_two_postings_on_one_board_stay_distinct(self):
        a = jobs_md.canonical_url("https://job-boards.greenhouse.io/parloa/jobs/1")
        b = jobs_md.canonical_url("https://job-boards.greenhouse.io/parloa/jobs/2")
        self.assertNotEqual(a, b)


def row(**over):
    base = {
        "id": "greenhouse:parloa:1", "title": "Machine Learning Engineer", "company": "Parloa",
        "registry_company": "Parloa", "location": "Berlin", "posted": "2026-08-12",
        "deadline": None, "url": "https://job-boards.greenhouse.io/parloa/jobs/1",
        "description": "Build LLM and RAG systems in PyTorch with evaluation harnesses.",
        "prefit_score": 71, "prefit_reasons": ["title matches"],
    }
    base.update(over)
    return base


def linkedin_entry(**over):
    base = {
        "title": "Machine Learning Engineer", "company": "Parloa", "location": "Berlin, Germany",
        "url": "https://www.linkedin.com/jobs/view/4451224579", "first_seen": "2026-08-12",
        "posted": "2026-08-12", "deadline": None, "fit": "high", "status": "ranked",
        "portal": "linkedin-search", "user_status": "star", "user_note": "referral via Anna",
        "note": "why note", "rank_score": 82, "rank_verdict": "strong fit",
        "strengths": ["NLP"], "gaps": ["German"],
        "description": "Build LLM and RAG systems in PyTorch with evaluation harnesses.",
    }
    base.update(over)
    return base


def silent(_message):
    pass


class MergeTest(unittest.TestCase):
    """Plan test 20: collapse only on four signals; never on two."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # No registry: alias resolution falls back to plain name matching.
        self._registry = ats_fetch.REGISTRY
        ats_fetch.REGISTRY = Path(self.tmp.name) / "companies.json"
        self.addCleanup(lambda: setattr(ats_fetch, "REGISTRY", self._registry))

    def test_a_true_cross_source_duplicate_collapses(self):
        seen = {"https://www.linkedin.com/jobs/view/4451224579": linkedin_entry()}
        stats = ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        self.assertEqual(stats["collapsed"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(len(seen), 1, "the row must not be duplicated")
        entry = seen["https://www.linkedin.com/jobs/view/4451224579"]
        # The key never moves; what changes is which link you click.
        self.assertEqual(entry["primary_source"], "ats-search")
        # And the row that predates source history keeps its own origin: a
        # one-entry history naming only the ATS would claim LinkedIn never
        # found it.
        self.assertEqual([s["portal"] for s in entry["sources"]],
                         ["linkedin-search", "ats-search"])
        self.assertEqual(entry["sources"][0]["url"], "https://www.linkedin.com/jobs/view/4451224579")
        self.assertEqual(entry["sources"][0]["first_seen"], "2026-08-12")

    def test_same_title_different_city_is_never_collapsed(self):
        """One company genuinely posts the same title for several cities."""
        seen = {"https://www.linkedin.com/jobs/view/4451224579": linkedin_entry(location="Munich, Germany")}
        stats = ats_fetch.merge(seen, [row(location="Berlin")], "2026-08-20", silent)
        self.assertEqual(stats["collapsed"], 0)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(len(seen), 2)

    def test_a_near_match_three_months_apart_is_not_collapsed(self):
        seen = {"https://www.linkedin.com/jobs/view/4451224579": linkedin_entry(posted="2026-05-01")}
        stats = ats_fetch.merge(seen, [row(posted="2026-08-12")], "2026-08-20", silent)
        self.assertEqual(stats["collapsed"], 0)
        self.assertEqual(stats["added"], 1)

    def test_insufficient_evidence_flags_both_rows_and_deletes_neither(self):
        seen = {"https://www.linkedin.com/jobs/view/4451224579": linkedin_entry(location="Munich, Germany")}
        ats_fetch.merge(seen, [row(location="Berlin")], "2026-08-20", silent)
        new_key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        self.assertIn(new_key, seen)
        self.assertEqual(seen[new_key]["possible_duplicate_of"],
                         ["https://www.linkedin.com/jobs/view/4451224579"])
        self.assertEqual(seen["https://www.linkedin.com/jobs/view/4451224579"]["possible_duplicate_of"],
                         [new_key])

    def test_disagreeing_descriptions_block_a_collapse(self):
        """Four signals agree, but the two texts are plainly different postings."""
        seen = {"https://www.linkedin.com/jobs/view/4451224579":
                linkedin_entry(description="Lead the Berlin sales organisation across DACH markets.")}
        stats = ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        self.assertEqual(stats["collapsed"], 0)
        self.assertEqual(stats["added"], 1)

    def test_the_same_posting_twice_is_recorded_once(self):
        seen = {}
        ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        stats = ats_fetch.merge(seen, [row()], "2026-08-21", silent)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["already_known"], 1)
        entry = seen["https://job-boards.greenhouse.io/parloa/jobs/1"]
        self.assertEqual(len(entry["sources"]), 1, "source history is appended, not duplicated")
        self.assertEqual(entry["last_seen_at"], "2026-08-21")


class SameSourceDedupTest(unittest.TestCase):
    """§11.1: inside one source the id is the id. No fuzzy matching, ever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._registry = ats_fetch.REGISTRY
        ats_fetch.REGISTRY = Path(self.tmp.name) / "companies.json"
        self.addCleanup(lambda: setattr(ats_fetch, "REGISTRY", self._registry))

    def test_two_requisitions_with_the_same_title_are_two_rows(self):
        """One company posts the same title twice; both are real jobs."""
        seen = {}
        ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        # Same company, same title, same city, same week, same description -
        # every fuzzy signal agrees - but a different requisition id.
        stats = ats_fetch.merge(
            seen,
            [row(id="greenhouse:parloa:2", url="https://job-boards.greenhouse.io/parloa/jobs/2")],
            "2026-08-20", silent)
        self.assertEqual(stats["collapsed"], 0, "a second requisition was swallowed by the first")
        self.assertEqual(stats["added"], 1)
        self.assertEqual(len(seen), 2)

    def test_a_row_already_seen_through_this_portal_is_never_fuzzy_matched(self):
        seen = {}
        ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        self.assertNotIn("possible_duplicate_of", seen[key])
        ats_fetch.merge(seen, [row(id="greenhouse:parloa:2",
                                   url="https://job-boards.greenhouse.io/parloa/jobs/2")],
                        "2026-08-20", silent)
        # Not even flagged: within a source the ids settle it.
        self.assertNotIn("possible_duplicate_of", seen[key])

    def test_a_different_source_is_still_deduped(self):
        seen = {"https://www.linkedin.com/jobs/view/4451224579": linkedin_entry()}
        stats = ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        self.assertEqual(stats["collapsed"], 1)


class CrossRunPortalTest(unittest.TestCase):
    """The merge is portal-agnostic: one click, one row, whichever source won."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._registry = ats_fetch.REGISTRY
        ats_fetch.REGISTRY = Path(self.tmp.name) / "companies.json"
        self.addCleanup(lambda: setattr(ats_fetch, "REGISTRY", self._registry))

    def test_the_same_job_from_ats_and_linkedin_in_one_run_is_one_row(self):
        seen = {}
        ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        linkedin_row = {
            "id": "4451224579", "title": "Machine Learning Engineer", "company": "Parloa",
            "location": "Berlin, Germany", "posted": "2026-08-12", "deadline": None,
            "url": "https://www.linkedin.com/jobs/view/4451224579",
            "description": "Build LLM and RAG systems in PyTorch with evaluation harnesses.",
            "status": "new", "note": "",
        }
        stats = ats_fetch.merge(seen, [linkedin_row], "2026-08-20", silent,
                                portal="linkedin-search")
        self.assertEqual(stats["collapsed"], 1)
        self.assertEqual(len(seen), 1)
        entry = seen["https://job-boards.greenhouse.io/parloa/jobs/1"]
        self.assertEqual([s["portal"] for s in entry["sources"]],
                         ["ats-search", "linkedin-search"])
        # An aggregator finding the same row does not demote the first-party link.
        self.assertEqual(entry["primary_source"], "ats-search")

    def test_a_pre_screened_row_keeps_the_status_it_arrived_with(self):
        seen = {}
        ats_fetch.merge(seen, [{
            "id": "1", "title": "Backend Engineer", "company": "Acme", "location": "Berlin",
            "posted": "2026-08-12", "deadline": None, "url": "https://example.test/1",
            "description": "", "status": "gate", "note": "AUTO-SCREEN: German ...",
        }], "2026-08-20", silent, portal="linkedin-search")
        entry = seen["https://example.test/1"]
        self.assertEqual(entry["user_status"], "gate")
        self.assertEqual(entry["portal"], "linkedin-search")


class AppendOnlyTest(unittest.TestCase):
    """Plan test 22: a fetch may add and append. It may never overwrite."""

    OWNED = ("title", "company", "location", "url", "fit", "status", "portal",
             "user_status", "user_note", "note", "rank_score", "rank_verdict",
             "strengths", "gaps", "first_seen", "posted")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._registry = ats_fetch.REGISTRY
        ats_fetch.REGISTRY = Path(self.tmp.name) / "companies.json"
        self.addCleanup(lambda: setattr(ats_fetch, "REGISTRY", self._registry))

    def test_a_collapse_changes_nothing_you_or_rank_own(self):
        key = "https://www.linkedin.com/jobs/view/4451224579"
        seen = {key: linkedin_entry()}
        before = {f: json.dumps(seen[key].get(f)) for f in self.OWNED}
        ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        for field in self.OWNED:
            self.assertEqual(json.dumps(seen[key].get(field)), before[field],
                             "merge modified %r, which it does not own" % field)

    def test_re_seeing_a_row_changes_nothing_you_own(self):
        seen = {}
        ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        key = "https://job-boards.greenhouse.io/parloa/jobs/1"
        seen[key]["user_status"] = "star"
        seen[key]["user_note"] = "mine"
        seen[key]["rank_score"] = 77
        ats_fetch.merge(seen, [row(title="Machine Learning Engineer (edited)")], "2026-08-21", silent)
        self.assertEqual(seen[key]["user_status"], "star")
        self.assertEqual(seen[key]["user_note"], "mine")
        self.assertEqual(seen[key]["rank_score"], 77)
        # Even the collector-owned title is left alone on a row that already exists.
        self.assertEqual(seen[key]["title"], "Machine Learning Engineer")

    def test_a_german_gated_row_is_filed_not_dropped(self):
        seen = {}
        ats_fetch.merge(seen, [row(description="Sehr gute Deutschkenntnisse sind erforderlich.")],
                        "2026-08-20", silent)
        entry = seen["https://job-boards.greenhouse.io/parloa/jobs/1"]
        self.assertEqual(entry["user_status"], "gate")
        self.assertIn("AUTO-SCREEN", entry["note"])

    def test_a_new_row_carries_its_prefit_and_its_reasons(self):
        seen = {}
        ats_fetch.merge(seen, [row()], "2026-08-20", silent)
        entry = seen["https://job-boards.greenhouse.io/parloa/jobs/1"]
        self.assertEqual(entry["prefit_score"], 71)
        self.assertTrue(entry["prefit_reasons"], "a score must never appear without its reasons")


class RoundTripTest(unittest.TestCase):
    """Plan test 21: the new fields survive seen_jobs.json and a jobs.md rebuild."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._paths = (jobs_md.SEEN, jobs_md.MD, jobs_md.CSV_ACTIVE, jobs_md.CSV_EXCLUDED)
        jobs_md.SEEN = root / "seen_jobs.json"
        jobs_md.MD = root / "jobs.md"
        jobs_md.CSV_ACTIVE = root / "active.csv"
        jobs_md.CSV_EXCLUDED = root / "excluded.csv"

        def restore():
            (jobs_md.SEEN, jobs_md.MD, jobs_md.CSV_ACTIVE, jobs_md.CSV_EXCLUDED) = self._paths
        self.addCleanup(restore)

    def test_sources_and_primary_source_survive_a_write_and_a_render(self):
        key = "https://www.linkedin.com/jobs/view/4451224579"
        entry = linkedin_entry()
        entry["sources"] = [
            {"portal": "linkedin-search", "url": key, "id": "4451224579", "first_seen": "2026-08-12"},
            {"portal": "ats-search", "url": "https://job-boards.greenhouse.io/parloa/jobs/1",
             "id": "greenhouse:parloa:1", "first_seen": "2026-08-20"},
        ]
        entry["primary_source"] = "ats-search"
        entry["possible_duplicate_of"] = ["https://job-boards.greenhouse.io/parloa/jobs/2"]
        jobs_md.save_seen({key: entry})
        jobs_md.MD.write_text(jobs_md.render({key: entry}), encoding="utf-8")
        jobs_md.write_csv({key: entry})

        reloaded = json.loads(jobs_md.SEEN.read_text(encoding="utf-8"))["seen"][key]
        self.assertEqual(reloaded["primary_source"], "ats-search")
        self.assertEqual(len(reloaded["sources"]), 2)
        self.assertEqual(reloaded["possible_duplicate_of"],
                         ["https://job-boards.greenhouse.io/parloa/jobs/2"])

        # A jobs.md sync must not strip them either: parse_md reads only the two
        # cells you own, and merge writes them back onto the same dict.
        merged, _added = jobs_md.merge({key: reloaded}, jobs_md.parse_md(jobs_md.MD))
        self.assertEqual(merged[key]["primary_source"], "ats-search")
        self.assertEqual(len(merged[key]["sources"]), 2)

    def test_markdown_offers_the_first_party_link_without_losing_the_key(self):
        key = "https://www.linkedin.com/jobs/view/4451224579"
        ats = "https://job-boards.greenhouse.io/parloa/jobs/1"
        entry = linkedin_entry()
        entry["sources"] = [
            {"portal": "linkedin-search", "url": key, "id": "4451224579", "first_seen": "2026-08-12"},
            {"portal": "ats-search", "url": ats, "id": "greenhouse:parloa:1", "first_seen": "2026-08-20"},
        ]
        entry["primary_source"] = "ats-search"
        jobs_md.MD.write_text(jobs_md.render({key: entry}), encoding="utf-8")
        jobs_md.write_csv({key: entry})

        markdown = jobs_md.MD.read_text(encoding="utf-8")
        # The FIRST link is the row's key - parse_md reads it back to decide which
        # row an edit belongs to, and two rows preferring the same first-party URL
        # would otherwise make that ambiguous.
        self.assertIn("[open](%s)" % key, markdown)
        # The first-party posting is one click away all the same.
        self.assertIn("[first-party](%s)" % ats, markdown)
        row = [l for l in markdown.splitlines() if "Machine Learning Engineer" in l][0]
        self.assertLess(row.index(key), row.index(ats))
        # The CSV is generated and never read back, so it can carry the good link.
        self.assertIn(ats, jobs_md.CSV_ACTIVE.read_text(encoding="utf-8-sig"))

    def test_an_edit_is_skipped_rather_than_applied_to_the_wrong_row(self):
        """Two rows claiming one URL: there is no honest way to choose."""
        shared = "https://job-boards.greenhouse.io/parloa/jobs/1"
        seen = {
            "https://www.linkedin.com/jobs/view/1": dict(
                linkedin_entry(), url="https://www.linkedin.com/jobs/view/1",
                sources=[{"portal": "ats-search", "url": shared, "id": "a"}]),
            "https://www.linkedin.com/jobs/view/2": dict(
                linkedin_entry(), url="https://www.linkedin.com/jobs/view/2",
                sources=[{"portal": "ats-search", "url": shared, "id": "b"}]),
        }
        self.assertIsNone(jobs_md.key_for_url(seen, shared))
        merged, added = jobs_md.merge(seen, {shared: {"status": "no", "user_note": ""}})
        self.assertEqual(added, 0, "an ambiguous edit must not become a new row")
        self.assertEqual([e["user_status"] for e in merged.values()], ["star", "star"],
                         "an ambiguous edit must not land on either row")
        self.assertEqual(jobs_md.merge.ambiguous, [shared])

    def test_an_edit_made_against_the_first_party_link_lands_on_the_right_row(self):
        """The hazard the previous test creates: jobs.md no longer shows the key.

        A sync that could not map the ATS link back onto the row's key would read
        the row as hand-added and create a second, empty duplicate.
        """
        key = "https://www.linkedin.com/jobs/view/4451224579"
        ats = "https://job-boards.greenhouse.io/parloa/jobs/1"
        entry = linkedin_entry()
        entry["sources"] = [
            {"portal": "linkedin-search", "url": key, "id": "4451224579", "first_seen": "2026-08-12"},
            {"portal": "ats-search", "url": ats, "id": "greenhouse:parloa:1", "first_seen": "2026-08-20"},
        ]
        entry["primary_source"] = "ats-search"
        seen = {key: entry}
        jobs_md.MD.write_text(jobs_md.render(seen), encoding="utf-8")
        # Simulate the hand edit: change the Status cell in the job row. (Anchored
        # on the Fit cell, so the status-legend table at the top is not what gets
        # edited.)
        edited = jobs_md.MD.read_text(encoding="utf-8").replace("| `star` | High |", "| `applied` | High |", 1)
        jobs_md.MD.write_text(edited, encoding="utf-8")

        merged, added = jobs_md.merge(seen, jobs_md.parse_md(jobs_md.MD))
        self.assertEqual(added, 0, "the ATS link was read as a hand-added row")
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[key]["user_status"], "applied")


class OrderingTest(unittest.TestCase):
    """Plan §12: a freshly collected batch must not sink to the bottom."""

    def test_prefit_orders_a_batch_before_rank_has_run(self):
        rows = {
            "a": {"user_status": "new", "prefit_score": 40, "posted": "2026-08-18"},
            "b": {"user_status": "new", "prefit_score": 82, "posted": "2026-08-10"},
            "c": {"user_status": "new", "fit": "", "posted": "2026-08-19"},
        }
        order = [k for k, _ in sorted(rows.items(), key=lambda kv: jobs_md.sort_key(kv[1]))]
        self.assertEqual(order, ["b", "a", "c"])

    def test_rank_score_outranks_prefit(self):
        ranked = {"user_status": "new", "rank_score": 30, "prefit_score": 95}
        unranked = {"user_status": "new", "prefit_score": 60}
        self.assertGreater(jobs_md.priority_score(unranked), jobs_md.priority_score(ranked))

    def test_your_status_still_wins_over_any_score(self):
        starred = {"user_status": "star", "prefit_score": 1}
        new = {"user_status": "new", "prefit_score": 99}
        self.assertLess(jobs_md.sort_key(starred), jobs_md.sort_key(new))


class AtomicWriteTest(unittest.TestCase):
    """Plan test 27: an interrupted write leaves the old file intact and valid."""

    def test_a_failed_write_does_not_truncate_the_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen_jobs.json"
            jobs_md.write_json_atomic(path, {"seen": {"a": {"title": "keep me"}}})
            original = path.read_text(encoding="utf-8")

            class Unserializable:
                pass

            with self.assertRaises(TypeError):
                jobs_md.write_json_atomic(path, {"seen": Unserializable()})

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertIn("keep me", json.loads(path.read_text(encoding="utf-8"))["seen"]["a"]["title"])
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "seen_jobs.json"]
            self.assertEqual(leftovers, [], "a failed write left a temp file behind")


class CliInvocationTest(unittest.TestCase):
    """Plan tests 23-25 from the Python side: what the orchestrator asks for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._registry = ats_fetch.REGISTRY
        ats_fetch.REGISTRY = Path(self.tmp.name) / "companies.json"
        self.addCleanup(lambda: setattr(ats_fetch, "REGISTRY", self._registry))

    def test_a_dry_run_does_not_consume_the_cadence(self):
        calls = []

        def runner(args):
            calls.append(args)
            return {"meta": {"companies": [], "requests": 0}, "results": []}

        ats_fetch.collect(silent, runner=runner, dry_run=True)
        self.assertIn("--no-write", calls[0])
        calls.clear()
        ats_fetch.collect(silent, runner=runner, dry_run=False)
        self.assertNotIn("--no-write", calls[0])

    def test_one_invocation_per_run_with_both_bounds(self):
        calls = []

        def runner(args):
            calls.append(args)
            return {"meta": {"companies": [], "requests": 0}, "results": []}

        ats_fetch.collect(silent, max_companies=8, max_new_jobs=40, runner=runner)
        self.assertEqual(len(calls), 1, "the CLI is called exactly once per run")
        args = calls[0]
        self.assertIn("--max-companies", args)
        self.assertEqual(args[args.index("--max-companies") + 1], "8")
        self.assertIn("--max-new-jobs", args)
        self.assertEqual(args[args.index("--max-new-jobs") + 1], "40")

    def test_inline_descriptions_mean_zero_detail_invocations(self):
        """Plan test 24: four of five vendors ship the description with the board."""
        calls = []

        def runner(args):
            calls.append(args)
            return {"meta": {"companies": [{"name": "Parloa", "status": "ok"}], "requests": 1},
                    "results": [{"id": "greenhouse:parloa:1", "title": "ML Engineer",
                                 "company": "Parloa", "location": "Berlin", "date": "2026-08-12",
                                 "url": "https://job-boards.greenhouse.io/parloa/jobs/1",
                                 "description": "PyTorch and RAG.", "prefit_score": 70,
                                 "prefit_reasons": ["title"]}]}

        seen = {}
        summary = ats_fetch.run(silent, seen, runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual([a for a in calls[0] if a == "detail"], [])
        self.assertEqual(summary["added"], 1)

    def test_a_degraded_run_still_persists_its_successes(self):
        """Plan test 23."""
        def runner(_args):
            return {"meta": {"degraded": True, "requests": 2,
                             "companies": [{"name": "Parloa", "status": "ok"},
                                           {"name": "BrokenCo", "status": "schema_changed",
                                            "message": "no jobs array"}]},
                    "results": [{"id": "greenhouse:parloa:1", "title": "ML Engineer",
                                 "company": "Parloa", "location": "Berlin", "date": "2026-08-12",
                                 "url": "https://job-boards.greenhouse.io/parloa/jobs/1",
                                 "description": "", "prefit_score": 60, "prefit_reasons": ["t"]}]}

        seen = {}
        summary = ats_fetch.run(silent, seen, runner=runner)
        self.assertTrue(summary["degraded"])
        self.assertEqual(summary["failed"], ["BrokenCo"])
        self.assertEqual(summary["added"], 1, "the company that worked is still merged")

    def test_a_cli_that_produced_nothing_usable_is_not_a_crash(self):
        summary = ats_fetch.run(silent, {}, runner=lambda _a: None)
        self.assertEqual(summary["added"], 0)
        self.assertEqual(summary["found"], 0)

    def test_german_counters_use_the_registry_name_not_the_displayed_employer(self):
        """A venture studio's board names the portfolio company on the posting."""
        ats_fetch.REGISTRY.write_text(json.dumps({
            "defaults": {}, "companies": [
                {"name": "Merantix", "tier": 3, "status": "verified", "route": "ats",
                 "stats": {"jobs_seen": 0, "german_gated": 0, "eligible_jobs": 0,
                           "last_eligible_at": None}}]}), encoding="utf-8")
        ats_fetch.merge({}, [row(company="Merantix Momentum", registry_company="Merantix",
                                 description="Sehr gute Deutschkenntnisse erforderlich.")],
                        "2026-08-20", silent)
        stats = ats_fetch.merge({}, [row(company="Merantix Momentum",
                                         registry_company="Merantix",
                                         description="Sehr gute Deutschkenntnisse erforderlich.")],
                                "2026-08-20", silent)
        self.assertEqual(stats["german_gated_by_company"], {"Merantix": 1})

    def test_the_german_counter_counts_every_gated_posting_the_run_saw(self):
        """Per-run yield, like jobs_seen - not only the rows that were new."""
        seen = {}
        gated = row(description="Sehr gute Deutschkenntnisse erforderlich.")
        first = ats_fetch.merge(seen, [gated], "2026-08-20", silent)
        self.assertEqual(first["german_gated_by_company"], {"Parloa": 1})
        second = ats_fetch.merge(seen, [gated], "2026-08-21", silent)
        self.assertEqual(second["already_known"], 1)
        self.assertEqual(second["german_gated_by_company"], {"Parloa": 1},
                         "an already-known gated posting still counts toward the run's yield")

    def test_a_dry_run_reports_the_counters_and_writes_none(self):
        ats_fetch.REGISTRY.write_text(json.dumps({
            "defaults": {}, "companies": [
                {"name": "Parloa", "tier": 3, "status": "verified", "route": "ats",
                 "stats": {"jobs_seen": 0, "german_gated": 0, "eligible_jobs": 0,
                           "last_eligible_at": None}}]}), encoding="utf-8")
        before = ats_fetch.REGISTRY.read_text(encoding="utf-8")
        ats_fetch.summarize({"german_gated_by_company": {"Parloa": 3}}, {}, 0, silent, dry_run=True)
        self.assertEqual(ats_fetch.REGISTRY.read_text(encoding="utf-8"), before)
        ats_fetch.summarize({"german_gated_by_company": {"Parloa": 3}}, {}, 0, silent, dry_run=False)
        registry = json.loads(ats_fetch.REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(registry["companies"][0]["stats"]["german_gated"], 3)

    def test_known_ids_are_collected_from_the_board_and_handed_to_the_cli(self):
        seen = {
            "https://job-boards.greenhouse.io/parloa/jobs/1": {
                "portal": "ats-search",
                "sources": [{"portal": "ats-search", "id": "greenhouse:parloa:1"}]},
            "https://www.linkedin.com/jobs/view/1": {
                "portal": "linkedin-search",
                "sources": [{"portal": "linkedin-search", "id": "1"}]},
        }
        self.assertEqual(ats_fetch.known_ids_from(seen), {"greenhouse:parloa:1"})

    def test_rows_that_predate_composite_ids_still_count_as_known(self):
        """Otherwise a board you have had for weeks eats the new-row budget."""
        seen = {
            # No `sources` at all - collected before source history existed.
            "https://jobs.ashbyhq.com/deepjudge/41c79388-fad4-4286-948c-b279c35c568d": {
                "portal": "ats-search"},
            # A source entry carrying only the bare vendor posting id.
            "https://jobs.lever.co/sonarsource/54743786-1ad7-4be7-bcbe-67f660e9c381": {
                "portal": "ats-search",
                "sources": [{"portal": "ats-search", "id": "54743786-1ad7-4be7-bcbe-67f660e9c381"}]},
            # Collapsed row: keyed on LinkedIn, ATS link in its history.
            "https://www.linkedin.com/jobs/view/4451224579": {
                "portal": "linkedin-search", "primary_source": "ats-search",
                "sources": [
                    {"portal": "linkedin-search", "url": "https://www.linkedin.com/jobs/view/4451224579"},
                    {"portal": "ats-search", "url": "https://job-boards.greenhouse.io/parloa/jobs/7"}]},
        }
        self.assertEqual(ats_fetch.known_ids_from(seen), {
            "ashby:deepjudge:41c79388-fad4-4286-948c-b279c35c568d",
            "lever:sonarsource:54743786-1ad7-4be7-bcbe-67f660e9c381",
            "greenhouse:parloa:7",
        })

    def test_a_non_ats_row_never_contributes_an_id(self):
        seen = {"https://www.linkedin.com/jobs/view/1": {"portal": "linkedin-search"}}
        self.assertEqual(ats_fetch.known_ids_from(seen), set())

    def test_the_registry_owner_survives_the_python_boundary(self):
        """A venture studio's board names the portfolio company on the posting."""
        rows = ats_fetch.to_rows({"results": [{
            "id": "personio:merantix:1", "title": "ML Engineer",
            "company": "Merantix Momentum", "registry_company": "Merantix",
            "location": "Berlin", "date": "2026-08-12",
            "url": "https://merantix.jobs.personio.de/job/1", "description": "x",
        }]})
        self.assertEqual(rows[0]["company"], "Merantix Momentum")
        self.assertEqual(rows[0]["registry_company"], "Merantix")

        seen_ids = []

        def runner(args):
            self.assertIn("--known-ids", args)
            path = args[args.index("--known-ids") + 1]
            seen_ids.append(json.loads(Path(path).read_text(encoding="utf-8")))
            return {"meta": {"companies": [], "requests": 0}, "results": []}

        ats_fetch.collect(silent, runner=runner, known_ids={"greenhouse:parloa:1"})
        self.assertEqual(seen_ids, [["greenhouse:parloa:1"]])

    def test_german_gated_counters_land_on_the_registry(self):
        ats_fetch.REGISTRY.write_text(json.dumps({
            "defaults": {}, "companies": [
                {"name": "Parloa", "tier": 3, "status": "verified", "route": "ats",
                 "stats": {"jobs_seen": 0, "german_gated": 0, "eligible_jobs": 0,
                           "last_eligible_at": None}}]}), encoding="utf-8")

        def runner(_args):
            return {"meta": {"companies": [{"name": "Parloa", "status": "ok"}], "requests": 1},
                    "results": [{"id": "greenhouse:parloa:9", "title": "ML Engineer",
                                 "company": "Parloa", "location": "Berlin", "date": "2026-08-12",
                                 "url": "https://job-boards.greenhouse.io/parloa/jobs/9",
                                 "description": "Sehr gute Deutschkenntnisse erforderlich.",
                                 "prefit_score": 50, "prefit_reasons": ["t"]}]}

        ats_fetch.run(silent, {}, runner=runner)
        registry = json.loads(ats_fetch.REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(registry["companies"][0]["stats"]["german_gated"], 1)


if __name__ == "__main__":
    unittest.main()
