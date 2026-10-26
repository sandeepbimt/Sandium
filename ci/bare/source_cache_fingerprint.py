#!/usr/bin/env python3
"""Fingerprint pinned source identity, not orchestration code that changes often.

Source-cache compatibility is determined by the pinned Chromium/Bare/toolchain
identity. Source contents are validated after restore by the bootstrap checks.
Build orchestration edits must not make a valid multi-gigabyte source cache
unreachable; use a new schema only when the cache layout/compatibility contract
actually changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SCHEMA = 2
INPUTS = ("ci/bare/source-pin.txt",)


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
    payload = {"schema": SCHEMA, "source_identity_inputs": entries}
    identity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    output = control / ".sandium-source-cache-fingerprint.txt"
    output.write_text(
        f"schema={SCHEMA}\nidentity_sha256={identity}\n"
        + "".join(f"{name}={value}\n" for name, value in sorted(entries.items())),
        encoding="utf-8",
    )
    print("SANDIUM_SOURCE_CACHE_FINGERPRINT=" + identity)
    print("SANDIUM_SOURCE_CACHE_FINGERPRINT_FILE=" + str(output))
    print("SANDIUM_SOURCE_CACHE_INPUTS=" + str(len(entries)))
    print("SANDIUM_SOURCE_CACHE_IDENTITY=PINNED_SOURCE_ONLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
