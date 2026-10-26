#!/usr/bin/env python3
"""Offline tests for the bounded Siso manifest preflight."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_manifest_preflight", HERE / "manifest_preflight.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class ManifestPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = self.root / "control"
        self.source = self.root / "src"
        self.output = self.source / "out"
        self.control.mkdir()
        self.source.mkdir()
        self.output.mkdir()
        self.log = self.root / "manifest-preflight.log"

    def tearDown(self):
        self.temp.cleanup()

    def fake_run(self, responses):
        responses = iter(responses)
        calls = []

        def run(command, *, cwd, stdout, stderr, check):
            calls.append((command, cwd))
            text, code = next(responses)
            stdout.write(text)
            return subprocess.CompletedProcess(command, code)

        return run, calls

    def call_preflight(self, run_mock):
        with patch.object(M, "identity_digest", return_value="stable-build-identity"), \
             patch.object(M.subprocess, "run", side_effect=run_mock):
            return M.run_preflight(
                control=self.control, root=self.root, source_root=self.source,
                output_dir=self.output, stage=2, log_path=self.log, timeout_seconds=45,
            )

    def test_no_regeneration_passes_after_one_dry_run(self):
        run, calls = self.fake_run([("ninja: no work to do.\n", 0)])
        self.assertEqual(self.call_preflight(run), 0)
        self.assertEqual(len(calls), 1)
        command, cwd = calls[0]
        self.assertIn("-n", command)
        self.assertEqual(command[-1], "chrome_public_apk")
        self.assertEqual(cwd, self.source)
        self.assertIn("SISO MANIFEST PREFLIGHT ATTEMPT 1", self.log.read_text())

    def test_single_regeneration_is_warmed_then_stability_checked(self):
        output = "[0/2] 0.7s S Regenerating ninja files\n[0/2] 12s F Regenerating ninja files\n12s Regenerating ninja files\n"
        run, calls = self.fake_run([(output, 0), ("ninja: no work to do.\n", 0)])
        self.assertEqual(self.call_preflight(run), 0)
        self.assertEqual(len(calls), 2)
        text = self.log.read_text()
        self.assertIn("ATTEMPT 1 EXIT CODE 0", text)
        self.assertIn("ATTEMPT 2 EXIT CODE 0", text)

    def test_repeated_regeneration_fails_before_compile_window(self):
        regen = "[0/2] Regenerating ninja files\n"
        run, calls = self.fake_run([(regen, 0), (regen, 0)])
        with self.assertRaises(M.ManifestPreflightError):
            self.call_preflight(run)
        self.assertEqual(len(calls), 2)

    def test_nonzero_dry_run_fails_closed(self):
        run, calls = self.fake_run([("Siso failed\n", 49)])
        with self.assertRaises(M.ManifestPreflightError):
            self.call_preflight(run)
        self.assertEqual(len(calls), 1)

    def test_build_identity_change_during_preflight_fails_closed(self):
        run, calls = self.fake_run([("ninja: no work to do.\n", 0)])
        with patch.object(M, "identity_digest", side_effect=["before", "after"]), \
             patch.object(M.subprocess, "run", side_effect=run):
            with self.assertRaises(M.ManifestPreflightError):
                M.run_preflight(
                    control=self.control, root=self.root, source_root=self.source,
                    output_dir=self.output, stage=2, log_path=self.log, timeout_seconds=45,
                )
        self.assertEqual(len(calls), 1)

    def test_manifest_regeneration_progress_lines_are_one_invocation_event(self):
        text = "[0/2] Regenerating ninja files\n[0/2] F Regenerating ninja files\n12s Regenerating ninja files\n"
        self.assertTrue(M.has_manifest_regeneration(text))
        self.assertFalse(M.has_manifest_regeneration("ninja: no work to do.\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
