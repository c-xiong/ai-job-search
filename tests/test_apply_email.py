"""The application address read out of a posting body (tools/apply_email.py)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import apply_email  # noqa: E402


class ApplyEmailTest(unittest.TestCase):
    def test_send_your_cv_to_is_the_application_address(self):
        text = ("Join us! Contact Please send CV and motivation letter to "
                "careers@acme-medical.ch Job Responsibilities and Essential Duties: Designing.")
        hits = apply_email.extract(text)
        self.assertEqual([(h["email"], h["apply"]) for h in hits],
                         [("careers@acme-medical.ch", True)])
        self.assertIn("send CV and motivation letter", hits[0]["context"])
        self.assertEqual(apply_email.best(text), "careers@acme-medical.ch")

    def test_german_and_obfuscated_addresses(self):
        self.assertEqual(apply_email.best("**Jetzt bewerben an karriere@beispiel.de**"),
                         "karriere@beispiel.de")
        self.assertEqual(apply_email.best("How to apply: send your CV to jane [at] acme [dot] com."),
                         "jane@acme.com")

    def test_non_application_inboxes_are_dropped(self):
        text = ("If you suspect fraud, contact hr@fundco.example. "
                "If you need any accommodation, reach out to hiring@codeco.example. "
                "For questions about your data, contact: careers@mailco.example. "
                "Datenschutz: privacy@acme.ch. Sent by noreply@acme.ch")
        self.assertEqual(apply_email.extract(text), [])

    def test_a_named_contact_is_returned_but_never_the_best(self):
        text = "If you'd like to learn more about the role, contact Jan at janne@bankco.example."
        self.assertEqual([h["apply"] for h in apply_email.extract(text)], [False])
        self.assertEqual(apply_email.best(text), "")

    def test_apply_address_ranks_before_an_earlier_contact(self):
        text = ("Questions? Ask Anna (anna@acme.ch). "
                "Please submit your application documents to jobs@acme.ch.")
        self.assertEqual([h["email"] for h in apply_email.extract(text)],
                         ["jobs@acme.ch", "anna@acme.ch"])

    def test_submission_help_is_not_an_application_recipient(self):
        text = "If you need help with an application error, email hiring@example.com."
        self.assertFalse(apply_email.extract(text)[0]["apply"])
        self.assertEqual(apply_email.best(text), "")

    def test_multiple_recipients_are_preserved(self):
        text = "Send your CV to one@example.com and two@example.com."
        self.assertEqual({h["email"] for h in apply_email.extract(text)},
                         {"one@example.com", "two@example.com"})

    def test_questions_heading_across_lines(self):
        hits = apply_email.extract("Have questions?\n\nContact our team\n\njobs@example.com")
        self.assertEqual(hits[0]["purpose"], "questions")
        self.assertEqual(apply_email.guidance(hits)["recipients"], [])

    def test_instruction_does_not_bleed_into_next_sentence(self):
        hits = apply_email.extract("Send your CV to jobs@example.com. Contact team@example.com.")
        self.assertEqual([h["apply"] for h in hits], [True, False])

    def test_negative_and_help_contexts(self):
        for text in ("Do not send applications to jobs@example.com.",
                     "If you need help, email apply@example.com."):
            self.assertEqual(apply_email.guidance(apply_email.extract(text))["mode"], "unconfirmed")

    def test_joint_and_alternative_recipients(self):
        joint = apply_email.guidance(apply_email.extract("Send your CV to one@example.com and two@example.com."))
        self.assertEqual(joint["recipients"], ["one@example.com", "two@example.com"])
        alternate = apply_email.guidance(apply_email.extract("Send your CV to one@example.com or two@example.com."))
        self.assertEqual(alternate["mode"], "choose")
        self.assertEqual(alternate["recipients"], [])

    def test_later_explicit_instruction_beats_bare_address(self):
        hits = apply_email.extract("jobs@example.com\nSend your CV to jobs@example.com.")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["apply"])

    def test_markup_is_stripped_and_duplicates_collapse(self):
        text = '<p>Send your CV to <a href="mailto:jobs@acme.ch">jobs@acme.ch</a>.</p>'
        self.assertEqual([h["email"] for h in apply_email.extract(text)], ["jobs@acme.ch"])
        self.assertEqual(apply_email.extract(""), [])


if __name__ == "__main__":
    unittest.main()
