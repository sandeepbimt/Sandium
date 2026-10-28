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

    def test_parses_completed_siso_dry_run_summary(self):
        output = """1m49.52s Build Succeeded: 56599 steps
noexec:0 local:0 remote:0 cache:0 skip:52498
  cache-write:0(err:0) fallback:0 retry:0
"""
        self.assertEqual(M.parse_pending_actions(output), 4101)

    def test_siso_summary_requires_both_counters(self):
        with self.assertRaisesRegex(M.RemainingWorkError, "unambiguous completed Siso"):
            M.parse_pending_actions("1m49.52s Build Succeeded: 56599 steps\n")

    def test_siso_counters_cannot_skip_more_than_total(self):
        output = "1m49.52s Build Succeeded: 100 steps\nnoexec:0 local:0 remote:0 cache:0 skip:101\n"
        with self.assertRaisesRegex(M.RemainingWorkError, "invalid Siso dry-run counters"):
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
        (out / "args.gn").write_text("use_siso = true\n", encoding="utf-8")
        return tmp, source, out

    def test_measure_runs_explicit_gn_generation_before_dry_run(self):
        tmp, source, out = self._measure_fixture()
        self.addCleanup(tmp.cleanup)
        report_path = Path(tmp.name) / "report.json"
        calls = []

        def fake_run(command, **kwargs):
            calls.append(list(command))
            if command[0].endswith("/gn"):
                return subprocess.CompletedProcess(command, 0, "Generating ninja files...\n")
            return subprocess.CompletedProcess(command, 0, "[1/2] compile object.o\n[2/2] link apk\n")

        with mock.patch.object(M.shutil, "which", side_effect=lambda name: "/fake/autoninja" if name == "autoninja" else None), \
             mock.patch.object(M.subprocess, "run", side_effect=fake_run):
            report = M.measure(source_root=source, output_dir=out, stage=1, phase="start",
                               report_path=report_path, timeout_seconds=30)
        self.assertEqual(report["pending_actions"], 2)
        self.assertEqual(report["measurement"], "explicit_gn_gen_then_autoninja_dry_run")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1:3], ["gen", "out/android_Debug_arm64"])
        self.assertEqual(calls[1][1], "-n")
        self.assertFalse(report["compile_started"])

    def test_measure_fails_after_single_dry_run_if_siso_only_regenerates_manifest(self):
        tmp, source, out = self._measure_fixture()
        self.addCleanup(tmp.cleanup)
        report_path = Path(tmp.name) / "report.json"
        calls = []
        regen_only = """schedule pending:1+ready:0
../../buildtools/linux64/gn --root=../.. -q --regeneration gen .
rebuild manifest finished
 0.08s Regenerating ninja files
 4.99s Build Succeeded: 0 steps - 0.00/s
"""

        def fake_run(command, **kwargs):
            calls.append(list(command))
            if command[0].endswith("/gn"):
                return subprocess.CompletedProcess(command, 0, "Generating ninja files...\n")
            return subprocess.CompletedProcess(command, 0, regen_only)

        with mock.patch.object(M.shutil, "which", side_effect=lambda name: "/fake/autoninja" if name == "autoninja" else None), \
             mock.patch.object(M.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(M.RemainingWorkError, "no Ninja action rows"):
                M.measure(source_root=source, output_dir=out, stage=1, phase="start",
                          report_path=report_path, timeout_seconds=30)
        self.assertEqual(len(calls), 2, "must not loop the same ineffective dry run")
        self.assertFalse(report_path.exists(), "failed measurement must not produce a report")


if __name__ == "__main__":
    unittest.main(verbosity=2)
