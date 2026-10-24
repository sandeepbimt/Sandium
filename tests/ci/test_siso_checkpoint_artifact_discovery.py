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
    def test_resumable_candidate_accepts_state_without_clean_shutdown_journal(self):
        data = b"checkpoint"
        output_only = manifest(data)
        state_only = manifest(data, siso_state_sha256="e" * 64)
        state_and_journal = manifest(
            data,
            siso_state_sha256="e" * 64,
            siso_journal_sha256="f" * 64,
        )
        malformed_journal = manifest(
            data,
            siso_state_sha256="e" * 64,
            siso_journal_sha256="not-a-digest",
        )
        self.assertFalse(module.has_siso_state_pair(output_only))
        self.assertTrue(module.has_siso_state_pair(state_only))
        self.assertTrue(module.has_siso_state_pair(state_and_journal))
        self.assertFalse(module.has_siso_state_pair(malformed_journal))

    def test_require_siso_state_filters_before_archive_download(self):
        source = SCRIPT.read_text(encoding="utf-8")
        filter_at = source.index("resumable = [item for item in usable if has_siso_state_pair(item[0])]")
        download_at = source.index("CHECKPOINT_CANDIDATE_ARCHIVE_DOWNLOAD_START")
        self.assertLess(filter_at, download_at)
        self.assertIn("--require-siso-state", source)
        self.assertIn("CROSS_WORKFLOW_CHECKPOINT=NO_RESUMABLE_SISO_CANDIDATE", source)

    def test_require_siso_state_does_not_claim_none_when_historical_search_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "CIRCLE_TOKEN": "test-token",
                "CIRCLE_BRANCH": "build/sandium-resumable-v2",
                "CIRCLE_PROJECT_SLUG": "gh/sandeepbimt/Sandium",
                "CIRCLE_SHA1": "a" * 40,
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(module, "discover_remote_urls", side_effect=RuntimeError("artifact listing failed")):
                    self.assertEqual(module.run(root, require_siso_state=True), 78)
            status = (root / ".sandium-checkpoint-discovery-status").read_text(encoding="utf-8")
            self.assertEqual(status.strip(), "DISCOVERY_INCOMPLETE")

    def test_require_siso_state_reports_no_candidate_only_after_complete_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "CIRCLE_TOKEN": "test-token",
                "CIRCLE_BRANCH": "build/sandium-resumable-v2",
                "CIRCLE_PROJECT_SLUG": "gh/sandeepbimt/Sandium",
                "CIRCLE_SHA1": "a" * 40,
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(module, "discover_remote_urls", return_value=[]):
                    self.assertEqual(module.run(root, require_siso_state=True), 0)
            status = (root / ".sandium-checkpoint-discovery-status").read_text(encoding="utf-8")
            self.assertEqual(status.strip(), "NO_RESUMABLE_SISO_CANDIDATE")

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


    def test_artifact_paths_require_both_metadata_and_archive(self):
        self.assertTrue(module.META_RE.search("stage-2-finish-build/checkpoint-artifact/sandium-checkpoint.meta"))
        self.assertTrue(module.ARCHIVE_RE.search("stage-2-finish-build/checkpoint-artifact/sandium-checkpoint.tar.zst"))
        self.assertFalse(module.META_RE.search("stage-2-finish-build/sandium-checkpoint.meta"))

    def test_only_build_stage_jobs_are_considered(self):
        self.assertTrue(module.JOB_RE.match("stage-1-source-foundation"))
        self.assertTrue(module.JOB_RE.match("stage-5-verify-publish"))
        self.assertFalse(module.JOB_RE.match("smoke-1-contract-and-foundation"))

    def test_corrupt_archive_is_rejected_before_ranking(self):
        archive = b"test archive bytes"
        meta = manifest(archive)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate.tar.zst"
            path.write_bytes(archive + b"corrupt")
            self.assertFalse(module.archive_integrity_matches(meta, path))

    def test_malformed_progress_and_identity_are_rejected(self):
        meta = manifest(b"test archive bytes")
        malformed = {**meta, "progress": {**meta["progress"], "object_file_count": -1}}
        self.assertFalse(module.valid_manifest(malformed))
        missing = {key: value for key, value in meta.items() if key != "gn_args_sha256"}
        self.assertFalse(module.valid_manifest(missing))

    def test_larger_compiled_checkpoint_ranks_above_verified_apk_flag(self):
        low = manifest(b"low archive", stage="stage-2")
        low["progress"]["object_file_count"] = 9000
        high = manifest(b"high archive", stage="stage-3")
        high["progress"]["artifact_verified"] = True
        high["progress"]["object_file_count"] = 2
        self.assertGreater(module.rank(low), module.rank(high))

    def test_artifact_listing_failure_does_not_claim_no_checkpoint_exists(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("artifact_listing_errors", source)
        self.assertIn("if artifact_listing_errors:", source)
        def fake_pages(url, token, key):
            # Match the most specific endpoint first: job URLs also contain
            # "/workflow/", so the workflow branch must not shadow the job.
            if "/artifacts" in url:
                raise RuntimeError("artifact API unavailable")
            elif "/job" in url:
                yield {"name": "stage-2-compile-continuation", "job_number": 123}
            elif "/workflow" in url:
                yield {"id": "workflow-1"}
            elif "/pipeline?" in url:
                yield {"id": "pipeline-1"}

        with patch.object(module, "pages", side_effect=fake_pages):
            with self.assertRaisesRegex(RuntimeError, "artifact listing failed"):
                module.discover_remote_urls("gh/sandeepbimt/Sandium", "build/sandium-resumable-v2", "token")

    def test_restore_loop_attempts_next_candidate_after_incompatibility(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("for metadata, archive_path, metadata_path, job_number, archive_url in usable:", source)
        self.assertIn("CROSS_WORKFLOW_CHECKPOINT=INCOMPATIBLE", source)
        self.assertIn("trying next candidate", source)

    def test_remote_archives_are_downloaded_only_after_metadata_ranking(self):
        source = SCRIPT.read_text(encoding="utf-8")
        sort_at = source.index("usable.sort(key=lambda item: rank(item[0]), reverse=True)")
        download_at = source.index("CHECKPOINT_CANDIDATE_ARCHIVE_DOWNLOAD_START")
        self.assertLess(sort_at, download_at)
        self.assertIn("if archive_url is not None:", source)

    def test_missing_context_uses_slug_candidate_list_not_removed_variable(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"slug_available": bool(slug_candidates)', source)
        self.assertNotIn('"slug_available": bool(slug),', source)

    def test_failed_candidate_restores_preexisting_output_and_cache_pair(self):
        data = b"local checkpoint"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "sandium-checkpoint.tar.zst"
            meta_path = root / "sandium-checkpoint.meta"
            archive.write_bytes(data)
            meta_path.write_text(json.dumps(manifest(data)), encoding="utf-8")
            out = root / "src/out/android_Debug_arm64"
            out.mkdir(parents=True)
            sentinel = out / "keep.txt"
            sentinel.write_text("prior output", encoding="utf-8")
            env = {
                "CIRCLE_TOKEN": "test-token",
                "CIRCLE_BRANCH": "build/sandium-resumable-v2",
                "CIRCLE_PROJECT_SLUG": "gh/sandeepbimt/Sandium",
                "CIRCLE_SHA1": "e" * 40,
            }
            failed_restore = __import__("subprocess").CompletedProcess(
                args=["checkpoint.py", "restore"], returncode=1,
                stdout="SANDIUM_CHECKPOINT_RESTORE=REJECTED",
            )
            with patch.dict(os.environ, env, clear=False):
                with patch.object(module, "discover_remote_urls", return_value=[]):
                    with patch.object(module.subprocess, "run", return_value=failed_restore):
                        self.assertEqual(module.run(root), 78)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "prior output")
            self.assertEqual(archive.read_bytes(), data)
            self.assertEqual(json.loads(meta_path.read_text(encoding="utf-8")), manifest(data))


if __name__ == "__main__":
    unittest.main()
