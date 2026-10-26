#!/usr/bin/env python3
"""Fingerprint only the expensive pinned source-sync inputs for CircleCI reuse."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import argparse

SCHEMA = 1
INPUTS = (
    "ci/bare/source-pin.txt",
    "ci/bare/prepare_source.sh",
    "ci/bare/bootstrap_chromium.sh",
    "ci/bare/write_cache_identity.py",
    "ci/bare/source_cache_fingerprint.py",
)

def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    args = parser.parse_args()
    root = args.root.resolve()
    control = root / "sandium-control"
    entries = {}
    for rel in INPUTS:
        path = control / rel
        if not path.is_file() or path.is_symlink():
            raise SystemExit("SANDIUM_SOURCE_CACHE_FINGERPRINT=FAIL missing/unsafe input: " + rel)
        entries[rel] = sha(path)
    payload = {"schema": SCHEMA, "source_inputs": entries}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    output = control / ".sandium-source-cache-fingerprint.txt"
    output.write_text(
        f"schema={SCHEMA}\nidentity_sha256={identity}\n" +
        "".join(f"{name}={value}\n" for name, value in sorted(entries.items())),
        encoding="utf-8",
    )
    print("SANDIUM_SOURCE_CACHE_FINGERPRINT=" + identity)
    print("SANDIUM_SOURCE_CACHE_FINGERPRINT_FILE=" + str(output))
    print("SANDIUM_SOURCE_CACHE_INPUTS=" + str(len(entries)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
