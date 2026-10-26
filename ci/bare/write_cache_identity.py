#!/usr/bin/env python3
"""Write a stable pre-build cache identity for the pinned Bare source/config."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[2]
EXPECTED_BARE = "a5a7ef74f3b77d45720b8c502cbacc30f2f3dbd5"
OUTPUT_NAME = ".sandium-bare-cache-identity.txt"


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()


def main() -> int:
    workspace = Path.home() / "project"
    try:
        CONTROL.relative_to(workspace)
    except ValueError:
        workspace = CONTROL

    bare = workspace / "bare-browser"
    if git(bare, "rev-parse", "HEAD") != EXPECTED_BARE:
        raise SystemExit("SANDIUM_CACHE_IDENTITY_ERROR=Bare source pin mismatch")
    pin_file = CONTROL / "ci" / "bare" / "source-pin.txt"
    gn_args = CONTROL / "ci" / "bare" / "gn_args.gn"
    backend = CONTROL / "ci" / "siso" / "buildbuddy_backend.star"
    helper = CONTROL / "ci" / "siso" / "buildbuddy-credential-helper.py"
    for path in (pin_file, gn_args, backend, helper):
        if not path.is_file():
            raise SystemExit("SANDIUM_CACHE_IDENTITY_ERROR=required identity input missing: " + str(path))

    patches = sorted((bare / "patches").glob("*.patch"))
    if not patches:
        raise SystemExit("SANDIUM_CACHE_IDENTITY_ERROR=patch series is empty")
    patch_hash = hashlib.sha256()
    for patch in patches:
        patch_hash.update(patch.name.encode("utf-8") + b"\0")
        patch_hash.update(bytes.fromhex(sha(patch)))
        patch_hash.update(b"\0")

    crx = bare / "third_party" / "ublock_origin" / "uBlockOrigin-1.75.0.crx"
    crx_sum = bare / "third_party" / "ublock_origin" / "uBlockOrigin-1.75.0.crx.sha256"
    if not crx.is_file() or not crx_sum.is_file():
        raise SystemExit("SANDIUM_CACHE_IDENTITY_ERROR=extension input/checksum missing")
    expected_crx = crx_sum.read_text(encoding="utf-8").split()[0]
    actual_crx = sha(crx)
    if expected_crx != actual_crx:
        raise SystemExit("SANDIUM_CACHE_IDENTITY_ERROR=extension checksum mismatch")

    pin_values = {}
    for line in pin_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            pin_values[key.strip()] = value.strip()
    toolchain_revision = pin_values.get("toolchain_depot_tools_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", toolchain_revision):
        raise SystemExit("SANDIUM_CACHE_IDENTITY_ERROR=depot_tools commit pin is missing or invalid")
    toolchain = Path.home() / "depot_tools"
    if (toolchain / ".git").is_dir():
        actual_toolchain = git(toolchain, "rev-parse", "HEAD")
        if actual_toolchain != toolchain_revision:
            raise SystemExit(
                "SANDIUM_CACHE_IDENTITY_ERROR=depot_tools pin mismatch: expected "
                + toolchain_revision + ", got " + actual_toolchain
            )
    values = {
        "schema": 1,
        "bare_commit": EXPECTED_BARE,
        "chromium_commit": "1228231501697ff7a4a0150ea8ae6df37bb99331",
        "chromium_version": "156.0.8078.25",
        "patch_count": len(patches),
        "patch_series_sha256": patch_hash.hexdigest(),
        "extension_crx_sha256": actual_crx,
        "target": "android-arm64-debug-apk",
        "gn_args_sha256": sha(gn_args),
        "backend_config_sha256": sha(backend),
        "credential_helper_sha256": sha(helper),
        "toolchain_revision": toolchain_revision,
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    values["identity_sha256"] = hashlib.sha256(canonical).hexdigest()
    out = workspace / OUTPUT_NAME
    out.write_text("\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n", encoding="utf-8")
    print("SANDIUM_CACHE_IDENTITY_FILE=" + str(out))
    print("SANDIUM_CACHE_IDENTITY_SHA256=" + values["identity_sha256"])
    print("SANDIUM_CACHE_IDENTITY_PATCH_COUNT=" + str(len(patches)))
    print("SANDIUM_CACHE_IDENTITY_TOOLCHAIN=" + toolchain_revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
