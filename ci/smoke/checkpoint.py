#!/usr/bin/env python3
"""Run one isolated SANDIUM smoke stage and verify cross-job checkpoint continuity.

This mode exercises CI orchestration only. It never syncs Chromium, compiles it,
or creates/publishes a production APK.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", type=int, choices=range(1, 6))
    parser.add_argument("previous_stage", type=int, choices=range(0, 5))
    parser.add_argument("--root", type=Path, default=Path("smoke-state"))
    args = parser.parse_args()
    if args.previous_stage != args.stage - 1:
        raise SystemExit("Invalid smoke stage dependency")

    workflow_id = os.environ.get("CIRCLE_WORKFLOW_ID", "")
    commit = os.environ.get("CIRCLE_SHA1", "")
    if not workflow_id or not commit:
        raise SystemExit("Smoke test requires CircleCI workflow and commit identity")

    args.root.mkdir(parents=True, exist_ok=True)
    previous_digest = None
    if args.previous_stage:
        previous_path = args.root / f"stage-{args.previous_stage}.json"
        if not previous_path.is_file():
            raise SystemExit(f"Missing prior smoke checkpoint: {previous_path}")
        previous_bytes = previous_path.read_bytes()
        previous = json.loads(previous_bytes)
        if (
            previous.get("mode") != "smoke"
            or previous.get("stage") != args.previous_stage
            or previous.get("workflow_id") != workflow_id
            or previous.get("commit") != commit
            or previous.get("result") != "PASS"
        ):
            raise SystemExit("Prior smoke checkpoint belongs to a different stage, workflow, or commit")
        previous_digest = hashlib.sha256(previous_bytes).hexdigest()

    if args.stage == 1:
        config = (REPO_ROOT / ".circleci/config.yml").read_text(encoding="utf-8")
        required = (
            "run_sandium_android_apk_build:",
            "run_sandium_ci_smoke_test:",
            "when: << pipeline.parameters.run_sandium_android_apk_build >>",
            "when: << pipeline.parameters.run_sandium_ci_smoke_test >>",
            "stage-1-source-foundation:",
            "stage-5-verify-publish:",
            "pnpm run build Debug",
        )
        missing = [token for token in required if token not in config]
        if missing:
            raise SystemExit("Mode 2/full-build contract is missing: " + ", ".join(missing))
        if "default: false" not in config:
            raise SystemExit("Both build modes must remain opt-in")
        print("SMOKE_MODE1_ISOLATION=PASS")
        print("MODE2_FULL_CHROMIUM_BUILD_PRESERVED=PASS")

    record = {
        "schema": 1,
        "mode": "smoke",
        "stage": args.stage,
        "workflow_id": workflow_id,
        "commit": commit,
        "previous_sha256": previous_digest,
        "result": "PASS",
        "real_chromium_sync": False,
        "real_chromium_compile": False,
        "production_apk_created": False,
    }
    path = args.root / f"stage-{args.stage}.json"
    path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"SANDIUM_SMOKE_STAGE_{args.stage}=PASS")
    print("SANDIUM_SMOKE_MODE_ONLY=true")

    if args.stage == 5:
        prior_digest = None
        for number in range(1, 6):
            checkpoint_path = args.root / f"stage-{number}.json"
            if not checkpoint_path.is_file():
                raise SystemExit(f"Missing required stage checkpoint: {checkpoint_path}")
            raw = checkpoint_path.read_bytes()
            saved = json.loads(raw)
            if (
                saved.get("mode") != "smoke"
                or saved.get("stage") != number
                or saved.get("workflow_id") != workflow_id
                or saved.get("commit") != commit
                or saved.get("result") != "PASS"
            ):
                raise SystemExit(f"Invalid stage {number} checkpoint")
            if saved.get("previous_sha256") != prior_digest:
                raise SystemExit(f"Broken checkpoint handover chain at stage {number}")
            if saved.get("production_apk_created") is not False:
                raise SystemExit("Smoke fixture must never be represented as a production APK")
            prior_digest = hashlib.sha256(raw).hexdigest()
        print("SANDIUM_SMOKE_FIVE_STAGE_HANDOVER=PASS")
        print("SANDIUM_SMOKE_TEST_ONLY_NO_APK=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
