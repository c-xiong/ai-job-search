import csv
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.board import docs, run_registry


PDF_TWO = (b"%PDF-1.4\n1 0 obj <</Type /Pages /Count 2>> endobj\n"
           b"2 0 obj <</Type /Page>> endobj\n3 0 obj <</Type /Page>> endobj\n%%EOF")
PDF_ONE = PDF_TWO.replace(b"/Count 2", b"/Count 1").replace(
    b"3 0 obj <</Type /Page>> endobj\n", b"")


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = {
            "root": docs.ROOT, "tracker": docs.TRACKER, "tracker_lock": docs.TRACKER_LOCK,
            "apps": docs.APPLICATIONS, "rr_root": run_registry.ROOT,
            "run_dirs": run_registry.RUN_DIRS, "run_state": run_registry.RUN_STATE,
        }
        docs.ROOT = self.root
        docs.TRACKER = self.root / "job_search_tracker.csv"
        docs.TRACKER_LOCK = self.root / ".tracker.lock"
        docs.APPLICATIONS = self.root / "documents" / "applications"
        run_registry.ROOT = self.root
        run_registry.RUN_DIRS = self.root / "documents" / "runs"
        run_registry.RUN_STATE = self.root / "job_scraper" / "run_state"
        self.record = {
            "id": "r-test", "slug": "acme-ml", "company": "Acme", "role": "ML Engineer",
            "job_url": "https://example.test/job", "targets": {
                "cv": "cv/main_acme.tex", "cover": "cover_letters/cover_acme.tex"},
            "fit": {"overall": 78, "sector": "AI", "role_type": "Full-time",
                    "channel": "portal", "contact_person": None, "deadline": None},
        }

    def tearDown(self):
        docs.ROOT = self.saved["root"]
        docs.TRACKER = self.saved["tracker"]
        docs.TRACKER_LOCK = self.saved["tracker_lock"]
        docs.APPLICATIONS = self.saved["apps"]
        run_registry.ROOT = self.saved["rr_root"]
        run_registry.RUN_DIRS = self.saved["run_dirs"]
        run_registry.RUN_STATE = self.saved["run_state"]
        self.tmp.cleanup()

    def test_supported_and_unsupported_toolchains(self):
        with mock.patch.object(docs.templates, "active", return_value={
                "compile": "lualatex -interaction=nonstopmode", "ext": ".tex",
                "name": "stock", "source": None}):
            self.assertEqual(docs.resolve_toolchain("cv")["engine"], "lualatex")
        with mock.patch.object(docs.templates, "active", return_value={
                "compile": "typst compile", "ext": ".typ", "name": "typst",
                "source": "x"}):
            self.assertEqual(docs.resolve_toolchain("cv")["kind"], "typst")
        with mock.patch.object(docs.templates, "active", return_value={
                "compile": "pandoc --pdf-engine=x", "ext": ".md", "name": "bad",
                "source": "x"}):
            with self.assertRaisesRegex(docs.DocumentError, "toolchain_unsupported"):
                docs.resolve_toolchain("cv")

    def test_compile_uses_no_shell_and_requires_fresh_pdf(self):
        source = self.root / "cv" / "main.tex"
        source.parent.mkdir(parents=True)
        source.write_text("source")

        def compile_fake(argv, cwd, timeout=120):
            build = source.parent / "build"
            build.mkdir(exist_ok=True)
            (build / "main.pdf").write_bytes(PDF_TWO)
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        active = {"compile": "pdflatex -interaction=nonstopmode", "ext": ".tex",
                  "name": "stock", "source": None}
        with mock.patch.object(docs.templates, "active", return_value=active), \
             mock.patch.object(docs, "_run", side_effect=compile_fake) as runner:
            pdf, evidence = docs.compile_one("cv", source)
        self.assertTrue(pdf.exists())
        self.assertEqual(evidence["cmd"][0], "pdflatex")
        self.assertNotIn("shell", runner.call_args.kwargs)

    def test_pdf_page_count_is_pdf_native(self):
        path = self.root / "two.pdf"
        path.write_bytes(PDF_TWO)
        self.assertEqual(docs.pdf_pages(path), 2)

    def test_machine_verify_keeps_visual_state_unverified(self):
        run_registry.run_dir("r-test").mkdir(parents=True)
        cv = self.root / "cv.pdf"
        cover = self.root / "cover.pdf"
        cv.write_bytes(PDF_ONE)
        cover.write_bytes(PDF_ONE)
        evidence = {"cv": {"toolchain": "latex", "cmd": ["x"], "exit": 0},
                    "cover": {"toolchain": "latex", "cmd": ["y"], "exit": 0}}
        with mock.patch.object(docs, "extract_text",
                               return_value=("Name\nemail@example.test\nExperience\nLLM",
                                             {"exit": 0})), \
             mock.patch.object(docs, "_contact_literals",
                               return_value=["email@example.test"]):
            result = docs.build_verify(self.record, {"cv": cv, "cover": cover},
                                       evidence, ["LLM", "Rust"])
        states = {check["id"]: check["state"] for check in result["checks"]}
        self.assertEqual(states["cv_page_count"], "pass")
        self.assertEqual(states["cover_page_count"], "pass")
        self.assertEqual(states["visual_layout"], "unverified")
        self.assertEqual(result["keywords"]["absent"], ["Rust"])

    def test_inspection_requires_both_exact_successful_reads(self):
        run_dir = run_registry.run_dir("r-test")
        run_dir.mkdir(parents=True)
        verify = {"schema": "jobflow.verify/1", "run_id": "r-test", "checks": [
            {"id": "visual_layout", "label": "Visual", "state": "unverified",
             "detail": "", "evidence": {}}], "keywords": {}}
        (run_dir / "verify.json").write_text(json.dumps(verify))
        cv, cover = self.root / "cv.pdf", self.root / "cover.pdf"
        cv.write_bytes(PDF_TWO)
        cover.write_bytes(PDF_TWO)
        stream = run_dir / "inspect-stream.jsonl"
        events = []
        for ident, path in (("a", cv), ("b", cover)):
            events.extend([
                {"type": "assistant", "message": {"content": [{
                    "type": "tool_use", "id": ident, "name": "Read",
                    "input": {"file_path": str(path)}}]}},
                {"type": "user", "message": {"content": [{
                    "type": "tool_result", "tool_use_id": ident, "is_error": False}]}}
            ])
        stream.write_text("\n".join(json.dumps(e) for e in events))
        inspect = {"schema": "jobflow.inspect/1", "verdict": "clean", "issues": []}
        result, proven = docs.apply_inspection(
            self.record, {"cv": cv, "cover": cover}, inspect, True, stream)
        self.assertTrue(proven)
        self.assertEqual(result["checks"][0]["state"], "pass")
        _result, proven = docs.apply_inspection(
            self.record, {"cv": cv, "cover": cover}, inspect, True, stream,
            stream_offset=stream.stat().st_size)
        self.assertFalse(proven, "a later inspection may not reuse an earlier pass's Reads")

    def test_a_cover_only_run_is_verified_as_one_document(self):
        """A CV that was never drafted has no page count to check and no text
        layer to extract - checking it anyway would invent a failing document."""
        cover = self.root / "cover.pdf"
        cover.write_bytes(PDF_TWO.replace(b"/Count 2", b"/Count 1").replace(
            b"3 0 obj <</Type /Page>> endobj\n", b""))
        evidence = {"cover": {"toolchain": "latex", "cmd": ["y"], "exit": 0}}
        record = dict(self.record, scope="cover")
        with mock.patch.object(docs, "extract_text",
                               return_value=("Name\nemail@example.test\nDear",
                                             {"exit": 0})), \
             mock.patch.object(docs, "_contact_literals",
                               return_value=["email@example.test"]):
            result = docs.build_verify(record, {"cover": cover}, evidence, ["LLM"])
        states = {check["id"]: check["state"] for check in result["checks"]}
        self.assertEqual(states["cover_page_count"], "pass")
        self.assertNotIn("cv_page_count", states)
        self.assertEqual(docs.doc_kinds(record), ("cover",))

    def test_a_cover_only_redraft_keeps_the_cv_the_tracker_already_has(self):
        self.assertEqual(docs.merge_tracker(self.record), "appended")
        # Same paths, same day: a retried publication is a no-op, not a redraft.
        self.assertEqual(docs.merge_tracker(dict(self.record, scope="cover")), "unchanged")
        with open(docs.TRACKER, newline="") as handle:
            row = list(csv.DictReader(handle))[0]
        self.assertEqual(row["cv_file"], "cv/main_acme.tex")
        self.assertEqual(row["cover_letter_file"], "cover_letters/cover_acme.tex")

    def test_tracker_append_update_and_final_append(self):
        self.assertEqual(docs.merge_tracker(self.record), "appended")
        with open(docs.TRACKER, newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["fit_rating"], "78")
        self.assertEqual(rows[0]["status"], "drafted")

        rows[0]["status"] = "applied"
        rows[0]["date"] = "2026-01-01"
        rows[0]["deadline"] = "2026-09-01"
        with open(docs.TRACKER, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=docs.CANONICAL_HEADER)
            writer.writeheader()
            writer.writerows(rows)
        self.assertEqual(docs.merge_tracker(self.record), "updated")
        with open(docs.TRACKER, newline="") as handle:
            updated = list(csv.DictReader(handle))[0]
        self.assertEqual(updated["status"], "applied")
        self.assertEqual(updated["date"], "2026-01-01")
        self.assertEqual(updated["deadline"], "2026-09-01")
        self.assertEqual(updated["notes"], "redrafted")

        updated["status"] = "no response"
        with open(docs.TRACKER, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=docs.CANONICAL_HEADER)
            writer.writeheader()
            writer.writerow(updated)
        self.assertEqual(docs.merge_tracker(self.record), "appended")
        with open(docs.TRACKER, newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 2)

    def test_republication_is_idempotent_and_keeps_an_existing_rating(self):
        """RESUME-6: repeating the tracker step changes nothing, and a run with no
        fit evaluation never blanks or zeroes a rating another evaluation stored."""
        self.assertEqual(docs.merge_tracker(self.record), "appended")
        self.assertEqual(docs.merge_tracker(self.record), "unchanged")
        unscored = dict(self.record, fit=None,
                        targets={"cv": "cv/main_acme_v2.tex",
                                 "cover": "cover_letters/cover_acme_v2.tex"})
        self.assertEqual(docs.merge_tracker(unscored, {"sector": "AI"}), "updated")
        with open(docs.TRACKER, newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fit_rating"], "78")
        self.assertEqual(rows[0]["status"], "drafted")
        self.assertEqual(rows[0]["cv_file"], "cv/main_acme_v2.tex")

    def test_a_new_unscored_application_writes_an_empty_rating_not_zero(self):
        record = dict(self.record, fit=None)
        docs.merge_tracker(record, {"sector": "AI", "deadline": "2026-10-01"})
        with open(docs.TRACKER, newline="") as handle:
            row = list(csv.DictReader(handle))[0]
        self.assertEqual(row["fit_rating"], "")
        self.assertEqual(row["sector"], "AI")
        self.assertEqual(row["deadline"], "2026-10-01")

    def test_cv_keywords_are_measured_on_the_cv_alone(self):
        """CHECK-1: a keyword that only the letter mentions does not count."""
        cv, cover = self.root / "cv.pdf", self.root / "cover.pdf"
        cv.write_bytes(PDF_ONE)
        cover.write_bytes(PDF_ONE)
        run_registry.run_dir("r-test").mkdir(parents=True)
        texts = {str(cv): "Name\nemail@example.test\nPython LLM",
                 str(cover): "Dear team, I love Kubernetes and LLM"}
        evidence = {"toolchain": "latex", "cmd": ["x"], "exit": 0}
        with mock.patch.object(docs, "extract_text",
                               side_effect=lambda path: (texts[str(path)], {"exit": 0})), \
             mock.patch.object(docs, "_contact_literals", return_value=[]):
            result = docs.build_verify(self.record, {"cv": cv, "cover": cover},
                                       {"cv": evidence, "cover": evidence},
                                       ["LLM", "Kubernetes"],
                                       sources={"cv": cv, "cover": cover})
        self.assertEqual(result["keywords"]["absent"], ["Kubernetes"])
        states = {c["id"]: c["state"] for c in result["checks"]}
        # An absent term is a screening note, never a flag on an untailored CV.
        self.assertEqual(states["cv_keywords"], "pass")

    def test_unresolved_placeholders_fail_the_document(self):
        source = self.root / "cover.tex"
        source.write_text("\\documentclass{cover}\n% [COMMENTED OUT] is fine\n"
                          "\\begin{document}\nDear [CONTACT PERSON OR HIRING TEAM],\n"
                          "\\end{document}\n")
        pdf = self.root / "cover.pdf"
        pdf.write_bytes(PDF_ONE)
        with mock.patch.object(docs, "extract_text", return_value=(None, {})):
            state, checks, _cov = docs.check_pdf("cover", pdf, source, [],
                                                 {"toolchain": "latex"})
        placeholder = [c for c in checks if c["id"] == "cover_placeholders"][0]
        self.assertEqual(placeholder["state"], "fail")
        self.assertIn("CONTACT PERSON", placeholder["detail"])
        self.assertEqual(state, "fail")
        # Extraction unavailable is shown as skipped, never folded into a pass.
        self.assertIn("skipped", {c["state"] for c in checks})

    def test_pages_inside_compressed_object_streams_are_counted(self):
        """pdfTeX writes page dictionaries into zlib object streams; a byte
        search of the raw file finds none (found against the real master)."""
        import zlib
        body = zlib.compress(b"1 0 obj << /Type /Pages /Count 1 /Kids [2 0 R] >> "
                             b"2 0 obj << /Type /Page /Parent 1 0 R >>")
        path = self.root / "packed.pdf"
        path.write_bytes(b"%PDF-1.5\n3 0 obj << /Type /ObjStm /Filter /FlateDecode >>\n"
                         b"stream\n" + body + b"\nendstream\nendobj\n%%EOF")
        self.assertEqual(docs.pdf_pages(path), 1)

    def test_a_cvtodo_inside_a_preamble_section_macro_is_caught(self):
        """The master defines its section bodies as macros before
        \\begin{document}, so a body-only scan would miss a marker there."""
        source = self.root / "cv.tex"
        source.write_text("\\documentclass{article}\n\\newcommand{\\cvTODO}[1]{[#1]}\n"
                          "\\newcommand{\\secSkills}{\\cvTODO{add skill}}\n"
                          "% \\cvTODO{commented out is fine}\n"
                          "\\begin{document}\\secSkills\\end{document}\n")
        pdf = self.root / "cv.pdf"
        pdf.write_bytes(PDF_ONE)
        with mock.patch.object(docs, "extract_text", return_value=(None, {})):
            _state, checks, _cov = docs.check_pdf("cv", pdf, source, ["x"], {})
        detail = [c for c in checks if c["id"] == "cv_placeholders"][0]
        self.assertEqual(detail["state"], "fail")
        self.assertIn("cvTODO", detail["detail"])

    def test_the_cover_base_follows_the_cv_variant_and_falls_back(self):
        letters = self.root / "cover_letters"
        letters.mkdir()
        (letters / "my_cover.tex").write_text("generic")
        (letters / "my_cover_ai.tex").write_text("ai")
        self.assertEqual(docs.cover_base("ai").name, "my_cover_ai.tex")
        self.assertEqual(docs.cover_base("sde").name, "my_cover.tex")
        self.assertEqual(docs.cover_base().name, "my_cover.tex")
        protected = docs.protected_paths()
        for rel in docs.COVER_BASE_RELS:
            self.assertIn(str(self.root / rel), protected)

    def test_a_letter_squeezed_onto_one_page_fails(self):
        source = self.root / "cover.tex"
        pdf = self.root / "cover.pdf"
        pdf.write_bytes(PDF_ONE)
        for body, squeezed in (("\\enlargethispage{2\\baselineskip}", True),
                               ("\\vspace{-4pt}", True), ("{\\small text}", True),
                               ("% \\enlargethispage{1pt}", False),
                               ("\\vspace{6pt}\\smallskip", False)):
            source.write_text("\\documentclass{cover}\n\\begin{document}\n%s\n"
                              "\\end{document}\n" % body)
            with mock.patch.object(docs, "extract_text", return_value=(None, {})):
                _state, checks, _cov = docs.check_pdf("cover", pdf, source, [], {})
            check = [c for c in checks if c["id"] == "cover_no_squeeze"][0]
            self.assertEqual(check["state"], "fail" if squeezed else "pass", body)

    def test_phone_numbers_match_on_digits_and_emails_exactly(self):
        self.assertTrue(docs._literal_present("(+41) 79 000 12 34",
                                              "Phone: +41 79 000 12 34"))
        self.assertFalse(docs._literal_present("(+41) 79 000 12 34", "Phone: +41 79 000"))
        self.assertTrue(docs._literal_present("a.b@example.org", "mail a.b@example.org"))
        self.assertFalse(docs._literal_present("a.b@example.org", "mail a_b@example.org"))

    def test_an_inspection_that_read_another_pdf_proves_nothing(self):
        stream = self.root / "stream.jsonl"
        current, stale = self.root / "cv.pdf", self.root / "cv-old.pdf"
        current.write_bytes(PDF_ONE)
        stale.write_bytes(PDF_ONE)
        stream.write_text("\n".join(json.dumps(e) for e in [
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "a", "name": "Read",
                "input": {"file_path": str(stale)}}]}},
            {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "a", "is_error": False}]}}]))
        proven, verdicts, _evidence = docs.judge_inspection(
            {"cv": current}, {"schema": "jobflow.inspect/1", "verdict": "clean",
                              "issues": []}, True, stream)
        self.assertFalse(proven)
        self.assertEqual(verdicts["cv"][0], "unverified")

    def test_the_stock_layout_policy_lives_in_one_place(self):
        with mock.patch.object(docs.templates, "GUIDANCE", {
                "cv": self.root / "missing-05.md", "cover": self.root / "missing-06.md"}):
            self.assertEqual(docs.expected_pages("cv"), 1)
            self.assertEqual(docs.expected_pages("cover"), 1)
            self.assertEqual(docs.resolve_toolchain("cv")["engine"], "pdflatex")
            self.assertEqual(docs.resolve_toolchain("cover")["engine"], "xelatex")

    def test_a_registered_page_limit_overrides_the_stock_one(self):
        guidance = self.root / "05.md"
        guidance.write_text("<!-- BEGIN ACTIVE-TEMPLATE -->\n"
                            "> - **Source extension:** `.tex`\n"
                            "> - **Compile command:** `pdflatex -interaction=nonstopmode`\n"
                            "> - **Page limit:** exactly 2 page(s)\n"
                            "<!-- END ACTIVE-TEMPLATE -->\n")
        with mock.patch.object(docs.templates, "GUIDANCE", {"cv": guidance,
                                                            "cover": guidance}), \
             mock.patch.object(docs.templates.jobs_md, "ROOT", self.root):
            self.assertEqual(docs.expected_pages("cv"), 2)

    def test_compilation_runs_from_the_template_home_into_the_given_build_dir(self):
        (self.root / "cover_letters").mkdir()
        source = self.root / "documents" / "runs" / "r-x" / "work" / "cover.tex"
        source.parent.mkdir(parents=True)
        source.write_text("x")
        build = source.parent / "build"
        seen = {}

        def compile_fake(argv, cwd, timeout=120):
            seen.update(argv=argv, cwd=cwd)
            build.mkdir(exist_ok=True)
            (build / "cover.pdf").write_bytes(PDF_ONE)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(docs, "_run", side_effect=compile_fake):
            docs.compile_one("cover", source, build=build)
        self.assertEqual(Path(seen["cwd"]), self.root / "cover_letters")
        self.assertEqual(seen["argv"][-1], str(source.resolve()))
        self.assertIn("-output-directory=%s" % build, seen["argv"])

    def test_publication_is_atomic_idempotent_and_refuses_symlinks(self):
        (self.root / "cv").mkdir()
        run_registry.run_dir("r-test").mkdir(parents=True)
        source, pdf = self.root / "src.tex", self.root / "src.pdf"
        source.write_text("\\documentclass{article}")
        pdf.write_bytes(PDF_ONE)
        first = docs.publish_document(self.record, "cv", source, pdf)
        self.assertEqual(len(first["written"]), 2)
        self.assertEqual(docs.publish_document(self.record, "cv", source, pdf)["written"], [])
        live = self.root / "cv" / "main_acme.tex"
        live.unlink()
        live.symlink_to(self.root / "elsewhere.tex")
        with self.assertRaises(docs.DocumentError):
            docs.publish_document(self.record, "cv", source, pdf)

    def test_archive_is_verbatim_and_never_overwrites(self):
        run_dir = run_registry.run_dir("r-test")
        run_dir.mkdir(parents=True)
        (run_dir / "posting.md").write_text("first\n")
        relative = docs.archive_posting(self.record)
        target = self.root / relative
        self.assertEqual(target.read_text(), "first\n")
        (run_dir / "posting.md").write_text("second\n")
        docs.archive_posting(self.record)
        self.assertEqual(target.read_text(), "first\n")

    def test_document_lock_reports_busy_without_blocking(self):
        entered = threading.Event()
        release = threading.Event()

        def holder():
            with docs.document_lock("acme-ml"):
                entered.set()
                release.wait(2)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaises(docs.DocumentBusy):
            with docs.document_lock("acme-ml", blocking=False):
                pass
        release.set()
        thread.join(2)


if __name__ == "__main__":
    unittest.main()
