#!/usr/bin/env python3
"""Offline tests for green-only compiled-output checkpoint selection."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_checkpoint_quality", HERE / "checkpoint_quality.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

IDENTITY = {
    "schema": 1, "bare_commit": "a" * 40, "chromium_commit": "b" * 40,
    "chromium_version": "156.0.8078.25", "target": "android-arm64-debug-apk",
    "toolchain_depot_tools_commit": "c" * 40, "gn_args_sha256": "d" * 64,
}
CONTROL_HASHES = {name: hashlib.sha256(name.encode()).hexdigest() for name in M.OC.CONTROL_INPUTS}


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def make_candidate(root: Path, *, stage=1, workflow="old-workflow", kind="completed-stage",
                   record_valid=True, min_payload=200) -> Path:
    checkpoint = root / M.OC.CHECKPOINT_NAME
    checkpoint.mkdir(parents=True)
    files = {
        "args.gn": b"use_siso=true\n",
        "build.ninja": b"build target\n",
        ".siso_fs_state": b"siso filesystem state\n",
        "obj-placeholder.bin": b"x" * max(1, min_payload),
    }
    for name, payload in files.items():
        (checkpoint / name).write_bytes(payload)
    objects = checkpoint / "obj" / "checkpoint_test"
    objects.mkdir(parents=True)
    for index in range(120):
        (objects / f"unit_{index:03d}.o").write_bytes(
            f"compiled-object-{index}\\n".encode() + b"x" * 12000
        )
    output_files = {name: digest_file(checkpoint / name) for name in files}
    metrics = root / "stage-1-metrics.jsonl"
    object_paths = sorted(p.relative_to(checkpoint).as_posix() for p in (checkpoint / "obj").rglob("*.o"))
    metrics.write_text("".join(json.dumps({
        "step_id": f"fixture-{index}", "outputs": [relative], "is_local": True
    }) + "\n" for index, relative in enumerate(object_paths)), encoding="utf-8")
    output_bytes = sum(
        path.stat().st_size for path in checkpoint.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    meta = {
        "schema": M.OC.SCHEMA,
        "checkpoint_name": M.OC.CHECKPOINT_NAME,
        "output_relative_path": M.OC.OUT_RELATIVE.as_posix(),
        "checkpoint_kind": kind,
        "stage_record_valid": record_valid,
        "stage": stage,
        "workflow_id": workflow,
        "build_identity": IDENTITY,
        "build_identity_sha256": M.OC.identity_digest(IDENTITY),
        "control_inputs": CONTROL_HASHES,
        "output_files": output_files,
        "output_bytes": output_bytes,
        "compiled_output_inventory": M.OC.compiled_output_inventory(checkpoint),
        "compiled_output_action_sample_schema": M.OC.ACTION_SAMPLE_SCHEMA,
        "compiled_output_action_samples": M.OC.compiled_output_action_samples(metrics, checkpoint),
        "compiled_output_action_samples_source_metrics_sha256": M.OC.sha256_file(metrics),
    }
    (checkpoint / M.OC.META_NAME).write_text(json.dumps(meta), encoding="utf-8")
    return checkpoint


class CheckpointQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = self.root / "control"
        self.control.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def validate(self, stage=2, workflow="current-workflow", min_bytes=100):
        with patch.object(M.OC, "control_inputs", return_value=CONTROL_HASHES), \
             patch.object(M, "source_identity", return_value=IDENTITY):
            return M.validate_candidate(
                root=self.root, control=self.control, stage=stage,
                workflow_id=workflow, min_output_bytes=min_bytes,
            )

    def test_accepts_substantial_completed_predecessor_with_matching_identity(self):
        make_candidate(self.root, stage=1, workflow="current-workflow", min_payload=200)
        result = self.validate()
        self.assertEqual(result["source_stage"], 1)
        self.assertTrue(result["stage_record_valid"])
        self.assertGreaterEqual(result["output_bytes"], 100)

    def test_produced_checkpoint_validates_after_pack_moves_src_out(self):
        checkpoint = make_candidate(self.root, stage=1, workflow="current-workflow", min_payload=200)
        with patch.object(M.OC, "control_inputs", return_value=CONTROL_HASHES), \
             patch.object(M, "source_identity", return_value=IDENTITY) as identity:
            result = M.validate_candidate(
                root=self.root, control=self.control, stage=1,
                workflow_id="current-workflow", min_output_bytes=100, produced=True,
            )
        self.assertEqual(result["source_stage"], 1)
        identity.assert_called_once_with(
            self.control, self.root, require_gn=True, output_dir_override=checkpoint
        )

    def test_migrates_legacy_aggregate_samples_at_quality_gate(self):
        checkpoint = make_candidate(
            self.root, stage=1, workflow="old-workflow", min_payload=200
        )
        meta_path = checkpoint / M.OC.META_NAME
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("compiled_output_action_sample_schema")
        # Reproduce an old checkpoint's sampled aggregate output. No file is
        # needed for this record because legacy aggregate samples are discarded.
        original_count = len(meta["compiled_output_action_samples"])
        meta["compiled_output_action_samples"][0]["path"] = "obj/checkpoint_test/legacy.a"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        result = self.validate(stage=1, workflow="new-workflow")
        self.assertEqual(result["compiled_output_action_sample_count"], original_count - 1)

    def test_current_sample_contract_rejects_aggregate_output(self):
        checkpoint = make_candidate(
            self.root, stage=1, workflow="current-workflow", min_payload=200
        )
        meta_path = checkpoint / M.OC.META_NAME
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["compiled_output_action_samples"][0]["path"] = "obj/checkpoint_test/legacy.a"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with self.assertRaisesRegex(M.CheckpointQualityError, "non-object output"):
            self.validate(stage=1, workflow="current-workflow")

    def test_rejects_metadata_only_16_byte_checkpoint(self):
        make_candidate(self.root, stage=1, workflow="current-workflow", min_payload=1)
        with self.assertRaisesRegex(M.CheckpointQualityError, "too_small"):
            self.validate(min_bytes=5_000_000)

    def test_rejects_failed_or_partial_stage_checkpoint(self):
        make_candidate(self.root, stage=1, workflow="current-workflow",
                       kind="partial-stage", record_valid=False, min_payload=200)
        with self.assertRaisesRegex(M.CheckpointQualityError, "not_a_green_completed_stage"):
            self.validate()

    def test_rejects_wrong_predecessor_stage(self):
        make_candidate(self.root, stage=1, workflow="current-workflow", min_payload=200)
        with self.assertRaisesRegex(M.CheckpointQualityError, "wrong_predecessor_stage"):
            self.validate(stage=3)

    def test_rejects_checkpoint_from_other_workflow_for_continuation(self):
        make_candidate(self.root, stage=1, workflow="old-workflow", min_payload=200)
        with self.assertRaisesRegex(M.CheckpointQualityError, "immediate_predecessor"):
            self.validate(stage=2, workflow="current-workflow")

    def test_allows_prior_workflow_candidate_for_stage_one_selection(self):
        make_candidate(self.root, stage=1, workflow="old-workflow", min_payload=200)
        result = self.validate(stage=1, workflow="new-workflow")
        self.assertEqual(result["source_stage"], 1)

    def test_rejects_build_identity_mismatch(self):
        make_candidate(self.root, stage=1, workflow="current-workflow", min_payload=200)
        other_identity = dict(IDENTITY, chromium_commit="e" * 40)
        with patch.object(M.OC, "control_inputs", return_value=CONTROL_HASHES), \
             patch.object(M, "source_identity", return_value=other_identity):
            with self.assertRaisesRegex(M.CheckpointQualityError, "identity_mismatch"):
                M.validate_candidate(
                    root=self.root, control=self.control, stage=2,
                    workflow_id="current-workflow", min_output_bytes=100,
                )

    def test_rejects_declared_actual_payload_size_mismatch(self):
        checkpoint = make_candidate(self.root, stage=1, workflow="current-workflow", min_payload=200)
        meta_path = checkpoint / M.OC.META_NAME
        meta = json.loads(meta_path.read_text())
        meta["output_bytes"] += 1
        meta_path.write_text(json.dumps(meta))
        with self.assertRaisesRegex(M.CheckpointQualityError, "size_mismatch"):
            self.validate()

    def test_quarantines_rejected_local_candidate_without_deleting_it(self):
        checkpoint = make_candidate(self.root, stage=1, workflow="current-workflow",
                                   kind="partial-stage", record_valid=False, min_payload=200)
        rejected = M.quarantine_candidate(self.root)
        self.assertIsNotNone(rejected)
        self.assertFalse(checkpoint.exists())
        self.assertTrue((rejected / M.OC.META_NAME).is_file())

    def test_legacy_checkpoint_is_stage_one_only_and_explicitly_unverified(self):
        checkpoint = make_candidate(
            self.root, stage=1, workflow="old-workflow", min_payload=200
        )
        meta_path = checkpoint / M.OC.META_NAME
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("compiled_output_inventory", None)
        meta.pop("compiled_output_action_samples", None)
        meta.pop("compiled_output_action_samples_source_metrics_sha256", None)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        with patch.object(M.OC, "control_inputs", return_value=CONTROL_HASHES), patch.object(M, "source_identity", return_value=IDENTITY):
            result = M.validate_candidate(
                root=self.root, control=self.control, stage=1,
                workflow_id="new-workflow", min_output_bytes=100,
            )
            self.assertEqual(result["compiled_output_inventory_status"], "LEGACY_UNVERIFIED")
            with self.assertRaisesRegex(M.CheckpointQualityError, "compiled_output_inventory_missing"):
                M.validate_candidate(
                    root=self.root, control=self.control, stage=2,
                    workflow_id="old-workflow", min_output_bytes=100,
                )

    def test_compiled_object_sample_hash_mismatch_is_rejected(self):
        checkpoint = make_candidate(
            self.root, stage=1, workflow="current-workflow", min_payload=200
        )
        meta = json.loads((checkpoint / M.OC.META_NAME).read_text(encoding="utf-8"))
        relative = next(iter(meta["compiled_output_inventory"]["sample_sha256"]))
        sample_path = checkpoint / relative
        original = sample_path.read_bytes()
        sample_path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with self.assertRaisesRegex(M.CheckpointQualityError, "compiled_output_inventory_invalid"):
            self.validate()

    def test_missing_candidate_is_not_treated_as_valid(self):
        with self.assertRaisesRegex(M.CheckpointQualityError, "missing_or_unsafe"):
            self.validate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
