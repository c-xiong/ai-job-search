"""Opt-in account-backed Codex contract; anonymous inputs, no application writes.

Run with JOBFLOW_LIVE_CODEX=1. Uses the signed-in ChatGPT allowance, never an API key.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.board_harness import ROOT
from board import providers, run_registry


@unittest.skipUnless(os.environ.get("JOBFLOW_LIVE_CODEX") == "1" and shutil.which("codex"),
                     "set JOBFLOW_LIVE_CODEX=1 to use the signed-in Codex allowance")
class LiveCodexContract(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xelatex") and shutil.which("pdftoppm"),
                         "image contract requires XeLaTeX and Poppler")
    def test_pdf_pages_are_attached_for_inspection(self):
        with tempfile.TemporaryDirectory(prefix="jobflow-live-images-") as directory:
            root = Path(directory)
            pdf_dir = root / "runs" / "anonymous"
            pdf_dir.mkdir(parents=True)
            subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error",
                            "-output-directory=" + str(pdf_dir),
                            str(ROOT / "tests/fixtures/latex/cover_fixture.tex")],
                           cwd=ROOT / "cover_letters", capture_output=True, check=True, timeout=60)
            pdf = pdf_dir / "cover_fixture.pdf"
            with mock.patch.object(run_registry, "ROOT", root), \
                    mock.patch.object(run_registry, "RUN_DIRS", root / "runs"), \
                    mock.patch.object(run_registry, "RUN_STATE", root / "state"):
                record = {"id": "anonymous", "provider": "codex"}
                target = pdf_dir / "inspect.json"
                prompt = ("Inspect the layout of the attached anonymous PDF `%s`. "
                          "Placeholders are intentional test content. Return inspect.json "
                          "with schema jobflow.inspect/1, verdict clean/fixable/blocked and "
                          "issues as objects with doc=cover, page, kind and fix_hint. "
                          "Inspect the actual image; do not infer appearance from text." % pdf)
                request = providers.CodexRequest(record, "pass_c", prompt, [target], "image-contract")
                try:
                    self.assertTrue(request.images)
                    env = {k: v for k, v in os.environ.items()
                           if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
                    result = subprocess.run(providers.command(
                        {"binary": shutil.which("codex"), "model": None}, request.workspace,
                        request.schema_path, request.images), input=request.prompt_path.read_text(),
                        cwd=request.workspace, env=env, capture_output=True, text=True, timeout=150)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
                    texts = [e["item"]["text"] for e in events if e.get("type") == "item.completed"
                             and e.get("item", {}).get("type") == "agent_message"]
                    (root / "state" / "anonymous").mkdir(parents=True)
                    request.accept(texts[-1])
                    self.assertIn(str(target.resolve()), request.approved)
                    self.assertEqual(request.pdf_hashes[str(pdf.resolve())], providers._digest(pdf))
                finally:
                    request.close()

    def test_native_web_available(self):
        execution = {"binary": shutil.which("codex"), "model": None}
        with tempfile.TemporaryDirectory(prefix="jobflow-live-web-") as directory:
            work = Path(directory)
            (work / ".git").mkdir()
            schema = work / "schema.json"
            schema.write_text(json.dumps({"type": "object", "properties": {
                "web_ok": {"type": "boolean"}, "source": {"type": "string"}},
                "required": ["web_ok", "source"], "additionalProperties": False}))
            prompt = ("Use the native web tool to search official OpenAI documentation for Codex "
                      "noninteractive JSONL output. Set web_ok true only if a tool actually "
                      "returns the page, and source to the URL. If it fails, set false and put "
                      "the error in source. Do not answer from memory.")
            env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY")}
            result = subprocess.run(providers.command(execution, work, schema), input=prompt,
                                    cwd=work, env=env, capture_output=True, text=True, timeout=150)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            texts = [e["item"]["text"] for e in events if e.get("type") == "item.completed"
                     and e.get("item", {}).get("type") == "agent_message"]
            answer = json.loads(texts[-1])
            self.assertTrue(answer["web_ok"], answer)
            self.assertTrue(any(domain in answer["source"] for domain in
                                ("developers.openai.com", "learn.chatgpt.com")), answer)

    def test_readonly_structured_output(self):
        execution = {"binary": shutil.which("codex"), "model": None}
        metadata = providers.preflight(execution)
        self.assertEqual(metadata["authentication"], "chatgpt")
        with tempfile.TemporaryDirectory(prefix="jobflow-live-codex-") as directory:
            work = Path(directory)
            (work / ".git").mkdir()
            protected = work / "protected.txt"
            protected.write_text("original")
            schema = work / "schema.json"
            schema.write_text(json.dumps({"type": "object", "properties": {
                "document": {"type": "string"}}, "required": ["document"],
                "additionalProperties": False}))
            expected = "\\documentclass{article}\n\\begin{document}\nAnonymous sample.\n\\end{document}\n"
            prompt = ("First try to overwrite %s with changed using any available write tool. "
                      "If none is available or permission is denied, continue without retrying. "
                      "Then return JSON document containing EXACTLY this text: %s"
                      % (protected, json.dumps(expected)))
            env = dict(os.environ)
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
                env.pop(key, None)
            result = subprocess.run(providers.command(execution, work, schema), input=prompt,
                                    cwd=work, env=env, capture_output=True, text=True, timeout=150)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            self.assertTrue(any(e.get("type") == "turn.completed" for e in events), result.stdout[-2000:])
            texts = [e["item"]["text"] for e in events if e.get("type") == "item.completed"
                     and e.get("item", {}).get("type") == "agent_message"]
            self.assertEqual(json.loads(texts[-1])["document"], expected)
            self.assertEqual(protected.read_text(), "original")
            forbidden = {"mcp_tool_call", "command_execution"}
            self.assertFalse(any(e.get("item", {}).get("type") in forbidden for e in events))
