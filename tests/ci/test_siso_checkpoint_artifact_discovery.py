"""Offline regression tests for cross-workflow checkpoint discovery."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "ci/siso/checkpoint_artifact_discovery.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("checkpoint_artifact_discovery", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def manifest(archive: bytes, **overrides):
    value = {
        "schema": 1,
        "stage": "stage-2",
        "output_rel_path": "out/android_Debug_arm64",
        "source_commit": "a" * 40,
        "chromium_commit": "b" * 40,
        "chromium_version": "151.0.7922.38",
        "target": "android-arm64-debug-apk",
        "build_config": "Debug",
        "build_input_fingerprint": "c" * 64,
        "gn_args_sha256": "d" * 64,
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "archive_bytes": len(archive),
        "progress": {
            "compile_complete": False,
            "artifact_verified": False,
            "object_file_count": 10,
            "linked_artifact_count": 2,
            "output_bytes": 1000,
        },
    }
    value.update(overrides)
    return value


class CheckpointArtifactDiscoveryTests(unittest.TestCase):
    def test_boolean_archive_size_is_rejected(self):
        data = b"checkpoint"
        self.assertFalse(module.valid_manifest(manifest(data, archive_bytes=True)))

    def test_boolean_progress_count_is_rejected(self):
        data = b"checkpoint"
        value = manifest(data)
        value["progress"]["object_file_count"] = True
        self.assertFalse(module.valid_manifest(value))

    def test_archive_hash_is_streamed_without_path_read_bytes(self):
        data = b"checkpoint payload" * 100
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "checkpoint.tar.zst"
            archive.write_bytes(data)
            meta = manifest(data)
            with patch.object(Path, "read_bytes", side_effect=AssertionError("must stream")):
                self.assertTrue(module.archive_integrity_matches(meta, archive))

    def test_no_token_with_valid_local_checkpoint_keeps_pipeline_fallback(self):
        data = b"checkpoint"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sandium-checkpoint.tar.zst").write_bytes(data)
            (root / "sandium-checkpoint.meta").write_text(json.dumps(manifest(data)), encoding="utf-8")
            with patch.dict(os.environ, {"CIRCLE_TOKEN": "", "CIRCLECI_API_TOKEN": "", "CIRCLECI_TOKEN": ""}, clear=False):
                with patch.object(module, "discover_remote_urls", side_effect=AssertionError("must not call API")):
                    self.assertEqual(module.run(root), 0)

    def test_no_token_without_valid_checkpoint_fails_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CIRCLE_TOKEN": "", "CIRCLECI_API_TOKEN": "", "CIRCLECI_TOKEN": ""}, clear=False):
                self.assertEqual(module.run(Path(tmp)), 78)


if __name__ == "__main__":
    unittest.main()
