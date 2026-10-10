"""Archive keeps artifacts and restores only Board status it still owns."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.board import archive, server

registry = archive.run_registry
jobs_md = archive.jobs_md
URL = "https://example.test/jobs/engineer"
RUN = "r-20261010-120000-example-abc123"
SECOND = "r-20261010-120001-example-abc124"


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for obj, attr, value in (
            (registry, "REGISTRY", self.root / "runs.json"),
            (registry, "RUNS_LOCK", self.root / ".runs.lock"),
            (jobs_md, "SEEN", self.root / "seen.json"),
        ):
            patch = mock.patch.object(obj, attr, value)
            patch.start()
            self.addCleanup(patch.stop)
        for patch in (mock.patch.object(archive.activity, "emit"),
                      mock.patch.object(archive.trash, "_sent", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.records = [{"id": RUN, "job_url": URL, "phase": "done", "company": "Example", "role": "Engineer"},
                        {"id": SECOND, "job_url": URL, "phase": "failed", "retry_of": RUN, "application_id": RUN}]
        registry.store({"runs": self.records, "ledger": {"spent_usd": 7}})
        self.write_board({URL: {"url": URL, "user_status": "yes", "user_note": "Keep this note"}})
        self.artifact = self.root / "document.pdf"
        self.artifact.write_bytes(b"unchanged artifact")

    def write_board(self, seen):
        with jobs_md.board_lock():
            jobs_md.save_seen(seen)

    def post(self, action, payload=None, token=None):
        handler = object.__new__(server.Handler)
        path = "/api/update" if action == "update" else "/api/runs/" + RUN + "/" + action
        handler.path = path + "?t=" + (server.TOKEN if token is None else token)
        body = json.dumps(payload or {}).encode()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        replies = []
        handler._send = lambda code, data: replies.append((code, json.loads(data)))
        handler.do_POST()
        return replies[0]

    def test_archive_and_restore_whole_family_preserve_data(self):
        code, result = self.post("archive", {"reason": "Not a match"})
        self.assertEqual(code, 200)
        self.assertEqual(set(result["ids"]), {RUN, SECOND})
        self.assertTrue(all(r["archived_at"] for r in registry.load()["runs"]))
        self.assertEqual(archive.state.load()[URL]["user_status"], "no")
        self.assertEqual(self.artifact.read_bytes(), b"unchanged artifact")
        self.assertEqual(registry.load()["ledger"], {"spent_usd": 7})
        self.assertEqual(self.post("unarchive")[0], 200)
        self.assertFalse(any(r.get("archived_at") for r in registry.load()["runs"]))
        self.assertEqual(archive.state.load()[URL]["user_status"], "yes")
        self.assertEqual(archive.state.load()[URL]["user_note"], "Keep this note")

    def test_manual_status_change_even_back_to_no_is_preserved(self):
        self.post("archive")
        for status in ("yes", "no"):
            self.assertEqual(self.post("update", {"url": URL, "status": status})[0], 200)
        self.post("unarchive")
        self.assertEqual(archive.state.load()[URL]["user_status"], "no")

    def test_idempotent_archive_does_not_overwrite_original_status(self):
        self.post("archive")
        self.post("archive")
        self.post("unarchive")
        self.assertEqual(archive.state.load()[URL]["user_status"], "yes")

    def test_active_and_applied_applications_are_rejected(self):
        self.records[1]["phase"] = "drafting"
        registry.store({"runs": self.records})
        self.assertEqual(self.post("archive")[0], 409)
        self.records[1]["phase"] = "failed"
        registry.store({"runs": self.records})
        with mock.patch.object(archive.trash, "_sent", return_value=True):
            self.assertEqual(self.post("archive")[0], 409)
        self.assertEqual(archive.state.load()[URL]["user_status"], "yes")

    def test_missing_board_row_still_archives(self):
        self.write_board({})
        code, result = self.post("archive")
        self.assertEqual(code, 200)
        self.assertEqual(result["board"], "missing")
        self.assertEqual(self.post("unarchive")[0], 200)

    def test_archived_application_cannot_be_regenerated_or_marked_applied(self):
        self.post("archive")
        for action in ("retry", "continue", "applied", "owner"):
            self.assertEqual(self.post(action)[0], 409)
        self.assertEqual(self.post("archive", token="wrong")[0], 403)

    def test_alias_url_still_updates_original_board_row(self):
        alias = "https://careers.example.test/engineer"
        seen = archive.state.load()
        seen[URL]["sources"] = [{"portal": "manual", "url": alias}]
        self.write_board(seen)
        self.records[0]["job_url"] = alias
        self.records[1]["job_url"] = alias
        registry.store({"runs": self.records})
        self.assertEqual(self.post("archive")[0], 200)
        self.assertEqual(archive.state.load()[URL]["user_status"], "no")

    def test_invalid_reason_and_deleted_records_are_rejected(self):
        self.assertEqual(self.post("archive", {"reason": ["invalid"]})[0], 400)
        self.records[0]["deleted_at"] = "2026-10-10"
        registry.store({"runs": self.records})
        self.assertEqual(self.post("archive")[0], 404)

    def test_registry_failure_rolls_board_back(self):
        with mock.patch.object(registry, "store", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                archive.set_archived(RUN, True)
        self.assertEqual(archive.state.load()[URL]["user_status"], "yes")
        self.assertFalse(registry.get(RUN).get("archived_at"))
