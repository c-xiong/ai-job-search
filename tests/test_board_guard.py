"""The write guard is the only thing between a headless run and the repository.

These tests exercise `tools/board/guard_write.py` as the CLI actually invokes
it - a JSON payload on stdin, a decision on stdout - because the in-process
functions and the hook contract are two different things and only one of them
is what runs during a paid model run.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from board import guard_write  # noqa: E402

GUARD = ROOT / "tools" / "board" / "guard_write.py"


def invoke(payload, allowlist_path, hook_log=None):
    """Run the guard the way the CLI does. Returns (decision, reason)."""
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
           "JOBFLOW_ALLOWLIST": str(allowlist_path)}
    if hook_log:
        env["JOBFLOW_HOOK_LOG"] = str(hook_log)
    proc = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None, None
    body = json.loads(proc.stdout)["hookSpecificOutput"]
    return body["permissionDecision"], body["permissionDecisionReason"]


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.run_dir = base / "runs" / "r-1"
        self.run_dir.mkdir(parents=True)
        self.cv = base / "cv" / "main_acme_engineer.tex"
        self.cv.parent.mkdir(parents=True)
        self.other_cv = base / "cv" / "main_someone_else.tex"
        self.other_cv.write_text("previous application", encoding="utf-8")
        self.allowlist = base / "allowlist.json"
        self.allowlist.write_text(json.dumps({
            "dirs": [str(self.run_dir.resolve())],
            "files": [str(self.cv.resolve())],
            "deny": [str((self.run_dir / ".guard-probe-ab12cd34").resolve()),
                     str((self.run_dir / "stream.jsonl").resolve())],
        }), encoding="utf-8")

    def test_write_inside_the_run_directory_is_allowed(self):
        decision, _ = invoke({"tool_name": "Write",
                              "tool_input": {"file_path": str(self.run_dir / "fit.json")}},
                             self.allowlist)
        self.assertEqual(decision, "allow")

    def test_the_target_document_is_allowed(self):
        decision, _ = invoke({"tool_name": "Write", "tool_input": {"file_path": str(self.cv)}},
                             self.allowlist)
        self.assertEqual(decision, "allow")

    def test_another_applications_cv_is_denied(self):
        """The point of exact paths rather than a `cv/` prefix: 'restore v1'
        must not be destroyable by the run producing v2."""
        decision, reason = invoke({"tool_name": "Edit",
                                   "tool_input": {"file_path": str(self.other_cv)}},
                                  self.allowlist)
        self.assertEqual(decision, "deny")
        self.assertIn("outside this run's allowlist", reason)
        self.assertEqual(self.other_cv.read_text(encoding="utf-8"), "previous application")

    def test_traversal_out_of_the_run_directory_is_denied(self):
        decision, _ = invoke(
            {"tool_name": "Write",
             "tool_input": {"file_path": str(self.run_dir / ".." / ".." / "escaped.tex")}},
            self.allowlist)
        self.assertEqual(decision, "deny")

    def test_the_canary_path_is_denied_and_logged_as_a_probe(self):
        log = Path(self.tmp.name) / "hook.jsonl"
        decision, _ = invoke({"tool_name": "Write",
                              "tool_input": {"file_path": str(self.run_dir / ".guard-probe-ab12cd34")}},
                             self.allowlist, hook_log=log)
        self.assertEqual(decision, "deny")
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(entry["probe"])
        self.assertEqual(entry["decision"], "deny")

    def test_the_transcript_is_not_model_writable(self):
        decision, reason = invoke(
            {"tool_name": "Write", "tool_input": {"file_path": str(self.run_dir / "stream.jsonl")}},
            self.allowlist)
        self.assertEqual(decision, "deny")
        self.assertIn("supervisor-owned", reason)

    def test_a_missing_allowlist_denies_everything(self):
        """Fail closed: an unreadable allowlist is not an excuse to allow."""
        decision, reason = invoke({"tool_name": "Write",
                                   "tool_input": {"file_path": str(self.cv)}},
                                  Path(self.tmp.name) / "nope.json")
        self.assertEqual(decision, "deny")
        self.assertIn("allowlist", reason)

    def test_notebook_edit_uses_its_own_path_key(self):
        decision, _ = invoke({"tool_name": "NotebookEdit",
                              "tool_input": {"notebook_path": str(self.other_cv)}},
                             self.allowlist)
        self.assertEqual(decision, "deny")

    def test_unguarded_tools_are_deferred_not_denied(self):
        """Reads stay wide - WebSearch is how the reviewer researches a company."""
        decision, _ = invoke({"tool_name": "WebSearch", "tool_input": {"query": "acme"}},
                             self.allowlist)
        self.assertIsNone(decision)


class BashShapeTest(unittest.TestCase):
    """The check is on the parsed argv, not a banned-character list."""

    def test_the_one_permitted_shape(self):
        allowed, _ = guard_write.check_bash("python3 tools/board/fetch_url.py https://x.com/a")
        self.assertTrue(allowed)

    def test_a_quoted_query_string_is_allowed(self):
        """Half the job boards on the internet put `&` in their URLs, so the
        guard has to accept them - but only quoted, because that is also the only
        form in which the shell treats them as one argument."""
        for url in ('"https://jobs.example.com/view?id=42&src=board"',
                    "'https://x.com/a?q=b&page=2#top'",
                    '"https://x.com/p;matrix=1"',
                    "https://x.com/a?q=b",
                    "https://x.com/plain"):
            allowed, reason = guard_write.check_bash(
                "python3 tools/board/fetch_url.py " + url)
            self.assertTrue(allowed, "%s -> %s" % (url, reason))

    def test_a_hash_cannot_hide_a_separator(self):
        """`shlex` treats `#` as a comment; Bash does not, mid-word. So
        `.../a#;id` lexed with comments on is three tokens ending at the `#` -
        the permitted shape - while the shell runs `id`."""
        for url in ("https://x.test/a#;id", "https://x.test/a#&id",
                    "https://x.test/a#|id"):
            allowed, reason = guard_write.check_bash(
                "python3 tools/board/fetch_url.py " + url)
            self.assertFalse(allowed, "%s -> %s" % (url, reason))

    def test_a_genuine_url_fragment_still_passes(self):
        for url in ("https://x.test/jobs/42#requirements",
                    '"https://x.test/jobs/42#requirements"'):
            allowed, reason = guard_write.check_bash(
                "python3 tools/board/fetch_url.py " + url)
            self.assertTrue(allowed, "%s -> %s" % (url, reason))

    def test_the_tokenizer_models_the_shell_not_a_config_file(self):
        """The property behind both injections: token count must match what Bash
        would do with the same string."""
        self.assertEqual(len(guard_write._tokenize("a b c")), 3)
        self.assertEqual(len(guard_write._tokenize("a b c;d")), 5)
        self.assertEqual(len(guard_write._tokenize("a b c#d")), 3)
        self.assertEqual(len(guard_write._tokenize("a b c#;d")), 5)
        self.assertEqual(len(guard_write._tokenize('a b "c;d"')), 3)

    def test_an_unquoted_separator_is_denied_even_without_a_space(self):
        """The hole this replaced: `shlex.split` does not treat `;` as an
        operator, so `.../a;id` came back as three tokens, looked like the
        permitted shape, and would have run `id`."""
        for url in ("https://x.test/a;id",
                    "https://x.test/a&id",
                    "https://x.test/a|id",
                    "https://x.test/a?q=1&rm=2"):
            allowed, reason = guard_write.check_bash(
                "python3 tools/board/fetch_url.py " + url)
            self.assertFalse(allowed, "%s -> %s" % (url, reason))
            self.assertIn("quoted", reason)

    def test_chained_commands_are_denied(self):
        for command in ("python3 tools/board/fetch_url.py https://x.com && rm -rf /",
                        "python3 tools/board/fetch_url.py https://x.com; cat ~/.ssh/id_rsa",
                        "python3 tools/board/fetch_url.py https://x.com | tee /tmp/x",
                        "python3 tools/board/fetch_url.py https://x.com > /tmp/x"):
            allowed, _ = guard_write.check_bash(command)
            self.assertFalse(allowed, command)

    def test_shell_expansion_inside_quotes_is_denied(self):
        """`shlex` keeps these as one token, but the shell would still expand
        them - so the argv would not be what it reads as."""
        for command in ('python3 tools/board/fetch_url.py "https://$(whoami).x.com"',
                        'python3 tools/board/fetch_url.py "https://`id`.x.com"',
                        'python3 tools/board/fetch_url.py "https://x.com/$HOME"',
                        'python3 tools/board/fetch_url.py "https://x.com/a\\nb"'):
            allowed, reason = guard_write.check_bash(command)
            self.assertFalse(allowed, command)

    def test_a_newline_is_more_than_one_command(self):
        allowed, reason = guard_write.check_bash(
            "python3 tools/board/fetch_url.py https://x.com\nrm -rf /")
        self.assertFalse(allowed)
        self.assertIn("newline", reason)

    def test_a_different_script_is_denied(self):
        for command in ("python3 tools/other.py https://x.com",
                        "curl https://x.com",
                        "python3 tools/board/fetch_url.py --output x https://x.com",
                        "python3 tools/board/fetch_url.py"):
            allowed, _ = guard_write.check_bash(command)
            self.assertFalse(allowed, command)

    def test_non_https_is_denied(self):
        for url in ("file:///etc/passwd", "http://x.com", "//x.com", "https:/x.com"):
            allowed, _ = guard_write.check_bash("python3 tools/board/fetch_url.py " + url)
            self.assertFalse(allowed, url)


if __name__ == "__main__":
    unittest.main()
