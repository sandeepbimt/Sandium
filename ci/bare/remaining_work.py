#!/usr/bin/env python3
"""Measure pending compile actions against the identity-validated restored graph."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PAIR = re.compile(r"^\s*\[\s*(\d+)\s*/\s*(\d+)\s*\]\s+\S", re.M)
NO_WORK = re.compile(r"(?:ninja: no work to do\.|no work to do)", re.I)
SISO_SUCCESS = re.compile(r"^\s*\d+(?:m\d+(?:\.\d+)?s|\.\d+s)\s+Build Succeeded: (\d+) steps(?:\s+-\s+[\d.]+/s)?\s*$", re.M)
SISO_SKIP = re.compile(r"^\s*noexec:\d+ local:\d+ remote:\d+ cache:\d+ skip:(\d+)\s*$", re.M)
SISO_EDGE = re.compile(r"\bedge:(\d+)\b", re.I)


class RemainingWorkError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def parse_siso_summary(output: str) -> dict[str, int] | None:
    """Parse Siso's independent pending, skipped and full-graph counters."""
    summaries = [int(n) for n in SISO_SUCCESS.findall(output)]
    skips = [int(n) for n in SISO_SKIP.findall(output)]
    if not summaries and not skips:
        return None
    if len(summaries) != 1 or len(skips) != 1:
        raise RemainingWorkError(
            "Siso dry-run requires an unambiguous completed Siso measurement: "
            "exactly one Build Succeeded summary and one explicit skip counter; "
            "otherwise no Ninja action rows provide a safe fallback"
        )
    pending, skipped = summaries[0], skips[0]
    if pending < 0 or skipped < 0:
        raise RemainingWorkError(f"negative Siso dry-run counters: steps={pending}, skip={skipped}")
    graph_total = pending + skipped
    edges = [int(n) for n in SISO_EDGE.findall(output)]
    if edges and edges[-1] != graph_total:
        raise RemainingWorkError(
            "Siso dry-run graph accounting mismatch: "
            f"steps={pending} skip={skipped} sum={graph_total} last_graph_edge_count={edges[-1]}"
        )
    if pending == 0 and not (graph_total > 0 and edges and edges[-1] == graph_total):
        raise RemainingWorkError(
            "zero Siso steps without an explicit no-work result and reconciled graph"
        )
    return {
        "pending_actions": pending,
        "siso_pending_steps": pending,
        "siso_skipped_actions": skipped,
        "graph_action_count": graph_total,
    }


def parse_pending_actions(output: str) -> int:
    """Count actual pending work; contradictory summaries fail closed."""
    summary = parse_siso_summary(output)
    if summary is not None:
        if NO_WORK.search(output) and summary["pending_actions"] != 0:
            raise RemainingWorkError(
                "contradictory dry-run output: explicit no-work marker conflicts "
                "with nonzero Siso pending steps"
            )
        return summary["pending_actions"]
    if NO_WORK.search(output):
        return 0
    pairs = [(int(a), int(b)) for a, b in PAIR.findall(output)]
    if not pairs:
        raise RemainingWorkError(
            "dry-run output has no Ninja action rows, explicit no-work result, "
            "or unambiguous completed Siso summary/skip counters"
        )
    totals = {total for _, total in pairs}
    if len(totals) != 1:
        raise RemainingWorkError(f"dry-run action denominator changed within one measurement: {sorted(totals)}")
    total = next(iter(totals))
    if total <= 0 or any(done <= 0 or done > total for done, _ in pairs):
        raise RemainingWorkError("dry-run emitted invalid action progress rows")
    return len(pairs)


def _tail(output: str, limit: int = 30) -> str:
    lines = output.splitlines()
    return "\n".join(line[:500] for line in lines[-limit:]) or "<no output>"


def _refresh_gn_manifest(*, source_root: Path, output_dir: Path,
                         args_gn: Path, timeout_seconds: int) -> tuple[str, str]:
    """Refresh GN directly so Siso's no-build mode doesn't own regeneration.

    A stale GN manifest can become Siso's only scheduled node during autoninja
    -n. The pinned Siso then repeats manifest regeneration and returns a
    successful zero-step summary without measuring the requested target. Retrying
    the same dry run repeats the condition. GN generation does not compile target
    actions, so run it explicitly and measure in a separate Siso invocation.
    """
    gn = source_root / "buildtools" / "linux64" / "gn"
    if not gn.is_file() or not gn.exists():
        discovered = shutil.which("gn")
        if discovered:
            gn = Path(discovered)
        else:
            raise RemainingWorkError(
                f"GN executable not found at {source_root / 'buildtools/linux64/gn'} or on PATH"
            )
    if gn.is_symlink() or not gn.is_file():
        raise RemainingWorkError(f"GN executable is missing/unsafe: {gn}")

    args_before = sha256_file(args_gn)
    try:
        output_arg = output_dir.relative_to(source_root).as_posix()
    except ValueError:
        output_arg = str(output_dir)
    command = [str(gn), "gen", output_arg]
    print("SANDIUM_PENDING_WORK_GN_GEN=START")
    try:
        run = subprocess.run(command, cwd=source_root, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace",
                             timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RemainingWorkError(
            f"GN manifest generation timed out after {timeout_seconds}s; compile was not started"
        ) from exc
    output = run.stdout or ""
    print(f"SANDIUM_PENDING_WORK_GN_GEN_EXIT={run.returncode}")
    print(f"SANDIUM_PENDING_WORK_GN_GEN_OUTPUT_LINES={len(output.splitlines())}")
    if run.returncode != 0:
        raise RemainingWorkError(
            f"GN manifest generation failed with exit {run.returncode}; tail: {_tail(output)}"
        )
    graph = output_dir / "build.ninja"
    if not graph.is_file() or graph.is_symlink() or graph.stat().st_size == 0:
        raise RemainingWorkError("GN generation did not leave a valid build.ninja")
    if not args_gn.is_file() or args_gn.is_symlink() or args_gn.stat().st_size == 0:
        raise RemainingWorkError("args.gn disappeared or became unsafe during GN generation")
    args_after = sha256_file(args_gn)
    if args_after != args_before:
        raise RemainingWorkError("args.gn changed during GN manifest generation")
    graph_hash = sha256_file(graph)
    print("SANDIUM_PENDING_WORK_GN_GEN=PASS")
    print(f"SANDIUM_PENDING_WORK_GN_GRAPH_SHA256={graph_hash}")
    print(f"SANDIUM_PENDING_WORK_ARGS_SHA256={args_after}")
    return graph_hash, args_after


def measure(*, source_root: Path, output_dir: Path, stage: int, phase: str,
            report_path: Path, timeout_seconds: int) -> dict[str, Any]:
    graph, args_gn, stamp = (output_dir / "build.ninja", output_dir / "args.gn",
                             output_dir / "build.ninja.stamp")
    for path in (graph, args_gn, stamp):
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise RemainingWorkError(f"required restored build input missing/unsafe: {path}")
    if timeout_seconds <= 0:
        raise RemainingWorkError("timeout must be positive")
    autoninja = shutil.which("autoninja")
    if not autoninja:
        raise RemainingWorkError("autoninja not found; cannot measure pending work safely")

    # Do not run GN generation over a restored output snapshot: it changes the
    # generator stamp and can make restored objects appear dirty before Siso reuse.
    graph_before, args_before = sha256_file(graph), sha256_file(args_gn)
    stamp_before = (sha256_file(stamp), stamp.stat().st_size, stamp.stat().st_mtime_ns)
    print("SANDIUM_PENDING_WORK_GN_GEN=SKIPPED_RESTORED_GRAPH")
    print(f"SANDIUM_PENDING_WORK_GN_GRAPH_SHA256={graph_before}")
    print(f"SANDIUM_PENDING_WORK_ARGS_SHA256={args_before}")
    print("SANDIUM_PENDING_WORK_STAMP_SHA256=" + stamp_before[0])

    command = [autoninja, "-strict_remote", "-n", "-C", str(output_dir), "-j80", "chrome_public_apk"]
    try:
        run = subprocess.run(command, cwd=source_root, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace",
                             timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RemainingWorkError(
            f"no-build dry run timed out after {timeout_seconds}s; compile was not started"
        ) from exc
    output = run.stdout or ""
    print("SANDIUM_PENDING_WORK_DRY_RUN_ATTEMPT=1/1")
    print(f"SANDIUM_PENDING_WORK_DRY_RUN_EXIT={run.returncode}")
    print(f"SANDIUM_PENDING_WORK_DRY_RUN_OUTPUT_LINES={len(output.splitlines())}")
    if run.returncode != 0:
        raise RemainingWorkError(f"no-build dry run failed with exit {run.returncode}; tail: {_tail(output)}")

    graph_after, args_after = sha256_file(graph), sha256_file(args_gn)
    stamp_after = (sha256_file(stamp), stamp.stat().st_size, stamp.stat().st_mtime_ns)
    if graph_after != graph_before or args_after != args_before or stamp_after != stamp_before:
        raise RemainingWorkError(
            "restored build.ninja, args.gn, or build.ninja.stamp changed during dry run; "
            "Siso attempted manifest regeneration, so compilation must not start: "
            f"before_graph={graph_before} after_graph={graph_after} "
            f"before_args={args_before} after_args={args_after} "
            f"before_stamp={stamp_before} after_stamp={stamp_after}; tail: {_tail(output)}"
        )
    try:
        count = parse_pending_actions(output)
        summary = parse_siso_summary(output)
    except RemainingWorkError as exc:
        print(f"SANDIUM_PENDING_WORK_DRY_RUN_PARSE_ERROR={exc}")
        print("SANDIUM_PENDING_WORK_DRY_RUN_SAMPLE_BEGIN")
        lines = output.splitlines()
        for line in lines[:30]:
            print(line[:500])
        if len(lines) > 30:
            print(f"... {len(lines) - 30} additional dry-run lines omitted ...")
            for line in lines[-10:]:
                print(line[:500])
        print("SANDIUM_PENDING_WORK_DRY_RUN_SAMPLE_END")
        raise

    if summary is not None:
        graph_total, skipped = summary["graph_action_count"], summary["siso_skipped_actions"]
        policy = "SISO_STEPS_ALREADY_EXCLUDES_SKIP"
    elif NO_WORK.search(output):
        edges = [int(n) for n in SISO_EDGE.findall(output)]
        if not edges or edges[-1] <= 0:
            raise RemainingWorkError("explicit no-work output lacks graph-edge count; continuity cannot be verified")
        graph_total, skipped, policy = edges[-1], edges[-1], "EXPLICIT_NO_WORK_ALL_GRAPH_NODES_UP_TO_DATE"
    else:
        pairs = [(int(a), int(b)) for a, b in PAIR.findall(output)]
        totals = {total for _, total in pairs}
        if len(totals) != 1:
            raise RemainingWorkError("plain Ninja dry-run has no stable graph denominator")
        graph_total, skipped, policy = next(iter(totals)), 0, "NINJA_PENDING_ROWS_VERSUS_GRAPH_TOTAL"

    if graph_total <= 0 or count > graph_total:
        raise RemainingWorkError(f"invalid measured pending/graph totals: pending={count}, graph_total={graph_total}")
    report = {
        "schema": 1, "stage": stage, "phase": phase,
        "pending_actions": count,
        "siso_pending_steps": summary["siso_pending_steps"] if summary else None,
        "siso_skipped_actions": skipped,
        "graph_action_count": graph_total,
        "pending_count_policy": policy,
        "build_ninja_sha256": graph_before,
        "args_gn_sha256": args_before,
        "build_ninja_stamp_sha256": stamp_before[0],
        "measurement": "restored_graph_autoninja_strict_remote_dry_run",
        "compile_started": False,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"SANDIUM_PENDING_COMPILE_ACTIONS_{phase.upper()}={count}")
    print(f"SANDIUM_TOTAL_ACTION_COUNT={count}")
    print(f"SANDIUM_GRAPH_ACTION_COUNT_{phase.upper()}={graph_total}")
    print(f"SANDIUM_SKIPPED_GRAPH_ACTIONS_{phase.upper()}={skipped}")
    print(f"SANDIUM_PENDING_COMPILE_COUNTING_POLICY={policy}")
    print(f"SANDIUM_PENDING_COMPILE_GRAPH_SHA256={graph_before}")
    print("SANDIUM_PENDING_COMPILE_MEASUREMENT=DRY_RUN_ONLY")
    print("SANDIUM_PENDING_WORK_GRAPH_STABLE=PASS")
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-root", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    p.add_argument("--phase", choices=("start", "end"), required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--timeout-seconds", type=int, default=180)
    a = p.parse_args()
    try:
        if a.timeout_seconds <= 0:
            raise RemainingWorkError("timeout must be positive")
        measure(source_root=a.source_root.resolve(), output_dir=a.output_dir.resolve(),
                stage=a.stage, phase=a.phase, report_path=a.report.resolve(),
                timeout_seconds=a.timeout_seconds)
        return 0
    except (RemainingWorkError, OSError, ValueError) as exc:
        print("SANDIUM_PENDING_COMPILE_MEASUREMENT=FAIL " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
