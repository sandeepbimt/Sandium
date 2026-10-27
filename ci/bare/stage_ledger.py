#!/usr/bin/env python3
"""Write identity-bound evidence ledgers for the five Bare build stages.

Stage 1 proves source/dependency readiness; Stage 2 proves generated GN/build
graph readiness; Stages 3-4 prove actual remote execution and cache reuse; Stage
5 proves the packaged APK. Siso counters are invocation-local and are never
summed across stages. This metadata does not persist the Chromium source tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import IdentityError, control_and_workspace, sha256, source_identity, verify_apk

SCHEMA = 2
STAGE_KINDS = {
    1: "source_dependency_readiness",
    2: "toolchain_and_build_graph_readiness",
    3: "remote_compilation",
    4: "remote_cache_continuation",
    5: "apk_packaging_and_verification",
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def stable_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Return pre-build identity, deliberately excluding generated GN output."""
    return {key: value for key, value in identity.items() if key != "gn_args_sha256"}


def parse_metrics(path: Path, log_path: Path) -> dict[str, int]:
    """Parse only evidence attributable to the current Siso invocation."""
    values = {
        "records": 0,
        "action_records": 0,
        "remote_executed_action_records": 0,
        "remote_cached_action_records": 0,
        "cached_action_records": 0,
        "local_action_records": 0,
        "local_fallback_action_records": 0,
        "no_exec_action_records": 0,
    }
    if not path.is_file() or path.is_symlink():
        raise IdentityError(f"Siso metrics file is missing or unsafe: {path}")

    with path.open(encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            values["records"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IdentityError(f"Invalid Siso metrics JSONL line {number}: {exc}") from exc
            if not isinstance(record, dict):
                continue
            action_record = (
                "output" in record
                or ("step_id" in record and isinstance(record.get("outputs"), list))
            )
            if not action_record:
                continue

            values["action_records"] += 1
            cached = record.get("cached") is True
            local = record.get("is_local") is True
            remote = record.get("is_remote") is True or record.get("is_local") is False
            no_exec = record.get("no_exec") is True
            fallback = record.get("fallback") is True
            values["cached_action_records"] += int(cached)
            values["local_action_records"] += int(local)
            values["local_fallback_action_records"] += int(fallback)
            values["no_exec_action_records"] += int(no_exec)
            values["remote_cached_action_records"] += int(remote and cached)
            values["remote_executed_action_records"] += int(remote and not cached and not no_exec)

    if not log_path.is_file() or log_path.is_symlink():
        raise IdentityError(f"Siso build log is missing or unsafe: {log_path}")
    log = log_path.read_text(encoding="utf-8", errors="replace")
    summary = re.compile(
        r"local:(?P<local>\d+)\s+remote:(?P<remote>\d+)\s+cache:(?P<cache>\d+)"
        r"(?:\s+cache-write:(?P<writes>\d+)(?:\(err:(?P<errors>\d+)\))?)?"
        r"(?:\s+fallback:(?P<fallback>\d+)\s+retry:(?P<retry>\d+)\s+skip:(?P<skip>\d+))?",
        re.IGNORECASE,
    )
    matches = list(summary.finditer(log))
    if matches:
        # A summary reports one invocation. Never add its counts to any prior stage.
        last = matches[-1]
        values["local_action_records"] = max(values["local_action_records"], int(last.group("local")))
        values["remote_executed_action_records"] = max(
            values["remote_executed_action_records"], int(last.group("remote"))
        )
        values["remote_cached_action_records"] = max(
            values["remote_cached_action_records"], int(last.group("cache"))
        )
        values["cached_action_records"] = max(
            values["cached_action_records"], int(last.group("cache"))
        )
        values["local_fallback_action_records"] = max(
            values["local_fallback_action_records"], int(last.group("fallback") or 0)
        )
    if values["action_records"] == 0 and not matches:
        raise IdentityError("No structured Siso metrics or action summary counters were found")
    return values


def validate_stage_evidence(
    stage: int, metrics: dict[str, int] | None
) -> tuple[bool, bool]:
    """Require only evidence a stage is responsible for producing."""
    if stage in (1, 2, 5):
        if metrics is not None:
            raise IdentityError(f"Stage {stage} does not consume Siso action metrics")
        return False, False
    if metrics is None:
        raise IdentityError(f"Stage {stage} requires current-invocation Siso metrics")

    remote_execution = metrics["remote_executed_action_records"] > 0
    cache_reuse = metrics["remote_cached_action_records"] > 0
    if stage == 3 and not remote_execution:
        raise IdentityError("Stage 3 did not prove any uncached remote action execution")
    if stage == 4 and not cache_reuse:
        raise IdentityError("Stage 4 did not prove any remote action-cache hits from prior work")
    return remote_execution, cache_reuse


def validate_source_log(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise IdentityError(f"Stage 1 source-preparation log is missing or unsafe: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    accepted_markers = (
        "SANDIUM_CHROMIUM_SOURCE_PREPARATION=PASS",
        "SANDIUM_SOURCE_CACHE=HIT_OR_PREPARED",
    )
    if not any(marker in text for marker in accepted_markers):
        raise IdentityError(
            "Stage 1 source log lacks a successful preparation/cache marker; "
            "a configured URL or pin alone is not readiness evidence"
        )
    return sha256(path)


def validate_build_graph(workspace: Path, identity: dict[str, Any]) -> dict[str, str]:
    output = workspace / "src" / "out" / "android_Debug_arm64"
    args = output / "args.gn"
    graph = output / "build.ninja"
    if not args.is_file() or args.is_symlink():
        raise IdentityError(f"Stage 2 GN args file is missing or unsafe: {args}")
    if not graph.is_file() or graph.is_symlink():
        raise IdentityError(f"Stage 2 generated Ninja build graph is missing or unsafe: {graph}")
    gn_hash = identity.get("gn_args_sha256")
    if not isinstance(gn_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", gn_hash):
        raise IdentityError("Stage 2 did not record a valid generated GN arguments fingerprint")
    return {"gn_args_sha256": gn_hash, "build_ninja_sha256": sha256(graph)}


def validate_artifact(
    artifact_path: Path, manifest_path: Path, identity: dict[str, Any], workspace: Path
) -> dict[str, Any]:
    for path, label in ((artifact_path, "APK"), (manifest_path, "build manifest")):
        if not path.is_file() or path.is_symlink():
            raise IdentityError(f"Stage 5 {label} is missing or unsafe: {path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IdentityError(f"Stage 5 build manifest is invalid JSON: {exc}") from exc

    if manifest.get("artifact_verified") is not True:
        raise IdentityError("Stage 5 manifest does not prove a verified artifact")
    if manifest.get("apksigner_verification") != "PASS":
        raise IdentityError("Stage 5 manifest does not prove successful apksigner verification")
    if manifest.get("apk_zip_integrity") != "PASS" or manifest.get("apk_manifest_and_dex") != "PASS":
        raise IdentityError("Stage 5 manifest lacks APK ZIP/manifest/DEX integrity evidence")
    expected_identity = digest(identity)
    if manifest.get("build_identity_sha256") != expected_identity:
        raise IdentityError("Stage 5 APK manifest does not match the current full source/GN identity")

    # Re-verify the artifact itself; do not trust PASS strings in JSON as proof.
    # This repeats ZIP/manifest/DEX inspection and executes apksigner at this stage.
    verified = verify_apk(
        artifact_path,
        {**identity, "artifact_name": manifest.get("artifact_name")},
        workspace=workspace,
        require_signature=True,
    )
    if verified.get("artifact_verified") is not True or verified.get("apksigner_verification") != "PASS":
        raise IdentityError("Stage 5 direct APK/signature verification did not pass")
    if manifest.get("artifact_bytes") != verified["artifact_bytes"] or manifest.get("artifact_sha256") != verified["artifact_sha256"]:
        raise IdentityError("Stage 5 APK size/SHA-256 does not match its build manifest")
    return {
        "artifact_sha256": verified["artifact_sha256"],
        "artifact_bytes": verified["artifact_bytes"],
        "artifact_manifest_sha256": sha256(manifest_path),
        "apksigner_verification": "PASS",
        "direct_apk_verification": "PASS",
    }


def validate_previous(
    stage: int, previous: dict[str, Any], identity_hash: str, gn_args_hash: str | None
) -> None:
    if previous.get("schema") != SCHEMA:
        raise IdentityError("Previous stage ledger uses an unsupported schema")
    if previous.get("stage") != stage - 1:
        raise IdentityError(
            f"Stage order mismatch: expected stage {stage - 1}, got {previous.get('stage')}"
        )
    if previous.get("build_identity_sha256") != identity_hash:
        raise IdentityError("Previous stage has a different stable source/toolchain identity")

    prior_gn = previous.get("gn_args_sha256")
    if stage == 2:
        # Stage 2 is the single allowed transition where generated GN evidence appears.
        if prior_gn is not None and prior_gn != gn_args_hash:
            raise IdentityError("Generated GN identity changed between stages 1 and 2")
        if gn_args_hash is None:
            raise IdentityError("Stage 2 must establish a generated GN arguments identity")
    else:
        if not isinstance(prior_gn, str) or prior_gn != gn_args_hash:
            raise IdentityError("Previous stage has a different or missing generated GN identity")

    if stage >= 4 and previous.get("remote_execution_proven") is not True:
        raise IdentityError("Prior stages have not proved actual Siso remote execution")
    if stage == 5 and previous.get("remote_cache_reuse_chain_proven") is not True:
        raise IdentityError("Prior compilation stages have not proved remote action-cache reuse")


def build_ledger_payload(
    *,
    stage: int,
    identity: dict[str, Any],
    identity_hash: str,
    previous: dict[str, Any] | None,
    metrics: dict[str, int] | None,
    previous_ledger_sha256: str | None,
    metrics_sha256: str | None,
    log_sha256: str | None,
    source_log_sha256: str | None,
    graph_evidence: dict[str, str] | None,
    artifact_evidence: dict[str, Any] | None,
    remote_execution: bool,
    cache_reuse: bool,
) -> dict[str, Any]:
    """Build a ledger with identity continuity and stage-local evidence."""
    counts = {
        "scope": "current_siso_invocation_only",
        "action_records": metrics["action_records"] if metrics else 0,
        "local_action_records": metrics["local_action_records"] if metrics else 0,
        "remote_executed_action_records": metrics["remote_executed_action_records"] if metrics else 0,
        "remote_cached_action_records": metrics["remote_cached_action_records"] if metrics else 0,
        "local_fallback_action_records": metrics["local_fallback_action_records"] if metrics else 0,
        "no_exec_action_records": metrics["no_exec_action_records"] if metrics else 0,
    }
    remote_chain = remote_execution or bool(previous and previous.get("remote_execution_proven") is True)
    cache_chain = cache_reuse or bool(previous and previous.get("remote_cache_reuse_chain_proven") is True)
    return {
        "schema": SCHEMA,
        "stage": stage,
        "stage_kind": STAGE_KINDS[stage],
        "build_identity": identity,
        "build_identity_sha256": identity_hash,
        "gn_args_sha256": identity.get("gn_args_sha256"),
        "previous_stage_ledger_sha256": previous_ledger_sha256,
        "siso_metrics_sha256": metrics_sha256,
        "siso_log_sha256": log_sha256,
        "source_preparation_log_sha256": source_log_sha256,
        "build_graph_evidence": graph_evidence,
        "artifact_evidence": artifact_evidence,
        "siso_metrics": metrics,
        "stage_action_counts": counts,
        "action_count_scope": "STAGE_LOCAL_ONLY_NOT_CUMULATIVE",
        "remote_execution_proven": remote_chain,
        "current_stage_remote_execution_proven": remote_execution,
        "remote_cache_reuse_proven": cache_reuse,
        "remote_cache_reuse_chain_proven": cache_chain,
        "evidence_contract": "STAGE_ROLE_SPECIFIC_SCHEMA_2",
    }


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise IdentityError(f"Previous stage ledger is missing or unsafe: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IdentityError(f"Previous stage ledger is invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise IdentityError("Previous stage ledger uses an unsupported schema")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--metrics", type=Path, default=None)
    parser.add_argument("--siso-log", type=Path, default=None)
    parser.add_argument("--source-log", type=Path, default=None)
    parser.add_argument("--artifact", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()

    control, inferred_root = control_and_workspace(Path(__file__))
    root = args.root.resolve() if args.root else inferred_root
    require_gn = args.stage >= 2
    identity = source_identity(control, root, require_gn=require_gn)
    identity_hash = digest(stable_identity(identity))

    previous = None
    if args.stage == 1:
        if args.previous is not None:
            raise IdentityError("Stage 1 must not consume a previous-stage ledger")
    else:
        if args.previous is None:
            raise IdentityError(f"Stage {args.stage} requires the previous stage's metadata ledger")
        previous = load_ledger(args.previous)
        validate_previous(args.stage, previous, identity_hash, identity.get("gn_args_sha256"))

    metrics_args_supplied = args.metrics is not None or args.siso_log is not None
    if (args.metrics is None) != (args.siso_log is None):
        raise IdentityError("--metrics and --siso-log must be provided together")
    metrics = None
    metrics_hash = None
    log_hash = None
    if metrics_args_supplied:
        if args.stage not in (3, 4):
            raise IdentityError(f"Stage {args.stage} must not consume Siso action metrics")
        metrics = parse_metrics(args.metrics, args.siso_log)
        metrics_hash = sha256(args.metrics)
        log_hash = sha256(args.siso_log)

    remote_execution, cache_reuse = validate_stage_evidence(args.stage, metrics)
    source_log_hash = None
    graph_evidence = None
    artifact_evidence = None

    if args.stage == 1:
        if args.source_log is None:
            raise IdentityError("Stage 1 requires --source-log from the source/dependency preparation step")
        source_log_hash = validate_source_log(args.source_log)
    elif args.source_log is not None:
        raise IdentityError(f"Stage {args.stage} does not accept --source-log")

    if args.stage == 2:
        graph_evidence = validate_build_graph(root, identity)
    elif args.stage < 2 and (args.artifact is not None or args.manifest is not None):
        raise IdentityError("Artifact inputs are only valid in Stage 5")

    if args.stage == 5:
        artifact = args.artifact.resolve() if args.artifact else (
            root / "sandium-output" / "Sandium-debug-arm64.apk"
        )
        manifest = args.manifest.resolve() if args.manifest else (
            root / "sandium-output" / "build-manifest.json"
        )
        artifact_evidence = validate_artifact(artifact, manifest, identity, root)
    elif args.artifact is not None or args.manifest is not None:
        raise IdentityError("Only Stage 5 accepts --artifact or --manifest")

    previous_digest = sha256(args.previous) if args.previous else None
    ledger = build_ledger_payload(
        stage=args.stage,
        identity=identity,
        identity_hash=identity_hash,
        previous=previous,
        metrics=metrics,
        previous_ledger_sha256=previous_digest,
        metrics_sha256=metrics_hash,
        log_sha256=log_hash,
        source_log_sha256=source_log_hash,
        graph_evidence=graph_evidence,
        artifact_evidence=artifact_evidence,
        remote_execution=remote_execution,
        cache_reuse=cache_reuse,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"SANDIUM_STAGE_LEDGER=PASS stage={args.stage} kind={STAGE_KINDS[args.stage]}")
    print("SANDIUM_BUILD_IDENTITY_SHA256=" + identity_hash)
    print("SANDIUM_GN_ARGS_SHA256=" + str(identity.get("gn_args_sha256")))
    print("SISO_REMOTE_EXECUTED_ACTION_RECORDS=" + str((metrics or {}).get("remote_executed_action_records", 0)))
    print("SISO_REMOTE_CACHED_ACTION_RECORDS=" + str((metrics or {}).get("remote_cached_action_records", 0)))
    print("SANDIUM_REMOTE_EXECUTION_CHAIN_PROVEN=" + str(ledger["remote_execution_proven"]).lower())
    print("SANDIUM_REMOTE_CACHE_REUSE_CHAIN_PROVEN=" + str(ledger["remote_cache_reuse_chain_proven"]).lower())
    print("SANDIUM_STAGE_LEDGER_PATH=" + str(args.output))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IdentityError, OSError, json.JSONDecodeError, KeyError) as exc:
        raise SystemExit("SANDIUM_STAGE_LEDGER_ERROR=" + str(exc))
