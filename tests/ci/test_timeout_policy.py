"""Regression checks for SANDIUM's isolated-probe and stage-budget timeout policy."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = (ROOT / "ci/siso/remote_preflight.py").read_text(encoding="utf-8")
CONTROLLER = (ROOT / "ci/siso/run_compile_window.py").read_text(encoding="utf-8")
CONFIG = (ROOT / ".circleci/config.yml").read_text(encoding="utf-8")


class TimeoutPolicyTests(unittest.TestCase):
    def test_remote_preflight_uses_bounded_probe_timeout_not_shell_timeout_wrapper(self):
        self.assertIn("PROBE_TIMEOUT_SECONDS = 60", PREFLIGHT)
        self.assertIn("probe_timeout = min(PROBE_TIMEOUT_SECONDS, max(1, available // 3))", PREFLIGHT)
        self.assertIn('raise ValueError("Strict-remote probe failed with exit code " + str(status))', PREFLIGHT)
        self.assertNotRegex(PREFLIGHT, r"subprocess\.run\([\s\S]{0,500}?timeout\s*=")
        self.assertNotIn('shutil.which("timeout")', PREFLIGHT)

    def test_remote_execution_is_not_killed_by_a_short_elapsed_gate(self):
        self.assertNotIn("--remote-gate-seconds", CONTROLLER)
        self.assertNotIn("args.remote_gate_seconds", CONTROLLER)
        self.assertNotIn("elapsed >= args.remote_gate_seconds", CONTROLLER)

    def test_compile_deadline_reserves_checkpoint_handover_time(self):
        self.assertIn("args.max_stage_seconds - elapsed_before_compile - args.handover_reserve_seconds", CONTROLLER)
        self.assertIn("elapsed >= remaining", CONTROLLER)
        # Each CircleCI stage uses the full sub-hour budget while reserving
        # five minutes for Siso shutdown, checkpoint packaging and upload.
        self.assertIn("--handover-reserve-seconds 300", CONFIG)
        self.assertIn("--max-stage-seconds 3300", CONFIG)

    def test_circleci_quiet_step_watchdog_does_not_preempt_one_hour_stage_limit(self):
        values = re.findall(r"no_output_timeout:\s*(\S+)", CONFIG)
        self.assertTrue(values, "expected explicit CircleCI no-output watchdog settings")
        self.assertTrue(all(value == "1h" for value in values), values)


if __name__ == "__main__":
    unittest.main()
