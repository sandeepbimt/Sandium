"""Offline regression tests for cross-workflow checkpoint discovery safeguards."""
from __future__ import annotations
import hashlib, importlib.util, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/"ci/siso/checkpoint_artifact_discovery.py"
spec=importlib.util.spec_from_file_location("checkpoint_artifact_discovery",SCRIPT)
assert spec and spec.loader
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def candidate(stage="stage-2", objects=200, verified=False):
    archive=b"test archive bytes"
    return ({"schema":1,"stage":stage,"created_at_utc":"2026-10-22T00:00:00Z","source_commit":"a"*40,"chromium_commit":"b"*40,"chromium_version":"151.0.7922.38","target":"android-arm64-debug-apk","build_config":"Debug","build_input_fingerprint":"c"*64,"gn_args_sha256":"d"*64,"archive_bytes":len(archive),"archive_sha256":hashlib.sha256(archive).hexdigest(),"progress":{"compile_complete":False,"artifact_verified":verified,"object_file_count":objects,"linked_artifact_count":3,"output_bytes":objects*100}},archive)
class ArtifactDiscoveryTests(unittest.TestCase):
    def test_artifact_paths_require_both_metadata_and_archive(self):
        self.assertTrue(module.META_RE.search("stage-2-finish-build/checkpoint-artifact/sandium-checkpoint.meta"))
        self.assertTrue(module.ARCHIVE_RE.search("stage-2-finish-build/checkpoint-artifact/sandium-checkpoint.tar.zst"))
        self.assertFalse(module.META_RE.search("stage-2-finish-build/sandium-checkpoint.meta"))
    def test_only_build_stage_jobs_are_considered(self):
        self.assertTrue(module.JOB_RE.match("stage-1-source-foundation")); self.assertTrue(module.JOB_RE.match("stage-5-verify-publish"))
        self.assertFalse(module.JOB_RE.match("smoke-1-contract-and-foundation"))
    def test_corrupt_archive_is_rejected_before_ranking(self):
        meta,archive=candidate()
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"candidate.tar.zst"; path.write_bytes(archive+b"corrupt")
            self.assertFalse(module.archive_integrity_matches(meta,path))
    def test_malformed_progress_and_identity_are_rejected(self):
        meta,_=candidate(); malformed={**meta,"progress":{**meta["progress"],"object_file_count":-1}}
        self.assertFalse(module.valid_manifest(malformed)); missing={k:v for k,v in meta.items() if k!="gn_args_sha256"}
        self.assertFalse(module.valid_manifest(missing))
    def test_verified_checkpoint_ranks_above_larger_unverified_candidate(self):
        low,_=candidate(objects=9000); high,_=candidate(stage="stage-3",objects=2,verified=True)
        self.assertGreater(module.rank(high),module.rank(low))
    def test_restore_loop_attempts_next_candidate_after_incompatibility(self):
        source=SCRIPT.read_text(encoding="utf-8")
        self.assertIn("for meta,archive,jobno in usable:",source); self.assertIn("CROSS_WORKFLOW_CHECKPOINT=INCOMPATIBLE",source)
        self.assertIn("trying next candidate",source)
if __name__=="__main__": unittest.main()
