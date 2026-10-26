#!/usr/bin/env python3
"""Create a short checksum input for the source, APK-state and build-output caches.

No source sync or build is performed. No secrets or credential values are read.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import argparse

SCHEMA = 2
INPUTS = (
    ".circleci/config.yml",
    "ci/bare/source-pin.txt",
    "ci/bare/gn_args.gn",
    "ci/bare/prepare_source.sh",
    "ci/bare/configure_siso.py",
    "ci/bare/build_identity.py",
    "ci/bare/stage_ledger.py",
    "ci/bare/state_handoff.py",
    "ci/bare/package_artifact.sh",
    "ci/bare/bootstrap_chromium.sh",
    "ci/bare/output_checkpoint.py",
    "ci/bare/fast_apk.py",
    "ci/bare/cache_fingerprint.py",
    "ci/bare/source_cache_fingerprint.py",
    "ci/siso/buildbuddy_backend.star",
    "ci/siso/buildbuddy-credential-helper.py",
)

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    args = parser.parse_args()
    root = args.root.resolve()
    control = root / "sandium-control"
    entries: dict[str, str] = {}
    for rel in INPUTS:
        path = control / rel
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"SANDIUM_CACHE_FINGERPRINT=FAIL missing/unsafe input: {rel}")
        entries[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {"schema": SCHEMA, "inputs": entries}
    digest = sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    output = control / ".sandium-cache-fingerprint.txt"
    output.write_text(
        f"schema={SCHEMA}\nidentity_sha256={digest}\n" +
        "".join(f"{name}={value}\n" for name, value in sorted(entries.items())),
        encoding="utf-8",
    )
    print(f"SANDIUM_CACHE_FINGERPRINT={digest}")
    print(f"SANDIUM_CACHE_FINGERPRINT_FILE={output}")
    print(f"SANDIUM_CACHE_FINGERPRINT_INPUTS={len(entries)}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
