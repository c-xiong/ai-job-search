"""The half of the grounding audit that can report a deletion.

`/apply`'s audit calls a claim grounded if any one of its three sources carries
it, so a fact removed from the CV but left in the profile is an absence and is
structurally unreportable. These pin the check that does report it - including
the case that produced it, where a project deleted from the CV went on being
quoted in fit evaluations in full numeric detail.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import profile_drift  # noqa: E402


PROFILE = """# Candidate Profile

## Identity
- **Name:** Example Person

## Professional Experience

### NLP Research Assistant - Example AI Lab (Oct 2025 - Present)
- Built a pipeline over 1M articles

## Independent Projects

- **Sentinel: An Agentic Code Review System** (Jun 2026 - Present) - Python, FastAPI
  - Built a four-tier evaluation harness over 20 sample PRs
- **Annotator (ACL 2025)** (Feb 2025 - May 2025) - TypeScript, React
  - Human-in-the-loop annotation interface

## Technical Skills
- **Primary:** Python, TypeScript, PyTorch, spaCy, BERTopic, LangGraph

## Awards
- **Best poster award**, Example University
"""

CV = r"""\documentclass{moderncv}
\cventry{Oct 2025--Present}{NLP Research Assistant}{Example AI Lab}{Example City}{}{
  Built a pipeline over 1M articles}
\cventry{Feb 2025--May 2025}{Annotator}{ACL 2025 demo}{}{}{
  Human-in-the-loop annotation interface}
\cvitem{Awards}{Best poster award, Example University}
"""


class DriftTest(unittest.TestCase):
    def missing(self, profile=PROFILE, cv=CV):
        return [item["claim"] for item in profile_drift.drift(profile, cv)]

    def test_a_project_deleted_from_the_cv_is_reported(self):
        """The case this exists for."""
        self.assertIn("Sentinel: An Agentic Code Review System", self.missing())

    def test_an_entry_the_cv_carries_is_not_reported(self):
        # The CV writes "Annotator", the profile "Annotator (ACL 2025)". A
        # comparison that demanded the same words would report every entry and
        # teach you to ignore the report.
        self.assertNotIn("Annotator (ACL 2025)", self.missing())
        self.assertFalse([c for c in self.missing() if c.startswith("NLP Research Assistant")])

    def test_sections_that_are_meant_to_read_differently_are_left_alone(self):
        # The CV's skills line is a compressed rewrite of the profile's by
        # design, and LangGraph appearing in one and not the other is not drift.
        self.assertNotIn("Primary:", " ".join(self.missing()))

    def test_removing_the_entry_clears_the_report(self):
        trimmed = PROFILE.replace(
            "- **Sentinel: An Agentic Code Review System** (Jun 2026 - Present) - Python, FastAPI\n"
            "  - Built a four-tier evaluation harness over 20 sample PRs\n", "")
        self.assertEqual(self.missing(profile=trimmed), [])

    def test_latex_markup_never_hides_a_match(self):
        # The entry is in the CV, wrapped in commands and a tilde. Flattening is
        # what keeps this from being reported as missing.
        cv = r"\cventry{}{\textbf{Annotator}~(ACL~2025)}{}{}{}{}"
        self.assertNotIn("Annotator (ACL 2025)", self.missing(cv=cv))


class CommandTest(unittest.TestCase):
    def run_cli(self, profile, cv, *args):
        with tempfile.TemporaryDirectory() as tmp:
            p, c = Path(tmp) / "profile.md", Path(tmp) / "cv.tex"
            p.write_text(profile, encoding="utf-8")
            c.write_text(cv, encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = profile_drift.main(["--profile", str(p), "--cv", str(c), *args])
            return code, out.getvalue()

    def test_a_report_is_not_a_failure_unless_you_ask_for_one(self):
        # Absence is not automatically wrong: a project can be deliberately off a
        # one-page CV. The default reports and exits 0; a hook opts into strict.
        code, text = self.run_cli(PROFILE, CV)
        self.assertEqual(code, 0)
        self.assertIn("Sentinel", text)
        self.assertEqual(self.run_cli(PROFILE, CV, "--strict")[0], 1)

    def test_a_clean_pair_says_so_and_passes_strict(self):
        trimmed = PROFILE.replace("## Independent Projects", "## Nothing Here")
        code, text = self.run_cli(trimmed, CV, "--strict")
        self.assertEqual(code, 0)
        self.assertIn("No drift", text)

    def test_json_output_is_machine_readable(self):
        code, text = self.run_cli(PROFILE, CV, "--json")
        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertTrue(any("Sentinel" in row["claim"] for row in payload["missing_from_cv"]))

    def test_a_missing_file_is_distinguishable_from_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = profile_drift.main(["--profile", str(Path(tmp) / "nope.md"),
                                       "--cv", str(Path(tmp) / "nope.tex")])
        self.assertEqual(code, 2)


class RealFilesTest(unittest.TestCase):
    def test_this_fork_is_currently_in_sync(self):
        """Personalized files are local-only, so this is a no-op upstream."""
        if not (profile_drift.PROFILE.exists() and profile_drift.CV.exists()):
            self.skipTest("no personalized profile or master CV in this checkout")
        missing = profile_drift.drift(
            profile_drift.PROFILE.read_text(encoding="utf-8"),
            profile_drift.CV.read_text(encoding="utf-8"))
        self.assertEqual(missing, [], "run python3 tools/profile_drift.py")


if __name__ == "__main__":
    unittest.main()
