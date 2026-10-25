#!/usr/bin/env python3
"""Create/verify Bare-aware Android output checkpoints without clearing caches."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import IdentityError, control_and_workspace, source_identity

ARCHIVE_NAME = "sandium-bare-checkpoint.tar.zst"
META_NAME = "sandium-bare-checkpoint.meta"
OUT_RELATIVE = Path("src/out/android_Debug_arm64")
SCHEMA = 1


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity_digest(identity: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_meta(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_and_validate(root: Path, control: Path, archive: Path, meta: Path) -> dict[str, Any]:
    if not archive.is_file() or archive.is_symlink():
        raise IdentityError(f"Checkpoint archive is missing or unsafe: {archive}")
    if not meta.is_file() or meta.is_symlink():
        raise IdentityError(f"Checkpoint metadata is missing or unsafe: {meta}")
    saved = json.loads(meta.read_text(encoding="utf-8"))
    if saved.get("schema") != SCHEMA:
        raise IdentityError("Unsupported Bare checkpoint metadata schema")
    if saved.get("archive_sha256") != sha256(archive):
        raise IdentityError("Checkpoint archive SHA-256 mismatch")
    current = source_identity(control, root, require_gn=False)
    saved_identity = saved.get("build_identity", {})
    changed = sorted(
        key for key, value in current.items()
        if key != "gn_args_sha256" and saved_identity.get(key) != value
    )
    if changed:
        raise IdentityError("Checkpoint source identity mismatch: " + ",".join(changed))
    # If an output tree is already present, compare the full generated-GN identity.
    if current.get("gn_args_sha256") is not None:
        current_full = source_identity(control, root, require_gn=True)
        if identity_digest(current_full) != saved.get("build_identity_sha256"):
            raise IdentityError("Checkpoint build identity mismatch, including generated GN arguments")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pack", "verify", "restore"))
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--archive", type=Path, default=None)
    parser.add_argument("--meta", type=Path, default=None)
    args = parser.parse_args()

    control, inferred_root = control_and_workspace(Path(__file__))
    root = args.root.resolve() if args.root else inferred_root
    archive = args.archive.resolve() if args.archive else root / ARCHIVE_NAME
    meta = args.meta.resolve() if args.meta else root / META_NAME
    output = root / OUT_RELATIVE

    if args.mode == "pack":
        if not output.is_dir() or output.is_symlink():
            raise IdentityError(f"Android output directory is missing or unsafe: {output}")
        identity = source_identity(control, root)
        archive.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".sandium-bare-checkpoint.", suffix=".tar.zst", dir=str(archive.parent))
        os.close(fd)
        temporary = Path(temp_name)
        try:
            subprocess.run(
                ["tar", "--use-compress-program=zstd", "-cf", str(temporary), "-C", str(root), str(OUT_RELATIVE)],
                check=True,
            )
            archive_digest = sha256(temporary)
            os.replace(temporary, archive)
        finally:
            temporary.unlink(missing_ok=True)
        payload = {
            "schema": SCHEMA,
            "source": "Bare",
            "build_identity": identity,
            "build_identity_sha256": identity_digest(identity),
            "archive_name": archive.name,
            "archive_sha256": archive_digest,
            "output_relative_path": str(OUT_RELATIVE),
        }
        write_meta(meta, payload)
        print("SANDIUM_BARE_CHECKPOINT=PACKED")
        print("SANDIUM_BARE_CHECKPOINT_SHA256=" + archive_digest)
        print("SANDIUM_BUILD_IDENTITY_SHA256=" + payload["build_identity_sha256"])
        return 0

    saved = load_and_validate(root, control, archive, meta)
    print("SANDIUM_BARE_CHECKPOINT_IDENTITY=PASS")
    print("SANDIUM_BARE_CHECKPOINT_SHA256=" + saved["archive_sha256"])
    if args.mode == "verify":
        return 0
    if output.is_dir() and not output.is_symlink():
        current_identity = source_identity(control, root, require_gn=True)
        if identity_digest(current_identity) == saved.get("build_identity_sha256"):
            print("SANDIUM_BARE_CHECKPOINT_RESTORE=SKIPPED_ALREADY_VALID")
            return 0

    # Extract to an isolated temporary root first. Preserve any pre-existing output
    # directory as a backup rather than deleting it; only swap after extraction passes.
    with tempfile.TemporaryDirectory(prefix="sandium-bare-restore.", dir=str(root)) as temp_name:
        temp_root = Path(temp_name)
        subprocess.run(
            ["tar", "--use-compress-program=zstd", "-xf", str(archive), "-C", str(temp_root), str(OUT_RELATIVE)],
            check=True,
        )
        staged_output = temp_root / OUT_RELATIVE
        if not staged_output.is_dir():
            raise IdentityError("Checkpoint archive does not contain the expected Android output directory")
        previous = output.with_name("android_Debug_arm64.pre-restore-backup")
        if previous.exists():
            raise IdentityError(f"Refusing to overwrite existing preserved output backup: {previous}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            os.replace(output, previous)
        try:
            os.replace(staged_output, output)
            restored_identity = source_identity(control, root, require_gn=True)
            if identity_digest(restored_identity) != saved.get("build_identity_sha256"):
                raise IdentityError("Restored output identity differs from checkpoint metadata")
        except Exception:
            if output.exists():
                failed = output.with_name("android_Debug_arm64.failed-restore")
                if not failed.exists():
                    os.replace(output, failed)
            if previous.exists():
                os.replace(previous, output)
            raise
        # The pre-restore backup remains intentionally preserved for manual recovery.
    print("SANDIUM_BARE_CHECKPOINT_RESTORE=PASS")
    print("SANDIUM_PREVIOUS_OUTPUT_PRESERVED=" + str(output.with_name("android_Debug_arm64.pre-restore-backup")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IdentityError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise SystemExit("SANDIUM_BARE_CHECKPOINT_ERROR=" + str(exc))
