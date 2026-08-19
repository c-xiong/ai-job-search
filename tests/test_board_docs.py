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
        cv.write_bytes(PDF_TWO)
        cover.write_bytes(PDF_TWO.replace(b"/Count 2", b"/Count 1").replace(
            b"3 0 obj <</Type /Page>> endobj\n", b""))
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
