"""Posting extraction and its quality gate.

fetch_url.py prints the raw HTTP body, so something has to decide what part of
a careers page is the posting - and, more importantly, when the page is not a
posting at all. The gate fails closed: on refusal the caller stores nothing and
does not upgrade the row's evidence, so these tests care as much about what is
rejected as about what is extracted.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import posting_text  # noqa: E402


BODY = (
    "We are hiring an AI Engineer in Zurich. Your tasks include building "
    "retrieval augmented generation pipelines in Python, shipping them behind "
    "FastAPI, and owning the evaluation harness end to end. Requirements: "
    "strong software engineering fundamentals, hands on experience with "
    "PyTorch and transformers, and comfort with Docker and AWS. We offer a "
    "learning budget, flexible hours, and a team that reviews each other's "
    "work carefully rather than rubber stamping it. Apply with a short note "
    "about something you have shipped and what you would do differently now."
)

CHROME = (
    "<nav><a href='/'>Home</a><a href='/jobs'>All openings</a></nav>"
    "<header><h1>Acme Careers</h1></header>"
    "<script>window.dataLayer=[{'cookieConsent':true}];</script>"
    "<style>.banner{display:none}</style>"
    "<footer><p>Acme AG. We use cookies to improve your experience. "
    "Impressum. Datenschutz.</p></footer>"
)


def _page(inner, extra=""):
    return "<html><body>%s%s%s</body></html>" % (CHROME, inner, extra)


class JsonLdTest(unittest.TestCase):
    def test_json_ld_description_wins(self):
        html = _page(
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"AI Engineer","description":"%s"}'
            "</script><div class='sidebar'>Related jobs</div>" % BODY)
        result = posting_text.extract(html)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.extractor, "json-ld")
        self.assertIn("retrieval augmented generation", result.text)
        self.assertNotIn("Related jobs", result.text)

    def test_html_inside_a_json_ld_description_is_stripped(self):
        html = _page(
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","description":"<p>%s</p><ul><li>Python</li></ul>"}'
            "</script>" % BODY)
        result = posting_text.extract(html)
        self.assertTrue(result.ok, result.reason)
        self.assertNotIn("<p>", result.text)
        self.assertIn("Python", result.text)

    def test_json_ld_nested_in_a_graph_is_found(self):
        html = _page(
            '<script type="application/ld+json">'
            '{"@context":"https://schema.org","@graph":['
            '{"@type":"Organization","name":"Acme"},'
            '{"@type":"JobPosting","description":"%s"}]}'
            "</script>" % BODY)
        result = posting_text.extract(html)
        self.assertEqual(result.extractor, "json-ld")
        self.assertTrue(result.ok, result.reason)

    def test_a_bare_list_of_json_ld_objects_is_found(self):
        html = _page(
            '<script type="application/ld+json">'
            '[{"@type":"WebSite"},{"@type":"JobPosting","description":"%s"}]'
            "</script>" % BODY)
        self.assertEqual(posting_text.extract(html).extractor, "json-ld")

    def test_invalid_json_ld_falls_through_instead_of_raising(self):
        html = _page(
            '<script type="application/ld+json">{ this is not json }</script>'
            "<div class='job-description'>%s</div>" % BODY)
        result = posting_text.extract(html)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.extractor, "container")

    def test_a_trailing_comma_is_repaired(self):
        html = _page(
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","description":"%s",}</script>' % BODY)
        self.assertEqual(posting_text.extract(html).extractor, "json-ld")

    def test_non_jobposting_structured_data_is_ignored(self):
        html = _page(
            '<script type="application/ld+json">'
            '{"@type":"BreadcrumbList","description":"Home > Jobs"}</script>'
            "<div class='job-description'>%s</div>" % BODY)
        result = posting_text.extract(html)
        self.assertEqual(result.extractor, "container")
        self.assertNotIn("Home > Jobs", result.text)


class ContainerTest(unittest.TestCase):
    def test_a_named_container_is_preferred_over_the_whole_page(self):
        html = _page("<div class='job-description'>%s</div>" % BODY)
        result = posting_text.extract(html)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.extractor, "container")
        self.assertNotIn("Impressum", result.text)
        self.assertNotIn("All openings", result.text)

    def test_data_attribute_containers_are_recognised(self):
        html = _page(
            '<div data-automation-id="jobPostingDescription">%s</div>' % BODY)
        self.assertEqual(posting_text.extract(html).extractor, "container")

    def test_the_largest_matching_container_wins(self):
        html = _page(
            "<div class='description'>Short teaser.</div>"
            "<div class='job-description'>%s</div>" % BODY)
        result = posting_text.extract(html)
        self.assertIn("retrieval augmented generation", result.text)

    def test_block_elements_become_line_breaks(self):
        html = _page(
            "<div class='job-description'><p>%s</p><ul><li>Python</li>"
            "<li>PyTorch</li></ul></div>" % BODY)
        result = posting_text.extract(html)
        self.assertIn("Python\nPyTorch", result.text)

    def test_entities_are_decoded(self):
        html = _page(
            "<div class='job-description'>%s R&amp;D team &mdash; join us."
            "</div>" % BODY)
        result = posting_text.extract(html)
        self.assertIn("R&D team", result.text)
        self.assertNotIn("&amp;", result.text)


class FallbackTest(unittest.TestCase):
    def test_page_without_a_named_container_still_extracts(self):
        html = _page("<div><span>%s</span></div>" % BODY)
        result = posting_text.extract(html)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.extractor, "fallback")

    def test_chrome_is_removed_even_on_the_fallback_path(self):
        html = _page("<div><span>%s</span></div>" % BODY)
        text = posting_text.extract(html).text
        self.assertNotIn("cookieConsent", text)
        self.assertNotIn("display:none", text)
        self.assertNotIn("Impressum", text)
        self.assertNotIn("All openings", text)


class PlainTextTest(unittest.TestCase):
    def test_plain_text_is_gated_but_not_parsed_as_html(self):
        result = posting_text.extract(BODY)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.extractor, "inline")
        self.assertIn("Requirements", result.text)

    def test_looks_like_html_detects_markup(self):
        self.assertTrue(posting_text.looks_like_html("<html><body>x</body></html>"))
        self.assertTrue(posting_text.looks_like_html("  <div class='a'>x</div>"))
        self.assertFalse(posting_text.looks_like_html(BODY))
        self.assertFalse(posting_text.looks_like_html(""))


class GateTest(unittest.TestCase):
    """The half that matters most: what must never be stored."""

    def test_a_login_wall_is_refused(self):
        # Long enough to clear the length floor, so the wall check is what
        # actually rejects it rather than the body being short.
        html = _page(
            "<div class='job-description'><h2>Sign in</h2>"
            "<p>Please log in to continue. Your session has expired and we "
            "need to verify you are human before showing this role. "
            "Enter your email address and password to see the requirements "
            "for this position and continue your application. Registered "
            "users can save searches, track the roles they have applied to, "
            "and receive alerts when a similar opening is published. If you "
            "have forgotten your password use the reset link below and we "
            "will send you a message within a few minutes.</p></div>")
        result = posting_text.extract(html)
        self.assertGreaterEqual(len(result.text), posting_text.MIN_CHARS)
        self.assertFalse(result.ok)
        self.assertIn("wall", result.reason)

    def test_a_bot_check_page_is_refused(self):
        html = _page(
            "<div class='description'><p>Checking your browser before "
            "accessing this site. This process is automatic and your "
            "experience will be verified shortly. Please enable JavaScript "
            "and cookies to continue to the requirements page.</p></div>")
        result = posting_text.extract(html)
        self.assertFalse(result.ok)

    def test_an_expired_posting_is_refused(self):
        html = _page(
            "<div class='job-description'><p>This position is no longer "
            "available. The role has been filled and we are no longer "
            "accepting applications. Please browse our other openings to "
            "find requirements that match your experience and skills.</p>"
            "</div>")
        self.assertFalse(posting_text.extract(html).ok)

    def test_a_long_page_mentioning_a_wall_phrase_still_passes(self):
        """A real posting may say "sign in to apply" - length is the tiebreaker."""
        html = _page(
            "<div class='job-description'>%s %s Please log in to continue "
            "your application once you have read the full description."
            "</div>" % (BODY, BODY))
        result = posting_text.extract(html)
        self.assertTrue(result.ok, result.reason)

    def test_too_short_is_refused(self):
        html = _page("<div class='job-description'>Requirements: Python.</div>")
        result = posting_text.extract(html)
        self.assertFalse(result.ok)
        self.assertIn("too short", result.reason)

    def test_prose_without_requirement_wording_is_refused(self):
        filler = (
            "Acme was founded in nineteen ninety eight and today serves "
            "customers across the continent from three regional hubs. Our "
            "culture rests on curiosity and a stubborn refusal to ship "
            "anything we would not use ourselves. The company remains "
            "privately held and reinvests its surplus into research. "
        ) * 2
        html = _page("<div class='job-description'>%s</div>" % filler)
        result = posting_text.extract(html)
        self.assertFalse(result.ok)
        self.assertIn("requirement", result.reason)

    def test_german_requirement_wording_passes_the_gate(self):
        german = (
            "Deine Aufgaben: Du entwickelst Pipelines in Python und bringst "
            "sie in Produktion. Du betreust die Evaluationsumgebung und "
            "arbeitest eng mit dem Forschungsteam zusammen. Dein Profil: "
            "abgeschlossenes Studium der Informatik, sehr gute Kenntnisse in "
            "PyTorch, Erfahrung mit Docker und Cloud Infrastruktur. "
            "Wir bieten flexible Arbeitszeiten und ein grosszuegiges Budget "
            "fuer Weiterbildung sowie ein modernes Buero beim Hauptbahnhof."
        )
        ok, reason = posting_text.check(german)
        self.assertTrue(ok, reason)

    def test_empty_body_is_refused_with_a_reason(self):
        result = posting_text.extract("")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "empty response body")

    def test_a_rejected_body_is_still_returned_for_diagnosis(self):
        html = _page("<div class='job-description'>Requirements: Python.</div>")
        result = posting_text.extract(html)
        self.assertFalse(result.ok)
        self.assertTrue(result.text)
        self.assertEqual(result.extractor, "container")


if __name__ == "__main__":
    unittest.main()
