#!/usr/bin/env python3
"""Measure pending build actions with a no-build Siso/Ninja dry run."""
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


def measure(*, source_root: Path, output_dir: Path, stage: int, phase: str,
            report_path: Path, timeout_seconds: int) -> dict[str, Any]:
    graph = output_dir / "build.ninja"
    args_gn = output_dir / "args.gn"
    for path in (graph, args_gn):
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise RemainingWorkError(f"required generated build input missing/unsafe: {path}")
    graph_before = sha256_file(graph)
    args_hash = sha256_file(args_gn)
    autoninja = shutil.which("autoninja")
    if not autoninja:
        raise RemainingWorkError("autoninja not found; cannot measure pending work safely")
    command = [autoninja, "-n", "-C", str(output_dir), "chrome_public_apk"]
    output = ""
    count = None
    # The pinned Siso version may use a no-build invocation only to regenerate
    # GN manifests, then report "Build Succeeded: 0 steps" without showing the
    # target's pending actions. Never interpret that as zero work. Retry as a
    # fresh process after regeneration, and accept only a parseable measurement.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        graph_before = sha256_file(graph)
        args_hash = sha256_file(args_gn)
        try:
            run = subprocess.run(command, cwd=source_root, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace",
                                 timeout=timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RemainingWorkError(
                f"no-build dry run attempt {attempt}/{max_attempts} timed out after {timeout_seconds}s"
            ) from exc
        output = run.stdout or ""
        print(f"SANDIUM_PENDING_WORK_DRY_RUN_ATTEMPT={attempt}/{max_attempts}")
        print(f"SANDIUM_PENDING_WORK_DRY_RUN_EXIT={run.returncode}")
        print(f"SANDIUM_PENDING_WORK_DRY_RUN_OUTPUT_LINES={len(output.splitlines())}")
        if run.returncode != 0:
            raise RemainingWorkError(
                f"no-build dry run attempt {attempt} failed with exit {run.returncode}"
            )
        try:
            count = parse_pending_actions(output)
            if sha256_file(graph) != graph_before or sha256_file(args_gn) != args_hash:
                raise RemainingWorkError(
                    "GN/Ninja graph or args changed during pending-work measurement"
                )
            break
        except RemainingWorkError as exc:
            regen_only = (
                "Regenerating ninja files" in output
                and re.search(r"Build Succeeded: 0 steps", output) is not None
            )
            if not regen_only or attempt == max_attempts:
                lines = output.splitlines()
                print(f"SANDIUM_PENDING_WORK_DRY_RUN_PARSE_ERROR={exc}")
                print("SANDIUM_PENDING_WORK_DRY_RUN_SAMPLE_BEGIN")
                for line in lines[:30]:
                    print(line[:500])
                if len(lines) > 30:
                    print(f"... {len(lines) - 30} additional dry-run lines omitted ...")
                    for line in lines[-10:]:
                        print(line[:500])
                print("SANDIUM_PENDING_WORK_DRY_RUN_SAMPLE_END")
                raise
            print(
                "SANDIUM_PENDING_WORK_DRY_RUN_RETRY=MANIFEST_REGENERATION_ONLY "
                f"next_attempt={attempt + 1}"
            )
    else:
        raise RemainingWorkError("pending-work dry run exhausted retries without a measurement")
    assert count is not None
    report = {
        "schema": 1, "stage": stage, "phase": phase,
        "pending_actions": count,
        "build_ninja_sha256": graph_before,
        "args_gn_sha256": args_hash,
        "measurement": "completed_autoninja_dry_run_action_rows",
        "compile_started": False,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"SANDIUM_PENDING_COMPILE_ACTIONS_{phase.upper()}={count}")
    print(f"SANDIUM_PENDING_COMPILE_GRAPH_SHA256={graph_before}")
    print(f"SANDIUM_PENDING_COMPILE_MEASUREMENT=DRY_RUN_ONLY")
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
