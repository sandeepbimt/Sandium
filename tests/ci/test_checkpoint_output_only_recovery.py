#!/usr/bin/env python3
"""Regression tests for the opt-in output-only checkpoint recovery gate."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "ci" / "siso" / "checkpoint_artifact_discovery.py"
SPEC = importlib.util.spec_from_file_location("checkpoint_artifact_discovery_under_test", SCRIPT)
DISCOVERY = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(DISCOVERY)


class OutputOnlyFingerprintCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "chromium_commit": "a" * 40,
            "chromium_version": "151.0.7922.38",
            "target": "android-arm64-debug-apk",
            "build_config": "Debug",
            "build_input_fingerprint": "c" * 64,
        }
        self.meta = {
            **self.expected,
            "source_commit": "b" * 40,
            "build_input_fingerprint": "d" * 64,
        }
        self.audit = {
            "historical_source_available": True,
            "historical_fingerprint": "d" * 64,
            "current_fingerprint": "c" * 64,
            "audit_error": None,
            "changed_tracked_input_paths": [{
                "path": "build/config/siso/brave_siso_config.star",
            }],
        }
        self.build_state = types.SimpleNamespace(
            _fingerprint_audit=lambda source, saved: self.audit,
        )

    def test_accepts_only_exact_siso_config_delta(self):
        with patch.dict(sys.modules, {"build_state": self.build_state}):
            self.assertTrue(
                DISCOVERY.output_only_fingerprint_compatible(self.meta, self.expected)
            )

    def test_rejects_additional_changed_build_input(self):
        self.audit["changed_tracked_input_paths"].append({"path": "build/android/BUILD.gn"})
        with patch.dict(sys.modules, {"build_state": self.build_state}):
            self.assertFalse(
                DISCOVERY.output_only_fingerprint_compatible(self.meta, self.expected)
            )

    def test_rejects_unavailable_historical_source(self):
        self.audit["historical_source_available"] = False
        with patch.dict(sys.modules, {"build_state": self.build_state}):
            self.assertFalse(
                DISCOVERY.output_only_fingerprint_compatible(self.meta, self.expected)
            )

    def test_rejects_mismatched_historical_fingerprint(self):
        self.audit["historical_fingerprint"] = "e" * 64
        with patch.dict(sys.modules, {"build_state": self.build_state}):
            self.assertFalse(
                DISCOVERY.output_only_fingerprint_compatible(self.meta, self.expected)
            )

    def test_rejects_other_identity_mismatch(self):
        altered = dict(self.meta)
        altered["chromium_commit"] = "f" * 40
        with patch.dict(sys.modules, {"build_state": self.build_state}):
            self.assertFalse(
                DISCOVERY.output_only_fingerprint_compatible(altered, self.expected)
            )


if __name__ == "__main__":
    unittest.main()
