#!/usr/bin/env python3
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_no_work_marker_cannot_override_nonzero_siso_pending_summary(self):
        output = """schedule pending:1+ready:0 (node:100 edge:100)
1.00s Build Succeeded: 10 steps
noexec:0 local:0 remote:0 cache:0 skip:90
ninja: no work to do.
"""
        with self.assertRaisesRegex(M.RemainingWorkError, "contradictory dry-run output"):
            M.parse_pending_actions(output)

    def test_parses_completed_siso_summary_without_subtracting_skip_twice(self):
        output = """schedule pending:99634+ready:9463 (node:591352 edge:109097)
1m49.52s Build Succeeded: 56599 steps
noexec:0 local:0 remote:0 cache:0 skip:52498
  cache-write:0(err:0) fallback:0 retry:0
"""
        self.assertEqual(M.parse_pending_actions(output), 56599)
        self.assertEqual(M.parse_siso_summary(output)["graph_action_count"], 109097)

    def test_observed_failure_summary_is_pending_work_not_steps_minus_skip(self):
        output = """schedule pending:99634+ready:9463 (node:591352 edge:109097)
1m20.07s Build Succeeded: 14989 steps - 187.20/s
noexec:0 local:0 remote:0 cache:0 skip:94108
"""
        self.assertEqual(M.parse_pending_actions(output), 14989)
        self.assertEqual(M.parse_siso_summary(output)["siso_skipped_actions"], 94108)
        self.assertEqual(M.parse_siso_summary(output)["graph_action_count"], 109097)

    def test_siso_summary_requires_both_counters(self):
        with self.assertRaisesRegex(M.RemainingWorkError, "unambiguous completed Siso"):
            M.parse_pending_actions("1m49.52s Build Succeeded: 56599 steps\n")

    def test_siso_summary_must_reconcile_with_graph_edge_count(self):
        output = """schedule pending:1+ready:0 (node:100 edge:110)
1.00s Build Succeeded: 10 steps
noexec:0 local:0 remote:0 cache:0 skip:90
"""
        with self.assertRaisesRegex(M.RemainingWorkError, "graph accounting mismatch"):
            M.parse_pending_actions(output)

    def test_zero_step_regeneration_only_summary_fails_closed(self):
        output = """schedule pending:1+ready:0 (node:100 edge:2)
../../buildtools/linux64/gn --root=../.. -q --regeneration gen .
4.99s Build Succeeded: 0 steps - 0.00/s
noexec:0 local:0 remote:0 cache:0 skip:0
"""
        with self.assertRaisesRegex(M.RemainingWorkError, "graph accounting mismatch|zero Siso steps"):
            M.parse_pending_actions(output)

    def test_missing_measurement_fails_closed(self):
        with self.assertRaisesRegex(M.RemainingWorkError, "no Ninja action rows"):
            M.parse_pending_actions("Siso finished successfully\n")

    def test_real_siso_manifest_regeneration_zero_steps_is_not_a_measurement(self):
        output = """depot_tools/siso.py: ninja -re_cache_enable_write=true -n -C out chrome_public_apk
schedule pending:1+ready:0 (node:591352 edge:2)
build start: Ready 1 Pending 1
../../buildtools/linux64/gn --root=../.. -q --regeneration gen .
rebuild manifest finished
 0.07s Regenerating ninja files
 8.34s Build Succeeded: 0 steps - 0.00/s
"""
        with self.assertRaisesRegex(M.RemainingWorkError, "no Ninja action rows"):
            M.parse_pending_actions(output)

    def test_mixed_denominators_fail_closed(self):
        with self.assertRaisesRegex(M.RemainingWorkError, "denominator changed"):
            M.parse_pending_actions("[1/2] compile a.o\n[1/3] compile b.o\n")


    def _measure_fixture(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        source = root / "src"
        out = source / "out" / "android_Debug_arm64"
        gn = source / "buildtools" / "linux64" / "gn"
        gn.parent.mkdir(parents=True)
        gn.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        gn.chmod(0o755)
        out.mkdir(parents=True)
        (out / "build.ninja").write_text("ninja graph\n", encoding="utf-8")
        (out / "build.ninja.stamp").write_text("stable stamp\n", encoding="utf-8")
        (out / "args.gn").write_text("use_siso = true\n", encoding="utf-8")
        return tmp, source, out

    def test_measure_uses_restored_graph_and_actual_siso_pending_count(self):
        tmp, source, out = self._measure_fixture()
        self.addCleanup(tmp.cleanup)
        report_path = Path(tmp.name) / "report.json"
        calls = []
        dry_run = """schedule pending:2+ready:2 (node:10 edge:4)
1.00s Build Succeeded: 2 steps
noexec:0 local:0 remote:0 cache:0 skip:2
"""

        def fake_run(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, dry_run)

        with mock.patch.object(M.shutil, "which", side_effect=lambda name: "/fake/autoninja" if name == "autoninja" else None), \
             mock.patch.object(M.subprocess, "run", side_effect=fake_run):
            report = M.measure(source_root=source, output_dir=out, stage=1, phase="start",
                               report_path=report_path, timeout_seconds=30)
        self.assertEqual(report["pending_actions"], 2)
        self.assertEqual(report["graph_action_count"], 4)
        self.assertEqual(report["measurement"], "restored_graph_autoninja_strict_remote_dry_run")
        self.assertEqual(len(calls), 1, "measurement must not regenerate the restored GN graph")
        self.assertIn("-strict_remote", calls[0])
        self.assertIn("-n", calls[0])
        self.assertIn("-j80", calls[0])
        self.assertFalse(report["compile_started"])

    def test_missing_legacy_stamp_is_recreated_only_when_graph_and_args_are_identical(self):
        tmp, source, out = self._measure_fixture()
        self.addCleanup(tmp.cleanup)
        report_path = Path(tmp.name) / "report.json"
        (out / "build.ninja.stamp").unlink()
        dry_run = (
            "schedule pending:2+ready:2 (node:10 edge:4)\n"
            "1.00s Build Succeeded: 2 steps\nnoexec:0 local:0 remote:0 cache:0 skip:2\n"
        )
        calls = []

        def fake_refresh(**kwargs):
            calls.append("gn-gen")
            (out / "build.ninja.stamp").write_text("recreated stamp\n", encoding="utf-8")
            return M.sha256_file(out / "build.ninja"), M.sha256_file(out / "args.gn")

        def fake_run(command, **kwargs):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, dry_run)

        with mock.patch.object(M.shutil, "which", side_effect=lambda name: "/fake/autoninja" if name == "autoninja" else None) as _which:
            with mock.patch.object(M, "_refresh_gn_manifest", side_effect=fake_refresh) as _refresh:
                with mock.patch.object(M.subprocess, "run", side_effect=fake_run) as _run:
                    report = M.measure(source_root=source, output_dir=out, stage=1, phase="start",
                                       report_path=report_path, timeout_seconds=30)
        self.assertEqual(report["pending_actions"], 2)
        self.assertEqual(calls[0], "gn-gen")
        self.assertEqual(len(calls), 2)
        self.assertTrue((out / "build.ninja.stamp").is_file())

    def test_missing_legacy_stamp_recovery_fails_if_gn_changes_graph(self):
        tmp, source, out = self._measure_fixture()
        self.addCleanup(tmp.cleanup)
        report_path = Path(tmp.name) / "report.json"
        (out / "build.ninja.stamp").unlink()

        def fake_refresh(**kwargs):
            (out / "build.ninja.stamp").write_text("recreated stamp\n", encoding="utf-8")
            (out / "build.ninja").write_text("changed graph\n", encoding="utf-8")
            return M.sha256_file(out / "build.ninja"), M.sha256_file(out / "args.gn")

        with mock.patch.object(M, "_refresh_gn_manifest", side_effect=fake_refresh):
            with self.assertRaisesRegex(M.RemainingWorkError, "changed build.ninja or args.gn identity"):
                M.measure(source_root=source, output_dir=out, stage=1, phase="start",
                          report_path=report_path, timeout_seconds=30)
        self.assertFalse(report_path.exists())

    def test_measure_fails_if_siso_changes_the_restored_build_stamp(self):
        tmp, source, out = self._measure_fixture()
        self.addCleanup(tmp.cleanup)
        report_path = Path(tmp.name) / "report.json"

        def fake_run(command, **kwargs):
            (out / "build.ninja.stamp").write_text("unexpected regeneration\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                command, 0,
                "schedule pending:1+ready:0 (node:10 edge:10)\n"
                "1.00s Build Succeeded: 1 steps\nnoexec:0 local:0 remote:0 cache:0 skip:9\n"
            )

        with mock.patch.object(M.shutil, "which", side_effect=lambda name: "/fake/autoninja" if name == "autoninja" else None), \
             mock.patch.object(M.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(M.RemainingWorkError, "restored build.ninja, args.gn, or build.ninja.stamp changed"):
                M.measure(source_root=source, output_dir=out, stage=1, phase="start",
                          report_path=report_path, timeout_seconds=30)
        self.assertFalse(report_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
