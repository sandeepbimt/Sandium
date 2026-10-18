#!/usr/bin/env python3
"""Apply Sandium's manual Chromium pin and reuse compatible sync metadata.

This helper is intentionally offline. It never fetches or checks out Chromium.
It only normalizes Brave's old timestamp-based sync record when the restored
Chromium checkout, source manifest, manual pin, and .gclient contents all agree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TAG_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
SOURCE_MANIFEST_SCHEMA = 3
SOURCE_TARGET = "android-arm64-debug-apk"


def read_manual_pin(core_dir: Path) -> str:
    pin_path = core_dir / "ci/pins/chromium-tag.txt"
    if not pin_path.is_file():
        raise FileNotFoundError(f"Manual Chromium pin file is missing: {pin_path}")
    tags = [
        line.strip()
        for line in pin_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(tags) != 1 or not TAG_PATTERN.fullmatch(tags[0]):
        raise ValueError(
            f"Manual Chromium pin must contain exactly one four-part numeric tag: {pin_path}"
        )
    return tags[0]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_text(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as temporary:
        temporary.write(json.dumps(value, indent=2) + "\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def prepare_reuse(workspace: Path, core_dir: Path) -> bool:
    """Normalize legacy sync metadata only when every cache fingerprint agrees.

    Returns True only when the existing Chromium checkout is already exactly at
    the manually approved tag and its prior successful sync metadata can safely
    be represented using a content hash rather than an unstable file mtime.
    False means Brave's normal sync path must decide whether the pinned ref
    needs fetching; no repository/network operation is performed here.
    """
    pin = read_manual_pin(core_dir)
    package = _load_json(core_dir / "package.json")
    declared_tag = package.get("config", {}).get("projects", {}).get("chrome", {}).get("tag")
    source_ref = f"refs/tags/{pin}"
    workspace = workspace.resolve()
    source = workspace / "src"
    manifest_path = workspace / ".sandium-source-cache-manifest.json"
    gclient_path = workspace / ".gclient"
    sync_info_path = workspace / ".brave_latest_successful_sync.json"

    print("CHROMIUM_UPDATE_POLICY=MANUAL_PIN")
    print("SANDIUM_PINNED_CHROMIUM_TAG=" + pin)
    print("BRAVE_DECLARED_CHROMIUM_TAG=" + str(declared_tag))
    if declared_tag != pin:
        print("BRAVE_DECLARED_TAG_DIFFERS_FROM_MANUAL_PIN=true")
        print("BRAVE_DECLARED_TAG_OVERRIDE=MANUAL_PIN_WINS")

    try:
        if not source.is_dir() or not gclient_path.is_file() or not manifest_path.is_file():
            raise ValueError("Chromium source cache or .gclient manifest is missing")
        actual_tag = git_text(source, "describe", "--tags", "--exact-match", "HEAD")
        commit = git_text(source, "rev-parse", "HEAD")
        if actual_tag != pin:
            raise ValueError(
                f"cached source tag {actual_tag!r} differs from manual pin {pin!r}"
            )
        if not TAG_PATTERN.fullmatch(actual_tag):
            raise ValueError(f"cached Chromium tag is not numeric: {actual_tag!r}")

        manifest = _load_json(manifest_path)
        if manifest.get("schema") != SOURCE_MANIFEST_SCHEMA:
            raise ValueError("unsupported source-cache manifest schema")
        if manifest.get("target") != SOURCE_TARGET:
            raise ValueError("source-cache manifest target mismatch")
        if manifest.get("chromium_tag") != pin or manifest.get("chromium_version") != pin:
            raise ValueError("source-cache manifest does not match the manual pin")
        if manifest.get("manual_chromium_pin") not in (None, pin):
            raise ValueError("source-cache manifest records a different manual pin")
        if manifest.get("chromium_commit") != commit:
            raise ValueError("source-cache manifest commit differs from the restored source")
        version_file = source / "chrome" / "VERSION"
        if not version_file.is_file() or manifest.get("version_file_sha256") != sha256(version_file):
            raise ValueError("Chromium VERSION fingerprint differs from the source manifest")
        gclient_hash = sha256(gclient_path)
        if manifest.get("gclient_sha256") != gclient_hash:
            raise ValueError(".gclient content differs from the source-cache manifest")

        latest = _load_json(sync_info_path)
        if latest.get("chromiumRef") != source_ref:
            raise ValueError("last successful sync ref does not match the manual pin")
        if latest.get("gclientHash") == gclient_hash:
            print("RESTORED_CHROMIUM_SOURCE_TAG=" + actual_tag)
            print("SOURCE_CACHE_FINGERPRINT=PASS")
            print("CHROMIUM_SYNC_METADATA=ALREADY_CONTENT_HASHED")
            print("CHROMIUM_SYNC_FAST_PATH=ELIGIBLE")
            return True
        if "gclientTimestamp" not in latest:
            raise ValueError("sync metadata is neither content-hashed nor migratable")

        # Older Brave versions stored only gclient file mtime. After cache restore
        # that mtime changes even for identical content; migrate only after the
        # pinned tag, source commit, VERSION hash, and .gclient hash all verified.
        write_json_atomically(
            sync_info_path,
            {"chromiumRef": source_ref, "gclientHash": gclient_hash},
        )
        print("RESTORED_CHROMIUM_SOURCE_TAG=" + actual_tag)
        print("SOURCE_CACHE_FINGERPRINT=PASS")
        print("CHROMIUM_SYNC_METADATA=MIGRATED_TIMESTAMP_TO_CONTENT_HASH")
        print("CHROMIUM_SYNC_FAST_PATH=ELIGIBLE")
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print("CHROMIUM_SYNC_FAST_PATH=NOT_ELIGIBLE")
        print("CHROMIUM_SYNC_FAST_PATH_REASON=" + str(exc))
        print("CHROMIUM_SYNC_DECISION=ALLOW_NORMAL_SYNC_TO_MANUAL_PIN")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument(
        "--require-reusable",
        action="store_true",
        help="Fail closed unless the restored Chromium source exactly matches the manual pin and cache manifest",
    )
    parser.add_argument(
        "--core",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Brave Core source directory (defaults to this script's source checkout)",
    )
    args = parser.parse_args()
    try:
        reusable = prepare_reuse(args.workspace, args.core)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("CHROMIUM_PIN_POLICY_ERROR=" + str(exc), file=sys.stderr)
        return 2
    if args.require_reusable and not reusable:
        print(
            "CHROMIUM_REUSE_REQUIRED_FAILED=true; refusing to run Brave dependency sync because it could refresh Chromium",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
