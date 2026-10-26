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

    def test_later_stage_accepts_smaller_incremental_slice_with_cumulative_progress(self):
        previous = {"max_completed_actions": 96, "total_action_count": 100}
        handoff = {"local_compiled_outputs_restored": True, "source_max_completed_actions": 96}
        metrics = {"remote_execution_proven": False, "remote_action_cache_reuse_proven": True}
        self.assertEqual(
            M.validate_progress(2, metrics, 3, 100, previous, handoff),
            "REMOTE_ACTION_CACHE_HIT_WITH_FORWARD_PROGRESS",
        )
        self.assertEqual(M.cumulative_progress(2, 3, 100, previous, handoff), 99)

    def test_later_stage_requires_some_new_progress(self):
        from build_identity import IdentityError
        previous = {"max_completed_actions": 90, "total_action_count": 100}
        handoff = {"local_compiled_outputs_restored": True, "source_max_completed_actions": 90}
        with self.assertRaisesRegex(IdentityError, "No parseable completed-action progress"):
            M.validate_progress(
                2, {"remote_execution_proven": True}, 0, 100, previous, handoff
            )

    def test_graph_total_must_be_stable_across_stages(self):
        from build_identity import IdentityError
        previous = {"max_completed_actions": 50, "total_action_count": 100}
        handoff = {"local_compiled_outputs_restored": True, "source_max_completed_actions": 50}
        with self.assertRaisesRegex(IdentityError, "Build graph action count changed"):
            M.validate_progress(
                2, {"remote_execution_proven": True}, 20, 101, previous, handoff
            )

    def test_stage_one_accumulates_a_compatible_prior_run_checkpoint(self):
        handoff = {"local_compiled_outputs_restored": True, "source_max_completed_actions": 70}
        self.assertEqual(M.cumulative_progress(1, 10, 100, None, handoff), 80)

    def test_state_capsule_does_not_claim_local_compiled_output_progress(self):
        handoff = {"local_compiled_outputs_restored": False, "source_max_completed_actions": 70}
        self.assertEqual(M.cumulative_progress(1, 10, 100, None, handoff), 10)

    def test_cumulative_count_cannot_exceed_action_graph(self):
        from build_identity import IdentityError
        handoff = {"local_compiled_outputs_restored": True, "source_max_completed_actions": 95}
        with self.assertRaisesRegex(IdentityError, "exceeds graph total"):
            M.cumulative_progress(1, 10, 100, None, handoff)


if __name__ == "__main__":
    unittest.main(verbosity=2)
