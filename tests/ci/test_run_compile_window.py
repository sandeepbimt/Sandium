"""Regression tests for Siso compile-window log initialization."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ci/siso/run_compile_window.py"
spec = importlib.util.spec_from_file_location("sandium_run_compile_window", MODULE_PATH)
assert spec and spec.loader
compile_window = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compile_window)


class ResetSisoInfoTests(unittest.TestCase):
    def test_replaces_symlink_without_truncating_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            target = root / "target.log"
            target.write_text("preserve this target", encoding="utf-8")
            info = root / "siso.INFO"
            info.symlink_to(target)

            compile_window.reset_siso_info(info, diagnostics, "stage-1")

            self.assertTrue(info.is_file())
            self.assertFalse(info.is_symlink())
            self.assertEqual(info.read_text(encoding="utf-8"), "")
            self.assertEqual(target.read_text(encoding="utf-8"), "preserve this target")

    def test_replaces_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            info = root / "siso.INFO"
            info.symlink_to(root / "missing-target")

            compile_window.reset_siso_info(info, diagnostics, "stage-1")

            self.assertTrue(info.is_file())
            self.assertFalse(info.is_symlink())
            self.assertEqual(info.read_text(encoding="utf-8"), "")

    def test_preserves_previous_regular_log_before_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            info = root / "siso.INFO"
            info.write_text("previous diagnostics", encoding="utf-8")

            compile_window.reset_siso_info(info, diagnostics, "stage-2")

            self.assertEqual(info.read_text(encoding="utf-8"), "")
            self.assertEqual(
                (diagnostics / "siso.INFO.before-stage-2").read_text(encoding="utf-8"),
                "previous diagnostics",
            )

    def test_rejects_directory_at_siso_info_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            info = root / "siso.INFO"
            info.mkdir()

            with self.assertRaisesRegex(ValueError, "expected a regular Siso info log"):
                compile_window.reset_siso_info(info, diagnostics, "stage-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
