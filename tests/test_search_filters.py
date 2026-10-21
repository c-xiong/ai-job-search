"""Intake filters (tools/job_filters.py) and search preferences (tools/search_prefs.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import collectors  # noqa: E402
import job_filters  # noqa: E402
import search_prefs  # noqa: E402

DEFAULTS = job_filters.settings({})


def hit(title, opts=DEFAULTS):
    found = job_filters.title_hit(title, opts)
    return found[0] if found else None


class TitleFilterTest(unittest.TestCase):
    def test_seniority_and_founding(self):
        for title in ("Senior Software Engineer", "Staff Full Stack Engineer", "Tech Lead AI",
                      "Engineering Manager", "Principal ML Engineer", "Software Engineer IV"):
            self.assertEqual(hit(title), "seniority", title)
        self.assertEqual(hit("Founding Engineer"), "founding")

    def test_what_must_pass(self):
        for title in ("AI Engineer", "Software Engineer III, Shopping", "Member of Technical Staff, ML",
                      "Engineering - Internal AI Transformation", "Data Scientist, Marketing Analytics",
                      "Software Engineer, Security Business Enablement", "Mobile Robotics Engineer",
                      "Machine Learning Engineer Intern", "Working Student AI"):
            self.assertIsNone(hit(title), title)

    def test_groups(self):
        self.assertEqual(hit("AI Research Intern (PhD)"), "academic")
        self.assertEqual(hit("Masterarbeit KI"), "academic")
        self.assertEqual(hit("Software Engineer in Test"), "qa_test")
        self.assertEqual(hit("Embedded Software Engineer"), "embedded")
        self.assertEqual(hit("(Junior) Software Engineer - iOS"), "mobile")
        self.assertEqual(hit("Application Security Engineer"), "security")
        self.assertEqual(hit("Account Executive DACH"), "non_engineering")

    def test_devops_is_off_by_default_and_groups_switch(self):
        self.assertIsNone(hit("DevOps Engineer"))
        opts = job_filters.settings({"filters": {"groups": {"devops": True, "seniority": False}}})
        self.assertEqual(hit("DevOps Engineer", opts), "devops")
        self.assertIsNone(hit("Senior AI Engineer", opts))

    def test_allow_list_wins(self):
        opts = job_filters.settings({"filters": {"allow": ["Lead Generation"]}})
        self.assertIsNone(hit("Lead Generation Engineer", opts))


class BodyFilterTest(unittest.TestCase):
    def body(self, text):
        found = job_filters.body_hit(text, DEFAULTS)
        return found[0] if found else None

    def test_explicit_only(self):
        self.assertEqual(self.body("U.S. citizenship required"), "citizenship")
        self.assertEqual(self.body("Active security clearance required."), "citizenship")
        self.assertEqual(self.body("A PhD is required for this role."), "phd_required")
        self.assertIsNone(self.body("EU citizens or a valid work permit"))
        self.assertIsNone(self.body("Currently pursuing a PhD or recently completed a Master's degree"))
        self.assertIsNone(self.body("Currently pursuing a PhD or Master degree in CS"))
        self.assertIsNone(self.body("PhD or MSc in Computer Science"))

    def test_pay_to_intern_programme(self):
        self.assertEqual(self.body("Program Fees: 5-Week Internship: €699"), "candidate_pays")
        self.assertEqual(self.body("12-Week Internship: €1,399"), "candidate_pays")
        self.assertEqual(self.body("A tuition fee of EUR 900 applies."), "candidate_pays")
        self.assertIsNone(self.body("There are no program fees."))
        self.assertIsNone(self.body("We cover relocation fees and travel costs."))
        self.assertIsNone(self.body("Internship salary: CHF 3,000 per month"))

    def test_screen_text_gates_on_body_rules(self):
        status, note = collectors.screen_text("Requirements: U.S. citizenship required.")
        self.assertEqual(status, "gate")
        self.assertTrue(note.startswith("AUTO-SCREEN:"))


class ScreenOrderTest(unittest.TestCase):
    def test_title_filter_spends_no_detail_request(self):
        budget = collectors.Budget(5)
        status, note, _ = collectors.screen(
            {"title": "Senior AI Engineer", "portal": "linkedin-search", "id": "1"}, print, budget)
        self.assertEqual(status, "gate")
        self.assertIn("Senior", note)
        self.assertEqual(budget.used, 0)


class ApplyExistingTest(unittest.TestCase):
    def test_only_open_rows_change_and_user_note_is_kept(self):
        seen = {
            "a": {"title": "Senior Engineer", "user_status": "backlog", "user_note": "mine"},
            "b": {"title": "Senior Engineer", "user_status": "applied"},
            "c": {"title": "AI Engineer", "user_status": "new"},
        }
        changed = job_filters.apply_existing(seen, DEFAULTS)
        self.assertEqual([url for url, _, _ in changed], ["a"])
        self.assertEqual(seen["a"]["user_status"], "gate")
        self.assertEqual(seen["a"]["user_note"], "mine")
        self.assertEqual(seen["b"]["user_status"], "applied")


class SearchPrefsTest(unittest.TestCase):
    CFG = {"search_preferences": {"enabled": True, "roles": ["AI Engineer"],
                                  "job_types": ["full_time", "intern", "working_student"],
                                  "locations": ["Switzerland", "Germany"], "pages": 2},
           "linkedin": [{"q": "AI Engineer", "l": "Switzerland"}, {"q": "LLM Engineer", "l": "EU"}]}

    def test_combinations_and_german_variants(self):
        plan = search_prefs.searches(self.CFG)
        queries = [(s["q"], s["l"]) for s in plan["linkedin"] if not s.get("custom")]
        self.assertIn(("AI Engineer Intern", "Switzerland"), queries)
        self.assertIn(("Praktikum AI Engineer", "Germany"), queries)
        self.assertIn(("Werkstudent AI Engineer", "Germany"), queries)
        self.assertNotIn(("Praktikum AI Engineer", "Switzerland"), queries)
        # working student is not restricted to Switzerland
        self.assertIn(("Working Student AI Engineer", "Germany"), queries)

    def test_custom_searches_dedupe_against_generated(self):
        custom = [s for s in search_prefs.searches(self.CFG)["linkedin"] if s.get("custom")]
        self.assertEqual([s["q"] for s in custom], ["LLM Engineer"])

    def test_freehire_maps_roles_to_categories(self):
        specs = search_prefs.searches(self.CFG)["freehire"]
        self.assertTrue(all(s["category"] == "ml_ai" and s["country"] == "CH,DE" for s in specs))
        self.assertEqual(len(specs), 3)

    def test_disabled_runs_custom_only(self):
        cfg = dict(self.CFG, search_preferences=dict(self.CFG["search_preferences"], enabled=False))
        self.assertEqual(len(search_prefs.searches(cfg)["linkedin"]), 2)

    def test_budget_bounds(self):
        self.assertEqual(search_prefs.search_budget({}), search_prefs.SEARCH_BUDGET_DEFAULT)
        self.assertEqual(search_prefs.search_budget({"keyword_search_budget": 999}),
                         search_prefs.SEARCH_BUDGET_CEILING)

    def test_save_keeps_other_keys_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scrape_config.json"
            path.write_text(json.dumps({"linkedin": [{"q": "X", "l": "Y"}], "jobage_days": 7}))
            _, error = search_prefs.save({"search_preferences": {"roles": ["AI Engineer"],
                                                                 "job_types": ["intern"],
                                                                 "locations": ["Zurich"]},
                                          "filters": {"groups": {"devops": True}},
                                          "keyword_search_budget": 30}, path)
            self.assertIsNone(error)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["linkedin"], [{"q": "X", "l": "Y"}])
            self.assertEqual(saved["jobage_days"], 7)
            self.assertTrue(saved["search_preferences"]["enabled"])
            self.assertEqual(saved["filters"]["groups"], {"devops": True})
            self.assertEqual(saved["keyword_search_budget"], 30)
            self.assertIsNotNone(search_prefs.save({"search_preferences": {"job_types": ["boss"]}},
                                                   path)[1])
            self.assertIsNotNone(search_prefs.save({"filters": {"groups": {"nope": True}}}, path)[1])

    def test_task_keys_match_the_collection_session(self):
        session = collectors.CollectionSession(self.CFG, ["linkedin"])
        spec = session.tasks[0]["spec"]
        self.assertEqual(search_prefs._task_key("linkedin", spec, session.tasks[0]["page"]),
                         session.tasks[0]["key"])


if __name__ == "__main__":
    unittest.main()
