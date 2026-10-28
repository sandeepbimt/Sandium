#!/usr/bin/env python3
"""Offline tests for full-output checkpoint cache safety."""
import hashlib
import importlib.util
import json
import os
import tempfile
import time
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

def make_output(path: Path, state_name: str = ".siso_fs_state") -> dict[str, str]:
    path.mkdir(parents=True)
    files = {"args.gn": "use_siso=true\n", "build.ninja": "build edge\n", state_name: "siso resumable state\n"}
    for name, contents in files.items():
        (path / name).write_text(contents, encoding="utf-8")
    # The generator stamp belongs to the same snapshot as build.ninja, Siso state,
    # and compiled objects. Warm restore must preserve it intact.
    stamp = path / "build.ninja.stamp"
    stamp.write_text("checkpoint generator stamp\n", encoding="utf-8")
    old_ns = 946684800 * 1_000_000_000
    for name in ("args.gn", "build.ninja", "build.ninja.stamp"):
        os.utime(path / name, ns=(old_ns, old_ns))
    objects = path / "obj" / "checkpoint_test"
    objects.mkdir(parents=True)
    for index in range(120):
        (objects / f"unit_{index:03d}.o").write_bytes((f"compiled-object-{index}\\n".encode() + b"x" * 12000))
    return {name: M.sha256_file(path / name) for name in files}


def add_checkpoint_evidence(root: Path, output: Path) -> dict:
    metrics = root / "stage-1-metrics.jsonl"
    paths = sorted(p.relative_to(output).as_posix() for p in (output / "obj").rglob("*.o"))
    metrics.write_text("".join(json.dumps({
        "step_id": f"fixture-{index}", "outputs": [relative], "is_local": True
    }) + "\n" for index, relative in enumerate(paths)), encoding="utf-8")
    return {
        "compiled_output_inventory": M.compiled_output_inventory(output),
        "compiled_output_action_sample_schema": M.ACTION_SAMPLE_SCHEMA,
        "compiled_output_action_samples": M.compiled_output_action_samples(metrics, output),
        "compiled_output_action_samples_source_metrics_sha256": M.sha256_file(metrics),
    }

class OutputCheckpointTests(unittest.TestCase):
    def test_action_samples_only_include_predecessor_local_compilation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "out"
            output.mkdir()
            obj = output / "obj"
            obj.mkdir()
            for name in ("local.o", "cached.o", "noexec.o"):
                (obj / name).write_bytes(name.encode() + b" compiled")
            metrics = root / "metrics.jsonl"
            rows = [
                {"step_id": "local", "outputs": ["obj/local.o"], "is_local": True},
                {"step_id": "cached", "outputs": ["obj/cached.o"], "cached": True},
                {"step_id": "noexec", "outputs": ["obj/noexec.o"], "no_exec": True},
            ]
            metrics.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            samples = M.compiled_output_action_samples(metrics, output)
            self.assertEqual([item["path"] for item in samples], ["obj/local.o"])

    def test_legacy_action_samples_keep_only_leaf_objects(self):
        legacy = [
            {"path": "obj/a/first.o", "sha256": "a" * 64, "bytes": 10},
            {"path": "obj/a/libfirst.a", "sha256": "b" * 64, "bytes": 20},
            {"path": "obj/a/libsecond.so", "sha256": "c" * 64, "bytes": 30},
            {"path": "obj/b/second.o", "sha256": "d" * 64, "bytes": 40},
        ]
        migrated = M.normalize_compiled_output_action_samples(legacy)
        self.assertEqual([item["path"] for item in migrated],
                         ["obj/a/first.o", "obj/b/second.o"])

    def test_current_action_sample_schema_rejects_aggregate_outputs(self):
        aggregate = [{"path": "obj/a/libfirst.a", "sha256": "b" * 64, "bytes": 20}]
        with self.assertRaisesRegex(M.CheckpointError, "non-object output"):
            M.normalize_compiled_output_action_samples(aggregate, M.ACTION_SAMPLE_SCHEMA)

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
                "control_inputs": CONTROL_HASHES, "output_files": files, "output_mtime_ns": M.output_mtime_manifest(checkpoint), "output_bytes": 30,
                **add_checkpoint_evidence(root, checkpoint),
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
                "control_inputs": CONTROL_HASHES, "output_files": files, "output_mtime_ns": M.output_mtime_manifest(checkpoint),
                **add_checkpoint_evidence(root, checkpoint),
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
                "control_inputs": CONTROL_HASHES, "output_files": files, "output_mtime_ns": M.output_mtime_manifest(checkpoint),
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

    def test_output_accepts_nonempty_journal_when_base_state_is_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            make_output(output, state_name=".siso_fs_state.journal")
            manifest = M.output_manifest(output)
            self.assertIn(".siso_fs_state.journal", manifest)
            self.assertNotIn(".siso_fs_state", manifest)

    def test_pack_and_restore_full_output_checkpoint_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            output = root / M.OUT_RELATIVE
            files = make_output(output)
            add_checkpoint_evidence(root, output)
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

                # A fresh output shell may exist in older callers. Its graph bytes
                # must match, but the restore must not graft the fresh stamp onto
                # the cached Siso state and object files.
                cached_stamp = checkpoint / "build.ninja.stamp"
                cached_stamp_text = cached_stamp.read_text(encoding="utf-8")
                cached_stamp_mtime = cached_stamp.stat().st_mtime_ns
                cached_graph_mtime = (checkpoint / "build.ninja").stat().st_mtime_ns
                cached_object = checkpoint / "obj" / "checkpoint_test" / "unit_000.o"
                cached_object_hash = M.sha256_file(cached_object)
                cached_object_mtime = cached_object.stat().st_mtime_ns
                output.mkdir(parents=True)
                (output / "args.gn").write_text("use_siso=true\n", encoding="utf-8")
                (output / "build.ninja").write_text("build edge\n", encoding="utf-8")
                fresh_stamp = output / "build.ninja.stamp"
                fresh_stamp.write_text("fresh job stamp must not win\n", encoding="utf-8")
                restored = M.command_restore(root, control, 2, "wf-1", required=True)
                self.assertEqual(restored, 0)
                self.assertTrue((output / ".siso_fs_state").is_file())
                self.assertEqual((output / "build.ninja").read_text(encoding="utf-8"), "build edge\n")
                self.assertEqual((output / "build.ninja.stamp").read_text(encoding="utf-8"), cached_stamp_text)
                self.assertEqual((output / "build.ninja.stamp").stat().st_mtime_ns, cached_stamp_mtime)
                self.assertEqual((output / "build.ninja").stat().st_mtime_ns, cached_graph_mtime)
                self.assertEqual(M.sha256_file(output / "obj" / "checkpoint_test" / "unit_000.o"), cached_object_hash)
                self.assertEqual((output / "obj" / "checkpoint_test" / "unit_000.o").stat().st_mtime_ns, cached_object_mtime)
                self.assertFalse(checkpoint.exists())
                self.assertFalse((output / M.META_NAME).exists())
                report = json.loads((root / M.REPORT_NAME).read_text(encoding="utf-8"))
                self.assertTrue(report["local_compiled_outputs_restored"])
                self.assertEqual(report["source_stage"], 1)

    def test_pack_and_restore_recovers_subsecond_mtimes_lost_by_cache_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            output = root / M.OUT_RELATIVE
            make_output(output)
            # Keep Ninja's dependency/build logs in the snapshot too.
            for name in (".ninja_log", ".ninja_deps"):
                (output / name).write_text("fixture state\n", encoding="utf-8")
            add_checkpoint_evidence(root, output)
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
            obj = output / "obj" / "checkpoint_test" / "unit_005.o"
            state = output / ".ninja_deps"
            expected = {p.relative_to(output).as_posix(): p.stat().st_mtime_ns
                        for p in (obj, state, output / "build.ninja", output / "build.ninja.stamp")}
            with patch.object(M, "control_inputs", return_value=CONTROL_HASHES), \\
                 patch.object(M, "source_identity", return_value=IDENTITY):
                self.assertEqual(M.command_pack(root, control, 1, "wf-1", ledger, partial=False), 0)
                checkpoint = root / M.CHECKPOINT_NAME
                # Simulate cache transport rounding archived mtimes to whole seconds.
                for path in checkpoint.rglob("*"):
                    if path.is_symlink() or not path.is_file() or path.name == M.META_NAME:
                        continue
                    stat = path.stat()
                    rounded = (stat.st_mtime_ns // 1_000_000_000) * 1_000_000_000
                    os.utime(path, ns=(stat.st_atime_ns, rounded))
                self.assertEqual(M.command_restore(root, control, 2, "wf-1", required=True), 0)
                for relative, expected_ns in expected.items():
                    self.assertEqual((output / relative).stat().st_mtime_ns, expected_ns, relative)

    def test_restore_full_snapshot_without_gn_first(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            output = root / M.OUT_RELATIVE
            make_output(output)
            add_checkpoint_evidence(root, output)
            cached_stamp = (output / "build.ninja.stamp").read_text(encoding="utf-8")
            cached_graph = M.sha256_file(output / "build.ninja")
            cached_graph_mtime = (output / "build.ninja").stat().st_mtime_ns
            cached_object = output / "obj" / "checkpoint_test" / "unit_005.o"
            cached_object_hash = M.sha256_file(cached_object)
            cached_object_mtime = cached_object.stat().st_mtime_ns
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
                self.assertEqual(M.command_pack(root, control, 1, "wf-1", ledger, partial=False), 0)
                self.assertFalse(output.exists())
                # A fresh source checkout has no generated out/ directory.
                output.parent.rmdir()
                self.assertEqual(M.command_restore(root, control, 2, "wf-1", required=True), 0)
                self.assertEqual(M.sha256_file(output / "build.ninja"), cached_graph)
                self.assertEqual((output / "build.ninja").stat().st_mtime_ns, cached_graph_mtime)
                self.assertEqual((output / "build.ninja.stamp").read_text(encoding="utf-8"), cached_stamp)
                self.assertEqual(M.sha256_file(output / "obj" / "checkpoint_test" / "unit_005.o"), cached_object_hash)
                self.assertEqual((output / "obj" / "checkpoint_test" / "unit_005.o").stat().st_mtime_ns, cached_object_mtime)
                self.assertTrue((output / ".siso_fs_state").is_file())
                self.assertFalse((root / M.CHECKPOINT_NAME).exists())

    def test_pack_and_restore_full_output_checkpoint_from_journal_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            output = root / M.OUT_RELATIVE
            make_output(output, state_name=".siso_fs_state.journal")
            add_checkpoint_evidence(root, output)
            ledger = root / "ledger.json"
            ledger.write_text(json.dumps({
                "stage": 1,
                "build_identity_sha256": M.identity_digest(IDENTITY),
                "max_completed_actions": 51781,
                "total_action_count": 108992,
                "remote_execution_proven": True,
                "remote_action_cache_reuse_proven": False,
                "artifact_verified": False,
            }), encoding="utf-8")
            with patch.object(M, "control_inputs", return_value=CONTROL_HASHES), \
                 patch.object(M, "source_identity", return_value=IDENTITY):
                self.assertEqual(M.command_pack(root, control, 1, "wf-1", ledger, partial=False), 0)
                checkpoint = root / M.CHECKPOINT_NAME
                self.assertTrue((checkpoint / ".siso_fs_state.journal").is_file())
                output.mkdir(parents=True)
                (output / "args.gn").write_text("use_siso=true\n", encoding="utf-8")
                (output / "build.ninja").write_text("build edge\n", encoding="utf-8")
                (output / "build.ninja.stamp").write_text("current job generator stamp\n", encoding="utf-8")
                self.assertEqual(M.command_restore(root, control, 2, "wf-1", required=True), 0)
                self.assertTrue((output / ".siso_fs_state.journal").is_file())
                self.assertFalse((output / ".siso_fs_state").exists())

    def test_restore_rejects_different_current_gn_graph_before_moving_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = root / "control"
            control.mkdir()
            output = root / M.OUT_RELATIVE
            make_output(output)
            add_checkpoint_evidence(root, output)
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
                self.assertEqual(M.command_pack(root, control, 1, "wf-1", ledger, partial=False), 0)
                checkpoint = root / M.CHECKPOINT_NAME
                output.mkdir(parents=True)
                (output / "args.gn").write_text("use_siso=true\n", encoding="utf-8")
                (output / "build.ninja").write_text("different graph\n", encoding="utf-8")
                (output / "build.ninja.stamp").write_text("fresh stamp\n", encoding="utf-8")
                with self.assertRaisesRegex(M.CheckpointError, "Fresh GN build.ninja differs"):
                    M.command_restore(root, control, 2, "wf-1", required=True)
                self.assertTrue(checkpoint.is_dir(), "incompatible checkpoint must be preserved for diagnosis")
                self.assertTrue(output.is_dir(), "fresh GN output must remain untouched on early rejection")
                self.assertTrue((output / "build.ninja").read_text(encoding="utf-8") == "different graph\n")

    def test_local_compiled_output_reuse_requires_unchanged_prior_action_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / M.OUT_RELATIVE
            make_output(output)
            evidence = add_checkpoint_evidence(root, output)
            report_path = root / M.REPORT_NAME
            report_path.write_text(json.dumps({
                "schema": 1,
                "SANDIUM_STATE_HANDOFF": "RESTORED",
                "source_stage": 1,
                "current_stage": 2,
                "local_compiled_outputs_restored": True,
                "compiled_output_inventory_status": "VERIFIED",
                "compiled_output_action_samples": evidence["compiled_output_action_samples"],
            }), encoding="utf-8")
            current_metrics = root / "stage-2-metrics.jsonl"
            object_paths = sorted(p.relative_to(output).as_posix() for p in (output / "obj").rglob("*.o"))
            current_metrics.write_text("".join(json.dumps({
                "step_id": f"stage-2-noexec-{index}", "outputs": [relative], "no_exec": True
            }) + "\n" for index, relative in enumerate(object_paths)), encoding="utf-8")

            result = M.verify_local_compiled_output_reuse(
                root=root, stage=2, metrics_path=current_metrics, report_path=report_path
            )
            self.assertEqual(result["status"], "VERIFIED_LOCAL_OUTPUT_REUSE")
            self.assertGreaterEqual(result["verified_local_reuse_count"], M.MIN_REUSED_ACTION_SAMPLES)
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["local_compiled_outputs_reuse_verified"])

    def test_continuation_fails_when_prior_sampled_objects_are_reexecuted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / M.OUT_RELATIVE
            make_output(output)
            evidence = add_checkpoint_evidence(root, output)
            report_path = root / M.REPORT_NAME
            report_path.write_text(json.dumps({
                "schema": 1,
                "SANDIUM_STATE_HANDOFF": "RESTORED",
                "source_stage": 1,
                "current_stage": 2,
                "local_compiled_outputs_restored": True,
                "compiled_output_action_samples": evidence["compiled_output_action_samples"],
            }), encoding="utf-8")
            current_metrics = root / "stage-2-metrics.jsonl"
            object_paths = sorted(p.relative_to(output).as_posix() for p in (output / "obj").rglob("*.o"))
            current_metrics.write_text("".join(json.dumps({
                "step_id": f"stage-2-exec-{index}", "outputs": [relative], "is_local": True
            }) + "\n" for index, relative in enumerate(object_paths)), encoding="utf-8")
            with self.assertRaisesRegex(M.CheckpointError, "could not prove unchanged prior-stage object outputs"):
                M.verify_local_compiled_output_reuse(
                    root=root, stage=2, metrics_path=current_metrics, report_path=report_path
                )

    def test_stage_one_can_migrate_legacy_checkpoint_but_marks_reuse_unverifiable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_path = root / M.REPORT_NAME
            report_path.write_text(json.dumps({
                "schema": 1,
                "SANDIUM_STATE_HANDOFF": "RESTORED",
                "source_stage": 1,
                "current_stage": 1,
                "local_compiled_outputs_restored": True,
            }), encoding="utf-8")
            metrics_path = root / "stage-1-metrics.jsonl"
            metrics_path.write_text("", encoding="utf-8")
            result = M.verify_local_compiled_output_reuse(
                root=root, stage=1, metrics_path=metrics_path, report_path=report_path
            )
            self.assertEqual(result["status"], "UNVERIFIABLE_NO_PRIOR_ACTION_SAMPLES")
            self.assertFalse(json.loads(report_path.read_text())["local_compiled_outputs_reuse_verified"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
