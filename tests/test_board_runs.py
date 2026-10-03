"""The run supervisor, driven end to end against a stand-in for the CLI.

`tests/fake_claude.py` reproduces the parts of `claude -p` this module depends
on - the `stream-json` envelope, and the `PreToolUse` hook call before a write.
Everything else is the real code path: the real wrapper, lock, guard, registry,
checkpoint manifest and stage machine. Recovery scenarios live in
`test_board_recovery.py`.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.board_harness import (ROOT, SupervisorCase, checkpoint, docs, run_registry,
                                 runs, wait_for)
from board import run_guard  # noqa: E402


class MinimalFlowTest(SupervisorCase):
    """FLOW-1: posting -> documents, with no scoring, salary lookup or gate."""

    def test_a_new_generation_drafts_without_scoring_or_an_approval_pause(self):
        run_id, phase = self.run_to_end()
        record = run_registry.get(run_id)
        self.assertEqual(phase, "done", record.get("error"))
        self.assertEqual(self.stages(), ["draft", "review"])
        self.assertIsNone(record.get("fit"))
        self.assertNotIn("awaiting_approval", [record["phase"]])
        self.assertFalse((run_registry.run_dir(run_id) / "fit.json").exists())
        # The tracker gets the brief's metadata and no invented score.
        args, _kwargs = self.mocks["merge_tracker"].call_args
        self.assertEqual(args[1]["sector"], "AI")
        self.assertNotIn("overall", args[1])

    def test_a_finished_run_can_be_regenerated_from_its_saved_posting(self):
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done")
        code, body = self.supervisor.retry(run_id)
        self.assertEqual(code, 202, body)
        record = run_registry.get(body["run_id"])
        self.assertEqual(record["retry_of"], run_id)
        self.assertEqual(record["attempt"], 2)
        self.assertEqual(self.settle(body["run_id"]), "done",
                         run_registry.get(body["run_id"]).get("error"))
        self.assertEqual(self.manifest(body["run_id"])["inputs"]["posting"]["origin"],
                         "adopted:%s" % run_id)

    def test_the_posting_is_saved_without_a_model_when_it_can_be(self):
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done")
        posting = self.manifest(run_id)["inputs"]["posting"]
        self.assertEqual(posting["origin"], "a direct fetch by the supervisor")
        self.assertNotIn("prepare", self.stages())

    def test_a_pasted_posting_is_used_verbatim(self):
        pasted = ("Pasted posting. Requirements: Python and RAG experience; you will "
                  "build LLM evaluation pipelines with our team in Zurich.\n") * 4
        run_id, phase = self.run_to_end(posting_text=pasted)
        self.assertEqual(phase, "done")
        self.assertEqual((run_registry.run_dir(run_id) / "posting.md").read_text().strip(),
                         pasted.strip())
        self.mocks["fetch_posting_text"].assert_not_called()

    def test_a_posting_the_supervisor_cannot_fetch_goes_to_a_short_model_pass(self):
        self.fetch_result = (None, "login wall")
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        self.assertEqual(self.stages(), ["prepare", "draft", "review"])

    def test_nothing_is_drafted_from_a_title_when_the_posting_is_unobtainable(self):
        self.fetch_result = (None, "login wall")
        os.environ["FAKE_PREPARE"] = "nothing"
        run_id, phase = self.run_to_end()
        record = run_registry.get(run_id)
        self.assertEqual(phase, "failed")
        self.assertEqual(record["failure_code"], "missing_input")
        self.assertNotIn("draft", self.stages())

    def test_a_too_short_pasted_posting_is_refused_before_any_work(self):
        code, body = self.start(posting_text="ML Engineer at Acme")
        self.assertEqual(code, 400)
        self.assertIn("complete job description", body["error"])

    def test_every_pass_is_a_fresh_session(self):
        """COST-1: nothing resumes or forks a long transcript."""
        self.run_to_end()
        rows = self.call_rows()
        self.assertTrue(rows)
        self.assertFalse(any(r["resume"] or r["fork"] for r in rows))
        self.assertEqual(len({r["session"] for r in rows}), len(rows))

    def test_usage_is_recorded_per_stage(self):
        run_id, _phase = self.run_to_end()
        usage = run_registry.get(run_id)["usage"]
        self.assertEqual([u["stage"] for u in usage], ["draft", "review"])
        self.assertEqual(usage[0]["input_tokens"], 1200)
        self.assertEqual(usage[0]["cache_read_input_tokens"], 5000)
        self.assertAlmostEqual(run_registry.get(run_id)["cost"]["total_usd"], 1.17, places=4)


class ScopeTest(SupervisorCase):
    """FLOW-2: a scope is enforced by the supervisor and the allowlist."""

    def test_employer_research_receives_posting_location_not_cv_country(self):
        record = {"role": "Engineer", "company": "Acme", "job_url": "https://example.test/job",
                  "job_location": "Munich, Germany", "cv_country": "CH"}
        prompt = self.supervisor._header(record, "DRAFT", ["Shared rules"])
        self.assertIn("Posting location (board metadata): Munich, Germany", prompt)
        self.assertNotIn("CH", prompt)
        record.pop("job_location")
        self.assertIn("Posting location (board metadata): unknown",
                      self.supervisor._header(record, "DRAFT", ["Shared rules"]))

    def test_a_cover_only_run_never_drafts_builds_or_publishes_a_cv(self):
        run_id, phase = self.run_to_end(scope="cover")
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        record = run_registry.get(run_id)
        self.assertFalse((self.home / record["targets"]["cv"]).exists())
        self.assertTrue((self.home / record["targets"]["cover"]).exists())
        self.assertEqual({kind for kind, _src in self.compiled}, {"cover"})
        manifest = self.manifest(run_id)
        self.assertEqual(set(manifest["docs"]), {"cover"})
        self.assertNotIn("consistency", manifest["checks"])

    def test_a_letter_draft_carries_the_letter_rules_and_asks_for_a_plan(self):
        run_id, _phase = self.run_to_end(scope="cover")
        prompts = [json.loads(spec.read_text())["argv"][-1]
                   for spec in run_registry.state_dir(run_id).glob("spec-*.json")]
        prompt = next(p for p in prompts if "stage: DRAFT" in p)
        self.assertIn("my_cover.tex", prompt)
        self.assertIn("letter_plan", prompt)
        self.assertIn("must not read as a CV recap", prompt)
        self.assertIn("not its title", prompt)
        self.assertIn("%d body words maximum" % docs.COVER_MAX_WORDS, prompt)
        self.assertIn("one concise sentence explaining which concrete company/role work", prompt)
        self.assertIn("Exactly three unmarked lettercontent paragraphs", prompt)
        self.assertIn("candidate profile is the authority for motivation, career direction and preferences", prompt)
        self.assertIn("agent-specific interest only when relevant to actual agent responsibilities", prompt)
        self.assertIn("do not force that interest into other work", prompt)
        self.assertIn("without repeating that sentence", prompt)
        self.assertIn("Consider nlp_models for applied AI, LLM and agent product roles", prompt)
        self.assertIn("not only research jobs", prompt)
        self.assertIn("distinct contribution in letter_plan's existing evidence strings", prompt)
        self.assertIn("Budget both tailored paragraphs", prompt)
        self.assertIn("before hollowing out these tailored ideas", prompt)
        self.assertIn("developing NLP pipelines and machine learning models in Python", prompt)
        self.assertIn("canonical preamble unchanged", prompt)
        self.assertIn("Obey every COVER_EXCLUSIVE group", prompt)
        self.assertIn("Use live web search and open official", prompt)
        self.assertIn("% CONTACT_RESEARCH", prompt)
        self.assertIn("absence from the posting is not a failed search", prompt)
        self.assertNotIn("280 body words", prompt)
        self.assertNotIn("about 47 words", prompt)

    def test_review_fix_and_repair_preserve_both_tailored_paragraphs(self):
        run_id, phase = self.run_to_end(scope="cover")
        self.assertEqual(phase, "done")
        record, manifest = run_registry.get(run_id), self.manifest(run_id)
        paths = {"cover": self.home / record["targets"]["cover"]}
        review = self.supervisor._prompt_review(record, manifest, ["cover"], {}, "a" * 32)
        fix = self.supervisor._prompt_fix(record, manifest, paths, [], "a" * 32)
        for prompt in (review, fix):
            with self.subTest(stage="review" if prompt is review else "fix"):
                self.assertIn("Exactly three unmarked lettercontent paragraphs", prompt)
                self.assertIn("candidate profile is the authority for motivation, career direction and preferences", prompt)
                self.assertIn("relevant confirmed direction from the supplied candidate profile", prompt)
                self.assertIn("Consider nlp_models for applied AI, LLM and agent product roles", prompt)
                self.assertIn("Budget both tailored paragraphs", prompt)
        repair = self.supervisor._prompt_repair(record, paths, [], "a" * 32)
        self.assertIn("customised role motivation and closing sentences", repair)
        self.assertIn("least relevant third highlight before hollowing out these ideas", repair)
        self.assertIn("role-motivation paragraph before the fixed introduction", repair)
        self.assertNotIn("shortening only customised closing", repair)
        self.assertNotIn("at most TWO", prompt)

    def test_a_cover_brief_without_a_letter_plan_is_rejected(self):
        os.environ["FAKE_DRAFT"] = "noplan"
        run_id, phase = self.run_to_end(scope="cover")
        self.assertEqual(phase, "failed")
        record = run_registry.get(run_id)
        self.assertIn("letter_plan is required", record["error"])
        self.assertNotIn("review", self.stages())

    def test_a_cv_only_brief_does_not_require_a_letter_plan(self):
        os.environ["FAKE_DRAFT"] = "noplan"
        run_id, phase = self.run_to_end(scope="cv")
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        self.assertNotIn("letter_plan", self.manifest(run_id)["brief"])

    def test_a_cv_only_run_reads_no_cover_base_into_its_prompt(self):
        run_id, phase = self.run_to_end(scope="cv")
        self.assertEqual(phase, "done")
        self.assertFalse((self.home / run_registry.get(run_id)["targets"]["cover"]).exists())
        prompts = [json.loads(spec.read_text())["argv"][-1]
                   for spec in run_registry.state_dir(run_id).glob("spec-*.json")]
        prompt = next(p for p in prompts if "stage: DRAFT" in p)
        # The CV is never drafted: the only model work is the requirement brief.
        self.assertIn("Scope: brief only", prompt)
        self.assertNotIn("write CV", prompt)
        self.assertNotIn("write cover letter", prompt)
        self.assertNotIn("my_cover.tex", prompt)

    def test_the_draft_allowlist_names_exactly_the_owned_files(self):
        run_id, _phase = self.run_to_end(scope="cv")
        body = json.loads((run_registry.state_dir(run_id) / "allowlist.json").read_text())
        # A CV-only run has one pass, the brief: it may write brief.json and nothing else.
        self.assertEqual(body["dirs"], [])
        self.assertEqual(body["files"], [str((run_registry.run_dir(run_id) / "brief.json")
                                             .resolve())])

    def test_an_unknown_scope_is_refused_before_any_model_call(self):
        code, body = self.start(scope="portfolio")
        self.assertEqual(code, 400)
        self.assertFalse(self.calls.exists())

    def test_the_country_is_never_inferred_from_the_posting(self):
        run_id, _phase = self.run_to_end()
        self.assertEqual(run_registry.get(run_id)["cv_country"], "ch")
        self.assertEqual(self.manifest(run_id)["inputs"]["variant"]["country"], "ch")
        code, body = self.start(url="https://example.com/jobs/2", cv_country="berlin")
        self.assertEqual(code, 400)


class ContentReviewTest(SupervisorCase):
    def test_a_must_fix_finding_gets_one_fix_and_a_focused_recheck(self):
        os.environ["FAKE_REVIEW"] = "revise_once"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        self.assertEqual(self.stages(), ["draft", "review", "fix", "review"])
        specs = sorted(run_registry.state_dir(run_id).glob("spec-*.json"),
                       key=lambda p: p.stat().st_mtime)
        second_review = json.loads(specs[-1].read_text())["argv"][-1]
        self.assertIn("focused re-check", second_review)
        self.assertIn("```diff", second_review)

    def test_unresolved_findings_stop_the_run_and_keep_the_drafts(self):
        os.environ["FAKE_REVIEW"] = "always_fix"
        run_id, phase = self.run_to_end()
        record = run_registry.get(run_id)
        self.assertEqual(phase, "failed")
        self.assertEqual(record["failure_code"], "content_unresolved")
        manifest = self.manifest(run_id)
        self.assertEqual(set(manifest["docs"]), {"cv", "cover"})
        self.assertFalse((self.home / record["targets"]["cv"]).exists(),
                         "an unreviewed CV was published")

    def test_a_blocked_review_surfaces_source_conflicts(self):
        os.environ["FAKE_REVIEW"] = "blocked"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertIn("disagree", run_registry.get(run_id)["error"])
        self.assertTrue(any("source conflict" in issue
                            for issue in self.manifest(run_id)["issues"]))

    def test_an_invalid_review_never_counts_as_a_pass(self):
        os.environ["FAKE_REVIEW"] = "bad"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertEqual(run_registry.get(run_id)["failure_code"], "verification_failed")
        self.assertNotIn("content_cover", self.manifest(run_id)["checks"])

    def test_the_cv_is_the_untailored_master_variant(self):
        run_id, phase = self.run_to_end(scope="cv")
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        self.assertEqual(self.stages(), ["draft"], "the CV bought a model pass")
        manifest = self.manifest(run_id)
        variant = manifest["inputs"]["variant"]
        master = docs.master_cv().read_bytes()
        expected = runs.seed_cv(master, variant["role"], variant["country"],
                                checkpoint.sha256_bytes(master))
        source = checkpoint.absolute(manifest["docs"]["cv"]["source"]["path"])
        self.assertEqual(source.read_bytes(), expected)
        self.assertEqual(manifest["docs"]["cv"]["origin"], "variant:%s" % variant["role"])
        self.assertIn("untailored", manifest["checks"]["content_cv"]["detail"])

    def test_a_finding_against_the_cv_is_never_fixed_by_editing_it(self):
        os.environ["FAKE_REVIEW"] = "always_fix"
        run_id, _phase = self.run_to_end()
        manifest = self.manifest(run_id)
        self.assertTrue(manifest["docs"]["cv"]["origin"].startswith("variant:"))


class HardConflictTest(SupervisorCase):
    def test_a_hard_conflict_stops_before_drafting_and_proceed_overrides_it(self):
        os.environ["FAKE_DRAFT"] = "conflict"
        run_id, phase = self.run_to_end()
        record = run_registry.get(run_id)
        self.assertEqual(phase, "failed")
        self.assertEqual(record["failure_code"], "hard_conflict")
        self.assertIn("German", record["error"])
        self.assertEqual(self.manifest(run_id)["docs"], {})

        code, body = self.supervisor.continue_run(run_id, {"proceed": True})
        self.assertEqual(code, 202, body)
        self.assertEqual(self.settle(body["run_id"]), "done",
                         run_registry.get(body["run_id"]).get("error"))
        self.assertTrue(run_registry.get(body["run_id"])["proceed_on_conflict"])

    def test_continue_without_proceed_stops_again_without_a_model_call(self):
        os.environ["FAKE_DRAFT"] = "conflict"
        run_id, _phase = self.run_to_end()
        before = len(self.stages())
        code, body = self.supervisor.continue_run(run_id)
        self.assertEqual(code, 202)
        self.assertEqual(self.settle(body["run_id"]), "failed")
        self.assertEqual(run_registry.get(body["run_id"])["failure_code"], "hard_conflict")
        self.assertEqual(len(self.stages()), before, "a known conflict bought another pass")


class InspectionTest(SupervisorCase):
    inspection = True

    def test_inspection_reads_every_exact_pdf_and_passes(self):
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        self.assertEqual(self.stages(), ["draft", "review", "inspect"])
        manifest = self.manifest(run_id)
        for kind in ("cv", "cover"):
            self.assertEqual(checkpoint.doc_state(
                manifest, kind, self.supervisor._toolchains([kind])[kind]), "verified")

    def test_missing_read_evidence_can_never_turn_green(self):
        """CHECK-1: a clean verdict with no proof of reading is not a pass."""
        os.environ["FAKE_INSPECT"] = "noread"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        manifest = self.manifest(run_id)
        self.assertEqual(manifest["checks"]["visual_cv"]["state"], "unverified")
        self.assertFalse((self.home / run_registry.get(run_id)["targets"]["cv"]).exists())

    def test_a_layout_repair_keeps_the_content_check_and_reinspects(self):
        os.environ["FAKE_INSPECT"] = "fixable_once"
        os.environ["FAKE_INSPECT_DOC"] = "cover"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done", run_registry.get(run_id).get("error"))
        # One repair, one re-inspection - and no second content review, because
        # the repair only added a layout command.
        self.assertEqual(self.stages(), ["draft", "review", "inspect", "repair", "inspect"])
        self.assertIn("carried over",
                      self.manifest(run_id)["checks"]["content_cover"]["detail"])

    def test_repairs_are_bounded(self):
        os.environ["FAKE_INSPECT"] = "fixable"
        os.environ["FAKE_INSPECT_DOC"] = "cover"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertEqual(self.stages().count("repair"), 2)
        self.assertIn("exhausted", run_registry.get(run_id)["error"])

    def test_a_cv_layout_issue_is_reported_never_repaired(self):
        os.environ["FAKE_INSPECT"] = "fixable"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertNotIn("repair", self.stages())
        self.assertIn("CV repository", run_registry.get(run_id)["error"])

    def test_a_blocked_inspection_is_a_failure(self):
        os.environ["FAKE_INSPECT"] = "blocked"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertEqual(self.manifest(run_id)["checks"]["visual_cv"]["state"], "fail")


class BuildTest(SupervisorCase):
    def test_a_compile_failure_is_repaired_or_reported_without_publication(self):
        self.compile_fail = {"cover"}
        run_id, phase = self.run_to_end()
        record = run_registry.get(run_id)
        self.assertEqual(phase, "failed")
        self.assertEqual(record["failure_code"], "compile_error")
        self.assertEqual(self.stages().count("repair"), 2)
        self.assertFalse((self.home / record["targets"]["cover"]).exists())

    def test_a_cv_compile_failure_is_reported_without_touching_the_cv(self):
        self.compile_fail = {"cv"}
        run_id, phase = self.run_to_end()
        record = run_registry.get(run_id)
        self.assertEqual(phase, "failed")
        self.assertEqual(record["failure_code"], "compile_error")
        self.assertNotIn("repair", self.stages())
        self.assertIn("CV repository", record["error"])

    def test_a_disabled_inspection_is_shown_as_missing_not_passed(self):
        run_id, phase = self.run_to_end()
        verify = json.loads((run_registry.run_dir(run_id) / "verify.json").read_text())
        visual = [c for c in verify["checks"] if c["id"] == "visual_cv"][0]
        self.assertEqual(visual["state"], "unverified")
        self.assertIn("disabled", visual["detail"])
        manifest = self.manifest(run_id)
        self.assertEqual(checkpoint.doc_state(
            manifest, "cv", self.supervisor._toolchains(["cv"])["cv"]), "PDF built")


class GuardEnforcementTest(SupervisorCase):
    def test_a_run_whose_guard_never_fires_is_killed(self):
        os.environ["FAKE_MODE"] = "noguard"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertIn("canary", run_registry.get(run_id)["error"])
        self.assertEqual(self.manifest(run_id)["docs"], {},
                         "unguarded output was adopted as a draft")

    def test_the_hook_log_records_the_refused_probe(self):
        run_id, _phase = self.run_to_end()
        logs = sorted(run_registry.state_dir(run_id).glob("hook-*.jsonl"))
        self.assertEqual(len(logs), 2, "one hook log per pass")
        for log in logs:
            entries = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([e["decision"] for e in entries if e["probe"]], ["deny"])

    def test_each_pass_proves_the_guard_with_its_own_nonce(self):
        run_id, _phase = self.run_to_end()
        nonces = set()
        for log in run_registry.state_dir(run_id).glob("hook-*.jsonl"):
            for line in log.read_text().splitlines():
                entry = json.loads(line)
                if entry["probe"]:
                    nonces.add(entry["target"].rsplit("-", 1)[-1])
        self.assertEqual(len(nonces), 2)

    def test_preflight_runs_again_before_every_pass(self):
        os.environ["FAKE_REVIEW"] = "revise_once"
        calls = []
        original = runs.preflight

        def counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with mock.patch.object(runs, "preflight", side_effect=counting):
            run_id, phase = self.run_to_end()
        self.assertEqual(phase, "done")
        # One at admission, then one before each of the four model passes.
        self.assertEqual(len(calls), 1 + len(self.stages()))

    def test_a_symlinked_target_is_refused_before_the_run_starts(self):
        secret = self.home / "CLAUDE.md"
        link = self.home / "cv" / "main_acme_ml_engineer.tex"
        link.symlink_to(secret)
        with self.assertRaises(run_guard.PreflightError):
            run_guard.write_allowlist("r-x", targets=[link], nonce="abcd1234")


class AdmissionTest(SupervisorCase):
    def test_unknown_cv_base_is_refused(self):
        code, body = self.start(base_cv="quantum")
        self.assertEqual(code, 400)
        self.assertIn("base_cv", body["error"])

    def test_a_non_http_url_is_refused(self):
        self.assertEqual(self.start(url="file:///etc/passwd")[0], 400)

    def test_a_second_run_for_the_same_posting_is_refused(self):
        os.environ["FAKE_MODE"] = "hang"
        code, body = self.start()
        self.assertEqual(code, 202)
        self.wait_phase(body["run_id"], "drafting")
        code, second = self.start()
        self.assertEqual(code, 409)
        self.assertEqual(second["run_id"], body["run_id"])
        self.supervisor.cancel(body["run_id"])

    def test_a_url_that_is_not_on_the_board_needs_a_company_and_role(self):
        code, body = self.supervisor.start({"job_url": "https://example.com/x"})
        self.assertEqual(code, 400)
        self.assertIn("company/role", body["error"])

    def test_the_daily_cap_refuses_a_run_it_cannot_afford(self):
        run_registry.debit(49.9)
        code, body = self.start()
        self.assertEqual(code, 429)
        self.assertIn("budget", body["error"])

    def test_standing_preferences_are_recorded_for_the_guarded_draft(self):
        code, body = self.start(remember="never quote the 2,000-user figure")
        self.assertEqual(code, 202, body)
        self.assertEqual(run_registry.get(body["run_id"])["remember"],
                         "never quote the 2,000-user figure")
        self.settle(body["run_id"])

    def test_revise_requires_a_completed_parent(self):
        code, body = self.start(kind="revise")
        self.assertEqual(code, 409)
        self.assertIn("completed parent", body["error"])

    def test_the_resolved_base_is_recorded_at_admission(self):
        code, body = self.start(role="Senior LLM Engineer", url="https://example.com/j/3")
        self.assertEqual(run_registry.get(body["run_id"])["resolved_base_cv"], "ai")
        self.settle(body["run_id"])
        code, body = self.start(base_cv="ml", url="https://example.com/j/4")
        self.assertEqual(run_registry.get(body["run_id"])["resolved_base_cv"], "ai")
        self.settle(body["run_id"])


class LedgerTest(SupervisorCase):
    def test_a_quota_failure_is_categorised_and_not_retried(self):
        os.environ["FAKE_MODE"] = "quota"
        run_id, phase = self.run_to_end()
        record = run_registry.get(run_id)
        self.assertEqual(phase, "failed")
        self.assertEqual(record["failure_code"], "quota_exhausted")
        self.assertFalse(record["model_started"])
        self.assertNotIn("canary", record["error"])
        self.assertEqual(len(self.stages()), 1, "a blocked account was retried")

    def test_a_transient_rate_limit_is_distinguished_from_an_exhausted_quota(self):
        os.environ["FAKE_MODE"] = "rate_limit"
        run_id, _phase = self.run_to_end()
        self.assertEqual(run_registry.get(run_id)["failure_code"], "rate_limited")

    def test_a_failed_pass_is_still_charged(self):
        os.environ["FAKE_MODE"] = "crash"
        run_id, phase = self.run_to_end()
        self.assertEqual(phase, "failed")
        self.assertAlmostEqual(run_registry.spent_today(), 0.01, places=4)

    def test_queued_runs_reserve_their_worst_case(self):
        os.environ["FAKE_MODE"] = "hang"
        self.write_config(daily_budget_usd=6.0)
        admitted = []
        for n in range(3):
            admitted.append(self.start(url="https://example.com/jobs/%d" % n)[0])
        self.assertEqual(admitted[0], 202)
        self.assertEqual(admitted[1], 429)
        for run in run_registry.load()["runs"]:
            self.supervisor.cancel(run["id"])

    def test_the_ledger_resets_on_a_new_day(self):
        run_registry.debit(3.0)
        with run_registry.runs_lock():
            data = run_registry.load()
            data["ledger"]["date"] = "2020-01-01"
            run_registry.store(data)
        self.assertEqual(run_registry.spent_today(), 0.0)

    def test_a_live_orphan_keeps_its_reservation(self):
        stub = self.spawn_stub()
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000002-orph-eeeeee", "phase": "orphaned",
                "pid": stub.pid, "pgid": stub.pid, "session_id": "x",
                "job_url": "https://x", "company": "A", "role": "B",
                "budget_usd": {"pass_a": 0.40, "pass_b": 2.00}, "cost": {"total_usd": 0.0}})
            run_registry.store(data)
        self.assertAlmostEqual(run_registry.reserved(), 2.40, places=4)

    def test_reserved_ignores_finished_runs(self):
        run_id, _phase = self.run_to_end()
        self.assertEqual(run_registry.reserved(), 0.0)

    def test_legacy_config_caps_carry_over_to_the_new_stages(self):
        self.write_config(budget_usd={"pass_a": 5, "pass_b": 12, "revise": 4})
        settings = run_registry.config()
        self.assertEqual(settings["budget_usd"]["draft"], 12)
        self.assertEqual(settings["budget_usd"]["prepare"], 5)
        self.assertEqual(settings["budget_usd"]["fix"], 4)


class PassBudgetTest(SupervisorCase):
    """A pass may use the reserved balance, without increasing that reservation."""

    def spawn_with_budget(self, reservation, spent, requested):
        run_id = run_registry.new_run_id("Acme")
        record = {"id": run_id, "phase": "drafting", "budget_usd": reservation,
                  "cost": {"total_usd": spent}}
        run_registry.append(record)
        job = mock.Mock(exit_code=0)
        job.usage.return_value = {"cost_usd": 0.0}
        with mock.patch.object(runs, "preflight") as preflight, \
                mock.patch.object(runs.run_proc, "Pass", return_value=job) as constructor:
            try:
                self.supervisor._spawn(record, "draft", "brief migration",
                                       lambda nonce: "brief only", requested, 10)
            except runs.RunFailure as exc:
                return run_id, exc, preflight, constructor
        return run_id, None, preflight, constructor

    def test_a_stage_ceiling_is_clipped_to_remaining_attempt_budget(self):
        reservation = {"revise": 4.0, "pass_c": 1.5}
        run_id, failure, _preflight, constructor = self.spawn_with_budget(
            reservation, 0.5868, 12.0)
        self.assertIsNone(failure)
        argv = constructor.call_args.kwargs["model_argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "4.91")
        self.assertEqual(constructor.call_args.args[4], 4.91)
        self.assertEqual(run_registry.get(run_id)["budget_usd"], reservation)
        self.assertEqual(run_registry.get(run_id)["cost"]["total_usd"], 0.5868)

    def test_a_lower_stage_ceiling_is_still_respected_and_rounded_down(self):
        _run_id, failure, _preflight, constructor = self.spawn_with_budget(
            {"draft": 5.50}, 0.5868, 1.009)
        self.assertIsNone(failure)
        argv = constructor.call_args.kwargs["model_argv"]
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1.00")

    def test_less_than_one_cent_or_an_overspent_reservation_starts_no_pass(self):
        for spent in (5.4901, 5.50, 5.60):
            with self.subTest(spent=spent):
                _run_id, failure, preflight, constructor = self.spawn_with_budget(
                    {"revise": 4.0, "pass_c": 1.5}, spent, 12.0)
                self.assertEqual(failure.code, "budget_cap")
                self.assertFalse(failure.model_started)
                preflight.assert_not_called()
                constructor.assert_not_called()


class CancelTest(SupervisorCase):
    def test_a_cancelled_run_is_not_resurrected_by_the_next_stage(self):
        os.environ["FAKE_MODE"] = "hang"
        code, body = self.start()
        run_id = body["run_id"]
        self.assertEqual(self.wait_phase(run_id, "drafting"), "drafting")
        self.assertEqual(self.supervisor.cancel(run_id)[0], 200)
        self.assertEqual(self.settle(run_id), "cancelled")
        time.sleep(0.5)
        self.assertEqual(self.phase_of(run_id), "cancelled")
        with self.assertRaises(runs.RunFailure):
            self.supervisor._phase(run_id, "reviewing")
        self.assertEqual(self.phase_of(run_id), "cancelled")

    def test_a_cancelled_run_is_not_marked_done(self):
        run_registry.append({"id": "r-20260101-000000-acme-aaaaaa", "phase": "cancelled",
                             "job_url": "https://x", "company": "A", "role": "B"})
        self.supervisor._settle_ok("r-20260101-000000-acme-aaaaaa", {})
        self.assertEqual(self.phase_of("r-20260101-000000-acme-aaaaaa"), "cancelled")

    def test_a_late_failure_does_not_relabel_a_cancelled_run(self):
        run_registry.append({"id": "r-20260101-000000-acme-bbbbbb", "phase": "cancelled",
                             "error": "cancelled", "job_url": "https://x",
                             "company": "A", "role": "B"})
        self.supervisor._fail("r-20260101-000000-acme-bbbbbb", "some later failure")
        self.assertEqual(run_registry.get("r-20260101-000000-acme-bbbbbb")["error"],
                         "cancelled")

    def test_cancelling_twice_reports_the_second_as_a_conflict(self):
        os.environ["FAKE_MODE"] = "hang"
        code, body = self.start()
        self.wait_phase(body["run_id"], "drafting")
        self.assertEqual(self.supervisor.cancel(body["run_id"])[0], 200)
        self.assertEqual(self.supervisor.cancel(body["run_id"])[0], 409)

    def test_stop_during_preparation_never_spawns(self):
        started = threading.Event()

        def slow(url):
            started.set()
            time.sleep(1.5)
            return self.fetch_result

        self.mocks["fetch_posting_text"].side_effect = slow
        code, body = self.start()
        self.assertTrue(started.wait(20))
        self.supervisor.stop(timeout=20)
        self.assertFalse(self.calls.exists(), "a model process ran after stop() returned")
        self.assertIn(self.phase_of(body["run_id"]), ("cancelled", "failed"))


class TwoBoardsTest(SupervisorCase):
    def test_a_second_board_leaves_a_live_boards_runs_alone(self):
        other_board = self.spawn_stub()
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20260101-000000-other-aaaa", "phase": "drafting",
                "pid": other_board.pid, "pgid": other_board.pid, "session_id": "x",
                "owner": "some-other-board", "owner_pid": other_board.pid,
                "owner_started": run_registry.process_start_time(other_board.pid),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        other = runs.Supervisor()
        self.assertEqual(other.reconcile(), [])
        self.assertEqual(self.phase_of("r-20260101-000000-other-aaaa"), "drafting")

    def test_shutdown_kills_only_this_boards_runs(self):
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20260101-000001-other-bbbb", "phase": "drafting",
                "pid": os.getpid(), "pgid": os.getpid(), "session_id": "x",
                "owner": "some-other-board", "owner_pid": os.getpid(),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        runs._SUPERVISOR = self.supervisor
        self.addCleanup(setattr, runs, "_SUPERVISOR", None)
        runs.shutdown()
        self.assertEqual(self.phase_of("r-20260101-000001-other-bbbb"), "drafting")


class OwnershipTest(SupervisorCase):
    def test_a_record_with_no_owner_is_reconciled_but_never_killed(self):
        stub = self.spawn_stub(marker="legacy")
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000000-legacy-cccccc", "phase": "drafting",
                "pid": stub.pid, "pgid": stub.pid, "session_id": "legacy",
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        self.supervisor.reconcile()
        self.assertEqual(self.phase_of("r-20250101-000000-legacy-cccccc"), "orphaned")
        runs._SUPERVISOR = self.supervisor
        self.addCleanup(setattr, runs, "_SUPERVISOR", None)
        runs.shutdown()
        self.assertIsNone(stub.poll(), "shutdown killed a run this board does not own")
        code, body = self.supervisor.kill("r-20250101-000000-legacy-cccccc")
        self.assertEqual(code, 409)
        self.assertIn("which board owns", body["error"])
        self.assertIsNone(stub.poll())

    def test_an_unreadable_ps_leaves_another_boards_run_alone(self):
        other = self.spawn_stub()
        record = {"owner_pid": other.pid, "owner": "board-a",
                  "owner_started": run_registry.process_start_time(other.pid)}
        self.assertIs(run_registry.board_alive(record), True)
        with mock.patch.object(run_registry, "_ps", return_value=None):
            self.assertIsNone(run_registry.board_alive(record))

    def test_a_record_with_no_owner_at_all_is_unknown_not_unowned(self):
        self.assertIsNone(run_registry.board_alive({"pid": 1234}))

    def test_kill_refuses_when_the_owning_board_turns_out_to_be_alive(self):
        other = self.spawn_stub(marker="sess-live")
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000004-owned-999999", "phase": "orphaned",
                "pid": other.pid, "pgid": other.pid, "session_id": "sess-live",
                "proc_started": run_registry.process_start_time(other.pid),
                "owner": "another-board", "owner_pid": other.pid,
                "owner_started": run_registry.process_start_time(other.pid),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        code, body = self.supervisor.kill("r-20250101-000004-owned-999999")
        self.assertEqual(code, 409)
        self.assertIsNone(other.poll())


class OrphanIdentityTest(SupervisorCase):
    def test_identity_that_cannot_be_proven_is_not_a_match(self):
        self.assertFalse(run_registry.process_matches({"pid": os.getpid()}))

    def test_both_recorded_checks_must_pass_for_a_strict_match(self):
        stub = self.spawn_stub(marker="sess-abc")
        base = {"pid": stub.pid, "session_id": "sess-abc",
                "proc_started": run_registry.process_start_time(stub.pid)}
        self.assertTrue(run_registry.process_matches(base))
        self.assertFalse(run_registry.process_matches(dict(base, session_id="other")))

    def test_an_unreadable_ps_surfaces_but_does_not_signal(self):
        stub = self.spawn_stub(marker="sess-xyz")
        record = {"pid": stub.pid, "session_id": "sess-xyz",
                  "proc_started": run_registry.process_start_time(stub.pid)}
        with mock.patch.object(run_registry, "_ps", return_value=None):
            self.assertTrue(run_registry.process_matches(record, strict=False))
            self.assertFalse(run_registry.process_matches(record, strict=True))

    def test_kill_revalidates_before_signalling(self):
        stub = self.spawn_stub(marker="not-this-run")
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({
                "id": "r-20250101-000003-recyc-ffffff", "phase": "orphaned",
                "pid": stub.pid, "pgid": stub.pid, "session_id": "a-different-session",
                "proc_started": run_registry.process_start_time(stub.pid),
                "owner": run_registry.OWNER, "owner_pid": os.getpid(),
                "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        code, body = self.supervisor.kill("r-20250101-000003-recyc-ffffff")
        self.assertEqual(code, 200)
        self.assertEqual(body["signals"], [])
        self.assertIsNone(stub.poll(), "an unrelated process was signalled")


class TerminationTest(SupervisorCase):
    def test_a_child_that_ignores_sigterm_is_still_killed(self):
        from board import run_proc
        import subprocess
        script = ("import os, signal, time, sys\n"
                  "os.setsid()\n"
                  "pid = os.fork()\n"
                  "if pid == 0:\n"
                  "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "    time.sleep(60)\n"
                  "    sys.exit(0)\n"
                  "print(os.getpgid(0), flush=True)\n"
                  "time.sleep(60)\n")
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)
        pgid = int(proc.stdout.readline().strip())
        self.assertEqual(run_proc.terminate(pgid, proc.pid, grace=1.0),
                         ["SIGTERM", "SIGKILL"])

    def test_the_wrapper_refuses_to_run_without_its_own_session(self):
        import subprocess
        spec = Path(self.tmp.name) / "spec.json"
        spec.write_text(json.dumps({
            "run_id": "r-x", "argv": [sys.executable, "-c", "print('should not run')"],
            "cwd": str(self.home), "registry": str(run_registry.REGISTRY),
            "runs_lock": str(run_registry.RUNS_LOCK),
            "model_lock": str(run_registry.MODEL_LOCK), "env": {}}), encoding="utf-8")
        wrapper = ROOT / "tools" / "board" / "run_wrapper.py"
        code = ("import os, runpy, sys\n"
                "os.setsid()\n"
                "sys.argv = ['run_wrapper.py', '--spec', %r]\n"
                "runpy.run_path(%r, run_name='__main__')\n" % (str(spec), str(wrapper)))
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("should not run", proc.stdout)


class ReconcileTest(SupervisorCase):
    def test_a_dead_staged_run_becomes_continuable_not_restarted(self):
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({"id": "r-20260101-000000-acme-cafe01", "pipeline": 2,
                                 "phase": "reviewing", "pid": 999999, "pgid": 999999,
                                 "session_id": "gone", "owner": "dead", "owner_pid": 999999,
                                 "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        self.assertEqual(self.supervisor.reconcile(), [])
        record = run_registry.get("r-20260101-000000-acme-cafe01")
        self.assertEqual(record["phase"], "failed")
        self.assertEqual(record["failure_code"], "interrupted")
        self.assertIn("Continue", record["error"])
        self.assertFalse(self.calls.exists(), "reconciliation spawned paid work")

    def test_a_live_orphan_is_surfaced_rather_than_adopted(self):
        marker = "jobflow-orphan-marker"
        proc = self.spawn_stub(marker=marker)
        with run_registry.runs_lock():
            data = run_registry.load()
            data["runs"].append({"id": "r-20260101-000001-acme", "phase": "drafting",
                                 "pid": proc.pid, "pgid": proc.pid, "session_id": marker,
                                 "owner": "a-dead-board", "owner_pid": 999999,
                                 "job_url": "https://x", "company": "A", "role": "B"})
            run_registry.store(data)
        self.supervisor.reconcile()
        self.assertEqual(self.phase_of("r-20260101-000001-acme"), "orphaned")
        code = None
        for _ in range(10):
            code, _body = self.supervisor.kill("r-20260101-000001-acme")
            if code != 409:
                break
            time.sleep(0.2)
        self.assertEqual(code, 200)
        self.assertIsNotNone(wait_for(lambda: proc.poll() is not None))


class PreflightTest(unittest.TestCase):
    def test_the_shipped_settings_file_is_the_shape_the_guard_expects(self):
        self.assertTrue(run_guard.validate_settings())

    def test_a_malformed_settings_file_is_caught_here_because_the_cli_will_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "settings.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(run_guard.PreflightError) as caught:
                run_guard.validate_settings(bad)
            self.assertIn("no guard", str(caught.exception))

    def test_a_narrowed_matcher_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            narrow = Path(tmp) / "settings.json"
            narrow.write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": "Write", "hooks": [
                    {"type": "command", "command": "python3 tools/board/guard_write.py"}]}]}}),
                encoding="utf-8")
            with self.assertRaises(run_guard.PreflightError):
                run_guard.validate_settings(narrow)

    def test_the_guard_is_executed_for_real_before_every_run(self):
        self.assertTrue(run_guard.probe_guard())


class ContractValidatorTest(unittest.TestCase):
    def test_the_brief_carries_no_score_and_nulls_are_explicit(self):
        from tests.fake_claude import BRIEF
        self.assertEqual(run_guard.validate_brief(BRIEF), [])
        without = dict(BRIEF)
        del without["deadline"]
        self.assertIn("deadline is required (use null when the posting does not state it)",
                      run_guard.validate_brief(without))
        self.assertTrue(run_guard.validate_brief(dict(BRIEF, keywords=[""])))

    def test_a_letter_plan_is_optional_but_must_be_complete(self):
        from tests.fake_claude import BRIEF
        plan = {"role_task": "evaluate agents", "role_task_source": "Responsibilities",
                "primary_evidence": "[A]", "secondary_evidence": "[B]",
                "connection": None, "unknowns": []}
        self.assertEqual(run_guard.validate_brief(dict(BRIEF, letter_plan=plan)), [])
        self.assertIn("letter_plan.primary_evidence must be a non-empty string",
                      run_guard.validate_brief(dict(BRIEF, letter_plan=dict(
                          plan, primary_evidence=""))))
        self.assertTrue(run_guard.validate_brief(dict(BRIEF, letter_plan=dict(
            plan, unknowns="none"))))

    def test_a_review_cannot_rule_on_an_item_it_was_not_asked_about(self):
        review = {"schema": "jobflow.review/1", "verdict": "revise", "findings": [
            {"doc": "cover", "severity": "must_fix", "category": "grounding",
             "issue": "x", "fix": "y"}]}
        self.assertTrue(run_guard.validate_review(review, ["cv"]))
        self.assertEqual(run_guard.validate_review(review, ["cv", "cover"]), [])
        self.assertTrue(run_guard.validate_review(dict(review, verdict="pass"),
                                                  ["cv", "cover"]))


class SlugTest(unittest.TestCase):
    def test_the_slug_matches_the_documents_convention(self):
        self.assertEqual(run_registry.slugify("DeepJudge", "Applied AI Engineer"),
                         "deepjudge_applied_ai_engineer")

    def test_a_run_id_is_a_run_id(self):
        from board import server
        run_id = run_registry.new_run_id("Acme AG")
        self.assertEqual(server.run_route("/api/runs/" + run_id), (run_id, None))
        self.assertEqual(server.run_route("/api/runs/" + run_id + "/continue"),
                         (run_id, "continue"))
        self.assertEqual(server.run_route("/api/runs/" + run_id + "/retry"),
                         (run_id, "retry"))
        self.assertEqual(server.run_route("/api/runs/../../etc/passwd"), (None, None))


if __name__ == "__main__":
    unittest.main()
