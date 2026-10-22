"""Offline contract tests for cross-workflow checkpoint artifact publication/discovery."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/"ci/siso/checkpoint_artifact_discovery.py"
spec=importlib.util.spec_from_file_location("checkpoint_artifact_discovery",SCRIPT)
assert spec and spec.loader
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class CheckpointArtifactDiscoveryTests(unittest.TestCase):
    def test_artifact_paths_require_checkpoint_meta_and_archive(self):
        self.assertTrue(module.META_RE.search("stage-2-finish-build/checkpoint-artifact/sandium-checkpoint.meta"))
        self.assertTrue(module.ARCHIVE_RE.search("stage-2-finish-build/checkpoint-artifact/sandium-checkpoint.tar.zst"))
        self.assertFalse(module.META_RE.search("stage-2-finish-build/sandium-checkpoint.meta"))

    def test_job_filter_is_limited_to_build_stages(self):
        self.assertTrue(module.JOB_RE.match("stage-1-source-foundation"))
        self.assertTrue(module.JOB_RE.match("stage-5-verify-publish"))
        self.assertFalse(module.JOB_RE.match("smoke-1-contract-and-foundation"))

if __name__=="__main__":
    unittest.main()
