"""Fixed prose survives generation; only the actual custom regions stay free."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.board import checkpoint, cover_blocks, docs


TEXTS = {
    "opening_ai": ("opening", "I build useful NLP systems. I research language models."),
    "agent": ("highlight", "I built a contract agent. It checks citations before answering."),
    "product": ("highlight", "I took a marketplace from design to production for 1,200 users."),
    "model": ("highlight", "I evaluated a language model on classification tasks."),
    "paper": ("research", "I first-authored a conference paper on reviewing edge cases."),
}
BASE = "% COVER_LIBRARY_V1\n" + "\n".join(
    "%% COVER_TEXT %s %s\n%% %s\n%% END_COVER_TEXT" % (ident, kind, text)
    for ident, (kind, text) in TEXTS.items())
PREAMBLE = ("\\documentclass{cover}\n\\usepackage{enumitem}\n"
            "\\geometry{left=2.3cm,right=2.3cm,top=1.6cm,bottom=2.0cm}\n"
            "\\pagestyle{empty}\n")


def block(ident):
    kind, text = TEXTS[ident]
    content = ("\\item " + text if kind == "highlight" else "\\lettercontent{" + text + "}")
    return "%% USE_COVER_TEXT %s\n%s\n%% END_USE_COVER_TEXT\n" % (ident, content)


def letter(research=True, highlights=("agent", "product")):
    return ("\\documentclass{cover}\n\\begin{document}\n"
            "\\senderblock{Jane}{Zurich\\\\\\href{mailto:jane@example.test}{jane@example.test}}\n"
            "\\recipientblock{Acme}\n\\currentdate{Zurich, 1 October 2026}\n"
            "\\subjectline{Application for Engineer}\n"
            "\\lettercontent{Dear Acme Team,}\n"
            "\\lettercontent{Acme's editorial workflows offer the chance to build dependable agents.}\n" +
            block("opening_ai") +
            "{\\raggedright\\letterbodyfont\n\\begin{itemize}[leftmargin=1.2em]\n" +
            "".join(block(ident) for ident in highlights) +
            "\\end{itemize}\\par}\n\\vspace{6pt}\n" +
            (block("paper") if research else "") +
            "\\lettercontent{Your work on automation interests me. I would welcome a conversation.}\n"
            "\\closing{Kind regards,}\n\\signature{Jane}\n\\end{document}\n")


LAYOUT_BASE = BASE + "\n" + letter().replace("\\documentclass{cover}\n", PREAMBLE)


class FixedBlocksTest(unittest.TestCase):
    def assert_valid(self, source, base=BASE):
        result = cover_blocks.validate_fixed_blocks(source, base)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["issues"], [])
        return result

    def assert_invalid(self, source, message, base=BASE):
        result = cover_blocks.validate_fixed_blocks(source, base)
        self.assertTrue(result["enabled"])
        self.assertIn(message, "; ".join(result["issues"]))

    def test_approved_text_and_optional_research(self):
        self.assertEqual(self.assert_valid(letter())["selected"],
                         ["opening_ai", "agent", "product", "paper"])
        self.assert_valid(letter(research=False))
        self.assert_valid(letter(highlights=("agent", "model", "product")))

    def test_whitespace_and_both_tailored_paragraphs_may_change(self):
        self.assert_valid(letter().replace("I built a contract agent.", "I built\n   a contract agent.")
                          .replace("Acme's editorial workflows offer the chance to build dependable agents.",
                                   "Building reliable tools for Acme's scheduling workflows interests me.")
                          .replace("Your work on automation interests me.",
                                   "Acme's focus on booking workflows interests me."))
        self.assert_valid(letter(), BASE.replace("I build useful", "I build\n% useful"))

    def test_preamble_instructions_are_not_selected_body_markers(self):
        instructions = "% USE_COVER_TEXT / END_USE_COVER_TEXT markers.\n"
        self.assert_valid(instructions + BASE + "\n" + letter())

    def test_rewriting_even_one_word_or_punctuation_fails(self):
        for old, new in (("It checks", "It validates"), ("1,200 users.", "1,200 users!")):
            with self.subTest(new=new):
                self.assert_invalid(letter().replace(old, new), "approved text was changed")

    def test_removed_markers_and_optional_text_rewritten_without_markers_fail(self):
        unmarked = block("agent").replace("% USE_COVER_TEXT agent\n", "").replace(
            "% END_USE_COVER_TEXT\n", "")
        self.assert_invalid(letter().replace(block("agent"), unmarked),
                            "every list item must use an approved text block")
        self.assert_invalid(letter().replace(block("paper"),
                            "\\lettercontent{I wrote a different paper.}\n"),
                            "exactly the salutation, role motivation and closing")

    def test_duplicate_or_unknown_selected_ids_fail(self):
        self.assert_invalid(letter(highlights=("agent", "agent")), "duplicate selected ID")
        self.assert_invalid(letter().replace("USE_COVER_TEXT agent", "USE_COVER_TEXT unknown"),
                            "unknown selected ID")

    def test_wrong_wrapper_fails(self):
        self.assert_invalid(letter().replace("\\item " + TEXTS["agent"][1],
                            "\\lettercontent{" + TEXTS["agent"][1] + "}"),
                            "invalid highlight wrapper")
        self.assert_invalid(letter().replace("\\lettercontent{" + TEXTS["opening_ai"][1] + "}",
                            "\\item " + TEXTS["opening_ai"][1]), "invalid opening wrapper")

    def test_kind_count_and_order_are_fixed_but_project_order_is_free(self):
        self.assert_valid(letter(highlights=("product", "agent")))
        self.assert_invalid(letter(highlights=("agent",)), "select one opening")
        self.assert_invalid(letter().replace(block("opening_ai"), "")
                            .replace("\\end{itemize}", "\\end{itemize}\n" + block("opening_ai")),
                            "must follow opening, highlights, research order")

    def test_exclusive_groups_prevent_reusing_the_same_experience(self):
        base = BASE + "\n% COVER_EXCLUSIVE model paper\n% COVER_EXCLUSIVE agent model\n"
        self.assert_valid(letter(), base)
        self.assert_valid(letter(research=False, highlights=("model", "product")), base)
        self.assert_invalid(letter(highlights=("model", "product")),
                            "mutually exclusive blocks selected: model, paper", base)
        self.assert_invalid(letter(research=False, highlights=("agent", "model", "product")),
                            "mutually exclusive blocks selected: agent, model", base)

    def test_invalid_exclusion_declarations_do_not_silently_disable_checks(self):
        for declaration in ("% COVER_EXCLUSIVE", "% COVER_EXCLUSIVE agent",
                            "% COVER_EXCLUSIVE agent agent", "% COVER_EXCLUSIVE Agent model",
                            "% COVER_EXCLUSIVE agent missing"):
            with self.subTest(declaration=declaration):
                self.assert_invalid(letter(), "COVER_EXCLUSIVE", BASE + "\n" + declaration)

    def test_research_and_highlights_must_use_their_correct_location(self):
        moved = letter().replace(block("paper"), "").replace(
            "\\end{itemize}", block("paper") + "\\end{itemize}")
        self.assert_invalid(moved, "only selected highlights may appear inside")
        moved = letter().replace(block("product"), "").replace(
            "\\end{itemize}", "\\end{itemize}\n" + block("product"))
        self.assert_invalid(moved, "only selected highlights may appear inside")

    def test_unmarked_prose_or_extra_lists_cannot_bypass_the_blocks(self):
        for extra in ("Unapproved new sentence.", "\\textbf{Unapproved sentence.}",
                      "\\lettercontent{An extra paragraph.}", "\\item A new item.",
                      "\\begin{itemize}\\end{itemize}",
                      "\\subjectline{Unapproved body prose.}"):
            with self.subTest(extra=extra):
                result = cover_blocks.validate_fixed_blocks(
                    letter().replace("\\closing{", extra + "\n\\closing{"), BASE)
                self.assertTrue(result["issues"])

    def test_missing_or_misplaced_custom_paragraphs_fail(self):
        self.assert_invalid(letter().replace("\\lettercontent{Dear Acme Team,}\n", ""),
                            "exactly the salutation, role motivation and closing")
        self.assert_invalid(letter().replace("Dear Acme Team,", "An unapproved introduction."),
                            "must be the salutation")
        source = letter().replace("\\lettercontent{Dear Acme Team,}\n", "")
        source = source.replace(block("opening_ai"), block("opening_ai") +
                                "\\lettercontent{Dear Acme Team,}\n")
        self.assert_invalid(source, "the first unmarked lettercontent must be the salutation")

    def test_role_motivation_is_required_before_the_fixed_introduction(self):
        motivation = ("\\lettercontent{Acme's editorial workflows offer the chance "
                      "to build dependable agents.}\n")
        self.assert_invalid(letter().replace(motivation, ""),
                            "exactly the salutation, role motivation and closing")
        self.assert_invalid(letter().replace(motivation, "\\lettercontent{  }\n"),
                            "role motivation must be non-empty")
        for placeholder in ("[ROLE MOTIVATION]", "[ROLE_MOTIVATION]"):
            with self.subTest(placeholder=placeholder):
                self.assert_invalid(letter().replace(motivation,
                                    "\\lettercontent{%s}\n" % placeholder),
                                    "role motivation must not contain unresolved placeholders")
        moved = letter().replace(motivation, "").replace(
            block("opening_ai"), block("opening_ai") + motivation)
        self.assert_invalid(moved, "role motivation must precede the fixed opening")
        in_list = letter().replace(motivation, "").replace(
            block("agent"), motivation + block("agent"))
        self.assert_invalid(in_list, "custom paragraphs must remain outside the highlight list")

    def test_tailored_closing_must_follow_all_selected_evidence(self):
        closing = ("\\lettercontent{Your work on automation interests me. "
                   "I would welcome a conversation.}\n")
        source = letter().replace(closing, "").replace(
            block("paper"), closing + block("paper"))
        self.assert_invalid(source, "closing must follow")

    def test_incomplete_or_malformed_markers_fail(self):
        for source in (letter().replace("% END_USE_COVER_TEXT", "", 1),
                       letter().replace("USE_COVER_TEXT agent", "USE_COVER_TEXT Agent"),
                       letter().replace("\\end{document}",
                                        "% END_USE_COVER_TEXT\n\\end{document}")):
            with self.subTest(source=source[-60:]):
                self.assertTrue(cover_blocks.validate_fixed_blocks(source, BASE)["issues"])

    def test_a_library_comment_cannot_pass_as_visible_selected_text(self):
        self.assert_invalid(letter().replace("\\item " + TEXTS["agent"][1],
                                             "% \\item " + TEXTS["agent"][1]),
                            "invalid highlight wrapper")
        self.assert_invalid(BASE + "\n" + letter().replace(block("agent"), ""),
                            "select one opening")

    def test_malformed_canonical_library_fails(self):
        for base in (BASE + "\n% COVER_TEXT agent highlight\n% Duplicate.\n% END_COVER_TEXT",
                     "% COVER_LIBRARY_V1\n", BASE.replace("agent highlight", "agent other"),
                     BASE + "\n% COVER_TEXT unfinished highlight\n% No end."):
            with self.subTest(base=base[-60:]):
                self.assertTrue(cover_blocks.validate_fixed_blocks(letter(), base)["issues"])

    def test_old_and_custom_bases_remain_compatible(self):
        self.assertEqual(cover_blocks.validate_fixed_blocks("arbitrary old letter", "old base"),
                         {"enabled": False, "selected": [], "issues": []})


class ProseBlocksTest(unittest.TestCase):
    """New paragraph bases preserve the same factual and structural boundaries."""

    def source(self):
        source = letter()
        source = source.replace("{\\raggedright\\letterbodyfont\n\\begin{itemize}[leftmargin=1.2em]\n", "")
        source = source.replace("\\end{itemize}\\par}\n\\vspace{6pt}\n", "")
        for ident in ("agent", "product"):
            source = source.replace("\\item " + TEXTS[ident][1],
                                    "\\lettercontent{" + TEXTS[ident][1] + "}")
        return source

    def check(self, source):
        return cover_blocks.validate_fixed_blocks(source, BASE + "\n% COVER_FORMAT paragraphs\n")

    def test_prose_preserves_approved_text_and_layout(self):
        self.assertEqual(self.check(self.source())["issues"], [])
        source = self.source().replace("\\documentclass{cover}\n", PREAMBLE)
        prose_base = BASE + "\n% COVER_FORMAT paragraphs\n" + source
        self.assertEqual(cover_blocks.validate_layout(source, prose_base)["issues"], [])

    def test_lists_and_mixed_wrappers_are_rejected(self):
        self.assertTrue(self.check(letter())["issues"])
        for extra in ("\\begin{itemize}\\end{itemize}",
                      "\\begin{enumerate}\\end{enumerate}", "\\item New claim."):
            with self.subTest(extra=extra):
                self.assertTrue(self.check(self.source().replace("\\closing{", extra + "\n\\closing{"))["issues"])

    def test_prose_does_not_relax_facts_markers_or_order(self):
        for source in (
                self.source().replace("1,200 users", "12,000 users"),
                self.source().replace("% USE_COVER_TEXT agent\n", ""),
                self.source().replace("\\closing{", "\\lettercontent{Extra claim.}\n\\closing{"),
                self.source().replace("USE_COVER_TEXT agent", "USE_COVER_TEXT product"),
                self.source().replace("USE_COVER_TEXT opening_ai", "USE_COVER_TEXT agent")):
            with self.subTest(source=source):
                self.assertTrue(self.check(source)["issues"])


class CoverLayoutTest(unittest.TestCase):
    def source(self):
        return letter().replace("\\documentclass{cover}\n", PREAMBLE)

    def test_approved_preamble_survives_comments_and_whitespace_changes(self):
        source = self.source().replace("left=2.3cm,", "left = 2.3cm,\n  ")
        source = source.replace("\\pagestyle{empty}", "% harmless assembly note\n\\pagestyle{empty}")
        self.assertEqual(cover_blocks.validate_layout(source, LAYOUT_BASE),
                         {"enabled": True, "issues": []})

    def test_margin_font_class_and_package_overrides_fail(self):
        for source in (
                self.source().replace("top=1.6cm", "top=0.5cm"),
                self.source().replace("\\pagestyle{empty}",
                                      "\\pagestyle{empty}\n\\renewcommand{\\letterbodyfont}{\\small}"),
                self.source().replace("\\begin{document}",
                                      "\\fontsize{8pt}{9pt}\\selectfont\n\\begin{document}"),
                self.source().replace("\\documentclass{cover}", "\\documentclass{article}"),
                self.source().replace("\\usepackage{enumitem}",
                                      "\\usepackage{enumitem}\n\\usepackage{parskip}")):
            with self.subTest(source=source[:120]):
                result = cover_blocks.validate_layout(source, LAYOUT_BASE)
                self.assertIn("approved cover preamble changed", "; ".join(result["issues"]))

    def test_geometry_cannot_be_reset_inside_the_body(self):
        for command in ("\\geometry{margin=1cm}", "\\newgeometry{margin=1cm}",
                        "\\restoregeometry", "\\loadgeometry{compact}"):
            with self.subTest(command=command):
                source = self.source().replace("\\begin{document}", "\\begin{document}\n" + command)
                self.assertIn("cover geometry must not be overridden",
                              "; ".join(cover_blocks.validate_layout(source, LAYOUT_BASE)["issues"]))

    def test_list_spacing_cannot_squeeze_the_letter(self):
        for options in ("leftmargin=1.2em,topsep=-20pt,itemsep=-20pt,parsep=-20pt",
                        "leftmargin=0.4em", "leftmargin=1.2em,itemsep=0pt"):
            with self.subTest(options=options):
                source = self.source().replace("[leftmargin=1.2em]", "[" + options + "]")
                self.assertIn("itemize options must match", "; ".join(
                    cover_blocks.validate_layout(source, LAYOUT_BASE)["issues"]))

    def test_vertical_spacing_cannot_be_removed_reduced_or_added(self):
        for replacement in ("", "\\vspace{0pt}", "\\vspace{3pt}",
                            "\\vspace*{6pt}", "\\vspace{6pt}\\vspace{0pt}"):
            with self.subTest(replacement=replacement):
                source = self.source().replace("\\vspace{6pt}", replacement)
                self.assertIn("vertical spacing commands must match", "; ".join(
                    cover_blocks.validate_layout(source, LAYOUT_BASE)["issues"]))

    def test_list_body_font_and_group_must_remain_intact(self):
        for source in (self.source().replace("\\raggedright\\letterbodyfont", ""),
                       self.source().replace("\\letterbodyfont\n", "\n"),
                       self.source().replace("\\raggedright", ""),
                       self.source().replace("\\end{itemize}\\par}", "\\end{itemize}}")):
            with self.subTest(source=source[source.index("\\begin{itemize}") - 35:][:100]):
                self.assertIn("approved raggedright/letterbodyfont group", "; ".join(
                    cover_blocks.validate_layout(source, LAYOUT_BASE)["issues"]))

    def test_legacy_and_custom_bases_without_layout_opt_in_keep_their_behaviour(self):
        for base in (BASE, PREAMBLE + "\\begin{document}old letter\\end{document}", "legacy"):
            with self.subTest(base=base[:40]):
                self.assertEqual(cover_blocks.validate_layout("arbitrary custom source", base),
                                 {"enabled": False, "issues": []})


class FixedBlocksIntegrationTest(unittest.TestCase):
    def test_legacy_base_adds_no_check_and_does_not_downgrade_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            base, source = Path(directory) / "base.tex", Path(directory) / "cover.tex"
            base.write_text("legacy base without a fixed-text library")
            source.write_text(letter())
            with mock.patch.object(docs, "cover_base", return_value=base), \
                 mock.patch.object(docs, "pdf_pages", return_value=1), \
                 mock.patch.object(docs, "extract_text", return_value=(
                     "Jane\njane@example.test\nApplication for Engineer", {})), \
                 mock.patch.object(docs, "_contact_literals", return_value=["jane@example.test"]):
                state, checks, _ = docs.check_pdf("cover", Path(directory) / "cover.pdf",
                                                   source, [], {})
            self.assertEqual(state, "pass")
            self.assertNotIn("cover_fixed_blocks", [check["id"] for check in checks])
            self.assertNotIn("cover_layout", [check["id"] for check in checks])

    def test_pdf_check_reports_fixed_prose_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            base, source = Path(directory) / "base.tex", Path(directory) / "cover.tex"
            base.write_text(BASE)
            source.write_text(letter().replace("It checks", "It guarantees"))
            with mock.patch.object(docs, "cover_base", return_value=base), \
                 mock.patch.object(docs, "pdf_pages", return_value=1), \
                 mock.patch.object(docs, "extract_text", return_value=(None, {})):
                state, checks, _ = docs.check_pdf("cover", Path(directory) / "cover.pdf",
                                                   source, [], {})
            self.assertEqual(state, "fail")
            fixed = next(check for check in checks if check["id"] == "cover_fixed_blocks")
            self.assertEqual(fixed["state"], "fail")
            self.assertIn("approved text was changed", fixed["detail"])

    def test_pdf_check_rejects_unapproved_layout_even_when_prose_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            base, source = Path(directory) / "base.tex", Path(directory) / "cover.tex"
            base.write_text(LAYOUT_BASE)
            source.write_text(letter().replace("\\documentclass{cover}\n", PREAMBLE)
                              .replace("left=2.3cm", "left=1cm"))
            with mock.patch.object(docs, "cover_base", return_value=base), \
                 mock.patch.object(docs, "pdf_pages", return_value=1), \
                 mock.patch.object(docs, "extract_text", return_value=(None, {})):
                state, checks, _ = docs.check_pdf("cover", Path(directory) / "cover.pdf",
                                                   source, [], {})
            self.assertEqual(state, "fail")
            states = {check["id"]: check["state"] for check in checks}
            self.assertEqual(states["cover_fixed_blocks"], "pass")
            self.assertEqual(states["cover_layout"], "fail")

    def test_cover_mechanical_cache_tracks_library_and_source_without_changing_cv_key(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.tex"
            base.write_text(BASE)
            with mock.patch.object(docs, "cover_base", return_value=base), \
                 mock.patch.object(checkpoint, "current_pdf_sha", return_value="same-pdf"), \
                 mock.patch.object(checkpoint, "current_source_sha", return_value="source-1"):
                before = checkpoint.mechanical_inputs({}, "cover")
                cv_before = checkpoint.mechanical_inputs({}, "cv")
                base.write_text(BASE + "\n% New approved policy.\n")
                after = checkpoint.mechanical_inputs({}, "cover")
                self.assertNotEqual(before, after)
                self.assertEqual(cv_before, checkpoint.mechanical_inputs({}, "cv"))
                with mock.patch.object(checkpoint, "current_source_sha", return_value="source-2"):
                    self.assertNotEqual(after, checkpoint.mechanical_inputs({}, "cover"))

    def test_cover_mechanical_cache_tracks_word_limit_and_validator_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            base, policy = Path(directory) / "base.tex", Path(directory) / "policy.py"
            pdf_checks = Path(directory) / "pdf_checks.py"
            base.write_text(BASE)
            policy.write_text("original validator")
            pdf_checks.write_text("original PDF checks")
            with mock.patch.object(docs, "cover_base", return_value=base), \
                 mock.patch.object(cover_blocks, "__file__", str(policy)), \
                 mock.patch.object(docs, "__file__", str(pdf_checks)), \
                 mock.patch.object(checkpoint, "current_pdf_sha", return_value="same-pdf"), \
                 mock.patch.object(checkpoint, "current_source_sha", return_value="same-source"):
                before = checkpoint.mechanical_inputs({}, "cover")
                cv_before = checkpoint.mechanical_inputs({}, "cv")
                with mock.patch.object(docs, "COVER_MAX_WORDS", 999):
                    self.assertNotEqual(before, checkpoint.mechanical_inputs({}, "cover"))
                    self.assertEqual(cv_before, checkpoint.mechanical_inputs({}, "cv"))
                policy.write_text("validator with an additional safety check")
                after_validator = checkpoint.mechanical_inputs({}, "cover")
                self.assertNotEqual(before, after_validator)
                pdf_checks.write_text("PDF checks with a changed word-count or squeeze rule")
                self.assertNotEqual(after_validator, checkpoint.mechanical_inputs({}, "cover"))
                self.assertEqual(cv_before, checkpoint.mechanical_inputs({}, "cv"))


if __name__ == "__main__":
    unittest.main()


class NarrativeTest(unittest.TestCase):
    base = "% COVER_NARRATIVE_V1\n" + PREAMBLE + r"\begin{document}\end{document}"
    source = PREAMBLE + r"""\begin{document}
\senderblock{Applicant}{Contact}
\recipientblock{Example Company}
\currentdate{4 October 2026}
\subjectline{Application}
\lettercontent{Dear Hiring Team,}
\lettercontent{This work interests me because it builds on my experience.}
\lettercontent{I built a service and investigated failures during testing.}
\lettercontent{I would bring that experience to your team.}
\closing{Kind regards,}
\signature{Applicant}
\end{document}"""

    def test_editable_prose_with_intact_frame(self):
        for source in (self.source, self.source.replace(
                'I built a service and investigated failures during testing.',
                'My project involved testing a service and tracing failures.')):
            self.assertEqual(cover_blocks.validate_narrative(source, self.base)['issues'], [])
            self.assertEqual(cover_blocks.validate_layout(source, self.base)['issues'], [])
        self.assertFalse(cover_blocks.validate_fixed_blocks(self.source, self.base)['enabled'])

    def test_structure_cannot_be_disabled_by_source_marker(self):
        mutations = [
            (r'\signature{Applicant}', ''),
            (r'\lettercontent{Dear Hiring Team,}', ''),
            ('This work interests me because it builds on my experience.', ''),
            ('This work interests me because it builds on my experience.', '[MOTIVATION]'),
            ('This work interests me because it builds on my experience.', r'\small injected'),
            (r'\closing{Kind regards,}', r'\item injected\closing{Kind regards,}'),
            (r'\end{document}', r'\end{document}unwrapped'),
            (r'\signature{Applicant}', r'\signature{Applicant}\signature{Duplicate}'),
        ]
        for old, new in mutations:
            with self.subTest(mutation=old):
                result = cover_blocks.validate_narrative(self.source.replace(old, new), self.base)
                self.assertTrue(result['enabled'])
                self.assertTrue(result['issues'])

    def test_layout_remains_protected(self):
        source = self.source.replace('left=2.3cm', 'left=1cm')
        self.assertTrue(cover_blocks.validate_layout(source, self.base)['issues'])

    def test_substantive_repair_requires_review_even_when_only_deleting(self):
        before = self.source.replace('I built a service', 'This was a course project.\nI built a service')
        self.assertFalse(checkpoint.narrative_text_unchanged(before, self.source))
        self.assertTrue(checkpoint.narrative_text_unchanged(self.source, '% comment\n' + self.source))
