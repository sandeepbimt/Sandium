#!/usr/bin/env python3
"""Normalize small Siso progress-denominator drift without weakening build identity.

Siso's displayed [done/total] counter can move by a few actions when a restored
manifest is regenerated. Stage ledgers otherwise require a stable denominator.
This helper permits <=1% counter-only variance only when the previous ledger's
full build identity exactly matches the current source/toolchain/GN identity,
then passes the previous denominator to the existing strict ledger validator.
It intentionally lives outside stage_ledger.py and output_checkpoint.py's
fingerprinted inputs so existing compiled-output caches remain compatible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import IdentityError, source_identity

MAX_GRAPH_TOTAL_DRIFT = 0.01


def identity_digest(control: Path, root: Path) -> str:
    identity = source_identity(control, root, require_gn=True)
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_total(
    *,
    stage: int,
    observed_total: int,
    previous_ledger: Path | None,
    control: Path,
    root: Path,
) -> int:
    if observed_total <= 0:
        raise IdentityError(f"Observed action denominator must be positive, got {observed_total}")
    if stage == 1:
        if previous_ledger is not None:
            raise IdentityError("Stage 1 must not normalize against a previous-stage ledger")
        return observed_total
    if previous_ledger is None or not previous_ledger.is_file() or previous_ledger.is_symlink():
        raise IdentityError("Continuation stage requires a safe previous-stage ledger")
    try:
        ledger = json.loads(previous_ledger.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IdentityError(f"Previous stage ledger is invalid JSON: {exc}") from exc
    if not isinstance(ledger, dict) or ledger.get("stage") != stage - 1:
        raise IdentityError(f"Previous ledger does not describe Stage {stage - 1}")
    current_identity = identity_digest(control, root)
    if ledger.get("build_identity_sha256") != current_identity:
        raise IdentityError("Cannot normalize action count when build identity differs")
    previous_total = int(ledger.get("total_action_count", 0) or 0)
    if previous_total <= 0:
        raise IdentityError("Previous ledger has no positive action denominator")
    if observed_total == previous_total:
        return observed_total
    drift = abs(observed_total - previous_total) / previous_total
    if drift > MAX_GRAPH_TOTAL_DRIFT:
        raise IdentityError(
            f"Graph action denominator drift {drift:.4%} exceeds "
            f"{MAX_GRAPH_TOTAL_DRIFT:.0%}: previous={previous_total} observed={observed_total}"
        )
    print(
        "SANDIUM_GRAPH_TOTAL_VARIANCE=ACCEPTED "
        f"previous={previous_total} observed={observed_total} "
        f"drift_pct={drift * 100:.4f}; using previous denominator",
        file=sys.stderr,
    )
    return previous_total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--observed-total", type=int, required=True)
    parser.add_argument("--previous-ledger", type=Path)
    parser.add_argument("--control", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    args = parser.parse_args()
    try:
        total = normalize_total(
            stage=args.stage,
            observed_total=args.observed_total,
            previous_ledger=args.previous_ledger.absolute() if args.previous_ledger else None,
            control=args.control.resolve(),
            root=args.root.resolve(),
        )
        print(total)
        return 0
    except (IdentityError, OSError, ValueError, KeyError) as exc:
        print(f"SANDIUM_GRAPH_TOTAL_VARIANCE=REJECTED reason={exc}", file=sys.stderr)
        return 53


if __name__ == "__main__":
    raise SystemExit(main())
