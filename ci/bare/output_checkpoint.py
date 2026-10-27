#!/usr/bin/env python3
"""Validate and move the full Bare/Chromium Android output tree between stages.

The checkpoint is stored beside src/, not under src/, so a restored output never
looks like a partial Chromium checkout to bootstrap_chromium.sh. Files are moved
on the same filesystem rather than copied locally; CircleCI's cache handles the
cross-job transfer. Checkpoints are rejected when source, toolchain, Siso, GN,
or build-affecting control inputs differ. Pipeline orchestration changes alone
must not invalidate otherwise compatible compiled output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import IdentityError, source_identity

SCHEMA = 2
OUT_RELATIVE = Path("src/out/android_Debug_arm64")
CHECKPOINT_NAME = "sandium-build-output-checkpoint"
META_NAME = ".sandium-output-checkpoint.meta.json"
REPORT_NAME = "sandium-state-handoff-report.json"
# Only source, GN, Siso and packaging inputs that affect build compatibility.
# CI orchestration/ledger implementation changes must not invalidate compatible output.
CONTROL_INPUTS = (
    "ci/bare/source-pin.txt",
    "ci/bare/gn_args.gn",
    "ci/bare/prepare_source.sh",
    "ci/bare/configure_siso.py",
    "ci/bare/bootstrap_chromium.sh",
    "ci/bare/package_artifact.sh",
    "ci/siso/buildbuddy_backend.star",
    "ci/siso/buildbuddy-credential-helper.py",
)
CRITICAL_ALWAYS = ("args.gn", "build.ninja")
SISO_STATE_NAMES = (".siso_fs_state", ".siso_fs_state.0")
SISO_JOURNAL_STATE_NAMES = (".siso_fs_state.journal",)
OPTIONAL_STATE_NAMES = (*SISO_JOURNAL_STATE_NAMES, ".ninja_log", ".ninja_deps")

class CheckpointError(RuntimeError):
    pass

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def identity_digest(identity: dict[str, Any]) -> str:
    blob = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()

def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CheckpointError(f"Missing/unsafe JSON file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CheckpointError(f"JSON must contain an object: {path}")
    return value

def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def control_root() -> Path:
    return Path(__file__).resolve().parents[2]

def control_inputs(control: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in CONTROL_INPUTS:
        path = control / relative
        if not path.is_file() or path.is_symlink():
            raise CheckpointError(f"Build compatibility input missing/unsafe: {relative}")
        result[relative] = sha256_file(path)
    return result


def verify_control_inputs(saved: Any, control: Path) -> None:
    """Validate build-affecting hashes while tolerating legacy extra CI-code hashes."""
    if not isinstance(saved, dict) or not saved:
        raise CheckpointError("Checkpoint build compatibility input manifest is missing")
    expected = control_inputs(control)
    mismatches = [
        relative for relative, digest in expected.items()
        if saved.get(relative) != digest
    ]
    if mismatches:
        raise CheckpointError(
            "Build compatibility input hash mismatch: " + ",".join(sorted(mismatches))
        )

def require_regular_nonempty(path: Path) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise CheckpointError(f"Required regular nonempty file missing/unsafe: {path}")

def output_manifest(output: Path) -> dict[str, str]:
    if not output.is_dir() or output.is_symlink():
        raise CheckpointError(f"Output directory missing/unsafe: {output}")
    hashes: dict[str, str] = {}
    for name in CRITICAL_ALWAYS:
        path = output / name
        require_regular_nonempty(path)
        hashes[name] = sha256_file(path)
    state_name = next((name for name in SISO_STATE_NAMES if (output / name).is_file()
                       and not (output / name).is_symlink()
                       and (output / name).stat().st_size > 0), None)
    if state_name is None:
        # On an interrupted first Siso build, the base file may not yet exist.
        # Chromium Siso recovers filesystem updates from a nonempty journal on
        # the next invocation, so preserve that journal as the filesystem state.
        state_name = next((name for name in SISO_JOURNAL_STATE_NAMES if (output / name).is_file()
                           and not (output / name).is_symlink()
                           and (output / name).stat().st_size > 0), None)
    if state_name is None:
        raise CheckpointError("No nonempty regular Siso filesystem state or recovery journal exists")
    hashes[state_name] = sha256_file(output / state_name)
    for name in OPTIONAL_STATE_NAMES:
        path = output / name
        if name not in hashes and path.is_file() and not path.is_symlink() and path.stat().st_size:
            hashes[name] = sha256_file(path)
    apk = output / "apks" / "ChromePublic.apk"
    if apk.is_file() and not apk.is_symlink() and apk.stat().st_size:
        hashes["apks/ChromePublic.apk"] = sha256_file(apk)
    return hashes

def verify_manifest_files(output: Path, entries: Any) -> None:
    if not isinstance(entries, dict) or not entries:
        raise CheckpointError("Checkpoint output-file manifest is missing")
    for relative, expected in entries.items():
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise CheckpointError(f"Unsafe output manifest path: {relative}")
        path = output / rel
        require_regular_nonempty(path)
        if sha256_file(path) != expected:
            raise CheckpointError(f"Checkpoint file hash mismatch: {relative}")

def validate_candidate(root: Path, control: Path, stage: int, workflow_id: str,
                       *, pre_source: bool, required: bool) -> dict[str, Any] | None:
    checkpoint = root / CHECKPOINT_NAME
    meta_path = checkpoint / META_NAME

    def miss(reason: str) -> None:
        print(f"SANDIUM_OUTPUT_CHECKPOINT=MISS reason={reason}")
        if required:
            raise CheckpointError(reason)
        # Quarantine only the restored working-copy candidate. CircleCI's remote
        # immutable cache remains untouched. This lets a later useful checkpoint
        # be saved under the same schema without overwriting the rejected directory.
        if checkpoint.exists() or checkpoint.is_symlink():
            quarantine = root / "sandium-rejected-output-checkpoints"
            quarantine.mkdir(parents=True, exist_ok=True)
            target = quarantine / (checkpoint.name + f".stage-{stage}.rejected")
            if target.exists() or target.is_symlink():
                target = quarantine / (checkpoint.name + f".stage-{stage}.{int(datetime.now().timestamp())}.rejected")
            os.replace(checkpoint, target)
        return None

    if not checkpoint.is_dir() or checkpoint.is_symlink() or not meta_path.is_file() or meta_path.is_symlink():
        return miss("checkpoint_directory_or_metadata_missing")
    try:
        meta = read_json(meta_path)
        if meta.get("schema") != SCHEMA or meta.get("output_relative_path") != OUT_RELATIVE.as_posix():
            return miss("unsupported_checkpoint_schema_or_path")
        if meta.get("checkpoint_name") != CHECKPOINT_NAME:
            return miss("checkpoint_name_mismatch")
        if meta.get("build_identity_sha256") != identity_digest(meta.get("build_identity", {})):
            return miss("build_identity_digest_mismatch")
        verify_control_inputs(meta.get("control_inputs"), control)
        verify_manifest_files(checkpoint, meta.get("output_files"))
        inventory = meta.get("compiled_output_inventory")
        if inventory is None:
            # One-time Stage 1 migration fallback: old caches are preserved and
            # may be restored once, but never qualify as a Stage 2+ predecessor.
            if stage > 1:
                return miss("compiled_output_inventory_missing_from_predecessor_checkpoint")
            print("SANDIUM_OUTPUT_CHECKPOINT_COMPILED_INVENTORY=LEGACY_UNVERIFIED")
        else:
            validate_compiled_output_inventory(checkpoint, inventory, require_minimum=True)
            action_samples = meta.get("compiled_output_action_samples")
            if not isinstance(action_samples, list) or len(action_samples) < MIN_ACTION_OUTPUT_SAMPLES:
                if stage > 1:
                    return miss("compiled_output_action_samples_missing_from_predecessor_checkpoint")
            else:
                for item in action_samples:
                    if not isinstance(item, dict):
                        return miss("invalid_compiled_output_action_sample")
                    relative = item.get("path", "")
                    rel = Path(relative) if isinstance(relative, str) else Path("/")
                    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "obj":
                        return miss("unsafe_compiled_output_action_sample_path")
                    sample_path = checkpoint / rel
                    if (not sample_path.is_file() or sample_path.is_symlink()
                            or sample_path.stat().st_size != int(item.get("bytes", -1))
                            or sha256_file(sample_path) != item.get("sha256")):
                        return miss("compiled_output_action_sample_mismatch:" + relative)
        if pre_source:
            if stage > 1 and (
                meta.get("stage") != stage - 1
                or meta.get("workflow_id") != workflow_id
                or meta.get("stage_record_valid") is not True
            ):
                return miss("previous_stage_checkpoint_missing_or_incompatible")
        else:
            identity = source_identity(control, root, require_gn=True)
            if identity_digest(identity) != meta.get("build_identity_sha256"):
                return miss("current_source_toolchain_or_gn_identity_mismatch")
            if stage > 1 and (
                meta.get("stage") != stage - 1
                or meta.get("workflow_id") != workflow_id
                or meta.get("stage_record_valid") is not True
            ):
                return miss("previous_stage_checkpoint_missing_or_incompatible")
        return meta
    except (CheckpointError, IdentityError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return miss("validation_error:" + str(exc))

def command_preflight(root: Path, control: Path, stage: int, workflow_id: str, required: bool) -> int:
    meta = validate_candidate(root, control, stage, workflow_id, pre_source=True, required=required)
    if meta is None:
        return 1
    print("SANDIUM_OUTPUT_CHECKPOINT_PREFLIGHT=PASS")
    print("SANDIUM_OUTPUT_CHECKPOINT_STAGE=" + str(meta.get("stage")))
    print("SANDIUM_OUTPUT_CHECKPOINT_BUILD_IDENTITY_SHA256=" + str(meta["build_identity_sha256"]))
    print("SANDIUM_OUTPUT_CHECKPOINT_BYTES=" + str(meta.get("output_bytes", 0)))
    return 0

def command_restore(root: Path, control: Path, stage: int, workflow_id: str, required: bool) -> int:
    meta = validate_candidate(root, control, stage, workflow_id, pre_source=False, required=required)
    if meta is None:
        return 42 if required else 1
    output = root / OUT_RELATIVE
    checkpoint = root / CHECKPOINT_NAME
    backup = output.with_name("android_Debug_arm64.pre-checkpoint-restore")
    if not output.is_dir() or output.is_symlink():
        raise CheckpointError(f"Fresh GN output directory missing/unsafe: {output}")
    if backup.exists() or backup.is_symlink():
        raise CheckpointError(f"Refusing to overwrite preserved output backup: {backup}")
    os.replace(output, backup)
    try:
        os.replace(checkpoint, output)
        verify_manifest_files(output, meta["output_files"])
        restored_identity = source_identity(control, root, require_gn=True)
        if identity_digest(restored_identity) != meta["build_identity_sha256"]:
            raise CheckpointError("Restored source/toolchain/GN identity differs from checkpoint")
        # The cache metadata is stored inside the cached directory so a save
        # cannot publish metadata without the matching output tree. Remove it
        # only after every validation passes; it is not a Chromium build input.
        (output / META_NAME).unlink()
    except Exception:
        if output.exists() or output.is_symlink():
            # Keep a failed candidate for forensics instead of deleting a restored tree.
            failed = output.with_name("android_Debug_arm64.failed-checkpoint-restore")
            if not failed.exists() and not failed.is_symlink():
                os.replace(output, failed)
        if backup.exists():
            os.replace(backup, output)
        raise
    # The backup is only freshly generated GN scaffolding, not cached compiled output.
    shutil.rmtree(backup)
    report = {
        "schema": 1,
        "SANDIUM_STATE_HANDOFF": "RESTORED",
        "SANDIUM_PRIOR_USEFUL_RUN": "CURRENT_WORKFLOW_PREVIOUS_STAGE" if stage > 1 else "ACCEPTED",
        "source_workflow_id": str(meta.get("workflow_id", "unknown")),
        "source_stage": int(meta.get("stage", 0)),
        "current_workflow_id": workflow_id or "unknown",
        "current_stage": stage,
        "build_identity_sha256": meta["build_identity_sha256"],
        "local_compiled_outputs_restored": True,
        "compiled_output_reuse": "Full identity-validated Chromium GN output tree restored from CircleCI cache",
        "checkpoint_kind": meta.get("checkpoint_kind", "unknown"),
        "source_max_completed_actions": meta.get("max_completed_actions", 0),
        "source_total_action_count": meta.get("total_action_count", 0),
        "source_remote_execution_proven": meta.get("remote_execution_proven", False),
        "source_remote_action_cache_reuse_proven": meta.get("remote_action_cache_reuse_proven", False),
        "compiled_output_inventory_status": (
            "VERIFIED" if isinstance(meta.get("compiled_output_inventory"), dict)
            else "LEGACY_UNVERIFIED"
        ),
        "compiled_output_inventory": meta.get("compiled_output_inventory"),
        "compiled_output_action_samples": meta.get("compiled_output_action_samples", []),
    }
    write_json_atomic(root / REPORT_NAME, report)
    print("SANDIUM_OUTPUT_CHECKPOINT=RESTORED")
    print("SANDIUM_OUTPUT_CHECKPOINT_STAGE=" + str(meta.get("stage")))
    print("SANDIUM_OUTPUT_CHECKPOINT_SOURCE_WORKFLOW_ID=" + str(meta.get("workflow_id", "unknown")))
    print("SANDIUM_OUTPUT_CHECKPOINT_BUILD_IDENTITY_SHA256=" + str(meta["build_identity_sha256"]))
    print("SANDIUM_OUTPUT_CHECKPOINT_LOCAL_COMPILED_OUTPUTS=RESTORED")
    return 0

def command_pack(root: Path, control: Path, stage: int, workflow_id: str,
                 ledger_path: Path | None, *, partial: bool) -> int:
    output = root / OUT_RELATIVE
    checkpoint = root / CHECKPOINT_NAME
    meta_path = checkpoint / META_NAME
    if checkpoint.is_dir() and not checkpoint.is_symlink() and meta_path.is_file() and not meta_path.is_symlink():
        print("SANDIUM_OUTPUT_CHECKPOINT=ALREADY_PACKED")
        return 0
    if not output.is_dir() or output.is_symlink():
        if partial:
            print("SANDIUM_OUTPUT_CHECKPOINT_PARTIAL=SKIP_NO_OUTPUT_DIRECTORY")
            return 0
        raise CheckpointError(f"Output directory missing/unsafe: {output}")
    try:
        files = output_manifest(output)
        identity = source_identity(control, root, require_gn=True)
        identity_hash = identity_digest(identity)
        if not checkpoint.exists() and not checkpoint.is_symlink():
            pass
        else:
            raise CheckpointError("A stale/unvalidated checkpoint directory already exists")
        ledger: dict[str, Any] = {}
        stage_record_valid = False
        if ledger_path and ledger_path.is_file() and not ledger_path.is_symlink():
            candidate_ledger = read_json(ledger_path)
            if (candidate_ledger.get("stage") == stage
                    and candidate_ledger.get("build_identity_sha256") == identity_hash):
                ledger = candidate_ledger
                stage_record_valid = True
        if not partial and not stage_record_valid:
            raise CheckpointError("No current stage ledger matches the output build identity")
        output_bytes = sum(p.stat().st_size for p in output.rglob("*") if p.is_file() and not p.is_symlink())
        inventory = compiled_output_inventory(output)
        metrics_path = root / f"stage-{stage}-metrics.jsonl"
        action_samples = (
            compiled_output_action_samples(metrics_path, output)
            if metrics_path.is_file() and not metrics_path.is_symlink()
            else []
        )
        payload = {
            "schema": SCHEMA,
            "source": "Bare",
            "checkpoint_name": CHECKPOINT_NAME,
            "output_relative_path": OUT_RELATIVE.as_posix(),
            "checkpoint_kind": "completed-stage" if stage_record_valid else "partial-stage",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "workflow_id": workflow_id or "unknown",
            "stage": stage,
            "stage_record_valid": stage_record_valid,
            "build_identity": identity,
            "build_identity_sha256": identity_hash,
            "control_inputs": control_inputs(control),
            "output_files": files,
            "output_bytes": output_bytes,
            "compiled_output_inventory": inventory,
            "compiled_output_action_samples": action_samples,
            "compiled_output_action_samples_source_metrics_sha256": (
                sha256_file(metrics_path) if metrics_path.is_file() and not metrics_path.is_symlink() else None
            ),
            "max_completed_actions": int(ledger.get("max_completed_actions", 0) or 0),
            "total_action_count": int(ledger.get("total_action_count", 0) or 0),
            "remote_execution_proven": bool(ledger.get("remote_execution_proven", False)),
            "remote_action_cache_reuse_proven": bool(ledger.get("remote_action_cache_reuse_proven", False)),
            "artifact_verified": ledger.get("artifact_verified") is True,
            "local_compiled_outputs_archived": True,
            "checkpoint_transport": "CircleCI cache of full output directory; no extra tar/zstd archive",
        }
        # Store metadata inside the checkpoint directory. CircleCI can then
        # save one atomic cache path; an absent output tree cannot leave a
        # misleading metadata-only cache entry behind.
        os.replace(output, checkpoint)
        try:
            write_json_atomic(meta_path, payload)
        except Exception:
            (checkpoint / META_NAME).unlink(missing_ok=True)
            os.replace(checkpoint, output)
            raise
        print("SANDIUM_OUTPUT_CHECKPOINT_PACK=PASS")
        print("SANDIUM_OUTPUT_CHECKPOINT_STAGE=" + str(stage))
        print("SANDIUM_OUTPUT_CHECKPOINT_KIND=" + payload["checkpoint_kind"])
        print("SANDIUM_OUTPUT_CHECKPOINT_BYTES=" + str(output_bytes))
        print("SANDIUM_OUTPUT_CHECKPOINT_BUILD_IDENTITY_SHA256=" + identity_hash)
        print("SANDIUM_OUTPUT_CHECKPOINT_COMPILED_OBJECT_FILES=" + str(inventory["object_file_count"]))
        print("SANDIUM_OUTPUT_CHECKPOINT_COMPILED_OBJECT_BYTES=" + str(inventory["object_file_bytes"]))
        print("SANDIUM_OUTPUT_CHECKPOINT_ACTION_SAMPLE_FILES=" + str(len(action_samples)))
        return 0
    except (CheckpointError, IdentityError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        if partial:
            print("SANDIUM_OUTPUT_CHECKPOINT_PARTIAL=SKIP reason=" + str(exc), file=sys.stderr)
            return 0
        raise CheckpointError(str(exc)) from exc

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "restore", "pack", "pack-partial"))
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--workflow-id", default="unknown")
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    control = control_root()
    if args.mode == "preflight":
        return command_preflight(root, control, args.stage, args.workflow_id, args.required)
    if args.mode == "restore":
        return command_restore(root, control, args.stage, args.workflow_id, args.required)
    ledger = args.ledger.resolve() if args.ledger else root / "ci-handoff" / f"stage-{args.stage}" / "ledger.json"
    return command_pack(root, control, args.stage, args.workflow_id, ledger,
                        partial=args.mode == "pack-partial")

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckpointError, IdentityError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("SANDIUM_OUTPUT_CHECKPOINT_ERROR=" + str(exc))
