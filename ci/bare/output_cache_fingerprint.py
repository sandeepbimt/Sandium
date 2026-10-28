#!/usr/bin/env python3
"""Fingerprint stable source, GN and remote-backend inputs for output reuse.

Do not hash shell orchestration or APK packaging scripts here: fixing a CI
runner must not make an otherwise compatible multi-gigabyte output cache
unreachable. The checkpoint's independent source/toolchain/GN/Siso identity,
manifest hashes, compiled-object inventory and live no-rebuild preflight remain
the authoritative compatibility gate. Removed script hashes in legacy
checkpoint metadata are tolerated as extra fields during migration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SCHEMA = 2
INPUTS = (
    "ci/bare/source-pin.txt",
    "ci/bare/gn_args.gn",
    "ci/siso/buildbuddy_backend.star",
    "ci/siso/buildbuddy-credential-helper.py",
)


def calculate_fingerprint(control: Path) -> tuple[str, str]:
    entries: dict[str, str] = {}
    for relative in INPUTS:
        path = control / relative
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"SANDIUM_OUTPUT_CACHE_FINGERPRINT=FAIL missing/unsafe input: {relative}")
        entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {"schema": SCHEMA, "inputs": entries}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    content = (
        f"schema={SCHEMA}\nidentity_sha256={digest}\n"
        + "".join(f"{name}={value}\n" for name, value in sorted(entries.items()))
    )
    return digest, content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    args = parser.parse_args()
    control = args.root.resolve() / "sandium-control"
    digest, content = calculate_fingerprint(control)
    output = control / ".sandium-output-cache-fingerprint.txt"
    output.write_text(content, encoding="utf-8")
    print("SANDIUM_OUTPUT_CACHE_FINGERPRINT=" + digest)
    print("SANDIUM_OUTPUT_CACHE_FINGERPRINT_FILE=" + str(output))
    print("SANDIUM_OUTPUT_CACHE_FINGERPRINT_INPUTS=" + str(len(INPUTS)))
    print("SANDIUM_OUTPUT_CACHE_FINGERPRINT_SCOPE=BUILD_COMPATIBILITY_ONLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
