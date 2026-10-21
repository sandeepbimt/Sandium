#!/usr/bin/env python3
"""Create a stable identity for the Chromium checkout/dependency cache.

This deliberately excludes CIRCLE_SHA1 and CI-only files. The cached tree contains
Chromium and gclient-managed dependencies, not Brave Core or build outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

INPUTS = (
    "ci/pins/chromium-tag.txt",
    "DEPS",
    "package.json",
    "pnpm-lock.yaml",
    "build/commands/lib/config.ts",
    "build/commands/lib/syncUtils.ts",
    "build/commands/scripts/sync.ts",
)
KEY_FILE = ".sandium-source-cache-key.txt"


def stable_source_fingerprint(core: Path) -> tuple[str, dict[str, str]]:
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    for relative in INPUTS:
        path = core / relative
        if not path.is_file():
            raise FileNotFoundError(f"Required source-cache identity input is missing: {path}")
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[relative] = file_digest
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_digest))
        digest.update(b"\0")
    return digest.hexdigest(), files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    args = parser.parse_args()
    root = args.root.resolve()
    core = root / "src" / "brave"
    fingerprint, files = stable_source_fingerprint(core)
    key_file = root / KEY_FILE
    key_file.write_text(f"schema=1\nfingerprint={fingerprint}\n", encoding="utf-8")
    print("SANDIUM_SOURCE_CACHE_KEY_FILE=" + str(key_file))
    print("SANDIUM_SOURCE_CACHE_INPUT_FINGERPRINT=" + fingerprint)
    print("SANDIUM_SOURCE_CACHE_IDENTITY_FILES=" + ",".join(sorted(files)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
