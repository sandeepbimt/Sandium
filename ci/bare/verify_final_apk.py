#!/usr/bin/env python3
"""Validate the final Sandium APK against the stage-5 status and manifest."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"SANDIUM_FINAL_APK_GATE=FAIL {message}")


if len(sys.argv) != 4:
    fail("expected STATUS APK MANIFEST paths")

status_path, apk_path, manifest_path = map(Path, sys.argv[1:4])
status = {}
for line in status_path.read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        status[key] = value

manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
digest = hashlib.sha256(apk_path.read_bytes()).hexdigest()
size = apk_path.stat().st_size
if size < 1_000_000:
    fail("APK is unexpectedly small")
if status.get("APK_SHA256") != digest or status.get("APK_SIZE_BYTES") != str(size):
    fail("APK hash/size differs from stage status")
if manifest.get("artifact_sha256") != digest or manifest.get("artifact_bytes") != size:
    fail("APK hash/size differs from manifest")
identity = manifest.get("build_identity_sha256")
if not identity or status.get("BUILD_IDENTITY_SHA256") != identity:
    fail("build identity differs between status and manifest")
if manifest.get("artifact_verified") is not True or manifest.get("apksigner_verification") != "PASS":
    fail("manifest lacks recorded APK signature verification")

with zipfile.ZipFile(apk_path) as archive:
    if archive.testzip() is not None:
        fail("APK ZIP integrity check failed")
    names = set(archive.namelist())
    if "AndroidManifest.xml" not in names or not any(
        name.startswith("classes") and name.endswith(".dex") for name in names
    ):
        fail("APK is missing AndroidManifest.xml or DEX")

print("SANDIUM_FINAL_APK_FILE=PASS")
print("SANDIUM_FINAL_APK_HASH_SIZE=PASS")
print("SANDIUM_FINAL_APK_BUILD_IDENTITY=PASS")
print("SANDIUM_FINAL_APK_SIGNATURE_RECORD=PASS")
print("SANDIUM_FINAL_APK_ZIP_STRUCTURE=PASS")
