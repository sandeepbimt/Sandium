#!/usr/bin/env python3
"""Record tiny, source-bound stage ledgers proving Siso execution and cache reuse.

The ledger is metadata only. It never uploads Chromium source or build outputs
to CircleCI storage; compiled action reuse must be demonstrated by Siso metrics.
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
from build_identity import IdentityError, control_and_workspace, source_identity

SCHEMA = 1


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_metrics(path: Path, log_path: Path) -> dict[str, int]:
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
    if path.is_file():
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
                # Chromium's current metrics output uses a singular "output"
                # field; accept the older structured shape as well.
                action_record = (
                    ("output" in record)
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

    if not log_path.is_file():
        raise IdentityError(f"Siso build log is missing: {log_path}")
    log = log_path.read_text(encoding="utf-8", errors="replace")
    summary = re.compile(
        r"local:(?P<local>\d+)\s+remote:(?P<remote>\d+)\s+cache:(?P<cache>\d+)"
        r"(?:\s+cache-write:(?P<writes>\d+)(?:\(err:(?P<errors>\d+)\))?)?"
        r"(?:\s+fallback:(?P<fallback>\d+)\s+retry:(?P<retry>\d+)\s+skip:(?P<skip>\d+))?",
        re.IGNORECASE,
    )
    matches = list(summary.finditer(log))
    if matches:
        last = matches[-1]
        values["local_action_records"] = max(values["local_action_records"], int(last.group("local")))
        values["remote_executed_action_records"] = max(values["remote_executed_action_records"], int(last.group("remote")))
        values["remote_cached_action_records"] = max(values["remote_cached_action_records"], int(last.group("cache")))
        values["cached_action_records"] = max(values["cached_action_records"], int(last.group("cache")))
        values["local_fallback_action_records"] = max(values["local_fallback_action_records"], int(last.group("fallback") or 0))
    if values["action_records"] == 0 and not matches:
        raise IdentityError("No structured Siso metrics or action summary counters were found")
    return values


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise IdentityError(f"Previous stage ledger is missing or unsafe: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IdentityError(f"Previous stage ledger is invalid JSON: {exc}") from exc
    if data.get("schema") != SCHEMA:
        raise IdentityError("Unsupported stage-ledger schema")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--siso-log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()

    control, inferred_root = control_and_workspace(Path(__file__))
    root = args.root.resolve() if args.root else inferred_root
    identity = source_identity(control, root, require_gn=True)
    identity_hash = digest(identity)

    previous = None
    if args.stage == 1:
        if args.previous is not None:
            raise IdentityError("Stage 1 must not consume a previous-stage ledger")
    else:
        if args.previous is None:
            raise IdentityError(f"Stage {args.stage} requires the previous stage's metadata ledger")
        previous = load_ledger(args.previous)
        if previous.get("stage") != args.stage - 1:
            raise IdentityError(
                f"Stage order mismatch: expected stage {args.stage - 1}, got {previous.get('stage')}"
            )
        if previous.get("build_identity_sha256") != identity_hash:
            raise IdentityError("Previous stage was built with a different source/toolchain/GN identity")
        if previous.get("remote_execution_proven") is not True:
            raise IdentityError("Previous stage did not prove actual Siso remote execution")

    metrics = parse_metrics(args.metrics, args.siso_log)
    remote_execution = metrics["remote_executed_action_records"] > 0
    cache_reuse = metrics["remote_cached_action_records"] > 0

    if args.stage == 1 and not remote_execution:
        raise IdentityError("Stage 1 did not prove any uncached remote action execution")
    if 2 <= args.stage <= 4 and not cache_reuse:
        raise IdentityError(f"Stage {args.stage} did not prove any remote action-cache hits from prior work")
    if args.stage == 5 and not (remote_execution or cache_reuse):
        raise IdentityError("Final stage has neither remote execution nor remote-cache evidence")

    ledger = {
        "schema": SCHEMA,
        "stage": args.stage,
        "build_identity": identity,
        "build_identity_sha256": identity_hash,
        "previous_stage_ledger_sha256": hashlib.sha256(args.previous.read_bytes()).hexdigest() if args.previous else None,
        "siso_metrics_sha256": hashlib.sha256(args.metrics.read_bytes()).hexdigest() if args.metrics.is_file() else None,
        "siso_log_sha256": hashlib.sha256(args.siso_log.read_bytes()).hexdigest(),
        "siso_metrics": metrics,
        "remote_execution_proven": remote_execution or bool(previous and previous.get("remote_execution_proven")),
        "remote_cache_reuse_proven": cache_reuse,
        "progress_contract": "REMOTE_ACTION_CACHE",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"SANDIUM_STAGE_LEDGER=PASS stage={args.stage}")
    print("SANDIUM_BUILD_IDENTITY_SHA256=" + identity_hash)
    print("SISO_REMOTE_EXECUTED_ACTION_RECORDS=" + str(metrics["remote_executed_action_records"]))
    print("SISO_REMOTE_CACHED_ACTION_RECORDS=" + str(metrics["remote_cached_action_records"]))
    print("SANDIUM_REMOTE_CACHE_REUSE_PROVEN=" + str(cache_reuse).lower())
    print("SANDIUM_STAGE_LEDGER_PATH=" + str(args.output))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IdentityError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit("SANDIUM_STAGE_LEDGER_ERROR=" + str(exc))
