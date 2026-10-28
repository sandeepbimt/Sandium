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
# Ninja's command and dependency histories must travel with the object files.
# A checkpoint without these logs can make valid outputs appear dirty or leave
# Siso unable to prove that a restored action is up to date.
REQUIRED_NINJA_HISTORY = (".ninja_log", ".ninja_deps")
SISO_STATE_NAMES = (".siso_fs_state", ".siso_fs_state.0")
SISO_JOURNAL_STATE_NAMES = (".siso_fs_state.journal",)
OPTIONAL_STATE_NAMES = (*SISO_JOURNAL_STATE_NAMES, ".ninja_log", ".ninja_deps")

# A full-output checkpoint must contain real Chromium compiled outputs, not
# merely generated assets and a resumable Siso state file.
COMPILED_OUTPUT_SUFFIXES = {".o", ".a", ".so"}
COMPILED_OUTPUT_MIN_FILES = 100
COMPILED_OUTPUT_MIN_BYTES = 1 * 1024 * 1024
COMPILED_OUTPUT_SAMPLE_LIMIT = 24
MIN_ACTION_OUTPUT_SAMPLES = 8
MIN_REUSED_ACTION_SAMPLES = 3
# Explicit contract for action samples: only leaf object files can prove local compile reuse.
ACTION_SAMPLE_SCHEMA = "leaf-object-v1"

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

def output_mtime_manifest(output: Path) -> dict[str, int]:
    """Capture nanosecond mtimes for every regular output file.

    CircleCI cache transport may round file mtimes to whole seconds. Siso/Ninja
    uses output mtimes together with .ninja_deps and generator logs; losing the
    fractional part makes restored object files look older/newer than their
    dependency records and triggers thousands of unnecessary rebuilds.
    Store the original mtimes as metadata and restore them after cache transport.
    """
    result: dict[str, int] = {}
    for path in output.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(output).as_posix()
        result[relative] = path.stat().st_mtime_ns
    if not result:
        raise CheckpointError("Cannot checkpoint an output tree with no regular files")
    return result


def validate_output_mtime_manifest(output: Path, entries: Any) -> dict[str, int]:
    """Validate timestamp metadata before applying it to a restored output tree."""
    if not isinstance(entries, dict) or not entries:
        raise CheckpointError("Checkpoint nanosecond-mtime manifest is missing")
    result: dict[str, int] = {}
    for relative, mtime_ns in entries.items():
        rel = Path(relative) if isinstance(relative, str) else Path("/")
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise CheckpointError(f"Unsafe output mtime path: {relative}")
        if relative == META_NAME:
            raise CheckpointError("Checkpoint mtime manifest must not include its own metadata file")
        if not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool) or mtime_ns < 0:
            raise CheckpointError(f"Invalid nanosecond mtime for output: {relative}")
        path = output / rel
        if not path.is_file() or path.is_symlink():
            raise CheckpointError(f"Missing/unsafe file in checkpoint mtime manifest: {relative}")
        result[rel.as_posix()] = mtime_ns
    return result


def restore_output_mtimes(output: Path, entries: dict[str, int]) -> None:
    """Restore exact file mtimes lost during cache archive transport."""
    for relative, mtime_ns in entries.items():
        path = output / relative
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, mtime_ns), follow_symlinks=False)
        if path.stat().st_mtime_ns != mtime_ns:
            raise CheckpointError(f"Could not restore exact nanosecond mtime: {relative}")
    print("SANDIUM_OUTPUT_CHECKPOINT_MTIME_RESTORE=PASS")
    print("SANDIUM_OUTPUT_CHECKPOINT_MTIME_FILES=" + str(len(entries)))


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
    for name in REQUIRED_NINJA_HISTORY:
        path = output / name
        require_regular_nonempty(path)
        hashes[name] = sha256_file(path)
    for name in OPTIONAL_STATE_NAMES:
        path = output / name
        if name not in hashes and path.is_file() and not path.is_symlink() and path.stat().st_size:
            hashes[name] = sha256_file(path)
    apk = output / "apks" / "ChromePublic.apk"
    if apk.is_file() and not apk.is_symlink() and apk.stat().st_size:
        hashes["apks/ChromePublic.apk"] = sha256_file(apk)
    return hashes

def compiled_object_paths(output: Path) -> list[Path]:
    """List regular compiled files under obj/, excluding symlinks and generators."""
    obj = output / "obj"
    if not obj.is_dir() or obj.is_symlink():
        return []
    files = []
    for path in obj.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() not in COMPILED_OUTPUT_SUFFIXES:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(output).as_posix())


def sample_indices(length: int, limit: int) -> list[int]:
    if length <= 0 or limit <= 0:
        return []
    if length <= limit:
        return list(range(length))
    return sorted({round(i * (length - 1) / (limit - 1)) for i in range(limit)})


def normalize_compiled_output_action_samples(
    samples: Any, schema: Any = None, *, output: Path | None = None
) -> list[dict[str, Any]]:
    """Validate action-sample records and migrate the old aggregate-inclusive contract.

    Missing schema identifies checkpoints written before leaf-object-only sampling.
    Those legacy records may contain .a/.so link outputs; discard those records and
    preserve only verified .o samples. Never infer compilation provenance from the
    filesystem inventory, and reject unknown/future sample contracts fail-closed.
    """
    legacy = schema is None
    if not isinstance(samples, list):
        if legacy and output is not None:
            # Old cache metadata predates the leaf-only sample contract. A missing
            # legacy list can be migrated from the verified output tree below.
            samples = []
        else:
            raise CheckpointError("compiled-output action samples are missing or not a list")
    if schema not in (None, ACTION_SAMPLE_SCHEMA):
        raise CheckpointError(f"unsupported compiled-output action-sample schema: {schema}")
    normalized: dict[str, dict[str, Any]] = {}
    for item in samples:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise CheckpointError("invalid compiled-output action-sample record")
        relative = item["path"]
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "obj":
            raise CheckpointError(f"unsafe compiled-output action-sample path: {relative}")
        suffix = rel.suffix.lower()
        if legacy:
            if suffix not in COMPILED_OUTPUT_SUFFIXES:
                raise CheckpointError(f"unsupported legacy compiled-output sample suffix: {relative}")
        elif suffix != ".o":
            raise CheckpointError(f"current leaf-object sample contract contains non-object output: {relative}")

        digest = item.get("sha256")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(ch not in "0123456789abcdefABCDEF" for ch in digest)):
            raise CheckpointError(f"invalid compiled-output sample SHA-256: {relative}")
        byte_count = item.get("bytes")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count <= 0:
            raise CheckpointError(f"invalid compiled-output sample byte count: {relative}")

        # Old checkpoints sampled link outputs alongside leaf objects. Their hashes
        # are not accepted as evidence of compile reuse; drop them during migration.
        if legacy and suffix in {".a", ".so"}:
            continue
        record = {"path": rel.as_posix(), "sha256": digest, "bytes": byte_count}
        previous = normalized.get(record["path"])
        if previous is not None and previous != record:
            raise CheckpointError(f"conflicting duplicate compiled-output samples: {relative}")
        normalized.setdefault(record["path"], record)

    # Legacy checkpoints could sample mostly .a/.so aggregate outputs. Those are
    # not compile-reuse evidence. If fewer than the minimum leaf objects survive,
    # derive a deterministic sample from real .o files in the checkpoint itself.
    # The caller still runs Siso's no-rebuild preflight against every selected
    # object; this migration never exempts a dirty target or relabels an archive.
    if legacy and output is not None and len(normalized) < MIN_ACTION_OUTPUT_SAMPLES:
        leaf_objects = sorted(
            path for path in compiled_object_paths(output)
            if path.suffix.lower() == ".o"
        )
        picked = [leaf_objects[i] for i in sample_indices(len(leaf_objects), COMPILED_OUTPUT_SAMPLE_LIMIT)]
        if len(picked) >= MIN_ACTION_OUTPUT_SAMPLES:
            normalized = {
                path.relative_to(output).as_posix(): {
                    "path": path.relative_to(output).as_posix(),
                    "sha256": sha256_file(path),
                    "bytes": path.stat().st_size,
                }
                for path in picked
            }
    return list(normalized.values())


def compiled_output_inventory(output: Path) -> dict[str, Any]:
    """Hash a deterministic sample while counting the whole compiled-object set."""
    files = compiled_object_paths(output)
    total_bytes = sum(path.stat().st_size for path in files)
    samples = {}
    for index in sample_indices(len(files), COMPILED_OUTPUT_SAMPLE_LIMIT):
        path = files[index]
        samples[path.relative_to(output).as_posix()] = sha256_file(path)
    return {
        "schema": 1,
        "root": "obj/",
        "accepted_suffixes": sorted(COMPILED_OUTPUT_SUFFIXES),
        "object_file_count": len(files),
        "object_file_bytes": total_bytes,
        "sample_sha256": samples,
        "minimum_required_files": COMPILED_OUTPUT_MIN_FILES,
        "minimum_required_bytes": COMPILED_OUTPUT_MIN_BYTES,
    }


def validate_compiled_output_inventory(output: Path, inventory: Any, *, require_minimum: bool) -> dict[str, Any]:
    if not isinstance(inventory, dict) or inventory.get("schema") != 1:
        raise CheckpointError("Compiled-output inventory is missing or has an unsupported schema")
    files = compiled_object_paths(output)
    total_bytes = sum(path.stat().st_size for path in files)
    if int(inventory.get("object_file_count", -1)) != len(files):
        raise CheckpointError("Compiled-output inventory object-file count mismatch")
    if int(inventory.get("object_file_bytes", -1)) != total_bytes:
        raise CheckpointError("Compiled-output inventory object-file byte count mismatch")
    samples = inventory.get("sample_sha256")
    if not isinstance(samples, dict) or not samples:
        raise CheckpointError("Compiled-output inventory has no sample hashes")
    for relative, expected in samples.items():
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "obj":
            raise CheckpointError(f"Unsafe compiled-output sample path: {relative}")
        path = output / rel
        require_regular_nonempty(path)
        if path.suffix.lower() not in COMPILED_OUTPUT_SUFFIXES:
            raise CheckpointError(f"Non-compiled output in sample manifest: {relative}")
        if sha256_file(path) != expected:
            raise CheckpointError(f"Compiled-output sample hash mismatch: {relative}")
    if require_minimum:
        if len(files) < COMPILED_OUTPUT_MIN_FILES:
            raise CheckpointError(
                f"Too few compiled object outputs: {len(files)} < {COMPILED_OUTPUT_MIN_FILES}"
            )
        if total_bytes < COMPILED_OUTPUT_MIN_BYTES:
            raise CheckpointError(
                f"Compiled object output payload too small: {total_bytes} < {COMPILED_OUTPUT_MIN_BYTES}"
            )
    return {
        "object_file_count": len(files),
        "object_file_bytes": total_bytes,
        "sample_file_count": len(samples),
        "minimum_thresholds_met": (
            len(files) >= COMPILED_OUTPUT_MIN_FILES and total_bytes >= COMPILED_OUTPUT_MIN_BYTES
        ),
    }


def _metrics_outputs(metrics_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Return output paths from the current Siso invocation, preserving evidence flags."""
    result: dict[str, list[dict[str, Any]]] = {}
    if not metrics_path.is_file() or metrics_path.is_symlink():
        raise CheckpointError(f"Current-stage Siso metrics are missing/unsafe: {metrics_path}")
    with metrics_path.open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CheckpointError(f"Invalid Siso metrics JSONL line {line_number}") from exc
            if not isinstance(row, dict):
                continue
            names = row.get("outputs")
            if not isinstance(names, list):
                names = [row.get("output")] if isinstance(row.get("output"), str) else []
            flags = {
                "cached": row.get("cached") is True,
                "no_exec": row.get("no_exec") is True,
                "is_local": row.get("is_local") is True,
                "is_remote": row.get("is_remote") is True or row.get("is_local") is False,
            }
            for name in names:
                if not isinstance(name, str):
                    continue
                rel = Path(name)
                if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                    continue
                relative = rel.as_posix()
                # Reuse proof must sample leaf compilation outputs only. Static
                # libraries and shared objects are aggregate/link outputs: their
                # rebuild commands can be scheduled because one member changed,
                # even when most restored object files are reusable. Mixing them
                # into the leaf-object gate produces misleading near-total failures.
                if relative.startswith("obj/") and rel.suffix.lower() == ".o":
                    result.setdefault(relative, []).append(flags)
    return result


def compiled_output_action_samples(metrics_path: Path, output: Path) -> list[dict[str, Any]]:
    """Sample object outputs named by this invocation's Siso action metrics."""
    records = _metrics_outputs(metrics_path)
    candidates = []
    for relative in sorted(records):
        # Reuse samples must come from real local compilation in the predecessor,
        # not remote-cache hits, copy/no-exec records or other generated outputs.
        previous_local_compile = any(
            flag["is_local"] and not flag["cached"] and not flag["no_exec"]
            for flag in records[relative]
        )
        if not previous_local_compile:
            continue
        path = output / relative
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
            candidates.append(path)
    picked = [candidates[index] for index in sample_indices(len(candidates), COMPILED_OUTPUT_SAMPLE_LIMIT)]
    return [
        {
            "path": path.relative_to(output).as_posix(),
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for path in picked
    ]


def verify_local_compiled_output_reuse(
    *, root: Path, stage: int, metrics_path: Path, report_path: Path
) -> dict[str, Any]:
    """Prove sampled previous-stage object outputs remain unchanged and weren't re-executed."""
    report = read_json(report_path)
    samples = report.get("compiled_output_action_samples")
    current_metrics_sha256 = sha256_file(metrics_path) if metrics_path.is_file() and not metrics_path.is_symlink() else None
    if not isinstance(samples, list) or len(samples) < MIN_ACTION_OUTPUT_SAMPLES:
        evidence = {
            "schema": 1,
            "status": "UNVERIFIABLE_NO_PRIOR_ACTION_SAMPLES",
            "previous_sample_count": len(samples) if isinstance(samples, list) else 0,
            "verified_local_reuse_count": 0,
            "minimum_required_reuse_count": MIN_REUSED_ACTION_SAMPLES,
            "current_metrics_sha256": current_metrics_sha256,
        }
        report["compiled_output_reuse_evidence"] = evidence
        report["local_compiled_outputs_reuse_verified"] = False
        write_json_atomic(report_path, report)
        print("SANDIUM_COMPILED_OUTPUT_REUSE_EVIDENCE=" + evidence["status"])
        if stage > 1:
            raise CheckpointError("Continuation stage has no verified prior-stage compiled-output action samples")
        return evidence

    output = root / OUT_RELATIVE
    current_records = _metrics_outputs(metrics_path)
    verified, unchanged, mentioned_cached, mentioned_executed = [], [], [], []
    for item in samples:
        if not isinstance(item, dict):
            continue
        relative = item.get("path", "")
        rel = Path(relative) if isinstance(relative, str) else Path("/")
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "obj":
            continue
        path = output / rel
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            continue
        if sha256_file(path) != item.get("sha256"):
            continue
        unchanged.append(relative)
        flags = current_records.get(relative, [])
        if any(flag["cached"] for flag in flags):
            mentioned_cached.append(relative)
        # no_exec is explicit evidence an output wasn't re-executed; a path
        # omitted from all current invocation output records also wasn't run.
        if not flags or all(flag["no_exec"] for flag in flags):
            verified.append(relative)
        elif any(not flag["cached"] and not flag["no_exec"] for flag in flags):
            mentioned_executed.append(relative)

    evidence = {
        "schema": 1,
        "status": "VERIFIED_LOCAL_OUTPUT_REUSE" if len(verified) >= MIN_REUSED_ACTION_SAMPLES else "LOCAL_OUTPUT_REUSE_NOT_PROVEN",
        "previous_sample_count": len(samples),
        "unchanged_sample_count": len(unchanged),
        "verified_local_reuse_count": len(verified),
        "current_stage_remote_or_action_cache_sample_count": len(mentioned_cached),
        "current_stage_uncached_execution_sample_count": len(mentioned_executed),
        "minimum_required_reuse_count": MIN_REUSED_ACTION_SAMPLES,
        "verified_sample_paths": verified,
        "unchanged_sample_paths": unchanged,
        "current_metrics_sha256": current_metrics_sha256,
    }
    report["compiled_output_reuse_evidence"] = evidence
    report["local_compiled_outputs_reuse_verified"] = len(verified) >= MIN_REUSED_ACTION_SAMPLES
    write_json_atomic(report_path, report)
    print("SANDIUM_COMPILED_OUTPUT_REUSE_EVIDENCE=" + evidence["status"])
    print("SANDIUM_COMPILED_OUTPUT_REUSE_VERIFIED_SAMPLES=" + str(len(verified)))
    print("SANDIUM_COMPILED_OUTPUT_REUSE_MINIMUM_SAMPLES=" + str(MIN_REUSED_ACTION_SAMPLES))
    if stage > 1 and len(verified) < MIN_REUSED_ACTION_SAMPLES:
        raise CheckpointError("Continuation stage could not prove unchanged prior-stage object outputs were reused locally")
    return evidence


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
                       *, pre_source: bool, required: bool,
                       preserve_on_miss: bool = False) -> dict[str, Any] | None:
    checkpoint = root / CHECKPOINT_NAME
    meta_path = checkpoint / META_NAME

    def miss(reason: str) -> None:
        print(f"SANDIUM_OUTPUT_CHECKPOINT=MISS reason={reason}")
        if required:
            raise CheckpointError(reason)
        # Quarantine only the restored working-copy candidate. CircleCI's remote
        # immutable cache remains untouched. This lets a later useful checkpoint
        # be saved under the same schema without overwriting the rejected directory.
        if not preserve_on_miss and (checkpoint.exists() or checkpoint.is_symlink()):
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
        mtime_entries = meta.get("output_mtime_ns")
        if mtime_entries is None:
            # Older saved caches predate the nanosecond-mtime manifest. Do not
            # invent the original timestamps: record the timestamps actually
            # present after cache transport, then let the real Siso no-rebuild
            # preflight decide whether any compiled actions are reusable.
            # This one-time migration is Stage 1 only; continuations must use
            # checkpoints produced with the current complete metadata contract.
            if stage > 1:
                return miss("nanosecond_mtime_manifest_missing_from_predecessor_checkpoint")
            mtime_entries = {
                path.relative_to(checkpoint).as_posix(): path.stat().st_mtime_ns
                for path in checkpoint.rglob("*")
                if path.is_file() and not path.is_symlink()
                and path.relative_to(checkpoint).as_posix() != META_NAME
            }
            if not mtime_entries:
                return miss("legacy_checkpoint_has_no_regular_files_for_mtime_migration")
            meta["output_mtime_ns"] = mtime_entries
            meta["output_mtime_migration"] = "reconstructed_from_restored_cache; requires_live_siso_reuse_preflight"
            write_json_atomic(meta_path, meta)
            print("SANDIUM_OUTPUT_CHECKPOINT_MTIME_MANIFEST=LEGACY_RECONSTRUCTED_FROM_RESTORED_CACHE")
            print("SANDIUM_OUTPUT_CHECKPOINT_MTIME_ORIGINAL_PRECISION=UNAVAILABLE")
            print("SANDIUM_OUTPUT_CHECKPOINT_MTIME_MIGRATION_REQUIRES_SISO_PREFLIGHT=true")
        validate_output_mtime_manifest(checkpoint, mtime_entries)
        inventory = meta.get("compiled_output_inventory")
        if inventory is None:
            # One-time Stage 1 migration fallback: old caches are preserved and
            # may be restored once, but never qualify as a Stage 2+ predecessor.
            if stage > 1:
                return miss("compiled_output_inventory_missing_from_predecessor_checkpoint")
            print("SANDIUM_OUTPUT_CHECKPOINT_COMPILED_INVENTORY=LEGACY_UNVERIFIED")
        else:
            validate_compiled_output_inventory(checkpoint, inventory, require_minimum=True)
            try:
                action_samples = normalize_compiled_output_action_samples(
                    meta.get("compiled_output_action_samples"),
                    meta.get("compiled_output_action_sample_schema"),
                    output=checkpoint,
                )
            except CheckpointError as exc:
                return miss("compiled_output_action_samples_invalid:" + str(exc))
            # Even Stage 1 may use a cached output tree only when enough *leaf*
            # objects survive validation. Aggregate-only samples cannot prove compile reuse.
            if len(action_samples) < MIN_ACTION_OUTPUT_SAMPLES:
                return miss(
                    "compiled_output_action_samples_too_few_leaf_objects:"
                    + str(len(action_samples))
                )
            for item in action_samples:
                relative = item["path"]
                sample_path = checkpoint / relative
                if (not sample_path.is_file() or sample_path.is_symlink()
                        or sample_path.stat().st_size != item["bytes"]
                        or sha256_file(sample_path) != item["sha256"]):
                    return miss("compiled_output_action_sample_mismatch:" + relative)
        if pre_source:
            if stage > 1 and (
                meta.get("stage") != stage - 1
                or meta.get("workflow_id") != workflow_id
                or meta.get("stage_record_valid") is not True
            ):
                return miss("previous_stage_checkpoint_missing_or_incompatible")
        else:
            active_output = root / OUT_RELATIVE
            if active_output.is_dir() and not active_output.is_symlink():
                identity = source_identity(control, root, require_gn=True)
            else:
                identity = source_identity(
                    control, root, require_gn=True, output_dir_override=checkpoint
                )
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

def command_preflight(root: Path, control: Path, stage: int, workflow_id: str, required: bool,
                      preserve_on_miss: bool = False) -> int:
    meta = validate_candidate(root, control, stage, workflow_id, pre_source=True,
                              required=required, preserve_on_miss=preserve_on_miss)
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
    output_was_present = output.exists() or output.is_symlink()
    if backup.exists() or backup.is_symlink():
        raise CheckpointError(f"Refusing to overwrite preserved output backup: {backup}")

    # A warm restore must be an atomic snapshot of build.ninja, its generator stamp,
    # Siso filesystem state, and compiled outputs. Combining cached Siso state with
    # fresh GN graph/stamp files makes Siso regard prior objects as dirty. The warm
    # path therefore leaves OUT absent until this restore finishes.
    if output_was_present:
        if output.is_symlink() or not output.is_dir():
            raise CheckpointError(f"Existing GN output path is unsafe: {output}")
        expected_files = meta.get("output_files", {})
        for name in ("args.gn", "build.ninja"):
            path = output / name
            require_regular_nonempty(path)
            expected = expected_files.get(name)
            if not isinstance(expected, str) or sha256_file(path) != expected:
                raise CheckpointError(
                    f"Fresh GN {name} differs from the identity-validated compiled-output checkpoint"
                )
        stamp = output / "build.ninja.stamp"
        if not stamp.is_file() or stamp.is_symlink():
            raise CheckpointError("Fresh GN build.ninja.stamp missing/unsafe; refusing incompatible output tree")
        print("SANDIUM_OUTPUT_CHECKPOINT_CURRENT_GRAPH_COMPATIBLE=PASS")
        os.replace(output, backup)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        print("SANDIUM_OUTPUT_CHECKPOINT_RESTORE_MODE=INTACT_SNAPSHOT_BEFORE_GN")

    try:
        os.replace(checkpoint, output)
        # Reapply the original nanosecond timestamps before Siso sees the tree.
        # Without this, cache archive timestamp rounding makes .o mtimes disagree
        # with .ninja_deps and invalidates otherwise reusable compiled outputs.
        mtime_manifest = validate_output_mtime_manifest(output, meta.get("output_mtime_ns"))
        restore_output_mtimes(output, mtime_manifest)
        verify_manifest_files(output, meta["output_files"])
        restored_identity = source_identity(control, root, require_gn=True)
        if identity_digest(restored_identity) != meta["build_identity_sha256"]:
            raise CheckpointError("Restored source/toolchain/GN identity differs from checkpoint")
        # Metadata travels inside the snapshot to keep its publication atomic,
        # but it is not a Chromium build input; remove it only after validation.
        (output / META_NAME).unlink()
    except Exception:
        if output.exists() or output.is_symlink():
            if not checkpoint.exists() and not checkpoint.is_symlink():
                # Preserve the cache candidate for diagnosis/retry instead of losing it.
                os.replace(output, checkpoint)
            else:
                failed = output.with_name(f"android_Debug_arm64.failed-checkpoint-restore-{int(datetime.now().timestamp())}")
                os.replace(output, failed)
        if backup.exists():
            os.replace(backup, output)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    print("SANDIUM_OUTPUT_CHECKPOINT_SNAPSHOT_INTACT=PASS")
    raw_action_samples = meta.get("compiled_output_action_samples", [])
    sample_schema = meta.get("compiled_output_action_sample_schema")
    raw_leaf_sample_count = sum(
        1 for item in raw_action_samples
        if isinstance(item, dict) and isinstance(item.get("path"), str)
        and Path(item["path"]).suffix.lower() == ".o"
    ) if isinstance(raw_action_samples, list) else 0
    action_samples = normalize_compiled_output_action_samples(
        raw_action_samples, sample_schema, output=output
    )
    derived_legacy_leaf_samples = (
        sample_schema is None and raw_leaf_sample_count < MIN_ACTION_OUTPUT_SAMPLES
        and len(action_samples) >= MIN_ACTION_OUTPUT_SAMPLES
    )
    sample_migration = (
        "legacy_leaf_objects_derived_from_verified_checkpoint"
        if derived_legacy_leaf_samples
        else (
            "legacy_aggregate_outputs_filtered"
            if sample_schema is None and len(action_samples) < len(raw_action_samples)
            else ("legacy_object_samples_validated" if sample_schema is None and action_samples else "none")
        )
    )
    sample_source = (
        "verified_checkpoint_leaf_object_migration"
        if derived_legacy_leaf_samples
        else str(meta.get("compiled_output_action_sample_source", "legacy_unknown"))
    )
    print("SANDIUM_OUTPUT_CHECKPOINT_ACTION_SAMPLE_SCHEMA=" + ACTION_SAMPLE_SCHEMA)
    print("SANDIUM_OUTPUT_CHECKPOINT_ACTION_SAMPLE_MIGRATION=" + sample_migration)
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
        "source_graph_action_count": meta.get("graph_action_count", meta.get("total_action_count", 0)),
        "source_required_actions_at_start": meta.get("required_actions_at_start"),
        "source_remaining_actions_after_window": meta.get("remaining_actions_after_window"),
        "build_ninja_sha256": meta.get("output_files", {}).get("build.ninja"),
        "source_remote_execution_proven": meta.get("remote_execution_proven", False),
        "source_remote_action_cache_reuse_proven": meta.get("remote_action_cache_reuse_proven", False),
        "compiled_output_inventory_status": (
            "VERIFIED" if isinstance(meta.get("compiled_output_inventory"), dict)
            else "LEGACY_UNVERIFIED"
        ),
        "compiled_output_inventory": meta.get("compiled_output_inventory"),
        "compiled_output_action_sample_schema": ACTION_SAMPLE_SCHEMA,
        "compiled_output_action_sample_migration": sample_migration,
        "compiled_output_action_sample_source": sample_source,
        "compiled_output_action_samples": action_samples,
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
            # Persist exact nanosecond mtimes; CircleCI cache transport may round them.
            "output_mtime_ns": output_mtime_manifest(output),
            "output_bytes": output_bytes,
            "compiled_output_inventory": inventory,
            "compiled_output_action_sample_schema": ACTION_SAMPLE_SCHEMA,
            "compiled_output_action_sample_source": "siso_metrics_local_compile",
            "compiled_output_action_samples": action_samples,
            "compiled_output_action_samples_source_metrics_sha256": (
                sha256_file(metrics_path) if metrics_path.is_file() and not metrics_path.is_symlink() else None
            ),
            "max_completed_actions": int(ledger.get("max_completed_actions", 0) or 0),
            "total_action_count": int(ledger.get("total_action_count", 0) or 0),
            "graph_action_count": int(ledger.get("graph_action_count", ledger.get("total_action_count", 0)) or 0),
            # Zero is meaningful evidence (no pending actions); do not turn it into unknown.
            "required_actions_at_start": (
                int(ledger["required_actions_at_start"])
                if ledger.get("required_actions_at_start") is not None else None
            ),
            "remaining_actions_after_window": (
                int(ledger["remaining_actions_after_window"])
                if ledger.get("remaining_actions_after_window") is not None else None
            ),
            "build_ninja_sha256": files.get("build.ninja"),
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
    parser.add_argument("mode", choices=("preflight", "restore", "pack", "pack-partial", "verify-reuse"))
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--workflow-id", default="unknown")
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--metrics", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--required", action="store_true")
    parser.add_argument("--preserve-on-miss", action="store_true",
                        help="keep a rejected cache candidate in place for the later source-aware validator")
    args = parser.parse_args()
    root = args.root.resolve()
    control = control_root()
    if args.mode == "preflight":
        return command_preflight(root, control, args.stage, args.workflow_id, args.required,
                                 args.preserve_on_miss)
    if args.mode == "restore":
        return command_restore(root, control, args.stage, args.workflow_id, args.required)
    if args.mode == "verify-reuse":
        metrics = args.metrics.resolve() if args.metrics else root / f"stage-{args.stage}-metrics.jsonl"
        report = args.report.resolve() if args.report else root / REPORT_NAME
        verify_local_compiled_output_reuse(
            root=root, stage=args.stage, metrics_path=metrics, report_path=report
        )
        return 0
    ledger = args.ledger.resolve() if args.ledger else root / "ci-handoff" / f"stage-{args.stage}" / "ledger.json"
    return command_pack(root, control, args.stage, args.workflow_id, ledger,
                        partial=args.mode == "pack-partial")

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckpointError, IdentityError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit("SANDIUM_OUTPUT_CHECKPOINT_ERROR=" + str(exc))
