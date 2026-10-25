#!/usr/bin/env python3
"""Validate source-bound Siso/BuildBuddy execution and cross-stage progress."""
from __future__ import annotations
import argparse, hashlib, json, re, sys
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

def validate_progress(stage: int, metrics: dict, done: int, total: int, previous: dict | None) -> str:
    if done <= 0 or total <= 0: raise IdentityError("No parseable completed-action progress in current build log")
    evidence = metrics.get("remote_execution_proven") or metrics.get("remote_action_cache_reuse_proven")
    if not evidence: raise IdentityError("No actual remote-execution or remote-action-cache evidence in this stage")
    if stage == 1: return "REMOTE_EXECUTION_OR_CACHE_WITH_PROGRESS"
    if previous is None: raise IdentityError("Continuation stage requires previous ledger")
    prior_total = int(previous.get("total_action_count", 0))
    prior_done = int(previous.get("max_completed_actions", 0))
    if prior_total and prior_total != total:
        raise IdentityError(f"Build graph action count changed: prior={prior_total} current={total}")
    if done <= prior_done: raise IdentityError(f"No forward progress: prior={prior_done} current={done}")
    if metrics.get("remote_action_cache_reuse_proven"): return "REMOTE_ACTION_CACHE_HIT_WITH_FORWARD_PROGRESS"
    return "REMOTE_EXECUTION_WITH_FORWARD_PROGRESS"

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    p.add_argument("--metrics", type=Path, required=True); p.add_argument("--siso-log", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True); p.add_argument("--previous", type=Path)
    p.add_argument("--root", type=Path); p.add_argument("--completed-actions", type=int, required=True)
    p.add_argument("--total-actions", type=int, required=True); p.add_argument("--apk", type=Path)
    p.add_argument("--manifest", type=Path); p.add_argument("--source-timestamp-policy", default="epoch_946684800_full_tree_v1")
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
    metrics = parse_metrics(a.metrics, a.siso_log)
    artifact = {}; complete = False
    if a.apk and a.manifest and a.apk.is_file():
        artifact = verify_artifact(a.apk, a.manifest, ident, ident_hash, root, control)
        complete = artifact.get("artifact_verified") is True
    evidence = "VERIFIED_APK_COMPLETE" if complete else validate_progress(a.stage, metrics, a.completed_actions, a.total_actions, previous)
    ledger = {"schema": SCHEMA, "stage": a.stage, "build_identity": ident, "build_identity_sha256": ident_hash,
              "previous_stage_ledger_sha256": sha(a.previous) if a.previous else None,
              "source_timestamp_policy": a.source_timestamp_policy,
              "remote_execution_proven": bool(metrics["remote_execution_proven"] or (previous and previous.get("remote_execution_proven"))),
              "remote_action_cache_reuse_proven": bool(metrics["remote_action_cache_reuse_proven"] or (previous and previous.get("remote_action_cache_reuse_proven"))),
              "current_stage_remote_execution_proven": metrics["remote_execution_proven"],
              "current_stage_remote_action_cache_reuse_proven": metrics["remote_action_cache_reuse_proven"],
              "metrics": metrics, "siso_metrics_sha256": sha(a.metrics) if a.metrics.is_file() else None,
              "siso_log_sha256": sha(a.siso_log) if a.siso_log.is_file() else None,
              "max_completed_actions": a.completed_actions, "total_action_count": a.total_actions,
              "evidence": evidence, "local_build_output_restored": False, **artifact}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"SANDIUM_STAGE_LEDGER=PASS stage={a.stage}")
    print("SANDIUM_STAGE_EVIDENCE="+evidence)
    print("SANDIUM_BUILD_IDENTITY_SHA256="+ident_hash)
    print("SISO_REMOTE_EXECUTED_ACTIONS="+str(metrics["remote_executed_action_records"]))
    print("SISO_REMOTE_ACTION_CACHE_HITS="+str(metrics["cached_action_records"]))
    print("SANDIUM_MAX_COMPLETED_ACTIONS="+str(a.completed_actions))
    print("SANDIUM_TOTAL_ACTION_COUNT="+str(a.total_actions))
    return 0

if __name__ == "__main__":
    try: raise SystemExit(main())
    except (IdentityError, OSError, json.JSONDecodeError, KeyError) as exc:
        raise SystemExit("SANDIUM_STAGE_LEDGER_ERROR="+str(exc))
