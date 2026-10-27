#!/usr/bin/env python3
"""Validate source-bound Siso/BuildBuddy execution and cross-stage progress."""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import IdentityError, control_and_workspace, read_pins, source_identity, verify_apk

SCHEMA = 3

def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def parse_metrics(metrics_path: Path, log_path: Path) -> dict:
    out = {"metrics_present": metrics_path.is_file(), "metric_records": 0, "action_records": 0,
           "cached_action_records": 0, "remote_executed_action_records": 0, "local_action_records": 0,
           "no_exec_action_records": 0, "summary_remote_actions": 0, "summary_cache_hits": 0}
    if metrics_path.is_file():
        with metrics_path.open(encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if not line.strip(): continue
                out["metric_records"] += 1
                try: row = json.loads(line)
                except json.JSONDecodeError as exc: raise IdentityError(f"Bad Siso JSONL line {n}: {exc}") from exc
                if not isinstance(row, dict): continue
                is_action = "output" in row or ("step_id" in row and isinstance(row.get("outputs"), list))
                if not is_action: continue
                out["action_records"] += 1
                cached = row.get("cached") is True
                local = row.get("is_local") is True
                remote = row.get("is_remote") is True or row.get("is_local") is False
                no_exec = row.get("no_exec") is True
                out["cached_action_records"] += int(cached)
                out["local_action_records"] += int(local)
                out["no_exec_action_records"] += int(no_exec)
                out["remote_executed_action_records"] += int(remote and not cached and not no_exec)
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    # These counters are Siso summaries, not regex-matched unique log-line counts.
    matches = list(re.finditer(r"local\s*[:=]\s*(\d+)[,\s]+remote\s*[:=]\s*(\d+)[,\s]+cache\s*[:=]\s*(\d+)", log, re.I))
    if matches:
        out["summary_remote_actions"] = int(matches[-1].group(2))
        out["summary_cache_hits"] = int(matches[-1].group(3))
    out["cached_action_records"] = max(out["cached_action_records"], out["summary_cache_hits"])
    out["remote_executed_action_records"] = max(out["remote_executed_action_records"], out["summary_remote_actions"])
    # Endpoint/configuration messages alone are deliberately not considered proof.
    out["remote_execution_log_evidence"] = bool(re.search(
        r'"is_remote"\s*:\s*true|is_local\s*=\s*false|step state:\s*remote exec|exec remote execute', log, re.I))
    out["remote_execution_proven"] = bool(out["remote_executed_action_records"] > 0 or out["remote_execution_log_evidence"])
    # Each job starts with a fresh output directory, so cache:true evidence is from
    # the configured Siso remote action cache, not restored local object files.
    out["remote_action_cache_reuse_proven"] = out["cached_action_records"] > 0
    return out

def load_ledger(path: Path) -> dict:
    if not path.is_file() or path.is_symlink(): raise IdentityError(f"Previous stage ledger missing/unsafe: {path}")
    try: data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc: raise IdentityError(f"Previous stage ledger is invalid JSON: {exc}") from exc
    if data.get("schema") != SCHEMA: raise IdentityError("Unsupported stage ledger schema")
    return data

def verify_artifact(apk: Path, manifest_path: Path, identity: dict, identity_hash: str, root: Path, control: Path) -> dict:
    if not manifest_path.is_file() or manifest_path.is_symlink(): raise IdentityError("Build manifest missing/unsafe")
    try: manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc: raise IdentityError(f"Build manifest is invalid JSON: {exc}") from exc
    if manifest.get("build_identity_sha256") != identity_hash: raise IdentityError("APK manifest source/build identity mismatch")
    for key, val in identity.items():
        if manifest.get(key) != val: raise IdentityError(f"APK manifest identity mismatch: {key}")
    result = verify_apk(apk, {**identity, "artifact_name": read_pins(control)["artifact_name"]},
                        workspace=root, require_signature=True)
    if manifest.get("artifact_verified") is not True or manifest.get("apksigner_verification") != "PASS":
        raise IdentityError("Build manifest lacks verified APK signature evidence")
    if manifest.get("artifact_sha256") != result["artifact_sha256"] or manifest.get("artifact_bytes") != result["artifact_bytes"]:
        raise IdentityError("APK checksum/size does not match manifest")
    return {"artifact_sha256": result["artifact_sha256"], "artifact_bytes": result["artifact_bytes"],
            "artifact_verified": True, "manifest_sha256": sha(manifest_path)}

def progress_accounting_fields(stage_done: int, watermark: int, total: int) -> dict:
    """Describe the progress fields without implying unique cumulative actions."""
    return {
        "progress_accounting_mode": "NON_ADDITIVE_WATERMARK",
        "progress_accounting_rule": "max(previous_normalized_watermark,current_invocation_completed_edges)",
        "progress_is_unique_cumulative_action_count": False,
        "max_completed_actions": watermark,
        # Retained as a compatibility alias for older metadata readers.
        "cumulative_completed_actions": watermark,
        "progress_watermark": watermark,
        "estimated_remaining_actions": max(0, total - watermark),
        "remaining_estimate_basis": "total_graph_edges_minus_non_additive_progress_watermark",
    }


def progress_watermark(ledger: dict) -> int:
    """Read a monotonic, non-additive progress watermark from a stage ledger.

    Older ledgers added the preceding checkpoint's progress to the current
    invocation's Siso numerator. Those counts overlap: the current invocation
    can include actions already represented by the checkpoint, especially
    remote-cache hits and no-exec actions. Detect that exact legacy shape and
    normalize it so the next stage can continue instead of failing above 100%.
    """
    raw = int(ledger.get("max_completed_actions",
                         ledger.get("cumulative_completed_actions", 0)) or 0)
    current = int(ledger.get("stage_completed_actions", 0) or 0)
    handoff = ledger.get("state_handoff")
    if not isinstance(handoff, dict):
        return raw
    source = int(handoff.get("source_max_completed_actions", 0) or 0)
    if (
        handoff.get("local_compiled_outputs_restored") is True
        and source > 0
        and current > 0
        and raw == source + current
    ):
        normalized = max(source, current)
        print(
            "SANDIUM_PROGRESS_WATERMARK_NORMALIZED=LEGACY_ADDITIVE_SUM "
            f"raw={raw} source_checkpoint={source} stage_actions={current} "
            f"effective={normalized}",
            file=sys.stderr,
        )
        return normalized
    return raw


def cumulative_progress(stage: int, done: int, total: int, previous: dict | None,
                        handoff: dict) -> int:
    """Return a conservative progress watermark across bounded invocations.

    Siso's per-invocation completed-action numerator is not a disjoint delta
    from the prior checkpoint: actions can overlap through local outputs,
    remote-cache hits and no-exec actions. Never add the two counters. Keep a
    monotonic watermark (the maximum observed count), and retain the stage-local
    action count separately in the ledger as evidence of work in this window.
    """
    if done < 0 or total <= 0 or done > total:
        raise IdentityError("Invalid completed-action or graph-total count")

    if stage == 1:
        # Stage 1 may resume from a compatible checkpoint saved by an earlier
        # workflow. A Siso-state capsule alone is not compiled-output reuse.
        baseline = 0
        if (handoff.get("local_compiled_outputs_restored") is True
                and handoff.get("local_compiled_outputs_reuse_verified") is True):
            baseline = int(handoff.get("source_max_completed_actions", 0) or 0)
    else:
        if previous is None:
            raise IdentityError("Continuation stage requires previous ledger")
        prior_total = int(previous.get("total_action_count", 0) or 0)
        if prior_total and prior_total != total:
            raise IdentityError(f"Build graph action count changed: prior={prior_total} current={total}")
        if handoff.get("local_compiled_outputs_restored") is not True:
            raise IdentityError("Continuation stage did not restore a full compiled-output checkpoint")
        if handoff.get("local_compiled_outputs_reuse_verified") is not True:
            raise IdentityError("Continuation stage did not prove local compiled-output reuse from prior object samples")
        raw_baseline = int(previous.get("max_completed_actions",
                                        previous.get("cumulative_completed_actions", 0)) or 0)
        handoff_baseline = int(handoff.get("source_max_completed_actions", raw_baseline) or 0)
        if handoff_baseline != raw_baseline:
            raise IdentityError(
                f"Restored checkpoint progress mismatch: ledger={raw_baseline} checkpoint={handoff_baseline}"
            )
        # Repair ledgers produced by the former additive implementation. The
        # handoff equality check above validates artifact lineage against the
        # original persisted ledger value; only then normalize the estimate.
        baseline = progress_watermark(previous)

    if baseline < 0:
        raise IdentityError("Restored checkpoint completed-action baseline is negative")
    watermark = max(baseline, done)
    if watermark > total:
        raise IdentityError(
            f"Completed-action watermark exceeds graph total: "
            f"baseline={baseline} current_stage={done} total={total}"
        )
    return watermark


def validate_progress(stage: int, metrics: dict, done: int, total: int,
                      previous: dict | None, handoff: dict) -> str:
    if done <= 0 or total <= 0:
        raise IdentityError("No parseable completed-action progress in current build log")
    evidence = metrics.get("remote_execution_proven") or metrics.get("remote_action_cache_reuse_proven")
    if not evidence:
        raise IdentityError("No actual remote-execution or remote-action-cache evidence in this stage")
    if stage == 1:
        if previous is not None:
            raise IdentityError("Stage 1 cannot consume a previous stage ledger")
        cumulative_progress(stage, done, total, previous, handoff)
        return "REMOTE_EXECUTION_OR_CACHE_WITH_PROGRESS"
    if previous is None:
        raise IdentityError("Continuation stage requires previous ledger")
    prior_total = int(previous.get("total_action_count", 0))
    if prior_total and prior_total != total:
        raise IdentityError(f"Build graph action count changed: prior={prior_total} current={total}")
    # In an incremental continuation the remaining slice should get smaller,
    # so per-invocation done may be lower than the prior slice. Validate the
    # cumulative count instead of requiring done > previous stage's raw count.
    cumulative_progress(stage, done, total, previous, handoff)
    if metrics.get("remote_action_cache_reuse_proven"):
        return "REMOTE_ACTION_CACHE_HIT_WITH_FORWARD_PROGRESS"
    return "REMOTE_EXECUTION_WITH_FORWARD_PROGRESS"

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    p.add_argument("--metrics", type=Path, required=True); p.add_argument("--siso-log", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True); p.add_argument("--previous", type=Path)
    p.add_argument("--root", type=Path); p.add_argument("--completed-actions", type=int, required=True)
    p.add_argument("--total-actions", type=int, required=True); p.add_argument("--apk", type=Path)
    p.add_argument("--manifest", type=Path); p.add_argument("--source-timestamp-policy", default="epoch_946684800_regular_files_v1")
    p.add_argument("--state-handoff-report", type=Path, required=True)
    a = p.parse_args()
    control, inferred_root = control_and_workspace(Path(__file__))
    root = a.root.resolve() if a.root else inferred_root
    ident = source_identity(control, root, require_gn=True); ident_hash = digest(ident)
    previous = None
    if a.stage == 1:
        if a.previous: raise IdentityError("Stage 1 cannot consume a prior ledger")
    else:
        if not a.previous: raise IdentityError(f"Stage {a.stage} requires stage {a.stage-1} ledger")
        previous = load_ledger(a.previous)
        if previous.get("stage") != a.stage - 1: raise IdentityError("Stage order mismatch")
        if previous.get("build_identity_sha256") != ident_hash: raise IdentityError("Source/toolchain/GN/Siso identity changed across stages")
        if previous.get("source_timestamp_policy") != a.source_timestamp_policy: raise IdentityError("Source timestamp policy changed across stages")
    if not a.state_handoff_report.is_file() or a.state_handoff_report.is_symlink():
        raise IdentityError("State handoff report missing/unsafe; stage continuity cannot be claimed")
    try:
        handoff = json.loads(a.state_handoff_report.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IdentityError("State handoff report is invalid JSON") from exc
    if not isinstance(handoff, dict):
        raise IdentityError("State handoff report must be a JSON object")
    if a.stage > 1:
        if handoff.get("SANDIUM_STATE_HANDOFF") != "RESTORED":
            raise IdentityError("Later stage must restore the immediately preceding stage's Siso state")
        if handoff.get("source_stage") != a.stage - 1 or handoff.get("source_workflow_id") != os.environ.get("CIRCLE_WORKFLOW_ID", "unknown"):
            raise IdentityError("State handoff does not originate from the immediately preceding stage in this workflow")
        if handoff.get("build_identity_sha256") != ident_hash:
            raise IdentityError("State handoff build identity differs from current stage")
    elif handoff.get("SANDIUM_STATE_HANDOFF") not in ("RESTORED", "MISS"):
        raise IdentityError("Stage 1 must explicitly record prior-run discovery outcome")
    metrics = parse_metrics(a.metrics, a.siso_log)
    artifact = {}; complete = False
    if a.apk and a.manifest and a.apk.is_file():
        artifact = verify_artifact(a.apk, a.manifest, ident, ident_hash, root, control)
        complete = artifact.get("artifact_verified") is True
    if complete:
        cumulative_done = a.total_actions
        evidence = "VERIFIED_APK_COMPLETE"
    else:
        cumulative_done = cumulative_progress(a.stage, a.completed_actions, a.total_actions, previous, handoff)
        evidence = validate_progress(a.stage, metrics, a.completed_actions, a.total_actions, previous, handoff)
    accounting = progress_accounting_fields(a.completed_actions, cumulative_done, a.total_actions)
    ledger = {"schema": SCHEMA, "stage": a.stage, "build_identity": ident, "build_identity_sha256": ident_hash,
              "previous_stage_ledger_sha256": sha(a.previous) if a.previous else None,
              "source_timestamp_policy": a.source_timestamp_policy,
              "remote_execution_proven": bool(metrics["remote_execution_proven"] or (previous and previous.get("remote_execution_proven"))),
              "remote_action_cache_reuse_proven": bool(metrics["remote_action_cache_reuse_proven"] or (previous and previous.get("remote_action_cache_reuse_proven"))),
              "current_stage_remote_execution_proven": metrics["remote_execution_proven"],
              "current_stage_remote_action_cache_reuse_proven": metrics["remote_action_cache_reuse_proven"],
              "metrics": metrics, "siso_metrics_sha256": sha(a.metrics) if a.metrics.is_file() else None,
              "siso_log_sha256": sha(a.siso_log) if a.siso_log.is_file() else None,
              "stage_completed_actions": a.completed_actions,
              **accounting,
              "total_action_count": a.total_actions,
              "evidence": evidence,
              "local_build_output_restored": handoff.get("local_compiled_outputs_restored") is True,
              "state_handoff": handoff, "state_handoff_report_sha256": sha(a.state_handoff_report), **artifact}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"SANDIUM_STAGE_LEDGER=PASS stage={a.stage}")
    print("SANDIUM_STAGE_EVIDENCE="+evidence)
    print("SANDIUM_BUILD_IDENTITY_SHA256="+ident_hash)
    print("SISO_REMOTE_EXECUTED_ACTIONS="+str(metrics["remote_executed_action_records"]))
    print("SISO_REMOTE_ACTION_CACHE_HITS="+str(metrics["cached_action_records"]))
    print("SANDIUM_STAGE_COMPLETED_ACTIONS="+str(a.completed_actions))
    print("SANDIUM_PROGRESS_ACCOUNTING_MODE=NON_ADDITIVE_WATERMARK")
    print("SANDIUM_PROGRESS_IS_UNIQUE_CUMULATIVE_ACTION_COUNT=false")
    print("SANDIUM_PROGRESS_WATERMARK="+str(cumulative_done))
    print("SANDIUM_MAX_COMPLETED_ACTIONS="+str(cumulative_done))
    print("SANDIUM_CUMULATIVE_COMPLETED_ACTIONS="+str(cumulative_done))
    print("SANDIUM_ESTIMATED_REMAINING_ACTIONS="+str(remaining_estimate))
    print("SANDIUM_TOTAL_ACTION_COUNT="+str(a.total_actions))
    return 0

if __name__ == "__main__":
    try: raise SystemExit(main())
    except (IdentityError, OSError, json.JSONDecodeError, KeyError) as exc:
        raise SystemExit("SANDIUM_STAGE_LEDGER_ERROR="+str(exc))
