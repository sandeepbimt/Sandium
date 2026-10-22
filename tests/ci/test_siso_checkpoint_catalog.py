"""Unit tests for progress-aware checkpoint manifest selection."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ci/siso/checkpoint_catalog.py"
spec = importlib.util.spec_from_file_location("checkpoint_catalog", SCRIPT)
assert spec and spec.loader
catalog_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog_module)


def manifest(stage="stage-1", objects=10, complete=False, verified=False, **overrides):
    item = {
        "schema": 1, "stage": stage, "created_at_utc": "2026-10-22T00:00:00+00:00",
        "source_commit": "a" * 40, "chromium_commit": "b" * 40,
        "chromium_version": "151.0.7922.38", "target": "android-arm64-debug-apk",
        "build_config": "Debug", "build_input_fingerprint": "c" * 64,
        "gn_args_sha256": "d" * 64,
        "progress": {"compile_complete": complete, "artifact_verified": verified,
                     "object_file_count": objects, "linked_artifact_count": 2,
                     "output_bytes": objects * 100},
    }
    item.update(overrides)
    return item


EXPECTED = manifest()["progress"] | {
    "source_commit": "a" * 40, "chromium_commit": "b" * 40,
    "chromium_version": "151.0.7922.38", "target": "android-arm64-debug-apk",
    "build_config": "Debug", "build_input_fingerprint": "c" * 64,
    "gn_args_sha256": "d" * 64,
}


class CheckpointCatalogTests(unittest.TestCase):
    def test_real_progress_beats_newer_stage_label(self):
        low = manifest(stage="stage-5", objects=100)
        high = manifest(stage="stage-2", objects=900)
        self.assertIs(catalog_module.choose([low, high], EXPECTED), high)

    def test_verified_apk_beats_larger_incomplete_checkpoint(self):
        incomplete = manifest(objects=9000)
        complete = manifest(stage="stage-3", objects=2, complete=True, verified=True)
        self.assertIs(catalog_module.choose([incomplete, complete], EXPECTED), complete)

    def test_incompatible_or_malformed_candidates_are_ignored(self):
        wrong_source = manifest(objects=9999, source_commit="e" * 40)
        malformed = manifest()
        del malformed["progress"]["object_file_count"]
        valid = manifest(objects=50)
        self.assertIs(catalog_module.choose([wrong_source, malformed, valid], EXPECTED), valid)

    def test_no_compatible_candidate_returns_none(self):
        self.assertIsNone(catalog_module.choose([manifest(source_commit="e" * 40)], EXPECTED))

    
    def test_boolean_progress_count_is_rejected(self):
        malformed = manifest()
        malformed["progress"]["object_file_count"] = True
        self.assertFalse(catalog_module.compatible(malformed, EXPECTED))


if __name__ == "__main__":
    unittest.main()