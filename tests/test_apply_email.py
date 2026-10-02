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

    def test_markup_is_stripped_and_duplicates_collapse(self):
        text = '<p>Send your CV to <a href="mailto:jobs@acme.ch">jobs@acme.ch</a>.</p>'
        self.assertEqual([h["email"] for h in apply_email.extract(text)], ["jobs@acme.ch"])
        self.assertEqual(apply_email.extract(""), [])


if __name__ == "__main__":
    unittest.main()
