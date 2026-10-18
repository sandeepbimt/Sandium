#!/usr/bin/env python3
"""Package and independently verify a completed SANDIUM Android ARM64 Debug APK."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = CORE_DIR.parent.parent
BUILD_STATE_TOOL = CORE_DIR / "ci" / "siso" / "build_state.py"
OUT_DIR = CORE_DIR.parent / "out" / "android_Debug_arm64"
APK_SOURCE = OUT_DIR / "apks" / "Bravearm64.apk"
STATE_SOURCE = OUT_DIR / ".sandium-build-state.json"
CHROMIUM_VERSION_PIN = "156.1.100.0"
TARGET = "android-arm64-debug-apk"
OUTPUT_DIR = PROJECT_DIR / "sandium-output"
APK_DEST = OUTPUT_DIR / "Brave-debug-arm64.apk"


class PackageError(RuntimeError):
    pass


def run(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except OSError as exc:
        raise PackageError(f"Cannot run {command[0]}: {exc}") from exc
    if result.returncode:
        raise PackageError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}"
        )
    return result.stdout


def find_tool(name: str) -> Path:
    sdk = CORE_DIR.parent / "third_party" / "android_sdk"
    matches = sorted(path for path in sdk.rglob(name) if path.is_file() and path.stat().st_mode & 0o111)
    if not matches:
        raise PackageError(f"Required Android SDK tool not found: {name} under {sdk}")
    return matches[0]


def parse_chromium_version() -> str:
    version_file = CORE_DIR.parent / "chrome" / "VERSION"
    if not version_file.is_file():
        raise PackageError(f"Chromium VERSION file missing: {version_file}")
    fields = dict(re.findall(r"^(MAJOR|MINOR|BUILD|PATCH)=(\d+)$", version_file.read_text(), re.M))
    if set(fields) != {"MAJOR", "MINOR", "BUILD", "PATCH"}:
        raise PackageError("Could not parse Chromium VERSION file")
    return ".".join(fields[key] for key in ("MAJOR", "MINOR", "BUILD", "PATCH"))


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_SOURCE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"Compile-state record is missing or invalid: {exc}") from exc
    if not isinstance(state, dict):
        raise PackageError("Compile-state record must be a JSON object")
    if state.get("schema") != 1 or state.get("target") != TARGET:
        raise PackageError("Compile-state record is for an unsupported target")
    if state.get("chromium_version") != CHROMIUM_VERSION_PIN:
        raise PackageError("Compile-state record has an unexpected Chromium version")
    if not state.get("build_input_fingerprint") or not state.get("gn_args_sha256"):
        raise PackageError("Compile-state record lacks source/GN fingerprints")
    return state


def verify_zip(apk: Path) -> None:
    try:
        with zipfile.ZipFile(apk) as archive:
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise PackageError(f"APK ZIP integrity check failed at {corrupt_member}")
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageError(f"Output is not a valid APK ZIP: {exc}") from exc
    if "AndroidManifest.xml" not in names:
        raise PackageError("APK is missing AndroidManifest.xml")
    if not any(name.startswith("classes") and name.endswith(".dex") for name in names):
        raise PackageError("APK is missing DEX bytecode")


def package(commit: str, stage: str) -> dict[str, Any]:
    if not APK_SOURCE.is_file() or APK_SOURCE.stat().st_size < 1_000_000:
        raise PackageError(f"Expected complete APK not present at {APK_SOURCE}")
    chromium_version = parse_chromium_version()
    if chromium_version != CHROMIUM_VERSION_PIN:
        raise PackageError(
            f"Expected Chromium {CHROMIUM_VERSION_PIN}, found {chromium_version}"
        )

    state = load_state()
    # A CI-only commit may change CIRCLE_SHA1 without changing browser build inputs.
    # Require the live Git checkout to match this run and compare the actual
    # build-input/GN fingerprints instead of rejecting reusable Chromium output.
    fingerprint_text = run([
        sys.executable, str(BUILD_STATE_TOOL), "fingerprint", "--commit", commit
    ])
    fingerprint_values = dict(
        line.split("=", 1) for line in fingerprint_text.splitlines() if "=" in line
    )
    if state.get("build_input_fingerprint") != fingerprint_values.get("BUILD_INPUT_FINGERPRINT"):
        raise PackageError("Cached compile output was built from different browser inputs")
    if state.get("gn_args_sha256") != fingerprint_values.get("GN_ARGS_SHA256"):
        raise PackageError("Cached compile output has different effective GN arguments")
    if state.get("compile_complete") is not True:
        raise PackageError("Compile-state record does not indicate a completed build")

    aapt = find_tool("aapt")
    apksigner = find_tool("apksigner")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(APK_SOURCE, APK_DEST)
    badging = run([str(aapt), "dump", "badging", str(APK_DEST)])
    signature_report = run([str(apksigner), "verify", "--verbose", str(APK_DEST)])
    verify_zip(APK_DEST)

    package_match = re.search(r"^package: name='([^']+)'", badging, re.M)
    if not package_match:
        raise PackageError("Could not parse app package name from aapt badging")
    if "Verified using v1 scheme" not in signature_report and "Verified using v2 scheme" not in signature_report and "Verified using v3 scheme" not in signature_report and "Verified using v4 scheme" not in signature_report:
        # apksigner's wording can change; successful exit is the primary guarantee.
        print("APK_SIGNER_REPORT_NOTE=successful exit; scheme wording was not recognized")

    (OUTPUT_DIR / "apk-badging.txt").write_text(badging, encoding="utf-8")
    (OUTPUT_DIR / "apksigner-verify.txt").write_text(signature_report, encoding="utf-8")
    digest = hashlib.sha256(APK_DEST.read_bytes()).hexdigest()
    size = APK_DEST.stat().st_size
    (OUTPUT_DIR / "sha256.txt").write_text(f"{digest}  {APK_DEST.name}\n", encoding="utf-8")

    # Mark the build verified only after the artifact passes actual APK checks.
    run([
        sys.executable, str(BUILD_STATE_TOOL), "record",
        "--commit", commit, "--stage", stage,
        "--compile-complete", "true", "--artifact-verified", "true",
    ])
    state = json.loads(STATE_SOURCE.read_text(encoding="utf-8"))
    shutil.copy2(STATE_SOURCE, OUTPUT_DIR / "build-state.json")
    manifest = {
        "source_commit": state["source_commit"],
        "brave_core_commit": state["brave_core_commit"],
        "chromium_commit": state["chromium_commit"],
        "chromium_version": chromium_version,
        "android_target": "arm64",
        "build_config": "Debug",
        "artifact": APK_DEST.name,
        "apk_bytes": size,
        "apk_sha256": digest,
        "app_package_name": package_match.group(1),
        "apk_zip_integrity": "PASS",
        "dalvik_bytecode_present": True,
        "apksigner_verification": "PASS",
        "artifact_verified": state.get("artifact_verified") is True,
        "build_input_fingerprint": state["build_input_fingerprint"],
        "gn_args_sha256": state["gn_args_sha256"],
        "last_compile_stage": stage,
        "remote_execution_usage": {
            "proven_from_siso_stats": state.get("remote_backend_proven", False),
            "metrics": state.get("remote_metrics", {}),
        },
    }
    (OUTPUT_DIR / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"APK_BYTES={size}")
    print(f"APK_SHA256={digest}")
    print(f"APK_PACKAGE={package_match.group(1)}")
    print("APK_ZIP_INTEGRITY=PASS")
    print("APK_SIGNATURE_VERIFICATION=PASS")
    print(f"APK_ARTIFACT_VERIFIED={str(manifest['artifact_verified']).lower()}")
    print(f"SISO_REMOTE_BACKEND_PROVEN={str(state.get('remote_backend_proven', False)).lower()}")
    print(f"SISO_REMOTE_ACTIONS={state.get('remote_metrics', {}).get('remote_actions', 0)}")
    print(f"SISO_REMOTE_CACHE_HITS={state.get('remote_metrics', {}).get('remote_cache_hits', 0)}")
    print(f"SISO_REMOTE_CACHE_WRITES={state.get('remote_metrics', {}).get('remote_cache_writes', 0)}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--stage", choices=("stage-2", "stage-3", "stage-4"), required=True)
    args = parser.parse_args()
    try:
        package(args.commit, args.stage)
    except PackageError as exc:
        print(f"APK_PACKAGE_VERIFICATION=FAIL ({exc})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
