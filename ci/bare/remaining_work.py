#!/usr/bin/env python3
"""Measure pending actions after an explicit GN manifest refresh."""
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


class RemainingWorkError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def parse_pending_actions(output: str) -> int:
    """Count pending actions from supported Ninja rows or Siso's completed summary."""
    pairs = [(int(a), int(b)) for a, b in PAIR.findall(output)]
    if not pairs:
        if NO_WORK.search(output):
            return 0
        # Siso's -n output can print commands and finish with aggregate counters
        # instead of Ninja [done/total] rows. Accept only a completed summary
        # plus its explicit skip counter; never infer zero from silence.
        summaries = [int(n) for n in SISO_SUCCESS.findall(output)]
        skipped = [int(n) for n in SISO_SKIP.findall(output)]
        if len(summaries) == 1 and len(skipped) == 1:
            steps, skip_count = summaries[0], skipped[0]
            if steps <= 0 or skip_count > steps:
                raise RemainingWorkError(
                    f"invalid Siso dry-run counters: steps={steps}, skip={skip_count}"
                )
            return steps - skip_count
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
    # Count emitted command rows, not the denominator: Siso's denominator can
    # describe the whole graph while this dry run emits only dirty actions.
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
    graph = output_dir / "build.ninja"
    args_gn = output_dir / "args.gn"
    for path in (graph, args_gn):
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise RemainingWorkError(f"required generated build input missing/unsafe: {path}")
    if timeout_seconds <= 0:
        raise RemainingWorkError("timeout must be positive")
    autoninja = shutil.which("autoninja")
    if not autoninja:
        raise RemainingWorkError("autoninja not found; cannot measure pending work safely")

    # Do not let autoninja -n handle GN regeneration: Siso can repeatedly
    # schedule just the regeneration node and report "Build Succeeded: 0 steps".
    # GN gen does not compile targets. Measure only after it returns successfully.
    graph_before, args_hash = _refresh_gn_manifest(
        source_root=source_root, output_dir=output_dir, args_gn=args_gn,
        timeout_seconds=timeout_seconds,
    )
    command = [autoninja, "-n", "-C", str(output_dir), "chrome_public_apk"]
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
        raise RemainingWorkError(
            f"no-build dry run failed with exit {run.returncode}; tail: {_tail(output)}"
        )

    # Siso must not alter the graph while measuring it. If it regenerates again,
    # fail with a concrete diagnostic instead of retrying the same operation or
    # interpreting a zero-step summary as proof that no compilation remains.
    graph_after = sha256_file(graph)
    args_after = sha256_file(args_gn)
    if graph_after != graph_before or args_after != args_hash:
        print("SANDIUM_PENDING_WORK_GRAPH_CHANGED_DURING_DRY_RUN=true")
        raise RemainingWorkError(
            "GN/Ninja graph or args changed during pending-work measurement; "
            f"before_graph={graph_before} after_graph={graph_after} "
            f"before_args={args_hash} after_args={args_after}; tail: {_tail(output)}"
        )
    try:
        count = parse_pending_actions(output)
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

    report = {
        "schema": 1, "stage": stage, "phase": phase,
        "pending_actions": count,
        "build_ninja_sha256": graph_before,
        "args_gn_sha256": args_hash,
        "measurement": "explicit_gn_gen_then_autoninja_dry_run",
        "compile_started": False,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"SANDIUM_PENDING_COMPILE_ACTIONS_{phase.upper()}={count}")
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
