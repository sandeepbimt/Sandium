#!/usr/bin/env python3
"""Validate Sandium's five-stage orchestration contract without starting a build.

This is a static gate, not a build runner. It deliberately does not invoke
Chromium, GN, Siso, BuildBuddy, CircleCI APIs, or artifact upload.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

STAGES = (
    {
        "stage": 1,
        "name": "source-foundation",
        "requires": [],
        "must_prove": ["pinned_source_identity", "patch_series_identity", "extension_checksum"],
        "may_compile": True,
    },
    {
        "stage": 2,
        "name": "toolchain-and-graph",
        "requires": [1],
        "must_prove": ["same_build_identity", "effective_gn_args", "siso_configuration"],
        "may_compile": True,
    },
    {
        "stage": 3,
        "name": "compile-and-ledger",
        "requires": [2],
        "must_prove": ["same_build_identity", "remote_execution", "stage_ledger"],
        "may_compile": True,
    },
    {
        "stage": 4,
        "name": "restore-and-continue",
        "requires": [3],
        "must_prove": ["same_build_identity", "previous_ledger", "remote_cache_hit_or_explicit_no_work"],
        "may_compile": True,
    },
    {
        "stage": 5,
        "name": "finalize-and-publish",
        "requires": [4],
        "must_prove": ["same_build_identity", "apk_integrity", "apk_manifest", "artifact_sha256"],
        "may_compile": True,
    },
)

FORBIDDEN_SIDE_EFFECTS = (
    "chromium_sync",
    "gn_generation",
    "compilation",
    "apk_packaging",
    "credential_mutation",
    "cache_deletion",
    "pipeline_trigger",
)


def validate() -> list[str]:
    errors: list[str] = []
    numbers = [stage["stage"] for stage in STAGES]
    if numbers != [1, 2, 3, 4, 5]:
        errors.append("stage numbers must be exactly 1 through 5 in order")
    names = [stage["name"] for stage in STAGES]
    if len(names) != len(set(names)):
        errors.append("stage names must be unique")
    for index, stage in enumerate(STAGES):
        expected = [] if index == 0 else [index]
        if stage["requires"] != expected:
            errors.append(f"stage {stage['stage']} must require {expected}")
        if not stage["must_prove"]:
            errors.append(f"stage {stage['stage']} has no evidence requirements")
    for required in ("remote_execution", "stage_ledger"):
        if required not in STAGES[2]["must_prove"]:
            errors.append(f"stage 3 must prove {required}")
    if "remote_cache_hit_or_explicit_no_work" not in STAGES[3]["must_prove"]:
        errors.append("stage 4 must prove cache reuse or explicitly verified no remaining work")
    for required in ("apk_integrity", "artifact_sha256"):
        if required not in STAGES[4]["must_prove"]:
            errors.append(f"stage 5 must prove {required}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the reviewed stage contract as JSON")
    args = parser.parse_args()
    errors = validate()
    if errors:
        for error in errors:
            print("SANDIUM_STAGE_CONTRACT_ERROR=" + error)
        return 1
    if args.json:
        print(json.dumps({"schema": 1, "stages": STAGES, "side_effects_during_validation": list(FORBIDDEN_SIDE_EFFECTS)}, indent=2))
    else:
        print("SANDIUM_STAGE_CONTRACT=PASS")
        print("SANDIUM_STAGE_COUNT=5")
        print("SANDIUM_BUILD_STARTED=no")
        print("SANDIUM_PIPELINE_TRIGGERED=no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
