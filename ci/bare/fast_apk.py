#!/usr/bin/env python3
"""Validate a cached Sandium APK and propagate stage state without source checkout.

A cache hit checks the recorded signature-verification evidence and current
pinned identity. It does not claim to rerun apksigner. Exit 0 means a verified
APK was accepted, 1 means a cache miss, and 2 means a purported completed APK
failed validation and must not be silently trusted.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import read_pins, IdentityError

MIN_APK_BYTES = 1_000_000

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing/unsafe JSON: {path}")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"JSON is not an object: {path}")
    return result

def read_status(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing/unsafe status: {path}")
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result

def verify_apk(root: Path, control: Path, candidate_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    apk = root / "sandium-output" / "Sandium-debug-arm64.apk"
    manifest_path = root / "sandium-output" / "build-manifest.json"
    if not apk.is_file() or apk.is_symlink() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("APK or build manifest missing/unsafe")
    manifest = read_json(manifest_path)
    pins = read_pins(control)
    expected = {
        "bare_commit": pins["bare_commit"],
        "chromium_commit": pins["chromium_commit"],
        "chromium_version": pins["chromium_version"],
        "target_abi": pins["target_abi"],
        "build_config": pins["build_config"],
        "artifact_name": pins["artifact_name"],
        "source": "Bare",
        "target": "android-arm64-debug-apk",
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f"cached APK manifest pin mismatch: {key}")
    ident = manifest.get("build_identity_sha256")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{64}", ident):
        raise ValueError("cached APK manifest has no valid build identity digest")
    size = apk.stat().st_size
    if size < MIN_APK_BYTES:
        raise ValueError("cached APK is unexpectedly small")
    digest = sha256(apk)
    if manifest.get("artifact_sha256") != digest or manifest.get("artifact_bytes") != size:
        raise ValueError("cached APK SHA-256/size differs from manifest")
    if manifest.get("artifact_verified") is not True or manifest.get("apksigner_verification") != "PASS":
        raise ValueError("cached APK lacks saved successful signature verification evidence")
    with zipfile.ZipFile(apk) as archive:
        if archive.testzip() is not None:
            raise ValueError("cached APK ZIP CRC verification failed")
        names = set(archive.namelist())
        if "AndroidManifest.xml" not in names or not any(n.startswith("classes") and n.endswith(".dex") for n in names):
            raise ValueError("cached APK lacks AndroidManifest.xml or DEX")
    ledger_path = candidate_dir / "ledger.json"
    ledger = read_json(ledger_path)
    status = read_status(candidate_dir / "status.txt")
    if ledger.get("artifact_verified") is not True or ledger.get("build_identity_sha256") != ident:
        raise ValueError("cached stage ledger does not match verified APK identity")
    if status.get("BUILD_FINISHED") != "true" or status.get("APK_ARCHIVE_VALID") != "true" or status.get("APK_SIGNATURE_VERIFIED") != "true":
        raise ValueError("cached stage status does not record completed archive/signature verification")
    if status.get("APK_SHA256") != digest or status.get("APK_SIZE_BYTES") != str(size) or status.get("BUILD_IDENTITY_SHA256") != ident:
        raise ValueError("cached status SHA-256/size/build identity differs from APK manifest")
    return manifest, ledger, status

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--workflow-id", default="unknown")
    args = parser.parse_args()
    root = args.root.resolve()
    control = Path(__file__).resolve().parents[2]
    current_stage = root / "ci-handoff" / f"stage-{args.stage}"
    stage_root = root / "ci-handoff"
    candidates = list(stage_root.glob("stage-*")) if stage_root.is_dir() else []
    # A separately cached, verified APK may originate from any completed stage
    # in any prior workflow. Prefer this workflow's predecessor when present,
    # then try the highest completed stage available in the restored cache.
    predecessor = f"stage-{args.stage - 1}"
    candidates = [
        p for p in candidates
        if p.is_dir() and (p / "ledger.json").is_file() and (p / "status.txt").is_file()
    ]
    def candidate_order(path: Path) -> tuple[int, int]:
        try:
            stage_number = int(path.name.removeprefix("stage-"))
        except ValueError:
            stage_number = 0
        return (0 if path.name == predecessor else 1, -stage_number)
    candidates.sort(key=candidate_order)
    if not candidates:
        print("SANDIUM_APK_FAST_PATH=MISS reason=no_saved_completed_stage")
        return 1
    failures = []
    for candidate in candidates:
        try:
            manifest, prior_ledger, prior_status = verify_apk(root, control, candidate)
            # The artifact identity/hash/signature record—not the prior stage number—
            # determines whether this cache is reusable. This allows every stage to
            # fast-exit when a verified APK exists in the dedicated cross-run cache.
            ledger = copy.deepcopy(prior_ledger)
            prior_bytes = (candidate / "ledger.json").read_bytes()
            ledger["previous_stage_ledger_sha256"] = hashlib.sha256(prior_bytes).hexdigest()
            ledger["stage"] = args.stage
            ledger["evidence"] = "VERIFIED_APK_REUSED_FROM_CACHE"
            ledger["state_handoff"] = {
                "schema": 1,
                "SANDIUM_STATE_HANDOFF": "SKIPPED_APK_ALREADY_VERIFIED",
                "source_stage": int(prior_status.get("STAGE", "0")),
                "source_workflow_id": args.workflow_id if args.stage > 1 else "prior-workflow-cache",
                "current_stage": args.stage,
                "current_workflow_id": args.workflow_id or "unknown",
                "build_identity_sha256": manifest["build_identity_sha256"],
                "local_compiled_outputs_restored": False,
                "reason": "Cached APK, manifest, ledger and saved signature-verification record were revalidated before full output-cache restore",
            }
            current_stage.mkdir(parents=True, exist_ok=True)
            (current_stage / "ledger.json").write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            apk = root / "sandium-output" / "Sandium-debug-arm64.apk"
            status = {
                "BUILD_FINISHED": "true",
                "APK_ARCHIVE_VALID": "true",
                "APK_SIGNATURE_VERIFIED": "true",
                "APK_SHA256": sha256(apk),
                "APK_SIZE_BYTES": str(apk.stat().st_size),
                "BUILD_IDENTITY_SHA256": manifest["build_identity_sha256"],
                "STAGE": str(args.stage),
                "WORKFLOW_ID": args.workflow_id or "unknown",
                "REUSED_VERIFIED_APK": "true",
            }
            (current_stage / "status.txt").write_text("".join(f"{k}={v}\n" for k, v in status.items()), encoding="utf-8")
            (root / "sandium-state-handoff-report.json").write_text(
                json.dumps(ledger["state_handoff"], indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print("SANDIUM_APK_FAST_PATH=PASS")
            print(f"SANDIUM_APK_FAST_PATH_SOURCE_STAGE={prior_status.get('STAGE', 'unknown')}")
            print(f"SANDIUM_APK_SHA256={status['APK_SHA256']}")
            print(f"SANDIUM_APK_BYTES={status['APK_SIZE_BYTES']}")
            print("SANDIUM_APK_SIGNATURE_CHECK=REUSED_SAVED_VERIFICATION_RECORD")
            return 0
        except (ValueError, OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            failures.append(f"{candidate.name}:{exc}")
    if args.stage > 1:
        # If the previous stage claims the APK is complete but its evidence fails,
        # stop rather than silently falling back to an expensive/corrupt rebuild.
        previous_status = root / "ci-handoff" / f"stage-{args.stage - 1}" / "status.txt"
        try:
            if read_status(previous_status).get("BUILD_FINISHED") == "true":
                print("SANDIUM_APK_FAST_PATH=FAIL " + "; ".join(failures), file=sys.stderr)
                return 2
        except (ValueError, OSError):
            pass
    print("SANDIUM_APK_FAST_PATH=MISS " + ("; ".join(failures) if failures else "no_compatible_candidate"))
    return 1

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IdentityError as exc:
        print("SANDIUM_APK_FAST_PATH=MISS reason=" + str(exc))
        raise SystemExit(1)
