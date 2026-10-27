#!/usr/bin/env python3
"""Regression tests for the precompile graph-continuity gate."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = HERE / "preflight_graph_continuity.py"
A = "a" * 64
B = "b" * 64


def run(stage: int, full: str, reuse: str | None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(GATE), "--stage", str(stage), "--full-graph-sha", full]
    if reuse is not None:
        cmd.extend(["--reuse-graph-sha", reuse])
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def main() -> None:
    matched = run(2, A, A)
    assert matched.returncode == 0 and "CONTINUITY=PASS" in matched.stdout, matched

    mismatch = run(2, A, B)
    assert mismatch.returncode == 59 and "CONTINUITY=FAIL" in mismatch.stderr, mismatch

    missing_continuation = run(2, A, None)
    assert missing_continuation.returncode == 59 and "no sampled-reuse graph hash" in missing_continuation.stderr, missing_continuation

    cold_stage = run(1, A, None)
    assert cold_stage.returncode == 0 and "CONTINUITY=SKIP" in cold_stage.stdout, cold_stage

    invalid_full = run(1, "not-a-hash", A)
    assert invalid_full.returncode == 59 and "invalid full dry-run" in invalid_full.stderr, invalid_full

    invalid_reuse = run(2, A, "not-a-hash")
    assert invalid_reuse.returncode == 59 and "invalid sampled-reuse" in invalid_reuse.stderr, invalid_reuse

    print("PREFLIGHT_GRAPH_CONTINUITY_TESTS=PASS cases=6")


if __name__ == "__main__":
    main()
