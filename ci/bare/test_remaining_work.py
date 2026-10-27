#!/usr/bin/env python3
import importlib.util
import unittest

HERE = __import__("pathlib").Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_remaining_work", HERE / "remaining_work.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class RemainingWorkTests(unittest.TestCase):
    def test_counts_scheduled_actions_not_graph_denominator(self):
        output = "\n".join(f"[{i}/4] c++ compile unit_{i}.o" for i in range(1, 5))
        self.assertEqual(M.parse_pending_actions(output), 4)

    def test_counts_dirty_actions_when_graph_denominator_is_larger(self):
        self.assertEqual(
            M.parse_pending_actions("[1/109068] compile changed.o\n[2/109068] link target\n"),
            2,
        )

    def test_explicit_no_work_is_zero(self):
        self.assertEqual(M.parse_pending_actions("ninja: no work to do.\n"), 0)

    def test_parses_completed_siso_dry_run_summary(self):
        output = """1m49.52s Build Succeeded: 56599 steps
noexec:0 local:0 remote:0 cache:0 skip:52498
  cache-write:0(err:0) fallback:0 retry:0
"""
        self.assertEqual(M.parse_pending_actions(output), 4101)

    def test_siso_summary_requires_both_counters(self):
        with self.assertRaisesRegex(M.RemainingWorkError, "unambiguous completed Siso"):
            M.parse_pending_actions("1m49.52s Build Succeeded: 56599 steps\\n")

    def test_siso_counters_cannot_skip_more_than_total(self):
        output = "1m49.52s Build Succeeded: 100 steps\\nnoexec:0 local:0 remote:0 cache:0 skip:101\\n"
        with self.assertRaisesRegex(M.RemainingWorkError, "invalid Siso dry-run counters"):
            M.parse_pending_actions(output)

    def test_missing_measurement_fails_closed(self):
        with self.assertRaisesRegex(M.RemainingWorkError, "neither parseable action rows"):
            M.parse_pending_actions("Siso finished successfully\n")

    def test_mixed_denominators_fail_closed(self):
        with self.assertRaisesRegex(M.RemainingWorkError, "denominator changed"):
            M.parse_pending_actions("[1/2] compile a.o\n[1/3] compile b.o\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
