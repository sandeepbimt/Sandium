#!/usr/bin/env python3
"""Fail closed unless measured pending compile work matches the saved stage boundary."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


class StageWorkGateError(RuntimeError):
    pass


def _read_object(path: Path, description: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise StageWorkGateError(f"{description} missing/unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageWorkGateError(f"{description} unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StageWorkGateError(f"{description} must be a JSON object: {path}")
    return value


def _optional_count(data: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = data.get(name)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise StageWorkGateError(f"invalid nonnegative count in {name}: {value!r}")
        return value
    return None


def validate_stage_work(
    *,
    stage: int,
    pending: int,
    graph_total: int,
    prior_remaining: int | None = None,
    prior_graph_total: int | None = None,
) -> dict[str, int | str | None]:
    """Validate the actual measured pending count against the stable graph and prior checkpoint."""
    if stage not in range(1, 6):
        raise StageWorkGateError(f"invalid stage number: {stage}")
    if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
        raise StageWorkGateError(f"invalid pending compile count: {pending!r}")
    if not isinstance(graph_total, int) or isinstance(graph_total, bool) or graph_total <= 0:
        raise StageWorkGateError(f"invalid graph action count: {graph_total!r}")
    if pending > graph_total:
        raise StageWorkGateError(
            f"pending compile count exceeds graph size: pending={pending} graph={graph_total}"
        )
    if pending >= graph_total:
        raise StageWorkGateError(
            f"no previous work was subtracted from the current compile total: "
            f"pending={pending} graph={graph_total}"
        )
    if prior_graph_total is not None and prior_graph_total != graph_total:
        raise StageWorkGateError(
            f"build graph total changed across the checkpoint boundary: "
            f"prior={prior_graph_total} current={graph_total}"
        )
    if prior_remaining is not None and prior_remaining != pending:
        raise StageWorkGateError(
            f"measured pending compile count differs from saved previous remaining work: "
            f"saved={prior_remaining} current={pending}"
        )
    if stage > 1 and prior_remaining is None:
        raise StageWorkGateError(
            f"Stage {stage} continuation lacks a saved predecessor remaining-work count"
        )
    return {
        "stage": stage,
        "pending_compile_actions": pending,
        "graph_action_count": graph_total,
        "previous_remaining_actions": prior_remaining,
        "previous_graph_action_count": prior_graph_total,
        "reused_or_up_to_date_graph_actions": graph_total - pending,
        "counting_basis": "measured_siso_dry_run_after_restored_output_reuse",
    }


def gate_from_files(
    *, stage: int, pending: int, graph_total: int,
    state_report_path: Path | None, previous_ledger_path: Path | None,
) -> dict[str, int | str | None]:
    prior_remaining = None
    prior_graph_total = None
    if stage == 1:
        if state_report_path is not None and state_report_path.exists():
            report = _read_object(state_report_path, "restored checkpoint handoff report")
            prior_remaining = _optional_count(report, "source_remaining_actions_after_window")
            prior_graph_total = _optional_count(
                report, "source_graph_action_count", "source_total_action_count"
            )
    else:
        if previous_ledger_path is None:
            raise StageWorkGateError(f"Stage {stage} requires the immediate predecessor ledger")
        ledger = _read_object(previous_ledger_path, "previous-stage ledger")
        prior_remaining = _optional_count(ledger, "remaining_actions_after_window")
        prior_graph_total = _optional_count(ledger, "graph_action_count", "total_action_count")
    return validate_stage_work(
        stage=stage, pending=pending, graph_total=graph_total,
        prior_remaining=prior_remaining, prior_graph_total=prior_graph_total,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--pending", type=int, required=True)
    parser.add_argument("--graph-total", type=int, required=True)
    parser.add_argument("--state-report", type=Path)
    parser.add_argument("--previous-ledger", type=Path)
    args = parser.parse_args()
    try:
        evidence = gate_from_files(
            stage=args.stage, pending=args.pending, graph_total=args.graph_total,
            state_report_path=args.state_report, previous_ledger_path=args.previous_ledger,
        )
    except (StageWorkGateError, OSError) as exc:
        print(f"SANDIUM_STAGE_WORK_GATE=FAIL reason={exc}", file=sys.stderr)
        print("SANDIUM_BUILD_NOT_STARTED=true", file=sys.stderr)
        return 1
    for key, value in evidence.items():
        print(f"SANDIUM_STAGE_WORK_{key.upper()}={value if value is not None else 'UNAVAILABLE_LEGACY'}")
    print("SANDIUM_STAGE_WORK_GATE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
