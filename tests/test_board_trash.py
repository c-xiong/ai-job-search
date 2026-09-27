"""Deleting applications and failed attempts: soft, undoable, budget-preserving."""

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from board_harness import SupervisorCase  # noqa: E402
from board import run_registry, trash  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import jobs_md  # noqa: E402


class TrashTest(SupervisorCase):
    automated_review = False

    def setUp(self):
        super().setUp()
        sent = mock.patch.object(trash.docs, "tracker_statuses", return_value={})
        self.tracker = sent.start()
        self.addCleanup(sent.stop)

    def snapshot_ids(self):
        return {r["id"] for r in self.supervisor.snapshot()["runs"]}

    def test_deleting_an_application_trashes_every_attempt_and_undo_brings_it_back(self):
        first, _ = self.run_to_end()
        code, body = self.supervisor.retry(first)
        self.assertEqual(code, 202, body)
        second = body["run_id"]
        self.settle(second)
        cost_before = run_registry.application_spent(first)

        deleted = trash.delete(second, "application")
        self.assertEqual(set(deleted), {first, second})
        self.assertFalse(run_registry.run_dir(first).exists())
        self.assertTrue((run_registry.RUN_DIRS / ".trash" / first).is_dir())
        self.assertEqual(self.snapshot_ids() & {first, second}, set())
        # The tombstone keeps the cost on the books.
        self.assertIsNotNone(run_registry.get(first))
        self.assertEqual(run_registry.application_spent(first), cost_before)

        restored = trash.undo(deleted)
        self.assertEqual(set(restored), {first, second})
        self.assertTrue(run_registry.run_dir(first).is_dir())
        self.assertTrue({first, second} <= self.snapshot_ids())

    def test_a_finished_attempt_cannot_be_deleted_on_its_own(self):
        run_id, _ = self.run_to_end()
        with self.assertRaises(trash.TrashError) as caught:
            trash.delete(run_id, "attempt")
        self.assertEqual(caught.exception.status, 409)

    def test_a_failed_attempt_can_be_deleted_alone(self):
        run_id, _ = self.run_to_end()
        run_registry.update(run_id, phase="failed")
        self.assertEqual(trash.delete(run_id, "attempt"), [run_id])

    def test_a_running_application_is_refused(self):
        run_id, _ = self.run_to_end()
        run_registry.update(run_id, phase="drafting")
        with self.assertRaises(trash.TrashError) as caught:
            trash.delete(run_id, "application")
        self.assertIn("cancel or kill", str(caught.exception))
        run_registry.update(run_id, phase="done")

    def test_a_sent_application_needs_the_company_name(self):
        run_id, _ = self.run_to_end()
        record = run_registry.get(run_id)
        key = trash.docs._tracker_key(record["company"], record["role"])
        self.tracker.return_value = {key: ("applied", "2026-09-28")}
        with self.assertRaises(trash.TrashError):
            trash.delete(run_id, "application")
        with self.assertRaises(trash.TrashError):
            trash.delete(run_id, "application", confirm="someone else")
        self.assertEqual(trash.delete(run_id, "application",
                                     confirm=" %s " % record["company"].upper()), [run_id])

    def test_the_purge_counts_from_the_delete_not_from_the_run(self):
        run_id, _ = self.run_to_end()
        folder = run_registry.run_dir(run_id)
        old = time.time() - 40 * 86400
        os.utime(folder, (old, old))
        trash.delete(run_id, "application")
        self.assertEqual(trash.purge(), 0)
        self.assertGreaterEqual(trash.purge(now=time.time() + 15 * 86400), 1)
        self.assertFalse((run_registry.RUN_DIRS / ".trash" / run_id).exists())
        with self.assertRaises(trash.TrashError) as caught:
            trash.undo([run_id])
        self.assertEqual(caught.exception.status, 410)


class LegacyStatusTest(unittest.TestCase):
    def test_retired_statuses_read_as_current_ones_and_are_never_offered(self):
        self.assertEqual(jobs_md.user_status({"user_status": "star"}), "yes")
        self.assertIn(jobs_md.user_status({"user_status": "maybe"}), jobs_md.STATUSES)
        self.assertEqual(jobs_md.user_status({}), "new")
        for retired in jobs_md.LEGACY_STATUSES:
            self.assertNotIn(retired, jobs_md.STATUSES)

    def test_a_save_rewrites_retired_statuses(self):
        seen = {"a": {"user_status": "star"}, "b": {"user_status": "maybe"},
                "c": {"user_status": "no"}}
        with mock.patch.object(jobs_md, "write_json_atomic") as write:
            jobs_md.save_seen(seen)
        written = write.call_args[0][1]["seen"]
        self.assertEqual([written[k]["user_status"] for k in "abc"],
                         ["yes", jobs_md.LEGACY_STATUSES["maybe"], "no"])


class DemoteUnreviewedTest(unittest.TestCase):
    """`new` names the latest fetch; a fetch moves older unreviewed rows on."""

    def test_only_unreviewed_rows_move_to_backlog(self):
        seen = {"a": {"user_status": "new"}, "b": {}, "c": {"user_status": "yes"},
                "d": {"user_status": "backlog"}, "e": {"user_status": "maybe"}}
        self.assertEqual(jobs_md.demote_unreviewed(seen), 2)
        self.assertEqual([jobs_md.user_status(seen[k]) for k in "abcde"],
                         ["backlog", "backlog", "yes", "backlog", "backlog"])

    def test_rows_from_the_kept_fetch_stay_new(self):
        seen = {"old": {"user_status": "new", "first_seen_at": "2026-09-27T10:00:00"},
                "run": {"user_status": "new", "first_seen_at": "2026-09-28T00:17:00"}}
        jobs_md.demote_unreviewed(seen, keep_since="2026-09-28T00:17:00")
        self.assertEqual(seen["old"]["user_status"], "backlog")
        self.assertEqual(seen["run"]["user_status"], "new")


if __name__ == "__main__":
    unittest.main()
