#!/usr/bin/env python3
"""Record and verify a reproducible Bare/Chromium Android build identity.

This utility is deliberately independent of CI commit IDs. It does not sync
sources, invoke Siso, compile code, sign an APK, or modify existing caches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

TARGET = "android-arm64-debug-apk"
MIN_APK_BYTES = 1_000_000


class IdentityError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(path: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise IdentityError(f"Git identity unavailable for {path}: {exc}") from exc


def control_and_workspace(script: Path) -> tuple[Path, Path]:
    control = script.resolve().parents[2]
    expected = (Path.home() / "project").resolve()
    try:
        control.relative_to(expected)
    except ValueError:
        return control, control
    return control, expected


def read_pins(control: Path) -> dict[str, str]:
    path = control / "ci" / "bare" / "source-pin.txt"
    if not path.is_file():
        raise IdentityError(f"Source pin file is missing: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise IdentityError(f"Invalid source pin line: {line!r}")
        key, value = line.split("=", 1)
        if key in values:
            raise IdentityError(f"Duplicate source pin key: {key}")
        values[key] = value
    required = {
        "schema", "bare_repository", "bare_commit", "chromium_commit",
        "chromium_version", "target_abi", "build_config", "artifact_name",
        "toolchain_depot_tools_commit",
    }
    if required - values.keys():
        raise IdentityError("Source pin file lacks required fields: " + ",".join(sorted(required - values.keys())))
    if values["schema"] != "1" or values["target_abi"] != "arm64" or values["build_config"] != "Debug":
        raise IdentityError("Unsupported source pin schema or build target")
    if not re.fullmatch(r"[0-9a-f]{40}", values["bare_commit"]):
        raise IdentityError("Bare commit pin is not a full SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", values["chromium_commit"]):
        raise IdentityError("Chromium commit pin is not a full SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", values["toolchain_depot_tools_commit"]):
        raise IdentityError("depot_tools commit pin is not a full SHA")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", values["chromium_version"]):
        raise IdentityError("Chromium version pin is invalid")
    return values


def source_identity(
    control: Path,
    workspace: Path,
    require_gn: bool = True,
    output_dir_override: Path | None = None,
) -> dict[str, Any]:
    pins = read_pins(control)
    bare = workspace / "bare-browser"
    chromium = workspace / "src"
    if not (bare / ".git").is_dir():
        raise IdentityError(f"Pinned Bare checkout is missing: {bare}")
    actual_bare = git(bare, "rev-parse", "HEAD")
    if actual_bare != pins["bare_commit"]:
        raise IdentityError(f"Bare pin mismatch: expected {pins['bare_commit']}, got {actual_bare}")

    patch_dir = bare / "patches"
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        raise IdentityError("Pinned Bare patch series is empty")
    patch_digest = hashlib.sha256()
    for patch in patches:
        patch_digest.update(patch.name.encode("utf-8"))
        patch_digest.update(b"\0")
        patch_digest.update(bytes.fromhex(sha256(patch)))
        patch_digest.update(b"\0")

    crx = bare / "third_party" / "ublock_origin" / "uBlockOrigin-1.75.0.crx"
    crx_checksum = bare / "third_party" / "ublock_origin" / "uBlockOrigin-1.75.0.crx.sha256"
    if not crx.is_file() or not crx_checksum.is_file():
        raise IdentityError("Pinned extension CRX or its checksum file is missing")
    expected_crx_hash = crx_checksum.read_text(encoding="utf-8").strip().split()[0]
    actual_crx_hash = sha256(crx)
    if expected_crx_hash != actual_crx_hash:
        raise IdentityError("Pinned extension CRX checksum mismatch")

    chromium_head = git(chromium, "rev-parse", "HEAD")
    if chromium_head == pins["chromium_commit"]:
        raise IdentityError("Bare patch series has not been applied to the pinned Chromium checkout")
    # Bare's ordered patches are applied as one commit per patch. Confirm the
    # exact pinned Chromium revision is the base below that patch series.
    try:
        base = git(chromium, "rev-parse", f"HEAD~{len(patches)}")
    except IdentityError:
        base = ""
    if base != pins["chromium_commit"]:
        raise IdentityError(
            f"Chromium source identity mismatch: HEAD={chromium_head}, "
            f"base_after_{len(patches)}_patches={base}, expected={pins['chromium_commit']}"
        )
    version_file = chromium / "chrome" / "VERSION"
    if not version_file.is_file():
        raise IdentityError("Chromium chrome/VERSION is missing")
    version_text = version_file.read_text(encoding="utf-8")
    version_values = {}
    for line in version_text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            version_values[k.strip()] = v.strip()
    resolved_version = ".".join(
        version_values.get(k, "") for k in ("MAJOR", "MINOR", "BUILD", "PATCH")
    )
    if resolved_version != pins["chromium_version"]:
        raise IdentityError(
            f"Chromium VERSION mismatch: expected {pins['chromium_version']}, got {resolved_version}"
        )

    gclient = workspace / ".gclient"
    backend = control / "ci" / "siso" / "buildbuddy_backend.star"
    helper = control / "ci" / "siso" / "buildbuddy-credential-helper.py"
    siso_env = chromium / "build" / "config" / "siso" / ".sisoenv"
    siso_rc = chromium / "build" / "config" / "siso" / ".sisorc"
    if not gclient.is_file() or not backend.is_file() or not helper.is_file():
        raise IdentityError("Required existing .gclient/backend/credential-helper file is missing")
    if require_gn and (not siso_env.is_file() or not siso_rc.is_file()):
        raise IdentityError("Generated Siso .sisoenv/.sisorc configuration is missing")

    # A completed output checkpoint is temporarily moved out of src/out while
    # its publication gate runs. Allow that gate to inspect the candidate tree
    # without pretending the active src/out directory still exists.
    out = output_dir_override if output_dir_override is not None else chromium / "out" / "android_Debug_arm64"
    if require_gn and (not out.is_dir() or out.is_symlink()):
        raise IdentityError(f"Generated GN output directory is missing/unsafe: {out}")
    args_files = [out / "args.gn", out / "args_generated.gni"]
    args_digest = hashlib.sha256()
    effective_args = ""
    for path in args_files:
        if path.is_file():
            data = path.read_bytes()
            args_digest.update(path.name.encode("utf-8") + b"\0" + data)
            effective_args += data.decode("utf-8", errors="replace") + "\n"
    if not effective_args and require_gn:
        raise IdentityError("Generated GN argument files are missing")
    if effective_args:
        for name, value in (("use_siso", "true"), ("use_reclient", "false"), ("use_remoteexec", "true")):
            if not re.search(rf"(?m)^\s*{name}\s*=\s*{value}\s*$", effective_args):
                raise IdentityError(f"Effective GN arguments do not prove {name}={value}")

    try:
        siso = subprocess.check_output(["siso", "version"], text=True, stderr=subprocess.STDOUT, timeout=10).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise IdentityError(f"Cannot identify the Siso executable/version: {exc}") from exc
    if not siso:
        raise IdentityError("Siso returned an empty version")

    toolchain_commit = git(Path.home() / "depot_tools", "rev-parse", "HEAD")
    if toolchain_commit != pins["toolchain_depot_tools_commit"]:
        raise IdentityError(
            "depot_tools pin mismatch: expected "
            + pins["toolchain_depot_tools_commit"] + ", got " + toolchain_commit
        )

    return {
        "schema": 1,
        "source": "Bare",
        "bare_repository": pins["bare_repository"],
        "bare_commit": actual_bare,
        "chromium_commit": pins["chromium_commit"],
        "chromium_patched_head": chromium_head,
        "chromium_version": resolved_version,
        "patch_count": len(patches),
        "patch_series_sha256": patch_digest.hexdigest(),
        "extension_version": "1.75.0",
        "extension_crx_sha256": actual_crx_hash,
        "target": TARGET,
        "target_abi": pins["target_abi"],
        "build_config": pins["build_config"],
        "toolchain_depot_tools_commit": toolchain_commit,
        "siso_version": siso.splitlines()[0][:500],
        "gn_args_sha256": args_digest.hexdigest() if effective_args else None,
        "gclient_sha256": sha256(gclient),
        "backend_config_sha256": sha256(backend),
        "credential_helper_sha256": sha256(helper),
        "siso_env_sha256": sha256(siso_env) if siso_env.is_file() else None,
        "siso_rc_sha256": sha256(siso_rc) if siso_rc.is_file() else None,
    }


def verify_apk(
    apk: Path,
    manifest: dict[str, Any],
    workspace: Path | None = None,
    require_signature: bool = True,
) -> dict[str, Any]:
    if not apk.is_file() or apk.is_symlink():
        raise IdentityError(f"APK is missing or not a regular file: {apk}")
    size = apk.stat().st_size
    if size < MIN_APK_BYTES:
        raise IdentityError(f"APK is unexpectedly small: {size} bytes")
    try:
        with zipfile.ZipFile(apk) as archive:
            bad_entry = archive.testzip()
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise IdentityError(f"APK ZIP integrity check failed: {exc}") from exc
    if bad_entry:
        raise IdentityError(f"APK ZIP CRC check failed at entry: {bad_entry}")
    if "AndroidManifest.xml" not in names or not any(
        name.startswith("classes") and name.endswith(".dex") for name in names
    ):
        raise IdentityError("APK lacks AndroidManifest.xml or DEX bytecode")
    signature_status = "NOT_REQUESTED"
    if require_signature:
        candidates: list[Path] = []
        resolved = shutil.which("apksigner")
        if resolved:
            candidates.append(Path(resolved))
        if workspace is not None:
            build_tools = workspace / "src" / "third_party" / "android_build_tools" / "sdk" / "build-tools"
            if build_tools.is_dir():
                candidates.extend(sorted(build_tools.glob("*/apksigner"), reverse=True))
        apksigner = next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
        if apksigner is None:
            raise IdentityError("Android apksigner is unavailable; refusing to mark APK verified")
        try:
            result = subprocess.run(
                [str(apksigner), "verify", "--verbose", "--print-certs", str(apk)],
                text=True, capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IdentityError(f"APK signature verification could not run: {exc}") from exc
        if result.returncode != 0:
            raise IdentityError("APK signature verification failed: " + (result.stderr or result.stdout)[:1200])
        signature_status = "PASS"
    return {
        "artifact_name": manifest["artifact_name"],
        "artifact_bytes": size,
        "artifact_sha256": sha256(apk),
        "apk_zip_integrity": "PASS",
        "apk_manifest_and_dex": "PASS",
        "apksigner_verification": signature_status,
        "artifact_verified": signature_status == "PASS" if require_signature else False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("record", "verify"), required=True)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--apk", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    control = Path(__file__).resolve().parents[2]
    _, inferred_root = control_and_workspace(Path(__file__))
    workspace = args.root.resolve() if args.root else inferred_root
    pins = read_pins(control)
    apk = args.apk.resolve() if args.apk else (
        workspace / "src" / "out" / "android_Debug_arm64" / "apks" / "ChromePublic.apk"
    )
    output_dir = args.output.resolve() if args.output else workspace / "sandium-output"
    manifest_path = output_dir / "build-manifest.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    identity = source_identity(control, workspace)
    if args.mode == "record":
        artifact = verify_apk(apk, {**identity, "artifact_name": pins["artifact_name"]}, workspace=workspace) if apk.exists() else {
            "artifact_name": pins["artifact_name"], "artifact_verified": False
        }
        manifest = {
            **identity,
            **artifact,
            "artifact_name": pins["artifact_name"],
            "build_identity_sha256": hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("SANDIUM_BUILD_IDENTITY=RECORDED")
        print("SANDIUM_BUILD_IDENTITY_SHA256=" + manifest["build_identity_sha256"])
        print("SANDIUM_BUILD_MANIFEST=" + str(manifest_path))
        print("SANDIUM_ARTIFACT_VERIFIED=" + str(manifest["artifact_verified"]).lower())
        return 0

    if not manifest_path.is_file():
        raise IdentityError(f"Build manifest is missing: {manifest_path}")
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    saved_identity = {k: saved.get(k) for k in identity}
    if saved_identity != identity:
        changed = sorted(k for k in identity if saved.get(k) != identity[k])
        raise IdentityError("Build/source identity mismatch in fields: " + ",".join(changed))
    artifact = verify_apk(apk, {**identity, "artifact_name": pins["artifact_name"]}, workspace=workspace)
    if saved.get("artifact_verified") is not True or saved.get("apksigner_verification") != "PASS":
        raise IdentityError("Saved manifest does not contain successful APK signature-verification evidence")
    if saved.get("artifact_sha256") != artifact["artifact_sha256"] or saved.get("artifact_bytes") != artifact["artifact_bytes"]:
        raise IdentityError("APK does not match the saved manifest SHA-256/size")
    print("SANDIUM_BUILD_IDENTITY=PASS")
    print("SANDIUM_APK_INTEGRITY=PASS")
    print("SANDIUM_APK_SHA256=" + artifact["artifact_sha256"])
    print("SANDIUM_APK_BYTES=" + str(artifact["artifact_bytes"]))
    print("SANDIUM_APKSIGNER_VERIFICATION=PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IdentityError, OSError, json.JSONDecodeError, KeyError) as exc:
        raise SystemExit("SANDIUM_BUILD_IDENTITY_ERROR=" + str(exc))
