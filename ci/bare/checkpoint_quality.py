#!/usr/bin/env python3
"""Reject tiny, partial, stale, or wrong-stage compiled-output cache candidates.

CircleCI restore_cache selects the first matching key prefix, not the largest or
healthiest archive. This preflight protects the build runner from a metadata-only
or failed-stage cache before the candidate can be restored into the active output
tree. The authoritative full file/hash/source validation still runs in
output_checkpoint.py; this helper adds the cache-quality and stage-lineage gate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import output_checkpoint as OC
from build_identity import IdentityError, source_identity

MIN_GOOD_OUTPUT_BYTES = 512 * 1024 * 1024


class CheckpointQualityError(RuntimeError):
    pass


def regular_file_bytes(root: Path) -> int:
    """Count regular payload bytes, excluding the checkpoint's own metadata file."""
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file() and path.relative_to(root).as_posix() != OC.META_NAME:
            total += path.stat().st_size
    return total


def load_candidate(root: Path) -> dict[str, Any]:
    checkpoint = root / OC.CHECKPOINT_NAME
    meta_path = checkpoint / OC.META_NAME
    if not checkpoint.is_dir() or checkpoint.is_symlink():
        raise CheckpointQualityError("checkpoint_directory_missing_or_unsafe")
    if not meta_path.is_file() or meta_path.is_symlink():
        raise CheckpointQualityError("checkpoint_metadata_missing_or_unsafe")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointQualityError("checkpoint_metadata_invalid_json") from exc
    if not isinstance(meta, dict):
        raise CheckpointQualityError("checkpoint_metadata_not_object")
    return meta


def validate_candidate(
    *,
    root: Path,
    control: Path,
    stage: int,
    workflow_id: str,
    min_output_bytes: int = MIN_GOOD_OUTPUT_BYTES,
    produced: bool = False,
) -> dict[str, Any]:
    if min_output_bytes < 1:
        raise CheckpointQualityError("minimum_output_bytes_must_be_positive")
    checkpoint = root / OC.CHECKPOINT_NAME
    meta = load_candidate(root)

    if meta.get("schema") != OC.SCHEMA:
        raise CheckpointQualityError("unsupported_checkpoint_schema")
    if meta.get("checkpoint_name") != OC.CHECKPOINT_NAME:
        raise CheckpointQualityError("checkpoint_name_mismatch")
    if meta.get("output_relative_path") != OC.OUT_RELATIVE.as_posix():
        raise CheckpointQualityError("output_relative_path_mismatch")
    if meta.get("checkpoint_kind") != "completed-stage" or meta.get("stage_record_valid") is not True:
        raise CheckpointQualityError("candidate_is_not_a_green_completed_stage")
    try:
        source_stage = int(meta.get("stage", 0))
        declared_bytes = int(meta.get("output_bytes", 0))
    except (TypeError, ValueError) as exc:
        raise CheckpointQualityError("invalid_stage_or_output_byte_count") from exc
    if source_stage not in range(1, 6):
        raise CheckpointQualityError("invalid_source_stage")
    if produced:
        if source_stage != stage:
            raise CheckpointQualityError(
                f"published_checkpoint_stage_mismatch expected={stage} actual={source_stage}"
            )
        if meta.get("workflow_id") != workflow_id:
            raise CheckpointQualityError("published_checkpoint_workflow_mismatch")
    elif stage > 1:
        if source_stage != stage - 1:
            raise CheckpointQualityError(
                f"wrong_predecessor_stage expected={stage - 1} actual={source_stage}"
            )
        if meta.get("workflow_id") != workflow_id:
            raise CheckpointQualityError("checkpoint_not_from_immediate_predecessor_in_current_workflow")
    if declared_bytes < min_output_bytes:
        raise CheckpointQualityError(
            f"checkpoint_too_small declared_bytes={declared_bytes} minimum={min_output_bytes}"
        )
    measured_bytes = regular_file_bytes(checkpoint)
    if measured_bytes < min_output_bytes:
        raise CheckpointQualityError(
            f"checkpoint_payload_too_small measured_bytes={measured_bytes} minimum={min_output_bytes}"
        )
    if measured_bytes != declared_bytes:
        raise CheckpointQualityError(
            f"checkpoint_size_mismatch declared_bytes={declared_bytes} measured_bytes={measured_bytes}"
        )

    output_files = meta.get("output_files")
    if not isinstance(output_files, dict) or not output_files:
        raise CheckpointQualityError("output_file_manifest_missing")
    required_names = {"args.gn", "build.ninja"}
    state_names = {".siso_fs_state", ".siso_fs_state.0", ".siso_fs_state.journal"}
    if not required_names.issubset(output_files):
        raise CheckpointQualityError("critical_generated_build_files_missing_from_manifest")
    if not state_names.intersection(output_files):
        raise CheckpointQualityError("siso_state_missing_from_manifest")

    embedded_identity = meta.get("build_identity")
    if not isinstance(embedded_identity, dict):
        raise CheckpointQualityError("build_identity_missing")
    if meta.get("build_identity_sha256") != OC.identity_digest(embedded_identity):
        raise CheckpointQualityError("embedded_build_identity_digest_mismatch")
    try:
        OC.verify_control_inputs(meta.get("control_inputs"), control)
    except OC.CheckpointError as exc:
        raise CheckpointQualityError("checkpoint_control_inputs_mismatch:" + str(exc)) from exc
    # Before restore, compare the candidate against the freshly generated src/out
    # identity. After pack, output_checkpoint.py has atomically moved src/out into
    # the candidate directory, so validate GN args from that directory instead.
    active_output = root / OC.OUT_RELATIVE
    if active_output.is_dir() and not active_output.is_symlink():
        current_identity = source_identity(control, root, require_gn=True)
    else:
        current_identity = source_identity(
            control, root, require_gn=True, output_dir_override=checkpoint
        )
    if OC.identity_digest(current_identity) != meta.get("build_identity_sha256"):
        raise CheckpointQualityError("checkpoint_source_toolchain_or_gn_identity_mismatch")

    # Verify both the basic graph files and a bounded inventory of real compiled
    # object outputs. Only Stage 1 may migrate a legacy checkpoint lacking this
    # evidence; every newly published checkpoint and every continuation must have it.
    OC.verify_manifest_files(checkpoint, output_files)
    inventory = meta.get("compiled_output_inventory")
    inventory_status = "VERIFIED"
    object_count = 0
    object_bytes = 0
    action_sample_count = 0
    if inventory is None:
        if stage > 1 or produced:
            raise CheckpointQualityError("compiled_output_inventory_missing")
        inventory_status = "LEGACY_UNVERIFIED"
    else:
        try:
            checked_inventory = OC.validate_compiled_output_inventory(
                checkpoint, inventory, require_minimum=True
            )
        except OC.CheckpointError as exc:
            raise CheckpointQualityError("compiled_output_inventory_invalid:" + str(exc)) from exc
        object_count = checked_inventory["object_file_count"]
        object_bytes = checked_inventory["object_file_bytes"]
        try:
            action_samples = OC.normalize_compiled_output_action_samples(
                meta.get("compiled_output_action_samples"),
                meta.get("compiled_output_action_sample_schema"),
            )
        except OC.CheckpointError as exc:
            raise CheckpointQualityError("compiled_output_action_samples_invalid:" + str(exc)) from exc
        if len(action_samples) < OC.MIN_ACTION_OUTPUT_SAMPLES:
            raise CheckpointQualityError(
                "compiled_output_action_samples_too_few_leaf_objects:"
                + str(len(action_samples))
            )
        action_sample_count = len(action_samples)
        for item in action_samples:
            relative = item["path"]
            sample_path = checkpoint / relative
            if (not sample_path.is_file() or sample_path.is_symlink()
                    or sample_path.stat().st_size != item["bytes"]
                    or OC.sha256_file(sample_path) != item["sha256"]):
                raise CheckpointQualityError("compiled_output_action_sample_mismatch:" + relative)
    return {
        "source_stage": source_stage,
        "workflow_id": str(meta.get("workflow_id", "unknown")),
        "checkpoint_kind": str(meta.get("checkpoint_kind")),
        "output_bytes": measured_bytes,
        "build_identity_sha256": str(meta.get("build_identity_sha256")),
        "stage_record_valid": True,
        "compiled_output_inventory_status": inventory_status,
        "compiled_object_file_count": object_count,
        "compiled_object_file_bytes": object_bytes,
        "compiled_output_action_sample_count": action_sample_count,
    }


def quarantine_candidate(root: Path) -> Path | None:
    checkpoint = root / OC.CHECKPOINT_NAME
    if not checkpoint.exists() and not checkpoint.is_symlink():
        return None
    rejected_root = root / "sandium-rejected-output-checkpoints"
    rejected_root.mkdir(parents=True, exist_ok=True)
    target = rejected_root / f"{OC.CHECKPOINT_NAME}.rejected-{time.time_ns()}"
    os.replace(checkpoint, target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    parser.add_argument("--control", type=Path, default=None)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--workflow-id", default="unknown")
    parser.add_argument("--min-output-bytes", type=int, default=MIN_GOOD_OUTPUT_BYTES)
    parser.add_argument("--produced", action="store_true",
                        help="validate the just-created current-stage checkpoint instead of a restore candidate")
    args = parser.parse_args()
    root = args.root.resolve()
    control = args.control.resolve() if args.control else OC.control_root()
    try:
        result = validate_candidate(
            root=root,
            control=control,
            stage=args.stage,
            workflow_id=args.workflow_id,
            min_output_bytes=args.min_output_bytes,
            produced=args.produced,
        )
        print("SANDIUM_OUTPUT_CHECKPOINT_QUALITY=PASS")
        print(f"SANDIUM_OUTPUT_CHECKPOINT_QUALITY_SOURCE_STAGE={result['source_stage']}")
        print(f"SANDIUM_OUTPUT_CHECKPOINT_QUALITY_BYTES={result['output_bytes']}")
        print(f"SANDIUM_OUTPUT_CHECKPOINT_QUALITY_BUILD_IDENTITY_SHA256={result['build_identity_sha256']}")
        print(f"SANDIUM_OUTPUT_CHECKPOINT_COMPILED_INVENTORY={result['compiled_output_inventory_status']}")
        print(f"SANDIUM_OUTPUT_CHECKPOINT_COMPILED_OBJECT_FILES={result['compiled_object_file_count']}")
        print(f"SANDIUM_OUTPUT_CHECKPOINT_COMPILED_OBJECT_BYTES={result['compiled_object_file_bytes']}")
        print(f"SANDIUM_OUTPUT_CHECKPOINT_ACTION_SAMPLE_FILES={result['compiled_output_action_sample_count']}")
        return 0
    except (CheckpointQualityError, OC.CheckpointError, IdentityError, OSError, ValueError, KeyError) as exc:
        print(f"SANDIUM_OUTPUT_CHECKPOINT_QUALITY=REJECT reason={exc}", file=sys.stderr)
        try:
            rejected = quarantine_candidate(root)
            if rejected is not None:
                print(f"SANDIUM_OUTPUT_CHECKPOINT_QUARANTINED={rejected}", file=sys.stderr)
        except OSError as quarantine_exc:
            print(f"SANDIUM_OUTPUT_CHECKPOINT_QUARANTINE_ERROR={quarantine_exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
