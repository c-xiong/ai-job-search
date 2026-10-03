"""Manual PDF review (the default): no model checks, owner-driven revisions."""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from board_harness import SupervisorCase  # noqa: E402
from board import checkpoint, docs, review, run_registry  # noqa: E402


class ManualReviewTest(SupervisorCase):
    automated_review = False

    def test_overflow_is_repaired_without_enabling_content_review(self):
        original = docs.pdf_pages

        def pages(pdf):
            if "cover" in Path(pdf).name and "repair" not in self.stages():
                return 2
            return original(pdf)

        with mock.patch.object(docs, "pdf_pages", side_effect=pages):
            run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        self.assertEqual(self.stages(run_id), ["draft", "repair"])
        self.assertEqual(run_registry.get(run_id)["budget_usd"]["pass_c"],
                         2 * run_registry.config()["budget_usd"]["pass_c"])

    def test_unresolved_overflow_stops_after_two_repairs_without_publication(self):
        original = docs.pdf_pages
        with mock.patch.object(docs, "pdf_pages", side_effect=lambda pdf:
                               2 if "cover" in Path(pdf).name else original(pdf)):
            run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertEqual(self.stages(run_id), ["draft", "repair", "repair"])
        self.assertNotIn("recorded", self.manifest(run_id)["publication"])
        self.assertFalse((self.home / run_registry.get(run_id)["targets"]["cover"]).exists())

    def test_saved_overflow_is_remeasured_in_manual_mode(self):
        run_id, _ = self.run_to_end()
        manifest = self.manifest(run_id)
        checkpoint.record_check(manifest, "mechanical_cover", "fail",
                                checkpoint.mechanical_inputs(manifest, "cover"), "overflow",
                                {"checks": [{"id": "cover_page_count", "state": "fail"}]})
        plan = checkpoint.plan(manifest, ["cover"],
                               self.supervisor._toolchains(["cover"]),
                               inspection_enabled=False, review_enabled=False)
        self.assertEqual(plan[0], ("mechanical", ["cover"]))

    def test_fit_repair_cannot_shrink_the_layout(self):
        original = docs.pdf_pages
        with mock.patch.dict(os.environ, {"FAKE_REPAIR": "squeeze"}), \
                mock.patch.object(docs, "pdf_pages", side_effect=lambda pdf:
                                  2 if "cover" in Path(pdf).name else original(pdf)):
            run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertIn("repair discarded", run_registry.get(run_id)["error"])
        source = checkpoint.absolute(self.manifest(run_id)["docs"]["cover"]["source"]["path"])
        self.assertNotIn("enlargethispage", source.read_text())

    def test_class_change_invalidates_the_saved_build(self):
        run_id, _ = self.run_to_end()
        manifest = self.manifest(run_id)
        (self.home / "cover_letters" / "cover.cls").write_text("% updated layout\n")
        plan = checkpoint.plan(manifest, ["cover"],
                               self.supervisor._toolchains(["cover"]),
                               inspection_enabled=False, review_enabled=False)
        self.assertEqual(plan[0], ("build", ["cover"]))

    def test_only_the_letter_is_drafted_and_nothing_reviews_the_pdfs(self):
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        self.assertEqual(self.stages(run_id), ["draft"])
        budget = run_registry.get(run_id)["budget_usd"]
        self.assertNotIn("review", budget)
        self.assertNotIn("fix", budget)

    def test_a_cv_revision_edits_only_the_cv_and_the_edit_survives(self):
        first, _ = self.run_to_end()
        cover_before = checkpoint.current_source_sha(self.manifest(first), "cover")
        code, body = self.start(kind="revise", parent=first, edit="cv",
                                note="change the title line")
        self.assertEqual(code, 202, body)
        new_id = body["run_id"]
        self.assertEqual(self.settle(new_id), "done", run_registry.get(new_id).get("error"))
        self.assertEqual(self.stages(new_id), ["revise"])
        manifest = self.manifest(new_id)
        cv = checkpoint.absolute(manifest["docs"]["cv"]["source"]["path"])
        self.assertIn("Fixed wording", cv.read_text())
        self.assertEqual(checkpoint.current_source_sha(manifest, "cover"), cover_before)
        prompt = next(p for p in (run_registry.state_dir(new_id)).glob("spec-*.json"))
        self.assertIn("small, targeted edits", prompt.read_text())

    def test_a_variant_switch_reseeds_the_cv_without_a_model(self):
        first, _ = self.run_to_end(base_cv="sde")
        before = len(self.stages())
        code, body = self.start(kind="revise", parent=first, base_cv="ai", note="")
        self.assertEqual(code, 202, body)
        new_id = body["run_id"]
        self.assertEqual(self.settle(new_id), "done", run_registry.get(new_id).get("error"))
        self.assertEqual(self.stages()[before:], [])
        self.assertEqual(self.manifest(new_id)["inputs"]["variant"]["role"], "ai")

    def test_a_revision_without_an_instruction_or_a_switch_is_refused(self):
        first, _ = self.run_to_end(base_cv="sde")
        code, body = self.start(kind="revise", parent=first, note="")
        self.assertEqual(code, 400, body)

    def test_marks_bind_to_the_exact_pdf(self):
        run_id, _ = self.run_to_end()
        record = run_registry.get(run_id)
        items = review.mark(record, "cv_layout", True)
        self.assertTrue(next(i for i in items if i["id"] == "cv_layout")["done"])
        pdf = run_registry.ROOT / record["artefacts"]["cv_pdf"]
        pdf.write_bytes(pdf.read_bytes() + b"\n% changed")
        again = next(i for i in review.checklist(record) if i["id"] == "cv_layout")
        self.assertFalse(again["done"])
        self.assertTrue(again["stale"])
        with self.assertRaises(review.ReviewError):
            review.mark(record, "not-an-item", True)

    def test_the_view_hides_model_checks_and_reveals_a_repo_file(self):
        run_id, _ = self.run_to_end()
        record = run_registry.get(run_id)
        view = review.view(record, {"checks": [
            {"id": "content_cv", "state": "unverified"},
            {"id": "visual_cover", "state": "unverified"},
            {"id": "cv_pages", "state": "pass"}]})
        self.assertEqual([c["id"] for c in view["checks"]], ["cv_pages"])
        self.assertEqual({i["doc"] for i in view["manual"]}, {"cv", "cover", "both"})
        self.assertEqual(set(view["files"]), {"cv", "cover"})
        with mock.patch.object(review.subprocess, "run") as run, \
                mock.patch.object(review.sys, "platform", "darwin"):
            shown = review.reveal(record, "cv")
        self.assertEqual(run.call_args[0][0][:2], ["open", "-R"])
        self.assertTrue((run_registry.ROOT / shown).is_file())
        self.assertEqual(shown, "documents/applications/%s/CV.pdf" % record["slug"])

    def test_the_submission_copy_is_named_for_the_candidate(self):
        (self.home / "CLAUDE.md").write_text("- **Name:** Jane Placeholder\n")
        run_id, _ = self.run_to_end()
        record = run_registry.get(run_id)
        with mock.patch.object(review.subprocess, "run"), \
                mock.patch.object(review.sys, "platform", "darwin"):
            shown = review.reveal(record, "cover")
        self.assertEqual(Path(shown).name, "Jane_Placeholder_Cover_Letter_%s.pdf"
                         % re.sub(r"[^\w-]+", "_", record["company"]).strip("_"))
        self.assertEqual(Path(shown).parent.name, record["slug"])


if __name__ == "__main__":
    unittest.main()
