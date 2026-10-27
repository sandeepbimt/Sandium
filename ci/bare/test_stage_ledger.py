#!/usr/bin/env python3
"""Unit tests for Siso evidence parsing and incremental progress gates."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_stage_ledger", HERE / "stage_ledger.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class LedgerTests(unittest.TestCase):
    def test_cached_record_without_locality_is_cache_reuse_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "metrics.jsonl").write_text(
                json.dumps({"step_id": "x", "outputs": ["a.o"], "cached": True}) + "\n"
            )
            (root / "siso.log").write_text('use reapi "remote.buildbuddy.io:443"\n')
            result = M.parse_metrics(root / "metrics.jsonl", root / "siso.log")
            self.assertTrue(result["remote_action_cache_reuse_proven"])
            self.assertFalse(result["remote_execution_proven"])

    def test_endpoint_alone_is_not_remote_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "siso.log").write_text('use reapi "remote.buildbuddy.io:443"\n')
            result = M.parse_metrics(root / "missing.jsonl", root / "siso.log")
            self.assertFalse(result["remote_execution_proven"])

    def test_siso_summary_counts_are_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "siso.log").write_text("Siso finished local:8, remote:81, cache:103\n")
            result = M.parse_metrics(root / "missing.jsonl", root / "siso.log")
            self.assertEqual(result["summary_remote_actions"], 81)
            self.assertEqual(result["summary_cache_hits"], 103)

    def test_stage_one_requires_progress_and_remote_or_cache_evidence(self):
        from build_identity import IdentityError
        with self.assertRaises(IdentityError):
            M.validate_progress(
                1, {"remote_execution_proven": False, "remote_action_cache_reuse_proven": False},
                1, 10, None, {"local_compiled_outputs_restored": False},
            )

    def test_later_stage_does_not_add_overlapping_action_counters(self):
        previous = {"max_completed_actions": 96, "total_action_count": 100}
        handoff = {"local_compiled_outputs_restored": True, "local_compiled_outputs_reuse_verified": True, "source_max_completed_actions": 96}
        metrics = {"remote_execution_proven": False, "remote_action_cache_reuse_proven": True}
        self.assertEqual(
            M.validate_progress(2, metrics, 3, 100, previous, handoff),
            "REMOTE_ACTION_CACHE_HIT_WITH_FORWARD_PROGRESS",
        )
        # Current-stage activity proves forward progress, but overlaps with the
        # previous checkpoint's graph-level count and must not be added.
        self.assertEqual(M.cumulative_progress(2, 3, 100, previous, handoff), 96)

    def test_later_stage_requires_some_new_progress(self):
        from build_identity import IdentityError
        previous = {"max_completed_actions": 90, "total_action_count": 100}
        handoff = {"local_compiled_outputs_restored": True, "local_compiled_outputs_reuse_verified": True, "source_max_completed_actions": 90}
        with self.assertRaisesRegex(IdentityError, "No parseable completed-action progress"):
            M.validate_progress(
                2, {"remote_execution_proven": True}, 0, 100, previous, handoff
            )

    def test_graph_total_must_be_stable_across_stages(self):
        from build_identity import IdentityError
        previous = {"max_completed_actions": 50, "total_action_count": 100}
        handoff = {"local_compiled_outputs_restored": True, "local_compiled_outputs_reuse_verified": True, "source_max_completed_actions": 50}
        with self.assertRaisesRegex(IdentityError, "Build graph action count changed"):
            M.validate_progress(
                2, {"remote_execution_proven": True}, 20, 101, previous, handoff
            )

    def test_continuation_requires_reuse_proof_not_just_restore_flag(self):
        from build_identity import IdentityError
        previous = {"max_completed_actions": 50, "total_action_count": 100}
        handoff = {
            "local_compiled_outputs_restored": True,
            "local_compiled_outputs_reuse_verified": False,
            "source_max_completed_actions": 50,
        }
        with self.assertRaisesRegex(IdentityError, "did not prove local compiled-output reuse"):
            M.cumulative_progress(
                2, 10, 100, previous, handoff
            )

    def test_stage_one_uses_a_non_additive_watermark_for_prior_checkpoint(self):
        handoff = {"local_compiled_outputs_restored": True, "local_compiled_outputs_reuse_verified": True, "source_max_completed_actions": 70}
        self.assertEqual(M.cumulative_progress(1, 10, 100, None, handoff), 70)

    def test_state_capsule_does_not_claim_local_compiled_output_progress(self):
        handoff = {"local_compiled_outputs_restored": False, "source_max_completed_actions": 70}
        self.assertEqual(M.cumulative_progress(1, 10, 100, None, handoff), 10)

    def test_stage_local_actions_are_not_added_to_checkpoint_watermark(self):
        handoff = {"local_compiled_outputs_restored": True, "local_compiled_outputs_reuse_verified": True, "source_max_completed_actions": 95}
        self.assertEqual(M.cumulative_progress(1, 10, 100, None, handoff), 95)

    def test_required_work_estimate_tracks_watermark_without_claiming_unique_compile_count(self):
        first = M.required_work_accounting(100, 40, None, {})
        self.assertEqual(first["required_actions_at_start"], 100)
        self.assertEqual(first["remaining_actions_after_window"], 60)
        self.assertEqual(first["required_work_reduction_vs_predecessor_remaining"], 40)
        self.assertFalse(first["required_work_is_unique_compiled_action_count"])

        previous = {"estimated_remaining_actions": 60}
        next_stage = M.required_work_accounting(
            100, 65, previous,
            {"local_compiled_outputs_restored": True,
             "local_compiled_outputs_reuse_verified": True,
             "source_max_completed_actions": 40},
        )
        self.assertEqual(next_stage["required_actions_at_start"], 60)
        self.assertEqual(next_stage["remaining_actions_after_window"], 35)
        self.assertEqual(next_stage["required_work_reduction_vs_predecessor_remaining"], 25)

    def test_required_work_estimate_rejects_invalid_watermark(self):
        with self.assertRaisesRegex(M.IdentityError, "Invalid graph total or progress watermark"):
            M.required_work_accounting(100, 101, None, {})

    def test_progress_fields_disclose_non_additive_watermark_semantics(self):
        fields = M.progress_accounting_fields(55453, 55453, 109040)
        self.assertEqual(fields["progress_accounting_mode"], "NON_ADDITIVE_WATERMARK")
        self.assertEqual(fields["progress_watermark"], 55453)
        self.assertEqual(fields["estimated_remaining_actions"], 53587)
        self.assertFalse(fields["progress_is_unique_cumulative_action_count"])
        self.assertEqual(
            fields["remaining_estimate_basis"],
            "total_graph_edges_minus_non_additive_progress_watermark",
        )

    def test_legacy_additive_ledger_is_repaired_for_stage_continuation(self):
        # Reproduce the actual failure: Stage 1 added 51,734 checkpoint actions
        # to 52,001 current-stage actions. Stage 2 then tried to add another
        # 55,453 and failed because 103,735 + 55,453 > 109,040.
        previous = {
            "schema": M.SCHEMA,
            "stage": 1,
            "max_completed_actions": 103735,
            "stage_completed_actions": 52001,
            "total_action_count": 109040,
            "state_handoff": {
                "local_compiled_outputs_restored": True,
                "source_max_completed_actions": 51734,
            },
        }
        handoff = {
            "local_compiled_outputs_restored": True,
            "local_compiled_outputs_reuse_verified": True,
            "source_max_completed_actions": 103735,
        }
        metrics = {"remote_execution_proven": False, "remote_action_cache_reuse_proven": True}
        self.assertEqual(M.progress_watermark(previous), 52001)
        self.assertEqual(M.cumulative_progress(2, 55453, 109040, previous, handoff), 55453)
        self.assertEqual(
            M.validate_progress(2, metrics, 55453, 109040, previous, handoff),
            "REMOTE_ACTION_CACHE_HIT_WITH_FORWARD_PROGRESS",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
