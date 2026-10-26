#!/usr/bin/env python3
"""Offline tests for prior-run discovery and inter-stage Siso handoff gates."""
import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_state_handoff", HERE / "state_handoff.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class StateHandoffTests(unittest.TestCase):
    def test_progress_with_remote_execution_is_a_useful_candidate(self):
        ledger = {
            "max_completed_actions": 25,
            "total_action_count": 100,
            "remote_execution_proven": True,
            "remote_action_cache_reuse_proven": False,
        }
        self.assertTrue(M.useful({"stage_ledger": ledger}))

    def test_progress_with_remote_cache_hits_is_a_useful_candidate(self):
        ledger = {
            "max_completed_actions": 12,
            "total_action_count": 100,
            "remote_execution_proven": False,
            "remote_action_cache_reuse_proven": True,
        }
        self.assertTrue(M.useful({"stage_ledger": ledger}))

    def test_verified_apk_is_a_useful_candidate_even_without_more_actions(self):
        ledger = {"max_completed_actions": 0, "total_action_count": 0, "artifact_verified": True}
        self.assertTrue(M.useful({"stage_ledger": ledger}))

    def test_green_or_nonempty_ledger_without_progress_or_remote_proof_is_rejected(self):
        ledger = {
            "max_completed_actions": 8,
            "total_action_count": 100,
            "remote_execution_proven": False,
            "remote_action_cache_reuse_proven": False,
        }
        self.assertFalse(M.useful({"stage_ledger": ledger}))
        self.assertFalse(M.useful({"stage_ledger": {"artifact_verified": False}}))
        self.assertFalse(M.useful({}))

    def test_capsule_only_allows_small_siso_state_files_not_output_directories(self):
        self.assertIn(".siso_fs_state", M.VALID_NAMES)
        self.assertIn(".ninja_log", M.VALID_NAMES)
        self.assertNotIn("build.ninja", M.VALID_NAMES)
        self.assertNotIn("obj", M.VALID_NAMES)
        self.assertEqual(str(M.OUT_RELATIVE), "src/out/android_Debug_arm64")

    def test_nonempty_siso_journal_is_valid_recovery_state_when_base_is_absent(self):
        self.assertTrue(M.has_resumable_siso_state([{"name": ".siso_fs_state", "size": 12}]))
        self.assertTrue(M.has_resumable_siso_state([{"name": ".siso_fs_state.journal", "size": 128}]))
        self.assertTrue(M.has_resumable_siso_state([{"name": ".siso_fs_state.0", "size": 8}]))
        self.assertFalse(M.has_resumable_siso_state([{"name": ".siso_fs_state.journal", "size": 0}]))
        self.assertFalse(M.has_resumable_siso_state([{"name": ".ninja_log", "size": 100}]))
        self.assertFalse(M.has_resumable_siso_state([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
