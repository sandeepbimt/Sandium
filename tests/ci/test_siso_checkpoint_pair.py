"""Regression tests for Siso filesystem-state checkpoint pairing."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ci/siso/checkpoint.py"
spec = importlib.util.spec_from_file_location("sandium_checkpoint", MODULE_PATH)
assert spec and spec.loader
checkpoint = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = checkpoint
spec.loader.exec_module(checkpoint)


class SisoCheckpointPairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "out"
        self.output.mkdir()
        self.state = self.output / ".siso_fs_state"
        self.journal = self.output / ".siso_fs_state.journal"

    def tearDown(self):
        self.temp.cleanup()

    def test_both_state_files_absent_is_allowed_and_explicit(self):
        checkpoint.validate_siso_checkpoint_pair(self.output)

    def test_both_nonempty_state_files_are_allowed(self):
        self.state.write_bytes(b"state")
        self.journal.write_bytes(b"journal")
        checkpoint.validate_siso_checkpoint_pair(self.output)

    def test_state_without_journal_is_rejected(self):
        self.state.write_bytes(b"state")
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "must be present together"
        ):
            checkpoint.validate_siso_checkpoint_pair(self.output)

    def test_journal_without_state_is_rejected(self):
        self.journal.write_bytes(b"journal")
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "must be present together"
        ):
            checkpoint.validate_siso_checkpoint_pair(self.output)

    def test_empty_state_or_journal_is_rejected(self):
        self.state.write_bytes(b"")
        self.journal.write_bytes(b"journal")
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "nonempty regular file"
        ):
            checkpoint.validate_siso_checkpoint_pair(self.output)


if __name__ == "__main__":
    unittest.main()
