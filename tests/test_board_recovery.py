"""Recovery, master protection and cost behaviour of the staged pipeline.

Each test names the acceptance criterion it covers (RESUME-n, OWN-n, COST-n).
Interruptions are injected at the exact point the criterion names, then
`Continue` is exercised and the set of model passes it spends is asserted - the
proof that only missing or invalid work is redone.
"""

import json
import os
import threading
import time
from pathlib import Path
from unittest import mock

from tests.board_harness import SupervisorCase, checkpoint, docs, run_registry, runs
from board import guard_write, run_guard  # noqa: E402


def interrupt_once(target, exception):
    """Make `target` raise `exception` on its first call only."""
    state = {"fired": False}
    original = target

    def wrapper(*args, **kwargs):
        if not state["fired"]:
            state["fired"] = True
            raise exception
        return original(*args, **kwargs)
    return wrapper


class ResumeTest(SupervisorCase):
    """RESUME-1: interrupt at each checkpoint; Continue does only what is missing."""

    def continue_and_settle(self, run_id, **payload):
        before = len(self.stages())
        code, body = self.supervisor.continue_run(run_id, payload)
        self.assertEqual(code, 202, body)
        new_id = body["run_id"]
        self.assertEqual(self.settle(new_id), "done", run_registry.get(new_id).get("error"))
        return new_id, self.stages()[before:]

    def test_after_the_posting_was_saved(self):
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="draft")
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertTrue(self.manifest(run_id)["inputs"]["posting"]["sha256"])
        os.environ["FAKE_MODE"] = "auto"
        self.fetch_result = (None, "the posting went offline")
        new_id, spent = self.continue_and_settle(run_id)
        self.assertEqual(spent, ["draft", "review"])
        self.assertTrue(self.manifest(new_id)["inputs"]["posting"]["origin"]
                        .startswith("adopted:"))

    def test_after_the_brief_was_saved_but_not_the_letter(self):
        os.environ["FAKE_DRAFT"] = "cv_then_crash"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        old = self.manifest(run_id)
        self.assertTrue(old["brief"], "the complete brief was not kept")
        self.assertNotIn("cover", old["docs"])
        os.environ["FAKE_DRAFT"] = "ok"
        new_id, spent = self.continue_and_settle(run_id)
        self.assertEqual(spent, ["draft", "review"])
        prompts = [json.loads(p.read_text())["argv"][-1]
                   for p in run_registry.state_dir(new_id).glob("spec-*.json")]
        draft = next(p for p in prompts if "stage: DRAFT" in p)
        self.assertIn("write cover letter", draft)
        self.assertNotIn("write CV", draft)
        self.assertNotIn("write brief", draft)
        self.assertIn("untailored master variant", draft)
        self.assertTrue(self.manifest(new_id)["docs"]["cv"]["origin"].startswith("variant:"))

    def test_after_all_drafts_before_the_review(self):
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="review")
        run_id, _phase = self.run_to_end()
        os.environ["FAKE_MODE"] = "auto"
        new_id, spent = self.continue_and_settle(run_id)
        self.assertEqual(spent, ["review"], "saved drafts were rewritten")

    def test_after_the_content_review_before_the_build(self):
        with mock.patch.object(runs.Supervisor, "_stage_build", interrupt_once(
                runs.Supervisor._stage_build,
                runs.RunFailure("interrupted", code="interrupted"))):
            run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        before = len(self.stages())
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 202)
        # COST-1: only mechanical work remained, so nothing was reserved and no
        # model was started.
        self.assertEqual(run_registry.get(body["run_id"])["budget_usd"], {})
        self.assertEqual(self.settle(body["run_id"]), "done")
        self.assertEqual(self.stages()[before:], [])

    def test_after_the_pdf_was_built_before_visual_inspection(self):
        self.write_config(inspection_enabled=True)
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="inspect")
        run_id, _phase = self.run_to_end()
        os.environ["FAKE_MODE"] = "auto"
        compiled = len(self.compiled)
        new_id, spent = self.continue_and_settle(run_id)
        self.assertEqual(spent, ["inspect"])
        self.assertEqual(len(self.compiled), compiled, "an unchanged source was rebuilt")

    def test_after_every_check_passed_before_publication_finished(self):
        self.mocks["archive_posting"].side_effect = interrupt_once(
            lambda record: "documents/applications/test/job_posting.md",
            docs.DocumentError("disk full"))
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        before = len(self.stages())
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(self.settle(body["run_id"]), "done")
        self.assertEqual(self.stages()[before:], [], "publication-only recovery used a model")
        # RESUME-6: the second publication wrote no source twice.
        publication = self.manifest(body["run_id"])["publication"]
        self.assertEqual(publication["cv"]["written"], [])


class RestartAndClickTest(SupervisorCase):
    """RESUME-2: no provider session, a board restart, and duplicate clicks."""

    def test_continue_after_a_board_restart_needs_no_session(self):
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="review")
        run_id, _phase = self.run_to_end()
        # The board died mid-review: its record says reviewing, its owner is gone.
        run_registry.update(run_id, phase="reviewing", owner="dead", owner_pid=999999,
                            session_id=None, failure_code=None)
        fresh = runs.Supervisor()
        self.addCleanup(fresh.stop)
        fresh.reconcile()
        self.assertEqual(run_registry.get(run_id)["failure_code"], "interrupted")
        os.environ["FAKE_MODE"] = "auto"
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 202, body)
        self.assertEqual(self.settle(body["run_id"]), "done")

    def test_duplicate_continue_clicks_start_one_attempt(self):
        os.environ["FAKE_DRAFT"] = "cv_then_crash"
        run_id, _phase = self.run_to_end()
        os.environ["FAKE_DRAFT"] = "ok"
        answers = []
        gate = threading.Barrier(3)

        def click():
            gate.wait()
            answers.append(self.supervisor.continue_run(run_id))

        threads = [threading.Thread(target=click) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ids = {body["run_id"] for _code, body in answers}
        self.assertEqual(len(ids), 1, answers)
        self.settle(ids.pop())
        followers = [r for r in run_registry.load()["runs"] if r.get("continue_of") == run_id]
        self.assertEqual(len(followers), 1)
        # And after it finished, another click returns the same attempt.
        code, again = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 200)
        self.assertEqual(again["run_id"], followers[0]["id"])

    def test_the_original_attempt_stays_visible_and_linked(self):
        os.environ["FAKE_DRAFT"] = "cv_then_crash"
        run_id, _phase = self.run_to_end()
        os.environ["FAKE_DRAFT"] = "ok"
        code, body = self.supervisor.continue_run(run_id)
        new = run_registry.get(body["run_id"])
        self.assertEqual(new["continue_of"], run_id)
        self.assertEqual(new["application_id"], run_id)
        self.assertEqual(new["attempt"], 2)
        self.assertEqual(run_registry.get(run_id)["phase"], "failed")
        self.settle(body["run_id"])


class IntegrityTest(SupervisorCase):
    """RESUME-3: truncated files and broken manifests never become good."""

    def test_a_brief_migration_keeps_adopted_metadata_when_no_pass_can_start(self):
        run_id, phase = self.run_to_end(scope="cover")
        self.assertEqual(phase, "done")
        manifest = self.manifest(run_id)
        manifest["brief"].pop("letter_plan")
        checkpoint.save(run_id, manifest)
        old_brief = json.loads(json.dumps(manifest["brief"]))
        brief_path = checkpoint.run_path(run_id, "brief.json")
        # The disk still holds a valid previous-pass brief. It must never be
        # treated as this pass's newly generated metadata without write proof.
        old_bytes = brief_path.read_bytes()
        record = run_registry.get(run_id)
        budget = record["budget_usd"]
        for cause in ("preflight", "budget"):
            with self.subTest(cause=cause):
                run_registry.update(run_id, budget_usd=budget if cause == "preflight" else {})
                error = (runs.PreflightError("guard unavailable")
                         if cause == "preflight" else None)
                with mock.patch.object(runs, "preflight", side_effect=error), \
                        mock.patch.object(runs.run_proc, "Pass") as model_pass:
                    with self.assertRaises(runs.PreflightError if cause == "preflight"
                                           else runs.RunFailure):
                        self.supervisor._stage_draft(record, manifest, [], ["cover"],
                                                     run_registry.config())
                    model_pass.assert_not_called()
                self.assertEqual(manifest["brief"], old_brief)
                self.assertEqual(self.manifest(run_id)["brief"], old_brief)
                self.assertEqual(brief_path.read_bytes(), old_bytes)

    def test_saved_cover_without_a_letter_plan_regenerates_only_the_brief(self):
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="review")
        run_id, phase = self.run_to_end(scope="cover")
        self.assertEqual(phase, "failed")
        manifest = self.manifest(run_id)
        source_sha = checkpoint.current_source_sha(manifest, "cover")
        manifest["brief"].pop("letter_plan")
        checkpoint.save(run_id, manifest)
        before = len(self.stages())
        os.environ["FAKE_MODE"] = "auto"
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 202, body)
        resumed_id = body["run_id"]
        self.assertEqual(self.settle(resumed_id), "done",
                         run_registry.get(resumed_id).get("error"))
        resumed = self.manifest(resumed_id)
        self.assertEqual(run_guard.validate_letter_plan(resumed["brief"]["letter_plan"]), [])
        self.assertEqual(checkpoint.current_source_sha(resumed, "cover"), source_sha)
        self.assertEqual(self.stages()[before:], ["draft", "review"])
        prompt = next(json.loads(spec.read_text())["argv"][-1]
                      for spec in run_registry.state_dir(resumed_id).glob("spec-*.json")
                      if "stage: DRAFT" in json.loads(spec.read_text())["argv"][-1])
        self.assertIn("Scope: brief only", prompt)
        self.assertIn("with its `letter_plan`", prompt)
        self.assertNotIn("- write cover letter:", prompt)

    def test_a_truncated_letter_is_not_adopted_and_the_brief_is_kept(self):
        os.environ["FAKE_DRAFT"] = "truncate_cover"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        manifest = self.manifest(run_id)
        self.assertNotIn("cover", manifest["docs"])
        self.assertTrue(manifest["brief"])
        self.assertTrue(any("end{document}" in issue for issue in manifest["issues"]))
        os.environ["FAKE_DRAFT"] = "ok"
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(self.settle(body["run_id"]), "done")

    def test_a_malformed_manifest_is_quarantined_and_recovered_conservatively(self):
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="review")
        run_id, _phase = self.run_to_end()
        checkpoint.manifest_path(run_id).write_text("{ truncated", encoding="utf-8")
        os.environ["FAKE_MODE"] = "auto"
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 202, body)
        self.assertEqual(self.settle(body["run_id"]), "done",
                         run_registry.get(body["run_id"]).get("error"))
        manifest = self.manifest(body["run_id"])
        self.assertTrue(any("checkpoint.json is unreadable" in issue
                            for issue in manifest["issues"]))
        # The broken file itself is left for inspection, not overwritten.
        self.assertIn("{ truncated", checkpoint.manifest_path(run_id).read_text())

    def test_a_truncated_edit_keeps_the_last_good_version(self):
        self.write_config(inspection_enabled=True)
        os.environ.update(FAKE_INSPECT="fixable", FAKE_INSPECT_DOC="cover",
                          FAKE_REPAIR="truncate")
        self.addCleanup(os.environ.pop, "FAKE_REPAIR", None)
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        manifest = self.manifest(run_id)
        path = checkpoint.absolute(manifest["docs"]["cover"]["source"]["path"])
        complete, reason = checkpoint.complete_source(path)
        self.assertTrue(complete, reason)
        self.assertEqual(checkpoint.sha256(path),
                         manifest["docs"]["cover"]["source"]["sha256"])
        self.assertTrue(any("last good version kept" in issue for issue in manifest["issues"]))
        self.assertFalse((self.home / run_registry.get(run_id)["targets"]["cover"]).exists())

    def test_an_edited_source_loses_its_verified_state(self):
        """RESUME-5 at the file level: editing bytes invalidates their checks."""
        run_id, _phase = self.run_to_end()
        manifest = self.manifest(run_id)
        tools = self.supervisor._toolchains(["cv", "cover"])
        self.assertEqual(checkpoint.plan(manifest, ["cv", "cover"], tools, False), [])
        path = checkpoint.absolute(manifest["docs"]["cover"]["source"]["path"])
        path.write_text(path.read_text().replace("Dear", "Hello"), encoding="utf-8")
        steps = dict(checkpoint.plan(manifest, ["cv", "cover"], tools, False))
        # The edit is not a checkpointed draft, so the cover is "missing" and
        # gets redrafted; the CV and its checks stay reusable.
        self.assertEqual(steps["draft"], ["cover"])
        self.assertEqual(steps["build"], ["cover"])
        self.assertEqual(checkpoint.doc_state(manifest, "cv", tools["cv"]), "PDF built")


class InvalidationTest(SupervisorCase):
    """RESUME-5: changed facts invalidate exactly the affected checks."""

    def plan_after(self, run_id, mutate):
        record = run_registry.get(run_id)
        manifest = self.manifest(run_id)
        tools = self.supervisor._toolchains(["cv", "cover"])
        self.assertEqual(checkpoint.plan(manifest, ["cv", "cover"], tools, False), [])
        mutate()
        self.supervisor._refresh_inputs(record, manifest, ["cv", "cover"])
        return dict(checkpoint.plan(manifest, ["cv", "cover"], tools, False)), manifest

    def test_a_master_change_repins_the_cv_and_rereviews_the_letter(self):
        run_id, _phase = self.run_to_end()
        steps, manifest = self.plan_after(run_id, lambda: self.master.write_text(
            self.master.read_text() + "\n% upstream edit\n"))
        # The CV is re-seeded from the new master, never reviewed; the letter
        # (and its agreement with the new CV) is certified again.
        self.assertEqual(steps["review"], ["cover", "consistency"])
        cv = checkpoint.absolute(manifest["docs"]["cv"]["source"]["path"])
        self.assertIn(b"% upstream edit", cv.read_bytes())
        self.assertIn("master_cv", manifest["changed_sources"])
        # Only content was invalidated; the unchanged PDFs are not rebuilt twice
        # for no reason by the plan's own logic - they are rebuilt because the
        # content they carry must be re-certified first.
        self.assertIn("build", steps)

    def test_a_cover_base_change_leaves_the_cv_reusable(self):
        run_id, _phase = self.run_to_end()
        base = self.home / "cover_letters" / "my_cover.tex"
        steps, _manifest = self.plan_after(run_id, lambda: base.write_text(
            base.read_text() + "\n% new standing paragraph\n"))
        self.assertEqual(steps["review"], ["cover", "consistency"])
        self.assertEqual(steps["build"], ["cover"])

    def test_continue_after_a_fact_change_rereviews_instead_of_redrafting(self):
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="review")
        run_id, _phase = self.run_to_end()
        os.environ["FAKE_MODE"] = "auto"
        self.master.write_text(self.master.read_text() + "\n% corrected date\n")
        before = len(self.stages())
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(self.settle(body["run_id"]), "done")
        self.assertEqual(self.stages()[before:], ["review"])


class LegacyRecoveryTest(SupervisorCase):
    """RESUME-4 and REGRESSION-1: records from the retired two-pass flow."""

    def legacy_record(self, run_id, phase, **extra):
        record = {"id": run_id, "phase": phase, "kind": "apply", "scope": "both",
                  "job_url": "https://example.com/jobs/legacy", "company": "Acme",
                  "role": "ML Engineer", "slug": "acme_ml_engineer",
                  "session_id": "0b6a0e7e-0000-4000-8000-000000000000",
                  "started_at": "2026-09-01T10:00:00", "ended_at": "2026-09-01T10:30:00",
                  "fit": {"overall": 78, "verdict": "good"},
                  "targets": {"cv": "cv/main_acme_ml_engineer.tex",
                              "cover": "cover_letters/cover_acme_ml_engineer.tex"},
                  "budget_usd": {"pass_a": 0.4, "pass_b": 2.0}, "cost": {"total_usd": 1.5}}
        record.update(extra)
        run_registry.append(record)
        directory = run_registry.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "posting.md").write_text(self.fetch_result[0], encoding="utf-8")
        return record

    def test_a_failed_legacy_run_recovers_its_drafts_without_an_evaluation(self):
        run_id = "r-20260901-100000-acme-0ld001"
        self.legacy_record(run_id, "failed", failed_phase="compiling")
        directory = run_registry.run_dir(run_id)
        (directory / "cv_source.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nold CV\n\\end{document}\n")
        # The letter exists only at its live path; the run's guard log proves the
        # write and its mtime sits inside the run's lifetime.
        live = self.home / "cover_letters" / "cover_acme_ml_engineer.tex"
        live.write_text("\\documentclass{cover}\n\\begin{document}\nold letter\n"
                        "\\end{document}\n")
        stamp = time.mktime(time.strptime("2026-09-01T10:20:00", "%Y-%m-%dT%H:%M:%S"))
        os.utime(live, (stamp, stamp))
        state = run_registry.state_dir(run_id)
        state.mkdir(parents=True, exist_ok=True)
        (state / "hook-abcd.jsonl").write_text(json.dumps({
            "tool": "Write", "decision": "allow", "resolved": os.path.realpath(str(live))}) + "\n")
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 202, body)
        new_id = body["run_id"]
        self.assertEqual(self.settle(new_id), "done", run_registry.get(new_id).get("error"))
        # A legacy run has no brief: one brief-only pass, then the review. The
        # adopted letter is kept; the CV is re-pinned to the master variant.
        self.assertEqual(self.stages(new_id), ["draft", "review"],
                         "the adopted letter was rewritten or re-evaluated")
        prompts = [json.loads(p.read_text())["argv"][-1]
                   for p in run_registry.state_dir(new_id).glob("spec-*.json")]
        self.assertIn("Scope: brief only", next(p for p in prompts if "stage: DRAFT" in p))
        origins = {k: v["origin"] for k, v in self.manifest(new_id)["docs"].items()}
        self.assertEqual(origins["cover"], "legacy:%s" % run_id)
        self.assertTrue(origins["cv"].startswith("variant:"))

    def test_a_live_file_without_provenance_is_not_adopted(self):
        run_id = "r-20260901-100000-acme-0ld002"
        self.legacy_record(run_id, "failed")
        live = self.home / "cv" / "main_acme_ml_engineer.tex"
        live.write_text("\\documentclass{article}\n\\begin{document}\nlast month\n"
                        "\\end{document}\n")
        code, body = self.supervisor.continue_run(run_id)
        new_id = body["run_id"]
        self.settle(new_id)
        manifest = self.manifest(new_id)
        self.assertTrue(any("not evidence of ownership" in issue for issue in manifest["issues"]))
        self.assertTrue(manifest["docs"]["cv"]["origin"].startswith("variant:"))
        self.assertIn("draft", self.stages(new_id))

    def test_an_awaiting_approval_record_continues_without_repeating_the_fit(self):
        run_id = "r-20260901-100000-acme-0ld003"
        self.legacy_record(run_id, "awaiting_approval", ended_at=None)
        code, body = self.supervisor.approve(run_id, "awaiting_approval", "ai", "cv")
        self.assertEqual(code, 202, body)
        new_id = body["run_id"]
        self.assertEqual(self.settle(new_id), "done", run_registry.get(new_id).get("error"))
        self.assertEqual(self.stages(new_id), ["draft"], "a CV-only run bought a review")
        old = run_registry.get(run_id)
        self.assertEqual(old["phase"], "cancelled")
        self.assertEqual(old["continued_by"], new_id)
        self.assertEqual(run_registry.get(new_id)["scope"], "cv")

    def test_legacy_records_stay_readable_in_the_snapshot(self):
        self.legacy_record("r-20260901-100000-acme-0ld004", "done")
        self.legacy_record("r-20260901-100000-acme-0ld005", "failed",
                           job_url="https://example.com/jobs/other")
        snapshot = self.supervisor.snapshot()
        ids = {r["id"] for r in snapshot["runs"]}
        self.assertIn("r-20260901-100000-acme-0ld004", ids)
        legacy = next(r for r in snapshot["runs"] if r["id"].endswith("0ld004"))
        self.assertEqual(legacy["fit"]["overall"], 78)
        self.assertNotIn("progress", legacy)


class MasterProtectionTest(SupervisorCase):
    """OWN-1 and OWN-2: the master and its upstream target are never written."""

    def fingerprint(self):
        link = self.home / "cv" / "my_cv.tex"
        return (os.readlink(str(link)), self.master.read_bytes(),
                (self.home / "cover_letters" / "my_cover.tex").read_bytes())

    def test_generate_revise_restore_and_continue_leave_the_master_untouched(self):
        before = self.fingerprint()
        first, _phase = self.run_to_end()
        code, body = self.start(kind="revise", parent=first, note="tighten the profile")
        self.assertEqual(code, 202, body)
        self.assertEqual(self.settle(body["run_id"]), "done",
                         run_registry.get(body["run_id"]).get("error"))
        self.assertEqual(self.supervisor.restore(first)[0], 200)
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="review")
        failed, _phase = self.run_to_end(url="https://example.com/jobs/2")
        os.environ["FAKE_MODE"] = "auto"
        code, body = self.supervisor.continue_run(failed)
        self.settle(body["run_id"])
        self.assertEqual(self.fingerprint(), before)
        self.assertTrue((self.home / "cv" / "my_cv.tex").is_symlink())

    def test_a_write_through_any_alias_of_the_master_is_refused(self):
        for alias in (str(self.home / "cv" / "my_cv.tex"), str(self.master),
                      str(self.home / "cv" / "." / "my_cv.tex")):
            os.environ["FAKE_DRAFT"] = "outside"
            os.environ["FAKE_MASTER"] = alias
            self.addCleanup(os.environ.pop, "FAKE_MASTER", None)
            before = self.master.read_bytes()
            run_id, _phase = self.run_to_end(url="https://example.com/jobs/%d" % len(alias))
            self.assertEqual(self.master.read_bytes(), before, alias)
            denied = [json.loads(line) for log in
                      run_registry.state_dir(run_id).glob("hook-*.jsonl")
                      for line in log.read_text().splitlines()]
            self.assertTrue(any(e["decision"] == "deny" and e["target"] == alias
                                for e in denied), alias)

    def test_a_pass_can_never_be_given_the_master_as_a_target(self):
        with self.assertRaises(run_guard.PreflightError):
            run_guard.write_allowlist("r-x", targets=[self.master], nonce="abcd1234")

    def test_the_guard_denies_the_master_even_inside_an_allowed_directory(self):
        allowlist = {"files": [], "dirs": [str(self.home)],
                     "deny": run_guard.protected_masters(), "error": None}
        allowed, reason = guard_write.check_write(str(self.home / "cv" / "my_cv.tex"),
                                                  allowlist)
        self.assertFalse(allowed, reason)

    def test_publication_and_restore_refuse_a_master_destination(self):
        record = {"id": "r-x", "targets": {"cv": "cv/my_cv.tex"}}
        with self.assertRaises(docs.DocumentError):
            docs.publish_document(record, "cv", self.master, self.master)
        with self.assertRaises(docs.DocumentError):
            docs.assert_writable(self.master, "upstream")

    def test_the_tailored_cv_is_independent_of_the_master(self):
        run_id, _phase = self.run_to_end(base_cv="ai", cv_country="de")
        manifest = self.manifest(run_id)
        seed = checkpoint.run_path(run_id, "work", "history")
        published = self.home / run_registry.get(run_id)["targets"]["cv"]
        frozen = published.read_bytes()
        self.master.write_text(self.master.read_text().replace("Placeholder", "Changed"))
        self.assertEqual(published.read_bytes(), frozen)
        self.assertNotRegex(frozen, rb"\\input\{[^}]*my_cv")
        self.assertEqual(manifest["inputs"]["variant"], {"role": "ai", "country": "de"})
        seeded = runs.seed_cv(b"\\documentclass{article}", "ai", "de", "x")
        self.assertIn(b"\\newcommand{\\cvrole}{ai}", seeded)
        self.assertIn(b"\\newcommand{\\cvcountry}{de}", seeded)
        self.assertNotIn(b"\\input", seeded)

    def test_a_missing_master_stops_the_run_without_creating_one(self):
        (self.home / "cv" / "my_cv.tex").unlink()
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertEqual(run_registry.get(run_id)["failure_code"], "missing_input")
        self.assertFalse((self.home / "cv" / "my_cv.tex").exists())

    def test_a_missing_cover_base_never_falls_back_to_the_example(self):
        (self.home / "cover_letters" / "my_cover.tex").unlink()
        run_id, phase = self.run_to_end(scope="cover")
        self.assertEqual(phase, "failed")
        self.assertIn("never a fallback", run_registry.get(run_id)["error"])


class RevisionTest(SupervisorCase):
    def legacy_revision_parent(self):
        self.write_config(automated_review=False,
                          budget_usd={"pass_a": 5, "pass_b": 12,
                                      "revise": 4, "pass_c": 1.5})
        first, phase = self.run_to_end(scope="cover")
        self.assertEqual(phase, "done", run_registry.get(first).get("error"))
        manifest = self.manifest(first)
        manifest["brief"].pop("letter_plan")
        checkpoint.save(first, manifest)
        return first

    def test_legacy_revision_migrates_the_brief_within_its_remaining_reservation(self):
        first = self.legacy_revision_parent()
        before = len(self.stages())
        original = runs.Supervisor._stage_draft

        def draft_after_reported_revision(supervisor, record, *args):
            # $0.5868 spent from the revision plus two-repair reservation.
            run_registry.debit(0.3868, record["id"])
            return original(supervisor, record, *args)

        with mock.patch.object(runs.Supervisor, "_stage_draft", draft_after_reported_revision):
            code, body = self.start(kind="revise", parent=first, scope="cover",
                                    edit="cover", note="lead with RAG")
            self.assertEqual(code, 202, body)
            new_id = body["run_id"]
            self.assertEqual(self.settle(new_id), "done", run_registry.get(new_id).get("error"))
        self.assertEqual(self.stages()[before:], ["revise", "draft"])
        record = run_registry.get(new_id)
        self.assertEqual(sum(record["budget_usd"].values()), 7.00)
        self.assertLess(record["cost"]["total_usd"], 5.50)
        self.assertEqual(run_guard.validate_letter_plan(
            self.manifest(new_id)["brief"]["letter_plan"]), [])
        argv = next(json.loads(spec.read_text())["argv"]
                    for spec in run_registry.state_dir(new_id).glob("spec-*.json")
                    if "stage: DRAFT" in json.loads(spec.read_text())["argv"][-1])
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "6.41")
        self.assertIn("Scope: brief only", argv[-1])

    def test_continue_migrates_a_legacy_brief_without_repeating_a_saved_revision(self):
        self.continue_saved_revision(keep_brief=True)

    def test_continue_rebuilds_a_missing_brief_without_repeating_a_saved_revision(self):
        self.continue_saved_revision(keep_brief=False)

    def continue_saved_revision(self, keep_brief):
        first = self.legacy_revision_parent()
        with mock.patch.object(runs.Supervisor, "_stage_draft", side_effect=
                               runs.RunFailure("interrupted", code="interrupted")):
            code, body = self.start(kind="revise", parent=first, scope="cover",
                                    edit="cover", note="lead with RAG")
            self.assertEqual(code, 202, body)
            failed = body["run_id"]
            self.assertEqual(self.settle(failed), "failed")
        saved = self.manifest(failed)
        self.assertTrue(saved["revision"]["applied"])
        saved_sha = checkpoint.current_source_sha(saved, "cover")
        if not keep_brief:
            # The old drafting guard removed the adopted brief before it
            # stopped on the budget check: match that live checkpoint exactly.
            saved.pop("brief", None)
            checkpoint.save(failed, saved)
            checkpoint.run_path(failed, "brief.json").unlink(missing_ok=True)
        before = len(self.stages())
        code, body = self.supervisor.continue_run(failed)
        self.assertEqual(code, 202, body)
        continued = body["run_id"]
        self.assertEqual(self.settle(continued), "done",
                         run_registry.get(continued).get("error"))
        self.assertEqual(self.stages()[before:], ["draft"])
        self.assertEqual(checkpoint.current_source_sha(self.manifest(continued), "cover"),
                         saved_sha)
        argv = next(json.loads(spec.read_text())["argv"]
                    for spec in run_registry.state_dir(continued).glob("spec-*.json")
                    if "stage: DRAFT" in json.loads(spec.read_text())["argv"][-1])
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "7.00")

    def test_a_revision_edits_the_adopted_version_in_a_fresh_session(self):
        first, _phase = self.run_to_end()
        run_registry.update(first, session_id=None)
        before = len(self.stages())
        code, body = self.start(kind="revise", parent=first, note="lead with RAG")
        self.assertEqual(code, 202, body)
        new_id = body["run_id"]
        self.assertEqual(self.settle(new_id), "done", run_registry.get(new_id).get("error"))
        self.assertEqual(self.stages()[before:], ["revise", "review"])
        prompts = [json.loads(p.read_text())["argv"][-1]
                   for p in run_registry.state_dir(new_id).glob("spec-*.json")]
        review = next(p for p in prompts if "stage: REVIEW" in p)
        self.assertIn("focused re-check", review)
        self.assertNotIn("--resume", " ".join(
            json.loads(p.read_text())["argv"][0]
            for p in run_registry.state_dir(new_id).glob("spec-*.json")))


class BudgetTest(SupervisorCase):
    """COST-2: attempts share the application's cap; nothing loops."""

    def test_continue_counts_earlier_attempts_against_the_application_cap(self):
        self.write_config(session_budget_usd=3.0)
        os.environ.update(FAKE_MODE="crash", FAKE_ONLY="review")
        code, body = self.start()
        self.assertEqual(code, 429, "a fresh attempt already exceeds a $3 application cap")
        self.write_config(session_budget_usd=6.0)
        run_id, _phase = self.run_to_end()
        run_registry.debit(3.0, run_id)
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 429)
        self.assertIn("across its attempts", body["error"])

    def test_mechanical_only_recovery_is_admitted_on_an_exhausted_day(self):
        with mock.patch.object(runs.Supervisor, "_stage_build", interrupt_once(
                runs.Supervisor._stage_build,
                runs.RunFailure("interrupted", code="interrupted"))):
            run_id, _phase = self.run_to_end()
        run_registry.debit(1000.0)
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 202, body)
        self.assertEqual(self.settle(body["run_id"]), "done")

    def test_a_quota_failure_is_never_retried_automatically(self):
        os.environ["FAKE_MODE"] = "quota"
        run_id, _phase = self.run_to_end()
        time.sleep(1.0)
        self.assertEqual(len(self.stages()), 1)
        self.assertEqual(len([r for r in run_registry.load()["runs"]]), 1)
