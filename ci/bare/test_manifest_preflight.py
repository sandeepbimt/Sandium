#!/usr/bin/env python3
"""Offline tests for the filesystem-only Siso manifest preflight."""
import importlib.util
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
        (self.output / "args.gn").write_text("use_siso=true\n", encoding="utf-8")
        (self.output / "build.ninja").write_text("build chrome_public_apk: phony\n", encoding="utf-8")
        self.log = self.root / "manifest-preflight.log"

    def tearDown(self):
        self.temp.cleanup()

    def call_preflight(self, identities=("stable-build-identity", "stable-build-identity")):
        with patch.object(M, "identity_digest", side_effect=identities):
            return M.run_preflight(
                control=self.control, root=self.root, source_root=self.source,
                output_dir=self.output, stage=2, log_path=self.log, timeout_seconds=45,
            )

    def test_passes_with_regular_nonempty_gn_files_without_running_siso(self):
        self.assertEqual(self.call_preflight(), 0)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("mode=filesystem-only", log)
        self.assertIn("gn_generation_in_preflight=no", log)
        self.assertIn("siso_dry_run_in_preflight=no", log)
        self.assertIn("manifest_regeneration_policy=handled_by_real_siso_build", log)

    def test_manifest_regeneration_is_not_a_preflight_failure_condition(self):
        # The observed Siso v1.6.3 run successfully regenerated the manifest on
        # every -n invocation; this preflight deliberately does not invoke -n.
        self.assertEqual(self.call_preflight(), 0)
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("mode=filesystem-only", text)
        self.assertIn("manifest_regeneration_policy=handled_by_real_siso_build", text)
        self.assertNotIn("Regenerating ninja files", text)

    def test_missing_build_ninja_fails_closed(self):
        (self.output / "build.ninja").unlink()
        with self.assertRaisesRegex(M.ManifestPreflightError, "build.ninja"):
            self.call_preflight()

    def test_empty_args_gn_fails_closed(self):
        (self.output / "args.gn").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(M.ManifestPreflightError, "args.gn"):
            self.call_preflight()

    def test_symlinked_build_ninja_fails_closed(self):
        path = self.output / "build.ninja"
        path.unlink()
        target = self.root / "elsewhere.ninja"
        target.write_text("build target: phony\n", encoding="utf-8")
        path.symlink_to(target)
        with self.assertRaisesRegex(M.ManifestPreflightError, "build.ninja"):
            self.call_preflight()

    def test_build_identity_change_fails_closed(self):
        with self.assertRaisesRegex(M.ManifestPreflightError, "identity changed"):
            self.call_preflight(("before", "after"))

    def test_source_root_symlink_or_missing_is_rejected(self):
        source = self.root / "missing"
        with patch.object(M, "identity_digest", return_value="stable"):
            with self.assertRaisesRegex(M.ManifestPreflightError, "source root"):
                M.run_preflight(self.control, self.root, source, self.output, 2, self.log)

    def test_timeout_argument_must_be_positive(self):
        with self.assertRaisesRegex(M.ManifestPreflightError, "must be positive"):
            M.run_preflight(
                self.control, self.root, self.source, self.output, 2, self.log,
                timeout_seconds=0,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
