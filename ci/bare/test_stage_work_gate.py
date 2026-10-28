#!/usr/bin/env python3
"""Regression tests for strict measured-pending-work continuity across stages."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("stage_work_gate", HERE / "stage_work_gate.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class StageWorkGateTests(unittest.TestCase):
    def test_stage_one_requires_real_reduction_from_graph_total(self):
        result = M.validate_stage_work(stage=1, pending=52001, graph_total=109040)
        self.assertEqual(result["pending_compile_actions"], 52001)
        self.assertEqual(result["reused_or_up_to_date_graph_actions"], 57039)
        self.assertEqual(result["counting_basis"], "measured_siso_dry_run_after_restored_output_reuse")

    def test_stage_one_matches_saved_checkpoint_remaining_count_exactly(self):
        result = M.validate_stage_work(
            stage=1, pending=52001, graph_total=109040,
            prior_remaining=52001, prior_graph_total=109040,
        )
        self.assertEqual(result["previous_remaining_actions"], 52001)

    def test_stage_one_rejects_larger_than_expected_pending_total(self):
        with self.assertRaisesRegex(M.StageWorkGateError, "differs from saved previous remaining work"):
            M.validate_stage_work(
                stage=1, pending=100000, graph_total=109040,
                prior_remaining=52001, prior_graph_total=109040,
            )

    def test_stage_one_rejects_graph_size_mismatch_from_checkpoint(self):
        with self.assertRaisesRegex(M.StageWorkGateError, "graph total changed"):
            M.validate_stage_work(
                stage=1, pending=52001, graph_total=109040,
                prior_remaining=52001, prior_graph_total=109039,
            )

    def test_stage_one_legacy_checkpoint_uses_current_real_dry_run_count(self):
        # Older cache metadata may lack the saved remaining count. It may proceed
        # only when the measured dry run itself proves some graph work is skipped.
        result = M.validate_stage_work(stage=1, pending=70000, graph_total=109040)
        self.assertEqual(result["pending_compile_actions"], 70000)

    def test_stage_one_rejects_no_subtracted_work_even_for_legacy_checkpoint(self):
        with self.assertRaisesRegex(M.StageWorkGateError, "no previous work was subtracted"):
            M.validate_stage_work(stage=1, pending=109040, graph_total=109040)

    def test_continuation_stage_must_match_predecessor_remaining_count(self):
        result = M.validate_stage_work(
            stage=2, pending=52001, graph_total=109040,
            prior_remaining=52001, prior_graph_total=109040,
        )
        self.assertEqual(result["pending_compile_actions"], 52001)

    def test_continuation_stage_rejects_mismatched_predecessor_count(self):
        with self.assertRaisesRegex(M.StageWorkGateError, "differs from saved previous remaining work"):
            M.validate_stage_work(
                stage=2, pending=60000, graph_total=109040,
                prior_remaining=52001, prior_graph_total=109040,
            )

    def test_continuation_stage_fails_closed_without_predecessor_count(self):
        with self.assertRaisesRegex(M.StageWorkGateError, "lacks a saved predecessor"):
            M.validate_stage_work(stage=2, pending=52001, graph_total=109040)

    def test_gate_reads_saved_stage_one_count_from_checkpoint_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "handoff.json"
            report.write_text(json.dumps({
                "local_compiled_outputs_restored": True,
                "source_remaining_actions_after_window": 52001,
                "source_graph_action_count": 109040,
            }), encoding="utf-8")
            result = M.gate_from_files(
                stage=1, pending=52001, graph_total=109040,
                state_report_path=report, previous_ledger_path=None,
            )
            self.assertEqual(result["previous_remaining_actions"], 52001)

    def test_saved_zero_remainder_is_not_treated_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "handoff.json"
            report.write_text(json.dumps({
                "local_compiled_outputs_restored": True,
                "source_remaining_actions_after_window": 0,
                "source_graph_action_count": 109040,
            }), encoding="utf-8")
            with self.assertRaisesRegex(M.StageWorkGateError, "differs from saved previous remaining work"):
                M.gate_from_files(
                    stage=1, pending=1000, graph_total=109040,
                    state_report_path=report, previous_ledger_path=None,
                )

    def test_stage_one_cannot_pass_using_placeholder_handoff_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "handoff.json"
            report.write_text(json.dumps({"SANDIUM_STATE_HANDOFF": "NOT_RUN"}), encoding="utf-8")
            with self.assertRaisesRegex(M.StageWorkGateError, "does not prove full compiled-output checkpoint restoration"):
                M.gate_from_files(
                    stage=1, pending=52001, graph_total=109040,
                    state_report_path=report, previous_ledger_path=None,
                )

    def test_gate_reads_stage_two_count_from_predecessor_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path(directory) / "stage-1.json"
            previous.write_text(json.dumps({
                "remaining_actions_after_window": 52001,
                "graph_action_count": 109040,
            }), encoding="utf-8")
            result = M.gate_from_files(
                stage=2, pending=52001, graph_total=109040,
                state_report_path=None, previous_ledger_path=previous,
            )
            self.assertEqual(result["previous_remaining_actions"], 52001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
