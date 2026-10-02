"""The deterministic fit score.

Everything here runs against the *tracked* example profile, never the private
one: CI only has the example, and a test that silently depended on a gitignored
file would pass locally and mean nothing on a fresh clone.

The properties worth protecting, in rough order of how much damage getting them
wrong would do:

  * A band never outranks a better-evidenced band. The displayed label and the
    sort order have to agree, or the column lies about the list it sits in.
  * /rank's judgement survives a recompute.
  * The skills component never subtracts (P1), and an unresolved location is
    neutral rather than zero.
  * Seniority is read from the title and the body by different rules, because
    "reporting to the Head of AI" is not a seniority requirement.
"""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fit_score  # noqa: E402
import postings  # noqa: E402


EXAMPLE = ROOT / "job_scraper" / "fit_profile.example.json"
AFFINITY_EXAMPLE = ROOT / "job_scraper" / "company_affinity.example.json"


def profile():
    return fit_score.load_profile(EXAMPLE)


def context(ratings=None, tiers=None, prof=None):
    return fit_score.Context(
        profile=prof or profile(),
        affinity={"default": 2, "companies": ratings or {}},
        tiers=tiers or {})


def geo_profile():
    """The example ships placeholder cities; give the geo tests real ones."""
    prof = copy.deepcopy(profile())
    prof["geo"].update({
        "home": ["zurich", "zürich"],
        "belt": ["zug", "winterthur"],
        "second": ["berlin"],
        "third": ["munich", "münchen"],
    })
    return prof


class ShippedConfigTest(unittest.TestCase):
    def test_the_tracked_example_loads_and_validates(self):
        self.assertEqual(sum(profile()["weights"]["max"].values()), 100)

    def test_the_tracked_affinity_example_is_valid_json(self):
        data = json.loads(AFFINITY_EXAMPLE.read_text(encoding="utf-8"))
        self.assertIn("companies", data)
        self.assertIn("default", data)

    def test_weights_that_do_not_sum_to_100_are_refused(self):
        broken = copy.deepcopy(profile())
        broken["weights"]["max"]["skills"] += 5
        with self.assertRaises(fit_score.ConfigError) as caught:
            fit_score.validate_profile(broken)
        self.assertIn("sum to 100", str(caught.exception))

    def test_inverted_bands_are_refused(self):
        broken = copy.deepcopy(profile())
        broken["weights"]["bands"] = {"high": 40, "medium": 60}
        with self.assertRaises(fit_score.ConfigError):
            fit_score.validate_profile(broken)

    def test_a_geo_bucket_without_a_score_is_refused(self):
        broken = copy.deepcopy(profile())
        broken["geo"]["order"] = list(broken["geo"]["order"]) + ["atlantis"]
        broken["geo"]["atlantis"] = ["atlantis"]
        with self.assertRaises(fit_score.ConfigError):
            fit_score.validate_profile(broken)

    def test_comment_keys_never_reach_the_scorer(self):
        self.assertNotIn("_comment", profile()["roles"])
        self.assertNotIn("_readme", profile())


class RoleFamilyTest(unittest.TestCase):
    def setUp(self):
        self.p = profile()

    def _score(self, title, body=""):
        return fit_score.role_family(title, body, self.p)[0]

    def test_the_families_are_graded_in_the_stated_order(self):
        self.assertEqual(self._score("AI Engineer"), 25)
        self.assertEqual(self._score("Software Engineer"), 22)
        self.assertEqual(self._score("Machine Learning Engineer"), 16)
        self.assertEqual(self._score("Data Scientist"), 12)

    def test_ai_engineer_and_software_engineer_sit_close_together(self):
        self.assertLessEqual(self._score("AI Engineer") - self._score("Software Engineer"), 4)

    def test_a_title_family_is_never_overridden_by_the_body(self):
        """A Data Scientist posting full of LLM wording is still Data Science."""
        body = "You will build LLM and RAG systems as an applied AI engineer."
        self.assertEqual(self._score("Data Scientist", body), 12)

    def test_a_generic_title_may_be_rescued_by_the_body_at_a_discount(self):
        body = "Join us as an applied AI engineer working on retrieval systems."
        points = self._score("Join our team", body)
        self.assertGreater(points, 0)
        self.assertLess(points, 25)

    def test_an_engineering_title_with_no_family_is_not_treated_as_off_target(self):
        """'Compiler Engineer' is a real target; 'Sales Manager' is not."""
        self.assertEqual(self._score("Compiler Engineer"), 14)
        self.assertEqual(self._score("Rust Engineer (80%-100%)"), 14)
        self.assertEqual(self._score("Junior Backend-Entwickler (m/w/d)"), 14)

    def test_a_non_engineering_title_scores_zero_and_trips_the_floor(self):
        self.assertEqual(self._score("Regional Sales Manager"), 0)
        self.assertEqual(self._score("Head of Marketing"), 0)

    def test_a_role_shape_word_caps_rather_than_competes(self):
        """An 'Agentic AI Consultant' is a consulting job about agents."""
        self.assertEqual(self._score("LLM & Agentic AI Consultant"), 5)
        self.assertEqual(self._score("AI Solutions Engineer"), 5)
        self.assertEqual(self._score("GEN AI engineer & trainer"), 5)

    def test_the_cap_does_not_lift_a_family_that_scored_below_it(self):
        self.assertEqual(self._score("Sales Engineer"), 5)

    def test_word_boundaries_hold(self):
        self.assertEqual(self._score("HTML Email Designer"), 0,
                         "'ml' must not match inside 'html'")


class SeniorityTest(unittest.TestCase):
    def setUp(self):
        self.p = profile()

    def _score(self, title, body=""):
        return fit_score.seniority(title, body, self.p)[0]

    def test_the_ladder_reads_from_the_title(self):
        self.assertEqual(self._score("Junior AI Engineer"), 25)
        self.assertEqual(self._score("AI Engineer"), 20)
        self.assertEqual(self._score("Senior AI Engineer"), 3)
        self.assertEqual(self._score("Lead AI Engineer"), 0)
        self.assertEqual(self._score("Head of AI"), 0)

    def test_the_three_year_cliff_is_where_it_was_specified(self):
        two = self._score("AI Engineer", "You have 2 years of experience.")
        three = self._score("AI Engineer", "You have 3+ years of experience.")
        self.assertEqual(two, 17)
        self.assertEqual(three, 8)
        self.assertGreaterEqual(two - three, 8, "the cliff must be a real demotion")

    def test_one_to_two_years_stays_applyable(self):
        none = self._score("AI Engineer")
        self.assertLessEqual(none - self._score("AI Engineer", "1-2 years of experience"), 3)

    def test_year_ranges_are_read_from_their_lower_bound(self):
        cases = {"0-2 years of experience": 25, "1-2 years of experience": 17,
                 "3-5 years of experience": 8, "4-5 years of experience": 3,
                 "7-10 years of experience": 0}
        for body, expected in cases.items():
            self.assertEqual(self._score("AI Engineer", body), expected, body)

    def test_en_and_em_dashes_are_read_as_ranges(self):
        for dash in ("-", "–", "—"):
            body = "3%s5 years of experience required" % dash
            self.assertEqual(self._score("AI Engineer", body), 8, dash)

    def test_years_far_from_an_experience_marker_are_ignored(self):
        body = "Our company was founded 10 years ago in a Zurich garage."
        self.assertEqual(self._score("AI Engineer", body), 20)

    def test_ladder_words_in_the_body_are_not_seniority_requirements(self):
        """The trap this rule exists for."""
        for body in ("You will report to the Head of AI.",
                     "You will work alongside senior engineers and a staff engineer.",
                     "Our lead architect will mentor you."):
            self.assertEqual(self._score("AI Engineer", body), 20, body)

    def test_when_title_and_body_disagree_the_lower_wins(self):
        self.assertEqual(self._score("Senior AI Engineer", "2 years of experience"), 3)
        self.assertEqual(self._score("AI Engineer", "8 years of experience"), 0)

    def test_the_lowest_stated_bar_is_the_one_that_counts(self):
        body = "3+ years of experience in ML. 8 years of experience with Python is a plus."
        self.assertEqual(self._score("AI Engineer", body), 8)

    def test_entry_wording_in_the_body_counts(self):
        self.assertEqual(self._score("AI Engineer", "This is a graduate programme."), 25)


class SkillOverlapTest(unittest.TestCase):
    def setUp(self):
        self.p = profile()
        self.max = self.p["weights"]["max"]["skills"]

    def _score(self, body):
        return fit_score.skill_overlap(body, self.p, self.max)[0]

    def test_it_saturates_fast(self):
        three = self._score("We use Python, PyTorch and an LLM stack.")
        self.assertGreaterEqual(three, 16)
        four = self._score("We use Python, PyTorch, LLM systems and FastAPI.")
        self.assertEqual(four, self.max)

    def test_it_never_goes_negative_and_never_exceeds_its_maximum(self):
        every = " ".join(self.p["skills"]["primary"] + self.p["skills"]["secondary"]
                         + self.p["skills"]["domain"])
        self.assertEqual(self._score(every), self.max)
        self.assertEqual(self._score("We build things with care."), 0)

    def test_an_unknown_stack_scores_zero_rather_than_a_penalty(self):
        """P1: absence is absence, not evidence against you."""
        score, hits, reason = fit_score.skill_overlap(
            "You will use Elixir and Phoenix.", self.p, self.max)
        self.assertEqual(score, 0)
        self.assertEqual(hits, [])
        self.assertNotIn("penal", reason.lower())

    def test_tiers_are_weighted(self):
        self.assertGreater(self._score("Python"), self._score("nginx"))


class ExclusionGateTest(unittest.TestCase):
    def setUp(self):
        self.p = profile()

    def _blocked(self, title, body=""):
        return fit_score.excluded_stack(title, body, self.p)

    def test_an_excluded_stack_in_the_title_gates(self):
        self.assertEqual(self._blocked(".NET Developer"), ".net")
        self.assertEqual(self._blocked("SAP ABAP Engineer"), "sap abap")

    def test_an_excluded_stack_in_a_must_have_line_gates(self):
        body = "Requirements:\nStrong C# and several years of production experience."
        self.assertEqual(self._blocked("Software Engineer", body), "c#")

    def test_a_passing_mention_does_not_gate(self):
        body = "Our billing service is still PHP; you will not have to touch it."
        self.assertIsNone(self._blocked("Backend Engineer", body))

    def test_migration_wording_is_a_counter_signal(self):
        """'C# to Java migration' is a Java job."""
        self.assertIsNone(self._blocked("C# to Java Migration Engineer"))
        body = "Requirements: experience migrating legacy PHP services to Python."
        self.assertIsNone(self._blocked("Backend Engineer", body))

    def test_nothing_excluded_means_nothing_blocked(self):
        self.assertIsNone(self._blocked("AI Engineer", "Python, PyTorch, FastAPI."))


class CompanyAffinityTest(unittest.TestCase):
    def setUp(self):
        self.p = profile()
        self.max = self.p["weights"]["max"]["company"]

    def _score(self, name, ratings=None, tiers=None):
        return fit_score.company_affinity(
            name, self.p, {"default": 2, "companies": ratings or {}},
            tiers or {}, self.max)[0]

    def test_the_full_scale_spans_the_component(self):
        self.assertEqual(self._score("X", {"X": 5}), self.max)
        self.assertEqual(self._score("X", {"X": 0}), 0)

    def test_your_rating_beats_the_registry_tier(self):
        self.assertEqual(self._score("X", {"X": 5}, {"x": 5}), self.max)

    def test_a_tier_seeds_an_unrated_company(self):
        tier_one = self._score("X", {}, {"x": 1})
        unrated = self._score("X")
        self.assertGreater(tier_one, unrated,
                           "a tier-1 company must not sit at the neutral default")

    def test_an_unrated_company_without_a_tier_is_neutral_not_zero(self):
        self.assertGreater(self._score("Nobody Has Rated This"), 0)

    def test_a_legal_suffix_does_not_break_the_match(self):
        self.assertEqual(self._score("DeepJudge AG", {"DeepJudge": 5}), self.max)

    def test_rating_a_company_lifts_it_by_a_full_band_width(self):
        """The owner requirement: rate an employer, all of its postings move."""
        self.assertGreaterEqual(self._score("X", {"X": 5}) - self._score("X"), 10)


class LocationTest(unittest.TestCase):
    def setUp(self):
        self.p = geo_profile()

    def _score(self, text):
        return fit_score.location_score(text, self.p)[0]

    def test_the_named_cities_are_tiered_but_close(self):
        home, second, third = self._score("Zurich"), self._score("Berlin"), self._score("Munich")
        self.assertGreater(home, second)
        self.assertGreater(second, third)
        self.assertLessEqual(home - third, 3, "the top tiers must stay close together")

    def test_a_commutable_town_ranks_with_the_second_city(self):
        self.assertGreaterEqual(self._score("Zug, Switzerland"), self._score("Berlin"))

    def test_the_regional_ladder_descends(self):
        self.assertGreater(self._score("Basel, Switzerland"), self._score("Hamburg, Germany"))
        self.assertGreater(self._score("Hamburg, Germany"), self._score("Lisbon, Portugal"))

    def test_an_unresolved_location_is_neutral_never_zero(self):
        for text in ("", None, "Somewhere unspecified"):
            self.assertGreater(self._score(text), 0, repr(text))

    def test_outside_europe_on_site_scores_zero(self):
        self.assertEqual(self._score("San Francisco, California"), 0)

    def test_the_most_specific_bucket_wins(self):
        self.assertEqual(self._score("Zurich, Switzerland"), self._score("Zurich"))


class BandAndPriorityTest(unittest.TestCase):
    """The invariant that keeps the column honest about the order it sits in."""

    def setUp(self):
        self.ctx = context(prof=geo_profile())

    def _score(self, title, company="Unrated", location="Zurich", body="", german=False):
        return fit_score.score(title, company, location, body, self.ctx, german_hard=german)

    BODY = ("Requirements: Python, PyTorch, FastAPI and LLM systems. You will own "
            "the retrieval pipeline and its evaluation harness end to end. We offer "
            "a learning budget and unhurried review.")

    def test_a_well_matched_evidenced_posting_reaches_high(self):
        result = self._score("AI Engineer", body=self.BODY)
        self.assertEqual(result["fit"], "high")
        self.assertEqual(result["fit_evidence"], "full")

    def test_a_title_only_row_can_never_claim_high(self):
        result = self._score("AI Engineer")
        self.assertEqual(result["fit_evidence"], "title-only")
        self.assertEqual(result["fit"], "medium")

    def test_a_capped_medium_sorts_below_a_real_high(self):
        """The reason the ceiling exists at all."""
        capped = self._score("AI Engineer", company="Loved")
        real = self._score("AI Engineer", body=self.BODY)
        self.assertEqual(real["fit"], "high")
        self.assertLess(capped["fit_priority_score"], real["fit_priority_score"])

    def test_a_gated_row_sorts_below_every_medium(self):
        gated = self._score("AI Engineer", body=self.BODY, german=True)
        medium = self._score("AI Engineer")
        self.assertEqual(gated["fit"], "low")
        self.assertLess(gated["fit_priority_score"], medium["fit_priority_score"])
        self.assertGreater(gated["fit_score"], medium["fit_score"],
                           "the raw number is preserved; only the priority is capped")

    def test_the_priority_ceiling_matches_the_band(self):
        for band, ceiling in fit_score.BAND_CEILING.items():
            self.assertLessEqual(ceiling, 100)
            self.assertGreaterEqual(ceiling, 0)
        self.assertLess(fit_score.BAND_CEILING["low"],
                        profile()["weights"]["bands"]["medium"])
        self.assertLess(fit_score.BAND_CEILING["medium"],
                        profile()["weights"]["bands"]["high"])

    def test_the_role_floor_forces_low_whatever_the_total(self):
        result = self._score("Regional Sales Manager", company="Loved", body=self.BODY)
        self.assertEqual(result["fit"], "low")

    def test_an_excluded_stack_forces_low(self):
        body = "Requirements:\nExpert C# and .NET across the whole stack, several years."
        result = self._score(".NET Developer", body=body)
        self.assertEqual(result["fit"], "low")

    def test_a_number_never_appears_without_reasons(self):
        result = self._score("AI Engineer", body=self.BODY)
        self.assertTrue(result["fit_reasons"])
        self.assertLessEqual(len(result["fit_reasons"]), fit_score.MAX_REASONS)

    def test_a_gate_always_states_itself_first(self):
        result = self._score("AI Engineer", body=self.BODY, german=True)
        self.assertIn("German", result["fit_reasons"][0])

    def test_the_parts_add_up_to_the_raw_score(self):
        result = self._score("AI Engineer", body=self.BODY)
        self.assertEqual(sum(result["fit_parts"].values()), result["fit_score"])

    def test_scoring_is_deterministic(self):
        a = self._score("AI Engineer", body=self.BODY)
        b = self._score("AI Engineer", body=self.BODY)
        self.assertEqual(a, b)

    def test_no_component_can_exceed_its_configured_maximum(self):
        maxima = profile()["weights"]["max"]
        result = self._score("AI Engineer", company="Loved", body=self.BODY)
        for name, points in result["fit_parts"].items():
            self.assertLessEqual(points, maxima[name], name)
            self.assertGreaterEqual(points, 0, name)


class RankAuthorityTest(unittest.TestCase):
    """A recompute must never quietly undo an LLM assessment."""

    def setUp(self):
        self.ctx = context(prof=geo_profile())

    def test_a_ranked_row_keeps_its_band_and_its_reasons(self):
        entry = {"title": "AI Engineer", "company": "X", "location": "Zurich",
                 "fit": "high", "fit_source": "ranked", "rank_score": 82,
                 "fit_reasons": ["/rank said so"]}
        fit_score.apply_to(entry, self.ctx)
        self.assertEqual(entry["fit"], "high")
        self.assertEqual(entry["fit_source"], "ranked")
        self.assertEqual(entry["fit_reasons"], ["/rank said so"])

    def test_a_ranked_row_still_gets_a_refreshed_number(self):
        entry = {"title": "AI Engineer", "company": "X", "location": "Zurich",
                 "fit": "high", "fit_source": "ranked", "rank_score": 82}
        fit_score.apply_to(entry, self.ctx)
        self.assertIn("fit_score", entry)
        self.assertIn("fit_parts", entry)
        self.assertEqual(entry["fit_version"], fit_score.FIT_VERSION)

    def test_a_ranked_rows_priority_respects_the_band_rank_gave_it(self):
        entry = {"title": "AI Engineer", "company": "X", "location": "Zurich",
                 "fit": "low", "fit_source": "ranked"}
        fit_score.apply_to(entry, self.ctx)
        self.assertLessEqual(entry["fit_priority_score"], fit_score.BAND_CEILING["low"])

    def test_an_unranked_row_is_scored_normally(self):
        entry = {"title": "AI Engineer", "company": "X", "location": "Zurich"}
        fit_score.apply_to(entry, self.ctx)
        self.assertEqual(entry["fit_source"], "deterministic")
        self.assertIn(entry["fit"], fit_score.BANDS)

    def test_recompute_is_idempotent(self):
        seen = {"u": {"title": "AI Engineer", "company": "X", "location": "Zurich"}}
        fit_score.recompute(seen, self.ctx)
        once = copy.deepcopy(seen["u"])
        fit_score.recompute(seen, self.ctx)
        self.assertEqual(seen["u"], once)

    def test_stale_only_skips_rows_already_at_the_current_version(self):
        seen = {"a": {"title": "AI Engineer", "fit_version": fit_score.FIT_VERSION},
                "b": {"title": "AI Engineer"}}
        touched, _ = fit_score.recompute(seen, self.ctx, only_stale=True)
        self.assertEqual(touched, 1)

    def test_recompute_reports_how_many_ranked_rows_it_preserved(self):
        seen = {"a": {"title": "AI Engineer", "fit": "high", "fit_source": "ranked"},
                "b": {"title": "AI Engineer"}}
        _, ranked = fit_score.recompute(seen, self.ctx)
        self.assertEqual(ranked, 1)


class BackfillTest(unittest.TestCase):
    """Fetching the bodies that would change an answer, and nothing else."""

    PAGE = ("<html><body><div class='job-description'>"
            "We are hiring an AI Engineer. Your tasks include building retrieval "
            "pipelines in Python and owning the evaluation harness end to end. "
            "Requirements: PyTorch, FastAPI, Docker and a habit of writing the "
            "test before the fix. You will work closely with researchers to move "
            "prototypes into production, and you will be asked to say plainly "
            "what the system cannot do yet rather than what it might one day. "
            "We offer a learning budget, flexible hours and a team that reviews "
            "each other's work carefully rather than rubber stamping it. Apply "
            "with a short note about something you shipped."
            "</div></body></html>")

    def setUp(self):
        self.ctx = context(prof=geo_profile())

    def row(self, **over):
        base = {"title": "AI Engineer", "company": "X", "location": "Zurich",
                "url": "https://example.test/1"}
        base.update(over)
        return base

    def test_eligibility_is_not_the_display_band(self):
        """The circularity this rule exists to break.

        A generic engineering row scores below `medium` precisely because it has
        no body. Gating the fetch on the band would mean it can never earn the
        body that would rescue it.
        """
        seen = {"u": self.row(title="Compiler Engineer", company="Nobody",
                              location="Hamburg, Germany")}
        scored = fit_score.score_entry(seen["u"], self.ctx)
        self.assertEqual(scored["fit"], "low")
        self.assertGreater(scored["fit_parts"]["role"], 0, "it is still an engineering job")
        self.assertEqual([url for url, _ in
                          fit_score.backfill_candidates(seen, self.ctx)], ["u"])

    def test_a_row_that_is_not_an_engineering_job_is_never_fetched(self):
        seen = {"u": self.row(title="Regional Sales Manager")}
        self.assertEqual(fit_score.backfill_candidates(seen, self.ctx), [])

    def test_rows_that_already_have_a_body_are_skipped(self):
        seen = {"u": self.row(**postings.describe("https://example.test/1", "x" * 500))}
        self.assertEqual(fit_score.backfill_candidates(seen, self.ctx), [])

    def test_rows_you_have_excluded_are_never_fetched(self):
        for status in ("no", "expired"):
            seen = {"u": self.row(user_status=status)}
            self.assertEqual(fit_score.backfill_candidates(seen, self.ctx), [], status)

    def test_the_best_candidates_come_first(self):
        seen = {"good": self.row(title="AI Engineer", location="Zurich"),
                "weak": self.row(title="Backend Engineer", location="Hamburg, Germany")}
        order = [url for url, _ in fit_score.backfill_candidates(seen, self.ctx)]
        self.assertEqual(order[0], "good")

    def test_a_stored_body_lifts_the_row_and_records_its_provenance(self):
        seen = {"u": self.row()}
        stats, pending = fit_score.backfill(
            seen, self.ctx, log=lambda _m: None,
            fetcher=lambda _url: (self.PAGE, ""))
        self.assertEqual(stats["stored"], 1)
        self.assertEqual(seen["u"]["posting_source"], "backfill")
        self.assertEqual(seen["u"]["posting_extractor"], "container")
        self.assertEqual(seen["u"]["fit_evidence"], "full")
        self.assertEqual([url for url, _ in pending], ["u"])

    def test_a_login_wall_stores_nothing_and_changes_nothing(self):
        wall = ("<html><body><div class='job-description'>Please log in to "
                "continue. Your session has expired and we need to verify you "
                "are human before showing this role. Enter your email and "
                "password to see the requirements for this position and to "
                "continue your application. Registered users can save searches "
                "and track the roles they have applied to over time."
                "</div></body></html>")
        seen = {"u": self.row()}
        before = dict(seen["u"])
        stats, pending = fit_score.backfill(
            seen, self.ctx, log=lambda _m: None, fetcher=lambda _url: (wall, ""))
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["stored"], 0)
        self.assertEqual(pending, [])
        self.assertEqual(seen["u"], before, "a rejected page must leave the row alone")

    def test_a_refused_fetch_stores_nothing(self):
        seen = {"u": self.row()}
        stats, pending = fit_score.backfill(
            seen, self.ctx, log=lambda _m: None,
            fetcher=lambda _url: (None, "robots: DISALLOWED"))
        self.assertEqual(stats["refused"], 1)
        self.assertEqual(pending, [])
        self.assertNotIn("posting_path", seen["u"])

    def test_a_transport_failure_is_counted_not_raised(self):
        def boom(_url):
            raise OSError("connection reset")
        seen = {"u": self.row()}
        stats, _pending = fit_score.backfill(
            seen, self.ctx, log=lambda _m: None, fetcher=boom)
        self.assertEqual(stats["failed"], 1)

    def test_the_run_budget_is_respected(self):
        seen = {str(i): self.row(url="https://example.test/%d" % i) for i in range(10)}
        stats, _pending = fit_score.backfill(
            seen, self.ctx, log=lambda _m: None, limit=3,
            fetcher=lambda _url: (self.PAGE, ""))
        self.assertEqual(stats["stored"], 3)

    def test_a_dry_run_fetches_nothing_and_stores_nothing(self):
        called = []
        seen = {"u": self.row()}
        stats, pending = fit_score.backfill(
            seen, self.ctx, log=lambda _m: None, dry_run=True,
            fetcher=lambda url: called.append(url) or (self.PAGE, ""))
        self.assertEqual(called, [])
        self.assertEqual(pending, [])
        self.assertEqual(stats["stored"], 0)
        self.assertNotIn("posting_path", seen["u"])

    def test_the_first_party_link_is_the_one_fetched(self):
        """A row keeps its LinkedIn key but the company's own page is the body."""
        fetched = []
        seen = {"https://www.linkedin.com/jobs/view/1": self.row(
            url="https://www.linkedin.com/jobs/view/1",
            primary_source="ats-search",
            sources=[{"portal": "ats-search",
                      "url": "https://jobs.ashbyhq.com/x/1"}])}
        fit_score.backfill(seen, self.ctx, log=lambda _m: None,
                           fetcher=lambda url: fetched.append(url) or (self.PAGE, ""))
        self.assertEqual(fetched, ["https://jobs.ashbyhq.com/x/1"])


class EntryIntegrationTest(unittest.TestCase):
    def test_a_german_gated_row_is_scored_low_from_its_stored_note(self):
        ctx = context(prof=geo_profile())
        entry = {"title": "AI Engineer", "company": "X", "location": "Zurich",
                 "user_status": "gate",
                 "note": "AUTO-SCREEN: German stated as a job condition - '...'"}
        fields = fit_score.score_entry(entry, ctx)
        self.assertEqual(fields["fit"], "low")

    def test_a_row_with_no_stored_body_is_title_only(self):
        ctx = context(prof=geo_profile())
        fields = fit_score.score_entry({"title": "AI Engineer", "company": "X"}, ctx)
        self.assertEqual(fields["fit_evidence"], "title-only")


class LanguageAndGapsTest(unittest.TestCase):
    """A German posting is a German job; a curated gap lowers a row, never buries it."""

    EN = ("Requirements: Python, PyTorch, FastAPI and LLM systems. You will own the "
          "retrieval pipeline and its evaluation harness end to end. We offer a learning "
          "budget and you will work with our team on the product that we build for you.")
    DE = ("Deine Aufgaben: Du entwickelst mit uns Python, PyTorch und LLM Systeme für "
          "unsere Kunden. Du bist Teil eines Teams, das die Zukunft der KI gestaltet. Wir "
          "bieten dir eine moderne Umgebung, in der du mit der neuesten Technologie arbeitest "
          "und dich bei uns weiterentwickeln kannst. Das ist dein Profil: Erfahrung mit RAG.")

    def setUp(self):
        self.ctx = context(prof=geo_profile())

    def _score(self, title, body):
        return fit_score.score(title, "Unrated", "Zurich", body, self.ctx)

    def test_a_posting_written_in_german_is_low_even_without_a_stated_level(self):
        english = self._score("AI Engineer", self.EN)
        german = self._score("AI Engineer", self.DE)
        self.assertEqual(english["fit_language"], "en")
        self.assertEqual(german["fit_language"], "de")
        self.assertEqual(german["fit"], "low")
        self.assertEqual(german["fit_reasons"][0], "posting is written in German")

    def test_a_bilingual_posting_is_capped_at_medium(self):
        result = self._score("AI Engineer", " ".join([self.EN, self.EN, self.DE]))
        self.assertEqual(result["fit_language"], "mixed")
        self.assertNotEqual(result["fit"], "high")

    def test_a_german_job_title_marks_a_title_only_row(self):
        self.assertEqual(fit_score.posting_language("Softwareentwickler (m/w/d)", "")[0], "de")
        self.assertEqual(fit_score.posting_language("Entwickler:in KI", "")[0], "de")
        self.assertIsNone(fit_score.posting_language("AI Engineer (m/w/d)", "")[0])

    def test_german_required_inside_an_english_posting_gates(self):
        result = self._score("AI Engineer", self.EN + " Fluent German is required.")
        self.assertEqual(result["fit"], "low")
        self.assertIn("German", result["fit_reasons"][0])

    def test_german_as_a_plus_is_named_but_does_not_gate(self):
        result = self._score("AI Engineer", self.EN + " Good German is a plus.")
        self.assertEqual(result["fit"], "high")
        self.assertEqual(result["fit_reasons"][0], "German mentioned as a plus")

    def _penalising(self, penalty=5, maximum=12):
        prof = geo_profile()
        prof["skills"].update(gap_penalty=penalty, gap_max=maximum)
        return context(prof=prof)

    def test_a_curated_gap_is_named_but_by_default_costs_nothing(self):
        clean = self._score("AI Engineer", self.EN)
        gapped = self._score("AI Engineer", self.EN.replace("FastAPI", "FastAPI, FPGA"))
        self.assertEqual(clean["fit_score"], gapped["fit_score"])
        self.assertEqual(gapped["fit"], "high")
        self.assertNotIn("gaps", gapped["fit_parts"])
        self.assertTrue(any("fpga" in r and "not scored" in r for r in gapped["fit_reasons"]))

    def test_a_gap_penalty_when_opted_into_deducts_and_is_capped(self):
        ctx = self._penalising()
        one = fit_score.score("AI Engineer", "Unrated", "Zurich",
                              self.EN.replace("FastAPI", "FastAPI, FPGA"), ctx)
        self.assertEqual(one["fit_parts"]["gaps"], -5)
        self.assertEqual(sum(one["fit_parts"].values()), one["fit_score"])
        many = fit_score.score("AI Engineer", "Unrated", "Zurich", self.EN.replace(
            "FastAPI", "FastAPI, FPGA, firmware, embedded, security clearance"), ctx)
        self.assertEqual(many["fit_parts"]["gaps"], -12)

    def test_a_gap_outside_the_requirements_is_ignored(self):
        result = fit_score.score("AI Engineer", "Unrated", "Zurich",
                                 "Our sister team builds FPGA boards. " + self.EN,
                                 self._penalising())
        self.assertNotIn("gaps", result["fit_parts"])

    def _gated(self):
        prof = geo_profile()
        prof["seniority"]["gate_levels"] = ["three_plus", "senior", "lead"]
        return context(prof=prof)

    def test_three_years_or_a_senior_title_gates_when_configured(self):
        ctx = self._gated()
        three = fit_score.score("AI Engineer", "Unrated", "Zurich",
                                self.EN + " You have 3+ years of experience.", ctx)
        self.assertEqual(three["fit"], "low")
        self.assertIn("beyond your experience", three["fit_reasons"][0])
        senior = fit_score.score("Senior AI Engineer", "Unrated", "Zurich", self.EN, ctx)
        self.assertEqual(senior["fit"], "low")

    def test_one_to_two_years_stays_applyable_under_the_gate(self):
        result = fit_score.score("AI Engineer", "Unrated", "Zurich",
                                 self.EN + " You have 2+ years of experience.", self._gated())
        self.assertNotEqual(result["fit"], "low")

    def test_an_unrated_company_gives_no_reason(self):
        result = self._score("AI Engineer", self.EN)
        self.assertFalse(any("rated" in reason for reason in result["fit_reasons"]))


if __name__ == "__main__":
    unittest.main()
