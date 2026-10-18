#!/usr/bin/env python3
"""Validate and checkpoint SANDIUM's resumable Brave/Chromium Android build.

This helper deliberately checks concrete generated files and APK hashes. It
never claims that a configured remote backend was used unless Siso's build
statistics report remote execution, remote-cache hits, or remote cache writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[2]
CHROMIUM_DIR = CORE_DIR.parent
OUTPUT_DIR = CHROMIUM_DIR / "out" / "android_Debug_arm64"
STATE_FILE = OUTPUT_DIR / ".sandium-build-state.json"
APK_FILE = OUTPUT_DIR / "apks" / "Bravearm64.apk"
CHROMIUM_TAG_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
TARGET = "android-arm64-debug-apk"
STATS_PATTERN = re.compile(
    r"local:(\d+)\s+remote:(\d+)\s+cache:(\d+)\s+"
    r"cache-write:(\d+)(?:\(err:(\d+)\))?\s+"
    r"fallback:(\d+)\s+retry:(\d+)\s+skip:(\d+)",
    re.IGNORECASE,
)
STATS_SUMMARY_PATTERN = re.compile(
    r"local:(?P<local>\d+)\s+remote:(?P<remote>\d+)\s+"
    r"cache:(?P<cache>\d+)(?:\s+skip:(?P<skip>\d+))?",
    re.IGNORECASE,
)
CACHE_WRITE_PATTERN = re.compile(
    r"cache-write:(?P<writes>\d+)(?:\(err:(?P<errors>\d+)\))?",
    re.IGNORECASE,
)
FALLBACK_RETRY_PATTERN = re.compile(
    r"fallback:(?P<fallback>\d+)\s+retry:(?P<retry>\d+)",
    re.IGNORECASE,
)


class StateError(RuntimeError):
    pass


def _git_head(path: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StateError(f"Cannot read Git HEAD for {path}: {exc}") from exc


def _chromium_version() -> str:
    """Return the version of the synced Chromium checkout, not Brave's pre-sync declaration.

    Brave's sync process may update the Chromium checkout independently of the
    initial package.json tag. The exact tag on the synced source commit is the
    build/cache identity; chrome/VERSION can be rewritten by downstream patches.
    """
    try:
        version = subprocess.check_output(
            ["git", "-C", str(CHROMIUM_DIR), "describe", "--tags", "--exact-match", "HEAD"],
            text=True,
            stderr=subprocess.PIPE,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StateError(f"Cannot identify the synced Chromium source tag: {exc}") from exc
    if not CHROMIUM_TAG_PATTERN.fullmatch(version):
        raise StateError(f"Chromium source tag is not a four-part numeric version: {version!r}")
    return version
def _gn_fingerprint() -> str:
    # GN's args.gn is the actual input generated and read by Brave's GN helper.
    # Do not depend on args_generated.gni, which is not a standard GN output.
    args_file = OUTPUT_DIR / "args.gn"
    if not args_file.is_file() or args_file.stat().st_size == 0:
        raise StateError(f"Required GN arguments missing or empty: {args_file}")

    arguments = args_file.read_text(encoding="utf-8")
    # use_remoteexec is Brave's build-command/runtime option (passed as
    # --use_remoteexec true), not a required GN arg in args.gn. Requiring it
    # here rejects otherwise correctly configured builds after compilation.
    # Siso itself is controlled through the real GN args below.
    expected_flags = {
        "use_siso": "true",
        "use_reclient": "false",
    }
    for name, value in expected_flags.items():
        if not re.search(rf"(?m)^\s*{re.escape(name)}\s*=\s*{value}\s*$", arguments):
            raise StateError(
                f"Effective GN args.gn must contain {name}={value}"
            )

    return hashlib.sha256(args_file.read_bytes()).hexdigest()


def _remote_metrics(log_path: str | None) -> tuple[dict[str, int], bool]:
    metrics = {
        "local_actions": 0,
        "remote_actions": 0,
        "remote_cache_hits": 0,
        "remote_cache_writes": 0,
        "remote_cache_write_errors": 0,
        "local_fallbacks": 0,
        "remote_retries": 0,
        "skipped_actions": 0,
    }
    if not log_path:
        return metrics, False
    path = Path(log_path)
    if not path.is_file():
        return metrics, False

    found = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        # Older Siso logs put all counters on one line. Current logs split the
        # cache-write/fallback/retry counters onto the following line.
        combined = STATS_PATTERN.search(line)
        if combined:
            found = True
            values = [int(value or 0) for value in combined.groups()]
            metrics.update(dict(zip(list(metrics), values)))
            continue

        summary = STATS_SUMMARY_PATTERN.search(line)
        if summary:
            found = True
            metrics["local_actions"] = int(summary.group("local"))
            metrics["remote_actions"] = int(summary.group("remote"))
            metrics["remote_cache_hits"] = int(summary.group("cache"))
            if summary.group("skip") is not None:
                metrics["skipped_actions"] = int(summary.group("skip"))

        cache_write = CACHE_WRITE_PATTERN.search(line)
        if cache_write:
            found = True
            metrics["remote_cache_writes"] = int(cache_write.group("writes"))
            if cache_write.group("errors") is not None:
                metrics["remote_cache_write_errors"] = int(cache_write.group("errors"))

        fallback_retry = FALLBACK_RETRY_PATTERN.search(line)
        if fallback_retry:
            found = True
            metrics["local_fallbacks"] = int(fallback_retry.group("fallback"))
            metrics["remote_retries"] = int(fallback_retry.group("retry"))

    return metrics, found


def _read_state() -> dict[str, Any] | None:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _build_input_fingerprint() -> str:
    """Hash tracked browser/build inputs while excluding CI-only control files."""
    try:
        entries = subprocess.check_output(
            ["git", "-C", str(CORE_DIR), "ls-tree", "-r", "-z", "--full-tree", "HEAD"],
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StateError(f"Cannot enumerate tracked build inputs: {exc}") from exc

    excluded_exact = {"docs/SANDIUM_PERSISTENT_CACHE_IMPLEMENTATION.md"}
    excluded_prefixes = (".circleci/", ".github/", "ci/siso/", "tests/ci/")
    digest = hashlib.sha256()
    for record in entries.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise StateError("Unexpected git ls-tree record while fingerprinting build inputs") from exc
        if path in excluded_exact or path.startswith(excluded_prefixes):
            continue
        digest.update(metadata)
        digest.update(b"\t")
        digest.update(raw_path)
        digest.update(b"\0")
    return digest.hexdigest()


def _context(expected_commit: str | None = None) -> dict[str, Any]:
    source_commit = _git_head(CORE_DIR)
    if expected_commit and source_commit != expected_commit:
        raise StateError(
            f"Checked-out Brave Core SHA {source_commit} does not match "
            f"CIRCLE_SHA1 {expected_commit}"
        )

    chromium_commit = _git_head(CHROMIUM_DIR)
    chromium_version = _chromium_version()
    return {
        "schema": 1,
        "source_commit": source_commit,
        "brave_core_commit": source_commit,
        "build_input_fingerprint": _build_input_fingerprint(),
        "chromium_commit": chromium_commit,
        "chromium_version": chromium_version,
        "target": TARGET,
        "build_config": "Debug",
        "output_dir": "src/out/android_Debug_arm64",
        "gn_args_sha256": _gn_fingerprint(),
    }


def _apk_info(required: bool) -> dict[str, Any]:
    if not APK_FILE.is_file():
        if required:
            raise StateError(f"Expected APK not found: {APK_FILE}")
        return {"apk_relative_path": None, "apk_sha256": None, "apk_bytes": None}
    size = APK_FILE.stat().st_size
    if required and size < 1_000_000:
        raise StateError(f"APK is unexpectedly small ({size} bytes): {APK_FILE}")
    return {
        "apk_relative_path": "apks/Bravearm64.apk",
        "apk_sha256": hashlib.sha256(APK_FILE.read_bytes()).hexdigest(),
        "apk_bytes": size,
    }


def _remote_backend_proven(metrics: dict[str, int]) -> bool:
    return (
        metrics.get("remote_actions", 0)
        + metrics.get("remote_cache_hits", 0)
        + metrics.get("remote_cache_writes", 0)
    ) > 0


def _valid_artifact_cache(expected_commit: str | None) -> dict[str, Any]:
    """Validate the lightweight packaged APK cache without restoring Chromium."""
    if expected_commit and _git_head(CORE_DIR) != expected_commit:
        raise StateError("Checked-out Brave Core SHA does not match CIRCLE_SHA1")

    project_dir = CORE_DIR.parent.parent
    output_dir = project_dir / "sandium-output"
    state_path = output_dir / "build-state.json"
    manifest_path = output_dir / "build-manifest.json"
    apk_path = output_dir / "Brave-debug-arm64.apk"
    badging_path = output_dir / "apk-badging.txt"
    signature_path = output_dir / "apksigner-verify.txt"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"Packaged APK cache metadata is missing or invalid: {exc}") from exc

    state_version = state.get("chromium_version")
    manifest_version = manifest.get("chromium_version")
    if (
        not isinstance(state_version, str)
        or not CHROMIUM_TAG_PATTERN.fullmatch(state_version)
        or state_version != manifest_version
    ):
        raise StateError("Packaged APK cache has missing or inconsistent synced Chromium version metadata")
    expected = {
        "schema": 1,
        "target": TARGET,
        "build_config": "Debug",
        "build_input_fingerprint": _build_input_fingerprint(),
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise StateError("Packaged APK cache does not match current browser inputs and Chromium build metadata")
    if state.get("compile_complete") is not True or state.get("artifact_verified") is not True:
        raise StateError("Packaged APK cache is not marked complete and verified")

    if not apk_path.is_file() or apk_path.stat().st_size < 1_000_000:
        raise StateError("Packaged APK cache is missing or unexpectedly small")
    digest = hashlib.sha256(apk_path.read_bytes()).hexdigest()
    size = apk_path.stat().st_size
    if digest != state.get("apk_sha256") or size != state.get("apk_bytes"):
        raise StateError("Packaged APK hash or size does not match build-state metadata")
    if digest != manifest.get("apk_sha256") or size != manifest.get("apk_bytes"):
        raise StateError("Packaged APK hash or size does not match build manifest")
    if (
        manifest.get("artifact_verified") is not True
        or manifest.get("apk_zip_integrity") != "PASS"
        or manifest.get("dalvik_bytecode_present") is not True
        or manifest.get("apksigner_verification") != "PASS"
    ):
        raise StateError("Build manifest lacks successful APK integrity/signature verification")
    if not badging_path.is_file() or not signature_path.is_file():
        raise StateError("Cached APK badging or signature-verification evidence is missing")
    try:
        with zipfile.ZipFile(apk_path) as archive:
            if archive.testzip() is not None:
                raise StateError("Cached APK ZIP CRC integrity failed")
            entries = set(archive.namelist())
            if "AndroidManifest.xml" not in entries:
                raise StateError("Cached APK is missing AndroidManifest.xml")
            if not any(name.startswith("classes") and name.endswith(".dex") for name in entries):
                raise StateError("Cached APK is missing DEX bytecode")
    except (OSError, zipfile.BadZipFile) as exc:
        raise StateError(f"Cached APK is not a valid ZIP/APK: {exc}") from exc

    badging = badging_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^package: name='([^']+)'", badging, re.M)
    if not match or match.group(1) != manifest.get("app_package_name"):
        raise StateError("Cached APK package metadata does not match the build manifest")
    if manifest.get("build_input_fingerprint") != expected["build_input_fingerprint"]:
        raise StateError("Cached build manifest has a stale browser-input fingerprint")
    return state


def _valid_state(mode: str, expected_commit: str | None) -> dict[str, Any]:
    if mode == "artifact-cache":
        return _valid_artifact_cache(expected_commit)

    context = _context(expected_commit)
    state = _read_state()
    if not state:
        raise StateError("Restored build-output cache has no SANDIUM state manifest")

    if mode == "cache":
        cache_keys = (
            "schema",
            "target",
            "chromium_commit",
            "chromium_version",
            "build_input_fingerprint",
            "gn_args_sha256",
        )
        if any(state.get(key) != context.get(key) for key in cache_keys):
            raise StateError("Restored build-output cache does not match current source, Chromium, or GN arguments")
        return state

    state = _read_state()
    if not state:
        raise StateError("No resumable SANDIUM build-state manifest")
    for key in (
        "schema",
        "build_input_fingerprint",
        "chromium_commit",
        "chromium_version",
        "target",
        "build_config",
        "gn_args_sha256",
    ):
        if state.get(key) != context.get(key):
            raise StateError(f"Build-state mismatch for {key}")

    if state.get("compile_complete") is not True:
        raise StateError("Previous compile window did not complete the APK build")

    apk_info = _apk_info(required=True)
    for key in ("apk_relative_path", "apk_sha256", "apk_bytes"):
        if state.get(key) != apk_info.get(key):
            raise StateError(f"Cached APK does not match its build-state record: {key}")

    metrics = state.get("remote_metrics")
    if not isinstance(metrics, dict):
        raise StateError("Build-state lacks Siso/BuildBuddy statistics")
    if mode == "verified" and state.get("artifact_verified") is not True:
        raise StateError("APK was compiled but did not pass the final signature/integrity checks")
    return state


def command_check(args: argparse.Namespace) -> int:
    try:
        state = _valid_state(args.mode, args.commit)
    except StateError as exc:
        print(f"BUILD_STATE_CHECK=MISS ({exc})")
        return 1
    print("BUILD_STATE_CHECK=PASS")
    print(f"BUILD_STATE_SOURCE_COMMIT={state['source_commit']}")
    print(f"BUILD_INPUT_FINGERPRINT={state['build_input_fingerprint']}")
    print(f"GN_ARGS_SHA256={state['gn_args_sha256']}")
    print(f"BUILD_STATE_CHROMIUM_COMMIT={state['chromium_commit']}")
    print(f"BUILD_STATE_APK_SHA256={state['apk_sha256']}")
    print(f"BUILD_STATE_ARTIFACT_VERIFIED={str(state.get('artifact_verified') is True).lower()}")
    return 0


def command_fingerprint(args: argparse.Namespace) -> int:
    try:
        context = _context(args.commit)
    except StateError as exc:
        print(f"BUILD_FINGERPRINT=FAIL ({exc})", file=sys.stderr)
        return 2
    print(f"BUILD_INPUT_FINGERPRINT={context['build_input_fingerprint']}")
    print(f"GN_ARGS_SHA256={context['gn_args_sha256']}")
    return 0


def command_record(args: argparse.Namespace) -> int:
    try:
        context = _context(args.commit)
        compile_complete = args.compile_complete == "true"
        apk_info = _apk_info(required=compile_complete)
        old = _read_state()
        parsed_metrics, _ = _remote_metrics(args.log)

        old_metrics: dict[str, int] = {}
        can_preserve_remote_proof = False
        if old and isinstance(old.get("remote_metrics"), dict):
            same_chromium_and_args = (
                old.get("build_input_fingerprint") == context["build_input_fingerprint"]
                and old.get("chromium_commit") == context["chromium_commit"]
                and old.get("chromium_version") == context["chromium_version"]
                and old.get("gn_args_sha256") == context["gn_args_sha256"]
                and old.get("target") == TARGET
            )
            same_apk = (
                apk_info.get("apk_sha256") is not None
                and old.get("apk_sha256") == apk_info.get("apk_sha256")
            )
            if same_chromium_and_args:
                old_metrics = {
                    key: int(old["remote_metrics"].get(key, 0))
                    for key in parsed_metrics
                }
                can_preserve_remote_proof = (
                    same_apk
                    and old.get("artifact_verified") is True
                    and _remote_backend_proven(old_metrics)
                )

        metrics = {
            key: old_metrics.get(key, 0) + parsed_metrics.get(key, 0)
            for key in parsed_metrics
        }
        artifact_verified = False
        if args.artifact_verified == "true":
            artifact_verified = True
        elif (
            args.artifact_verified == "auto"
            and compile_complete
            and can_preserve_remote_proof
        ):
            artifact_verified = True

        state: dict[str, Any] = {
            **context,
            "last_stage": args.stage,
            "compile_complete": compile_complete,
            "artifact_verified": artifact_verified,
            **apk_info,
            "remote_metrics": metrics,
            "remote_backend_proven": _remote_backend_proven(metrics),
        }
        if args.artifact_verified == "true" and (
            not compile_complete or apk_info.get("apk_sha256") is None
        ):
            raise StateError(
                "Cannot mark the APK verified without a complete build and a real APK file."
            )

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        print(f"BUILD_STATE_SAVED={STATE_FILE}")
        print(f"BUILD_STATE_STAGE={args.stage}")
        print(f"BUILD_COMPILE_COMPLETE={str(compile_complete).lower()}")
        print(f"BUILD_ARTIFACT_VERIFIED={str(artifact_verified).lower()}")
        print(f"SISO_REMOTE_ACTIONS={metrics['remote_actions']}")
        print(f"SISO_REMOTE_CACHE_HITS={metrics['remote_cache_hits']}")
        print(f"SISO_REMOTE_CACHE_WRITES={metrics['remote_cache_writes']}")
        print(f"SISO_REMOTE_BACKEND_PROVEN={str(state['remote_backend_proven']).lower()}")
        return 0
    except StateError as exc:
        print(f"BUILD_STATE_RECORD=FAIL ({exc})", file=sys.stderr)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="validate a restored build state")
    check.add_argument("--mode", choices=("artifact-cache", "cache", "compiled", "verified"), required=True)
    check.add_argument("--commit", default=None, help="expected Brave Core commit SHA")
    check.set_defaults(func=command_check)

    fingerprint = subparsers.add_parser("fingerprint", help="print validated build-input and GN fingerprints")
    fingerprint.add_argument("--commit", default=None, help="expected Brave Core commit SHA")
    fingerprint.set_defaults(func=command_fingerprint)

    record = subparsers.add_parser("record", help="record a completed or timed-out build window")
    record.add_argument("--commit", required=True)
    record.add_argument("--stage", required=True)
    record.add_argument("--compile-complete", choices=("true", "false"), required=True)
    record.add_argument("--artifact-verified", choices=("auto", "true", "false"), default="auto")
    record.add_argument("--log", default=None, help="Siso/autoninja log to collect remote statistics from")
    record.set_defaults(func=command_record)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
