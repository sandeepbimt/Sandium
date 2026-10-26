#!/usr/bin/env python3
"""Fingerprint only build-compatible inputs for reusable compiled-output checkpoints.

CI orchestration and validator implementation files intentionally do not belong
in this fingerprint: changing stage bookkeeping must not orphan a compatible
multi-gigabyte output checkpoint. The authoritative checkpoint validator still
checks these build inputs and the generated source/toolchain/GN/Siso identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SCHEMA = 1
INPUTS = (
    "ci/bare/source-pin.txt",
    "ci/bare/gn_args.gn",
    "ci/bare/prepare_source.sh",
    "ci/bare/configure_siso.py",
    "ci/bare/bootstrap_chromium.sh",
    "ci/bare/package_artifact.sh",
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
