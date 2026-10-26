#!/usr/bin/env python3
"""Regression tests for the Siso evidence formats observed in proven builds."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("siso_evidence", HERE / "siso_evidence.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class SisoEvidenceTests(unittest.TestCase):
    def test_successful_sandfox_afterbird_summary_proves_remote_and_cache(self):
        # Captured format from SANDFOX GitHub Actions run 38078381386.
        evidence = M.parse_text(
            "local:16660 remote:16674 cache:30278 cache-write:0(err:0) "
            "fallback:0 retry:0 skip:34067\n"
            "reapi: ops: 80319(err:16675) / r:93906(err:0)\n"
        )
        self.assertEqual(evidence.remote_actions, 16674)
        self.assertEqual(evidence.cache_hits, 30278)
        self.assertTrue(evidence.remote_proven)
        self.assertTrue(evidence.cache_reuse_proven)

    def test_structured_remote_and_cache_actions_are_detected(self):
        evidence = M.parse_text(
            '{"action":"compile","is_remote":true}\n'
            '{"action":"compile","cached":true}\n'
            '{"remote_action_count":3,"cache_hit_count":2}\n'
        )
        self.assertEqual(evidence.remote_actions, 3)
        self.assertEqual(evidence.cache_hits, 2)
        self.assertTrue(evidence.remote_proven)
        self.assertTrue(evidence.cache_reuse_proven)

    def test_cache_only_is_not_misreported_as_remote_execution(self):
        evidence = M.parse_text('{"action":"compile","cached":true}\n')
        self.assertFalse(evidence.remote_proven)
        self.assertTrue(evidence.cache_reuse_proven)

    def test_empty_or_local_only_logs_do_not_prove_remote_execution(self):
        evidence = M.parse_text(
            "local:40 remote:0 cache:0 cache-write:0 fallback:40 retry:0 skip:0\n"
        )
        self.assertFalse(evidence.remote_proven)
        self.assertFalse(evidence.cache_reuse_proven)
        self.assertFalse(M.parse_text("build finished\n").remote_proven)

    def test_missing_and_symlinked_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.log"
            target = root / "real.log"
            target.write_text("local:0 remote:1 cache:1 cache-write:0 fallback:0 retry:0 skip:0\n")
            link = root / "link.log"
            link.symlink_to(target)
            evidence = M.inspect_files([missing, link])
            self.assertFalse(evidence.remote_proven)


if __name__ == "__main__":
    unittest.main(verbosity=2)
