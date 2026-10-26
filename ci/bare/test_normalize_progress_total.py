#!/usr/bin/env python3
"""Offline tests for safe Siso progress-denominator normalization."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_normalize_progress_total", HERE / "normalize_progress_total.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class ProgressTotalNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = self.root / "control"
        self.control.mkdir()
        self.previous = self.root / "previous.json"
        self.previous.write_text(json.dumps({
            "stage": 1,
            "build_identity_sha256": "same-identity",
            "total_action_count": 109081,
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_stage_one_keeps_observed_total(self):
        self.assertEqual(M.normalize_total(
            stage=1, observed_total=109081, previous_ledger=None,
            control=self.control, root=self.root,
        ), 109081)

    def test_equal_total_is_unchanged(self):
        with patch.object(M, "identity_digest", return_value="same-identity"):
            self.assertEqual(M.normalize_total(
                stage=2, observed_total=109081, previous_ledger=self.previous,
                control=self.control, root=self.root,
            ), 109081)

    def test_six_action_variance_is_normalized_with_exact_identity(self):
        with patch.object(M, "identity_digest", return_value="same-identity"):
            self.assertEqual(M.normalize_total(
                stage=2, observed_total=109087, previous_ledger=self.previous,
                control=self.control, root=self.root,
            ), 109081)

    def test_material_graph_drift_is_rejected(self):
        with patch.object(M, "identity_digest", return_value="same-identity"):
            with self.assertRaisesRegex(M.IdentityError, "exceeds 1%"):
                M.normalize_total(
                    stage=2, observed_total=111000, previous_ledger=self.previous,
                    control=self.control, root=self.root,
                )

    def test_identity_mismatch_is_rejected_even_for_small_variance(self):
        with patch.object(M, "identity_digest", return_value="different-identity"):
            with self.assertRaisesRegex(M.IdentityError, "build identity differs"):
                M.normalize_total(
                    stage=2, observed_total=109087, previous_ledger=self.previous,
                    control=self.control, root=self.root,
                )

    def test_wrong_predecessor_stage_is_rejected(self):
        self.previous.write_text(json.dumps({
            "stage": 3, "build_identity_sha256": "same-identity",
            "total_action_count": 109081,
        }), encoding="utf-8")
        with patch.object(M, "identity_digest", return_value="same-identity"):
            with self.assertRaisesRegex(M.IdentityError, "Stage 1"):
                M.normalize_total(
                    stage=2, observed_total=109087, previous_ledger=self.previous,
                    control=self.control, root=self.root,
                )

    def test_nonpositive_observed_total_is_rejected(self):
        with self.assertRaisesRegex(M.IdentityError, "must be positive"):
            M.normalize_total(
                stage=1, observed_total=0, previous_ledger=None,
                control=self.control, root=self.root,
            )

    def test_stage_one_rejects_previous_ledger_argument(self):
        with self.assertRaisesRegex(M.IdentityError, "must not normalize"):
            M.normalize_total(
                stage=1, observed_total=10, previous_ledger=self.previous,
                control=self.control, root=self.root,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
