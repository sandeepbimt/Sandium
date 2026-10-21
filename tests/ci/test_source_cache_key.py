"""Tests for the stable Brave sync-input fingerprint used by source-cache validation."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ci/siso/source_cache_key.py"
SPEC = importlib.util.spec_from_file_location("source_cache_key", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class SourceCacheKeyTests(unittest.TestCase):
    def _make_core(self, root: Path) -> Path:
        core = root / "src" / "brave"
        for relative in MODULE.INPUTS:
            path = core / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture:" + relative + "\n", encoding="utf-8")
        return core

    def test_fingerprint_is_stable_and_ignores_ci_commit_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_core(root)
            first, first_files = MODULE.stable_source_fingerprint(root / "src" / "brave")
            second, second_files = MODULE.stable_source_fingerprint(root / "src" / "brave")
            self.assertEqual(first, second)
            self.assertEqual(first_files, second_files)
            self.assertEqual(set(first_files), set(MODULE.INPUTS))

    def test_sync_input_change_invalidates_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self._make_core(root)
            before, _ = MODULE.stable_source_fingerprint(core)
            target = core / MODULE.INPUTS[0]
            target.write_text(target.read_text() + "# changed pin\n", encoding="utf-8")
            after, _ = MODULE.stable_source_fingerprint(core)
            self.assertNotEqual(before, after)

    def test_missing_sync_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self._make_core(root)
            (core / MODULE.INPUTS[-1]).unlink()
            with self.assertRaises(FileNotFoundError):
                MODULE.stable_source_fingerprint(core)


if __name__ == "__main__":
    unittest.main()
