#!/usr/bin/env python3
"""Fail-closed comparison of the full dry-run graph and sampled reuse graph."""
from __future__ import annotations

import argparse
import re
import sys


SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate(stage: int, full_graph_sha: str, reuse_graph_sha: str) -> tuple[int, str]:
    if not SHA256.fullmatch(full_graph_sha):
        return 59, "SANDIUM_PREFLIGHT_GRAPH_CONTINUITY=FAIL invalid full dry-run graph SHA-256"
    if not reuse_graph_sha:
        if stage > 1:
            return 59, "SANDIUM_PREFLIGHT_GRAPH_CONTINUITY=FAIL continuation stage has no sampled-reuse graph hash"
        return 0, "SANDIUM_PREFLIGHT_GRAPH_CONTINUITY=SKIP no restored compiled outputs to verify"
    if not SHA256.fullmatch(reuse_graph_sha):
        return 59, "SANDIUM_PREFLIGHT_GRAPH_CONTINUITY=FAIL invalid sampled-reuse graph SHA-256"
    if full_graph_sha != reuse_graph_sha:
        return 59, f"SANDIUM_PREFLIGHT_GRAPH_CONTINUITY=FAIL full_dry_run={full_graph_sha} sampled_reuse={reuse_graph_sha}"
    return 0, f"SANDIUM_PREFLIGHT_GRAPH_CONTINUITY=PASS sha256={full_graph_sha}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--full-graph-sha", required=True)
    parser.add_argument("--reuse-graph-sha", default="")
    args = parser.parse_args()
    rc, message = validate(args.stage, args.full_graph_sha, args.reuse_graph_sha)
    print(message, file=sys.stdout if rc == 0 else sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
