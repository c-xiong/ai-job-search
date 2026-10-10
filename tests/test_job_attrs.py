"""Job attributes (tools/job_attrs.py) and the fit deductions that read them.

Runs against the tracked example fit profile only, like test_fit_score.py.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import fit_score  # noqa: E402
import job_attrs  # noqa: E402

EXAMPLE = ROOT / "job_scraper" / "fit_profile.example.json"


def context():
    return fit_score.Context(profile=fit_score.load_profile(EXAMPLE),
                             affinity={"default": 2, "companies": {}}, tiers={})


BODY = ("We are looking for an AI Engineer to build LLM applications in Python. "
        "You will work with a small team on production systems and evaluation. ")


class JobTypeTest(unittest.TestCase):
    def test_title_decides(self):
        self.assertEqual(job_attrs.job_type("Software Engineer", ""), ["full_time"])
        self.assertEqual(job_attrs.job_type("Machine Learning Intern 2027", ""), ["intern"])
        self.assertEqual(job_attrs.job_type("Working Student (m/f/d) Applied AI", ""),
                         ["working_student"])

    def test_combined_german_ad_is_both(self):
        self.assertEqual(job_attrs.job_type("Praktikum / Werkstudent KI-Entwicklung", ""),
                         ["intern", "working_student"])

    def test_internal_is_not_intern(self):
        self.assertEqual(job_attrs.job_type("Engineering - Internal AI Transformation", ""),
                         ["full_time"])

    def test_body_only_when_title_is_silent(self):
        self.assertEqual(job_attrs.job_type("AI Engineering", "Apply as a working student."),
                         ["working_student"])
        self.assertEqual(job_attrs.job_type("Software Engineer", "Our internship program..."),
                         ["full_time"])


class CountryTest(unittest.TestCase):
    def test_locations(self):
        self.assertEqual(job_attrs.country("Zurich, Zurich, Switzerland"), "CH")
        self.assertEqual(job_attrs.country("Geneva, GENEVA, ch"), "CH")
        self.assertEqual(job_attrs.country("Berlin (Zentrale)"), "DE")
        self.assertEqual(job_attrs.country("Baden-Württemberg"), "DE")
        self.assertEqual(job_attrs.country("Baden, Aargau"), "CH")
        self.assertEqual(job_attrs.country("Amsterdam, Netherlands"), "EU")
        self.assertEqual(job_attrs.country("London"), "other")
        self.assertEqual(job_attrs.country(""), "unknown")

    def test_title_fallback(self):
        self.assertEqual(job_attrs.country("", "Machine Learning Intern 2027 (Zurich)"), "CH")


class WorkModeTest(unittest.TestCase):
    def test_location_tag_wins(self):
        self.assertEqual(job_attrs.work_mode("AI Engineer", "Zurich (Hybrid)", "fully remote"),
                         ("hybrid", ""))
        self.assertEqual(job_attrs.work_mode("Engineer", "Remote - Europe", ""),
                         ("remote", "region"))

    def test_body_remote_with_scope(self):
        body = "LOCATION This role is remote and can be executed globally."
        self.assertEqual(job_attrs.work_mode("Engineer", "United Kingdom", body),
                         ("remote", "anywhere"))
        self.assertEqual(job_attrs.work_mode(
            "Engineer", "Berlin", "Benefits: work from anywhere in Germany."),
            ("remote", "local"))

    def test_remote_perk_is_not_remote(self):
        for body in ('Option to "work from anywhere" (6 weeks/year)',
                     "100% remote work option during pregnancy",
                     "work from anywhere for up to 8 weeks per year"):
            self.assertNotEqual(job_attrs.work_mode("Engineer", "Munich", body)[0], "remote", body)

    def test_hybrid_wording(self):
        self.assertEqual(job_attrs.work_mode(
            "Engineer", "Berlin", "Based in Berlin, minimum of three days per week onsite.")[0],
            "hybrid")

    def test_silence_is_unknown(self):
        self.assertEqual(job_attrs.work_mode("Engineer", "Berlin", BODY), ("unknown", ""))


class VisaTest(unittest.TestCase):
    def test_only_explicit_wording_counts(self):
        self.assertEqual(job_attrs.visa(BODY), "unknown")
        self.assertEqual(job_attrs.visa("We are unable to sponsor visas."), "none")
        self.assertEqual(job_attrs.visa("Visa sponsorship is available."), "offered")
        self.assertEqual(job_attrs.visa("A valid Swiss work permit is required."),
                         "permit_required")


class ManualOverrideTest(unittest.TestCase):
    def test_manual_wins_and_scope_follows_mode(self):
        entry = {"attrs": {"job_type": ["full_time"], "work_mode": "remote",
                           "remote_scope": "anywhere", "country": "DE"},
                 "attrs_manual": {"work_mode": "hybrid"}}
        merged = job_attrs.effective(entry)
        self.assertEqual(merged["work_mode"], "hybrid")
        self.assertEqual(merged["remote_scope"], "")

    def test_clean_manual_validates(self):
        self.assertEqual(job_attrs.clean_manual({"job_type": ["working_student", "intern"]}),
                         ({"job_type": ["intern", "working_student"]}, None))
        self.assertIsNotNone(job_attrs.clean_manual({"job_type": []})[1])
        self.assertIsNotNone(job_attrs.clean_manual({"work_mode": "sometimes"})[1])
        self.assertIsNotNone(job_attrs.clean_manual({"salary": "high"})[1])

    def test_apply_never_touches_manual(self):
        entry = {"title": "AI Engineer", "location": "Berlin",
                 "attrs_manual": {"country": "CH"}}
        job_attrs.apply(entry, body=BODY, posting_language="en")
        self.assertEqual(entry["attrs_manual"], {"country": "CH"})
        self.assertEqual(entry["attrs"]["country"], "DE")


class DeductionTest(unittest.TestCase):
    def score(self, attrs):
        return fit_score.score("AI Engineer", "Acme", "Berlin", BODY, context(), attrs=attrs)

    def test_remote_costs_more_than_local_remote(self):
        base = self.score(None)["fit_score"]
        anywhere = self.score({"work_mode": "remote", "remote_scope": "anywhere",
                               "job_type": ["full_time"], "country": "DE"})
        local = self.score({"work_mode": "remote", "remote_scope": "local",
                            "job_type": ["full_time"], "country": "DE"})
        self.assertEqual(base - anywhere["fit_score"], 15)
        self.assertEqual(base - local["fit_score"], 5)
        self.assertEqual(anywhere["fit_parts"]["remote"], -15)
        self.assertTrue(any("remote" in loss for loss in anywhere["fit_losses"]))

    def test_german_working_student_only(self):
        base = self.score(None)["fit_score"]
        ws = self.score({"work_mode": "hybrid", "job_type": ["working_student"], "country": "DE"})
        both = self.score({"work_mode": "hybrid", "job_type": ["intern", "working_student"],
                           "country": "DE"})
        swiss = self.score({"work_mode": "hybrid", "job_type": ["working_student"],
                            "country": "CH"})
        self.assertEqual(base - ws["fit_score"], 20)
        self.assertEqual(both["fit_score"], base)
        self.assertEqual(swiss["fit_score"], base)

    def test_profile_can_override(self):
        ctx = context()
        ctx.profile["penalties"] = {"remote": {"anywhere": 3}}
        out = fit_score.score("AI Engineer", "Acme", "Berlin", BODY, ctx,
                              attrs={"work_mode": "remote", "remote_scope": "anywhere"})
        self.assertEqual(out["fit_parts"]["remote"], -3)

    def test_recompute_does_not_detect(self):
        entry = {"title": "AI Engineer", "company": "Acme", "location": "Remote"}
        fit_score.recompute({"u": entry}, context())
        self.assertNotIn("attrs", entry)
        fit_score.apply_to(entry, context(), body=BODY)
        self.assertEqual(entry["attrs"]["work_mode"], "remote")
        self.assertIn("remote", entry["fit_parts"])


if __name__ == "__main__":
    unittest.main()
