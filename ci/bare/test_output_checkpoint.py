#!/usr/bin/env python3
"""Offline tests for full-output checkpoint cache safety."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_output_checkpoint", HERE / "output_checkpoint.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

IDENTITY = {
    "schema": 1, "bare_commit": "a" * 40, "chromium_commit": "b" * 40,
    "chromium_version": "156.0.8078.25", "target": "android-arm64-debug-apk",
    "toolchain_depot_tools_commit": "c" * 40, "gn_args_sha256": "d" * 64,
}
CONTROL_HASHES = {name: hashlib.sha256(name.encode()).hexdigest() for name in M.CONTROL_INPUTS}

def make_output(path: Path) -> dict[str, str]:
    path.mkdir(parents=True)
    files = {"args.gn": "use_siso=true\n", "build.ninja": "build edge\n", ".siso_fs_state": "siso state\n"}
    for name, contents in files.items():
        (path / name).write_text(contents, encoding="utf-8")
    return {name: M.sha256_file(path / name) for name in files}

class OutputCheckpointTests(unittest.TestCase):
    def test_identity_digest_is_order_independent(self):
        reversed_identity = dict(reversed(list(IDENTITY.items())))
        self.assertEqual(M.identity_digest(IDENTITY), M.identity_digest(reversed_identity))

    def test_valid_previous_stage_checkpoint_passes_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            checkpoint = root / M.CHECKPOINT_NAME
            files = make_output(checkpoint)
            (checkpoint / M.META_NAME).write_text(json.dumps({
                "schema": M.SCHEMA, "checkpoint_name": M.CHECKPOINT_NAME,
                "output_relative_path": M.OUT_RELATIVE.as_posix(),
                "build_identity": IDENTITY, "build_identity_sha256": M.identity_digest(IDENTITY),
                "control_inputs": CONTROL_HASHES, "output_files": files, "output_bytes": 30,
                "stage": 1, "workflow_id": "wf-1", "stage_record_valid": True,
            }), encoding="utf-8")
            with patch.object(M, "control_inputs", return_value=CONTROL_HASHES):
                meta = M.validate_candidate(root, control, 2, "wf-1", pre_source=True, required=True)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["stage"], 1)

    def test_wrong_workflow_stage_checkpoint_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            checkpoint = root / M.CHECKPOINT_NAME
            files = make_output(checkpoint)
            (checkpoint / M.META_NAME).write_text(json.dumps({
                "schema": M.SCHEMA, "checkpoint_name": M.CHECKPOINT_NAME,
                "output_relative_path": M.OUT_RELATIVE.as_posix(),
                "build_identity": IDENTITY, "build_identity_sha256": M.identity_digest(IDENTITY),
                "control_inputs": CONTROL_HASHES, "output_files": files,
                "stage": 1, "workflow_id": "old-wf", "stage_record_valid": True,
            }), encoding="utf-8")
            with patch.object(M, "control_inputs", return_value=CONTROL_HASHES):
                with self.assertRaises(M.CheckpointError):
                    M.validate_candidate(root, control, 2, "new-wf", pre_source=True, required=True)

    def test_output_file_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            checkpoint = root / M.CHECKPOINT_NAME
            files = make_output(checkpoint)
            files["build.ninja"] = "0" * 64
            (checkpoint / M.META_NAME).write_text(json.dumps({
                "schema": M.SCHEMA, "checkpoint_name": M.CHECKPOINT_NAME,
                "output_relative_path": M.OUT_RELATIVE.as_posix(),
                "build_identity": IDENTITY, "build_identity_sha256": M.identity_digest(IDENTITY),
                "control_inputs": CONTROL_HASHES, "output_files": files,
                "stage": 1, "workflow_id": "wf-1", "stage_record_valid": True,
            }), encoding="utf-8")
            with patch.object(M, "control_inputs", return_value=CONTROL_HASHES):
                result = M.validate_candidate(root, control, 1, "new-wf", pre_source=True, required=False)
            self.assertIsNone(result)
            self.assertFalse(checkpoint.exists())
            self.assertTrue((root / "sandium-rejected-output-checkpoints").is_dir())

    def test_output_requires_a_siso_state_file(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            output.mkdir()
            (output / "args.gn").write_text("args\n")
            (output / "build.ninja").write_text("build\n")
            with self.assertRaises(M.CheckpointError):
                M.output_manifest(output)

    def test_pack_and_restore_full_output_checkpoint_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            output = root / M.OUT_RELATIVE
            files = make_output(output)
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps({
                "stage": 1,
                "build_identity_sha256": M.identity_digest(IDENTITY),
                "max_completed_actions": 25,
                "total_action_count": 100,
                "remote_execution_proven": True,
                "remote_action_cache_reuse_proven": True,
                "artifact_verified": False,
            }), encoding="utf-8")
            with patch.object(M, "control_inputs", return_value=CONTROL_HASHES), \
                 patch.object(M, "source_identity", return_value=IDENTITY):
                packed = M.command_pack(root, control, 1, "wf-1", ledger, partial=False)
                self.assertEqual(packed, 0)
                checkpoint = root / M.CHECKPOINT_NAME
                self.assertTrue(checkpoint.is_dir())
                self.assertTrue((checkpoint / M.META_NAME).is_file())
                self.assertFalse(output.exists())

                # A later stage runs GN first and creates a fresh output shell.
                output.mkdir(parents=True)
                (output / "args.gn").write_text("fresh args\\n", encoding="utf-8")
                (output / "build.ninja").write_text("fresh graph\\n", encoding="utf-8")
                restored = M.command_restore(root, control, 2, "wf-1", required=True)
                self.assertEqual(restored, 0)
                self.assertTrue((output / ".siso_fs_state").is_file())
                self.assertEqual((output / "build.ninja").read_text(encoding="utf-8"), "build edge\\n")
                self.assertFalse(checkpoint.exists())
                self.assertFalse((output / M.META_NAME).exists())
                report = json.loads((root / M.REPORT_NAME).read_text(encoding="utf-8"))
                self.assertTrue(report["local_compiled_outputs_restored"])
                self.assertEqual(report["source_stage"], 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
