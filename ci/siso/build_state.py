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

SISO_METRIC_LINE_PATTERN = re.compile(
    r"^SISO_(?P<metric>REMOTE_EXECUTED_ACTION_RECORDS|LOCAL_ACTION_RECORDS|"
    r"CACHED_ACTION_RECORDS|NO_EXEC_ACTION_RECORDS)=(?P<value>\d+)\s*$"
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
    # Brave's GN wrapper writes --gn key:value options into
    # args_generated.gni and imports that file from args.gn. In that normal
    # case the assignments are not repeated literally in args.gn, so validate
    # the imported generated file instead of rejecting a valid configuration.
    args_file = OUTPUT_DIR / "args.gn"
    if not args_file.is_file() or args_file.stat().st_size == 0:
        raise StateError(f"Required GN arguments missing or empty: {args_file}")

    arguments = args_file.read_text(encoding="utf-8")
    generated_file = OUTPUT_DIR / "args_generated.gni"
    import_line = 'import("//out/android_Debug_arm64/args_generated.gni")'
    generated_imported = re.search(
        r'(?m)^\s*import\(["\']//out/android_Debug_arm64/args_generated\.gni["\']\)\s*$',
        arguments,
    )

    if generated_imported:
        if not generated_file.is_file() or generated_file.stat().st_size == 0:
            raise StateError(
                f"args.gn imports generated GN arguments but file is missing or empty: {generated_file}"
            )
        effective_arguments = generated_file.read_text(encoding="utf-8")
        source_description = str(generated_file)
    else:
        # Keep support for valid/direct args.gn assignments (including tests
        # and manually maintained GN configs) where no generated file import
        # is present.
        effective_arguments = arguments
        source_description = str(args_file)

    # use_remoteexec is Brave's build-command/runtime option (passed as
    # --use_remoteexec true), not a required GN arg in args.gn.
    expected_flags = {
        "use_siso": "true",
        "use_reclient": "false",
    }
    for name, value in expected_flags.items():
        if not re.search(
            rf"(?m)^\s*{re.escape(name)}\s*=\s*{value}\s*$",
            effective_arguments,
        ):
            raise StateError(
                f"Effective GN arguments in {source_description} must contain {name}={value}"
            )

    # Fingerprint both the wrapper's args.gn and the imported generated file so
    # changes to CLI-provided GN settings invalidate saved build state.
    digest = hashlib.sha256(args_file.read_bytes())
    if generated_imported:
        digest.update(generated_file.read_bytes())
    return digest.hexdigest()


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
        "preflight_remote_actions": 0,
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

        preflight_match = re.match(
            r"^SANDIUM_PREFLIGHT_REMOTE_EXECUTED_ACTION_RECORDS=(\d+)$",
            line.strip(),
        )
        if preflight_match:
            found = True
            # Reused by every stage, not an action that should be summed per stage.
            metrics["preflight_remote_actions"] = max(
                metrics["preflight_remote_actions"], int(preflight_match.group(1))
            )

        # Actual compile-window actions remain separate from the preflight.
        siso_metric = SISO_METRIC_LINE_PATTERN.match(line.strip())
        if siso_metric:
            found = True
            value = int(siso_metric.group("value"))
            metric_name = siso_metric.group("metric")
            if metric_name == "REMOTE_EXECUTED_ACTION_RECORDS":
                metrics["remote_actions"] = value
            elif metric_name == "LOCAL_ACTION_RECORDS":
                metrics["local_actions"] = value
            elif metric_name == "NO_EXEC_ACTION_RECORDS":
                metrics["skipped_actions"] = value

    return metrics, found


def _read_state() -> dict[str, Any] | None:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _tree_records(ref: str) -> dict[str, bytes]:
    """Return tracked tree entries keyed by path for an exact Git ref."""
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(CORE_DIR), "ls-tree", "-r", "-z", "--full-tree", ref],
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StateError(f"Cannot enumerate tracked build inputs at {ref!r}: {exc}") from exc
    records: dict[str, bytes] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise StateError("Unexpected git ls-tree record while fingerprinting build inputs") from exc
        records[path] = metadata
    return records


def _build_input_fingerprint(
    ref: str = "HEAD",
    *,
    canonical_build_ts_from: str | None = None,
) -> str:
    """Hash build inputs, optionally ignoring only the CI timestamp-resume hook.

    The latter is a narrow migration bridge: an older cached state stores the
    tracked-tree fingerprint from before the source timestamp hook was added to
    build.ts. We may reuse it only when the historical build.ts blob can be
    substituted and every other tracked build input is identical.
    """
    current = _tree_records(ref)
    if canonical_build_ts_from:
        previous = _tree_records(canonical_build_ts_from)
        path = "build/commands/lib/build.ts"
        old_metadata = previous.get(path)
        new_metadata = current.get(path)
        if old_metadata is None or new_metadata is None:
            raise StateError("Cannot compare build.ts for the timestamp-hook cache migration")
        old_kind = old_metadata.split(b" ", 2)[:2]
        new_kind = new_metadata.split(b" ", 2)[:2]
        if old_kind != new_kind:
            raise StateError("build.ts mode/type changed; timestamp-hook cache migration is unsafe")
        current[path] = old_metadata

    excluded_exact = {"docs/SANDIUM_PERSISTENT_CACHE_IMPLEMENTATION.md"}
    excluded_prefixes = (".circleci/", ".github/", "ci/siso/", "tests/ci/")
    digest = hashlib.sha256()
    for path in sorted(current):
        if path in excluded_exact or path.startswith(excluded_prefixes):
            continue
        digest.update(current[path])
        digest.update(b"\t")
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _fingerprint_audit(source_commit: str, saved_fingerprint: str) -> dict[str, Any]:
    """Explain a checkpoint fingerprint mismatch without restoring its archive."""
    excluded_exact = {"docs/SANDIUM_PERSISTENT_CACHE_IMPLEMENTATION.md"}
    excluded_prefixes = (".circleci/", ".github/", "ci/siso/", "tests/ci/")

    def included(ref: str) -> dict[str, bytes]:
        return {
            path: metadata
            for path, metadata in _tree_records(ref).items()
            if path not in excluded_exact and not path.startswith(excluded_prefixes)
        }

    result: dict[str, Any] = {
        "source_commit": source_commit,
        "saved_fingerprint": saved_fingerprint,
        "historical_source_available": False,
        "historical_fingerprint": None,
        "current_fingerprint": None,
        "current_with_timestamp_hook_canonicalized": None,
        "timestamp_hook_migration_compatible": False,
        "changed_tracked_input_paths": [],
        "audit_error": None,
    }
    try:
        historical_records = included(source_commit)
        current_records = included("HEAD")
        result["historical_source_available"] = True
        result["historical_fingerprint"] = _build_input_fingerprint(source_commit)
        result["current_fingerprint"] = _build_input_fingerprint("HEAD")
        try:
            migrated = _build_input_fingerprint(
                "HEAD", canonical_build_ts_from=source_commit
            )
            result["current_with_timestamp_hook_canonicalized"] = migrated
            result["timestamp_hook_migration_compatible"] = (
                result["historical_fingerprint"] == saved_fingerprint
                and migrated == saved_fingerprint
            )
        except StateError as exc:
            result["audit_error"] = f"timestamp-hook migration: {exc}"

        paths = sorted(set(historical_records) | set(current_records))
        changed = []
        for path in paths:
            if historical_records.get(path) != current_records.get(path):
                changed.append({
                    "path": path,
                    "historical_blob": historical_records.get(path, b"").decode("ascii", errors="replace"),
                    "current_blob": current_records.get(path, b"").decode("ascii", errors="replace"),
                })
        result["changed_tracked_input_paths"] = changed
    except StateError as exc:
        result["audit_error"] = str(exc)
    return result


def _cache_input_compatible(state: dict[str, Any], context: dict[str, Any]) -> bool:
    """Accept a previous checkpoint only when build inputs truly still match."""
    if state.get("build_input_fingerprint") == context.get("build_input_fingerprint"):
        return True
    source_commit = state.get("source_commit")
    saved_fingerprint = state.get("build_input_fingerprint")
    if not isinstance(source_commit, str) or not isinstance(saved_fingerprint, str):
        return False
    try:
        historical = _build_input_fingerprint(source_commit)
        current_with_only_timestamp_hook_canonicalized = _build_input_fingerprint(
            "HEAD", canonical_build_ts_from=source_commit
        )
    except StateError:
        return False
    return (
        historical == saved_fingerprint
        and current_with_only_timestamp_hook_canonicalized == saved_fingerprint
    )


def _source_timestamp_policy(log_path: str | None) -> str | None:
    """Return the normalized source-time policy only when its pass marker is logged."""
    if not log_path:
        return None
    log = Path(log_path)
    if not log.is_file():
        return None
    text = log.read_text(encoding="utf-8", errors="replace")
    policy = "epoch_946684800_full_tree_v1"
    if (
        f"SOURCE_TIMESTAMP_POLICY={policy}" in text
        and "SOURCE_TIMESTAMP_VERIFY=PASS" in text
    ):
        return policy
    return None


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
        + metrics.get("preflight_remote_actions", 0)
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
            "gn_args_sha256",
        )
        if any(state.get(key) != context.get(key) for key in cache_keys):
            raise StateError("Restored build-output cache does not match current Chromium source, target, or GN arguments")
        if not _cache_input_compatible(state, context):
            raise StateError("Restored build-output cache does not match current browser/build inputs")
        if state.get("build_input_fingerprint") != context.get("build_input_fingerprint"):
            print("BUILD_INPUT_COMPATIBILITY=PASS reason=timestamp-hook-only")
        policy = state.get("source_timestamp_policy")
        if policy not in (None, "epoch_946684800_full_tree_v1"):
            raise StateError(f"Unrecognized cached source timestamp policy: {policy!r}")
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
        timestamp_policy = _source_timestamp_policy(args.log)

        old_metrics: dict[str, int] = {}
        can_preserve_remote_proof = False
        old_input_compatible = False
        if old:
            old_input_compatible = (
                old.get("chromium_commit") == context["chromium_commit"]
                and old.get("chromium_version") == context["chromium_version"]
                and old.get("gn_args_sha256") == context["gn_args_sha256"]
                and old.get("target") == TARGET
                and _cache_input_compatible(old, context)
            )
        if old and isinstance(old.get("remote_metrics"), dict):
            same_chromium_and_args = old_input_compatible
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
            key: (
                max(old_metrics.get(key, 0), parsed_metrics.get(key, 0))
                if key == "preflight_remote_actions"
                else old_metrics.get(key, 0) + parsed_metrics.get(key, 0)
            )
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

        if timestamp_policy is None and old_input_compatible and old:
            # Stage 4 may deliberately skip Ninja because an earlier stage
            # completed successfully. Preserve the prior policy only for this
            # identical input/checkpoint case; an ordinary build log must prove
            # the timestamp normalization itself.
            timestamp_policy = old.get("source_timestamp_policy")
        state: dict[str, Any] = {
            **context,
            "last_stage": args.stage,
            "compile_complete": compile_complete,
            "artifact_verified": artifact_verified,
            "source_timestamp_policy": timestamp_policy,
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
        print(f"SISO_PREFLIGHT_REMOTE_ACTIONS={metrics['preflight_remote_actions']}")
        print(f"SISO_REMOTE_BACKEND_PROVEN={str(state['remote_backend_proven']).lower()}")
        return 0
    except StateError as exc:
        print(f"BUILD_STATE_RECORD=FAIL ({exc})", file=sys.stderr)
        return 2


def command_audit_fingerprint(args: argparse.Namespace) -> int:
    report = _fingerprint_audit(args.source_commit, args.saved_fingerprint)
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["historical_source_available"] or report["audit_error"]:
        return 2
    return 0


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

    audit = subparsers.add_parser(
        "audit-fingerprint",
        help="explain checkpoint fingerprint compatibility without restoring an archive",
    )
    audit.add_argument("--source-commit", required=True)
    audit.add_argument("--saved-fingerprint", required=True)
    audit.set_defaults(func=command_audit_fingerprint)

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
