#!/usr/bin/env python3
"""Pack and restore validated, resumable SANDIUM Android build checkpoints."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = 1
OUTPUT_REL = Path("out/android_Debug_arm64")
STATE_NAME = ".sandium-build-state.json"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_STAGES = ("stage-1", "stage-2", "stage-3", "stage-4", "stage-5")


class CheckpointError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError("Cannot read JSON at {}: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise CheckpointError("JSON metadata is not an object: {}".format(path))
    return value


def required_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise CheckpointError("Required checkpoint input is missing or empty: {}".format(path))



def _path_entry_exists(path: Path) -> bool:
    """Include dangling symlinks, which Path.exists() intentionally ignores."""
    return path.exists() or path.is_symlink()


def quarantine_incomplete_siso_checkpoint_pair(output: Path, root: Path, stage: str) -> None:
    """Preserve, but exclude, an unusable partial Siso state/journal pair.

    A one-sided or malformed pair cannot safely resume Siso state. Move any
    present entries outside the checkpointed output tree, keeping all compiled
    outputs intact. Siso can rebuild its metadata from the retained outputs.
    """
    state_path = output / ".siso_fs_state"
    journal_path = output / ".siso_fs_state.journal"
    paths = (state_path, journal_path)
    valid = all(
        path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        for path in paths
    )
    if valid or not any(_path_entry_exists(path) for path in paths):
        return

    diagnostics = root / "build-diagnostics" / "siso-checkpoint-orphans"
    diagnostics.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if not _path_entry_exists(path):
            continue
        base = diagnostics / f"{stage}-{path.name}"
        destination = base
        suffix = 1
        while _path_entry_exists(destination):
            destination = diagnostics / f"{base.name}-{suffix}"
            suffix += 1
        shutil.move(str(path), str(destination))
        print(f"SANDIUM_SISO_CHECKPOINT_ORPHAN_PRESERVED={destination}")
    print("SANDIUM_SISO_CHECKPOINT_PAIR=ABSENT; incomplete pair quarantined; build outputs preserved")


def validate_siso_checkpoint_pair(output: Path, *, require_pair: bool = False) -> None:
    """Validate an optional Siso state pair in a compiled-output checkpoint.

    A complete pair preserves Siso filesystem-state reuse. When Siso omits the
    pair at shutdown, the checkpoint can still preserve compatible compiled
    outputs so Ninja/Siso can schedule only stale or missing outputs. Never
    fabricate state or accept a one-sided/invalid pair.
    """
    state_path = output / ".siso_fs_state"
    journal_path = output / ".siso_fs_state.journal"
    state_exists = _path_entry_exists(state_path)
    journal_exists = _path_entry_exists(journal_path)
    if require_pair and not state_exists and not journal_exists:
        raise CheckpointError(
            "Cannot pack a resumable checkpoint without Siso filesystem state and journal"
        )
    if state_exists != journal_exists:
        raise CheckpointError(
            "Siso filesystem state and journal must be present together "
            "(state={}, journal={})".format(state_exists, journal_exists)
        )
    if state_exists:
        for path in (state_path, journal_path):
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                raise CheckpointError(
                    "Siso checkpoint file is not a nonempty regular file: {}".format(path.name)
                )


def validate_state(state: dict[str, Any]) -> None:
    for key in ("source_commit", "chromium_commit", "target", "build_config",
                "build_input_fingerprint", "gn_args_sha256"):
        if not isinstance(state.get(key), str) or not state[key]:
            raise CheckpointError("Build state is missing required identity field: {}".format(key))
    version = state.get("chromium_version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise CheckpointError("Build state has an invalid Chromium version: {!r}".format(version))
    if state.get("target") != "android-arm64-debug-apk" or state.get("build_config") != "Debug":
        raise CheckpointError("Build state is not the Android ARM64 Debug APK target")
    for key in ("build_input_fingerprint", "gn_args_sha256"):
        if not FINGERPRINT_RE.fullmatch(state[key]):
            raise CheckpointError("{} is not a SHA-256 digest".format(key))


def list_members(archive: Path) -> list[str]:
    try:
        result = subprocess.run(["tar", "-t", "-I", "zstd -T1 -d", "-f", str(archive)],
                                check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise CheckpointError("Cannot list checkpoint archive: {}".format(detail)) from exc
    names = [line for line in result.stdout.splitlines() if line]
    prefix = OUTPUT_REL.as_posix()
    if not names:
        raise CheckpointError("Checkpoint archive is empty")
    for name in names:
        member = PurePosixPath(name)
        normalized = name.rstrip("/")
        if member.is_absolute() or ".." in member.parts:
            raise CheckpointError("Unsafe archive path: {!r}".format(name))
        if normalized != prefix and not normalized.startswith(prefix + "/"):
            raise CheckpointError("Archive contains path outside build output: {!r}".format(name))
    return names


def metadata_for(output: Path, state: dict[str, Any], stage: str, *, require_siso_state: bool = False) -> dict[str, Any]:
    # Strict compile-window handovers require the Siso state/journal pair. The
    # permissive mode remains available for explicitly output-only checkpoints.
    validate_siso_checkpoint_pair(output, require_pair=require_siso_state)
    paths = {
        "state_sha256": output / STATE_NAME,
        "args_gn_sha256": output / "args.gn",
        "build_ninja_sha256": output / "build.ninja",
    }
    for path in paths.values():
        required_file(path)
    meta: dict[str, Any] = {
        "schema": SCHEMA, "stage": stage,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_rel_path": OUTPUT_REL.as_posix(),
        "source_commit": state["source_commit"],
        "chromium_commit": state["chromium_commit"],
        "chromium_version": state["chromium_version"],
        "target": state["target"], "build_config": state["build_config"],
        "build_input_fingerprint": state["build_input_fingerprint"],
        "gn_args_sha256": state["gn_args_sha256"],
    }
    for key, path in paths.items():
        meta[key] = sha256_file(path)

    # Persist measurable progress in the manifest so a future catalog can rank
    # candidates by work completed rather than cache age or stage label alone.
    object_count = sum(1 for path in output.rglob("*.o") if path.is_file())
    archive_count = sum(1 for suffix in ("*.a", "*.so", "*.jar") for path in output.rglob(suffix) if path.is_file())
    meta["progress"] = {
        "compile_complete": state.get("compile_complete") is True,
        "artifact_verified": state.get("artifact_verified") is True,
        "object_file_count": object_count,
        "linked_artifact_count": archive_count,
        "output_bytes": sum(path.stat().st_size for path in output.rglob("*") if path.is_file()),
    }
    for key, name in (("siso_state_sha256", ".siso_fs_state"),
                      ("siso_journal_sha256", ".siso_fs_state.journal")):
        path = output / name
        meta[key] = sha256_file(path) if path.is_file() else None
    return meta


def pack(root: Path, stage: str, *, require_siso_state: bool = False) -> None:
    root = root.resolve()
    if stage not in ALLOWED_STAGES:
        raise CheckpointError("Unsupported compile stage: {}".format(stage))
    source, output = root / "src", root / "src" / OUTPUT_REL
    if not source.is_dir() or not output.is_dir():
        raise CheckpointError("Expected Chromium source/output directory is missing")
    state = read_json(output / STATE_NAME)
    validate_state(state)
    # Siso may leave an orphan journal or dangling state symlink on shutdown.
    # Quarantine that invalid pair before archiving; never discard build outputs.
    quarantine_incomplete_siso_checkpoint_pair(output, root, stage)
    if require_siso_state:
        # Fail before creating/replacing an archive if Siso did not leave a
        # complete state pair after a real compile window. Never silently label
        # an output-only archive as a resumable compile checkpoint.
        validate_siso_checkpoint_pair(output, require_pair=True)
        print("SANDIUM_CHECKPOINT_REQUIRE_SISO_STATE=PASS")
    archive, manifest = root / "sandium-checkpoint.tar.zst", root / "sandium-checkpoint.meta"
    fd, temp_name = tempfile.mkstemp(prefix=".sandium-checkpoint-", suffix=".tar.zst.tmp", dir=root)
    os.close(fd)
    temp_archive, temp_manifest = Path(temp_name), root / ".sandium-checkpoint.meta.tmp"
    try:
        try:
            subprocess.run(["tar", "-c", "-I", "zstd -T2 -3", "--format=posix",
                            "--pax-option=delete=atime,delete=ctime", "-f", str(temp_archive),
                            "-C", str(source), OUTPUT_REL.as_posix()], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CheckpointError("tar/zstd checkpoint pack failed: {}".format(exc)) from exc
        list_members(temp_archive)
        meta = metadata_for(output, state, stage, require_siso_state=require_siso_state)
        meta["archive_bytes"] = temp_archive.stat().st_size
        meta["archive_sha256"] = sha256_file(temp_archive)
        temp_manifest.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp_archive, archive)
        os.replace(temp_manifest, manifest)
    finally:
        temp_archive.unlink(missing_ok=True)
        temp_manifest.unlink(missing_ok=True)
    print("SANDIUM_CHECKPOINT_PACK=PASS")
    print("SANDIUM_CHECKPOINT_STAGE={}".format(stage))
    print("SANDIUM_CHECKPOINT_BYTES={}".format(archive.stat().st_size))
    print("SANDIUM_CHECKPOINT_SHA256={}".format(meta["archive_sha256"]))
    print("SANDIUM_CHECKPOINT_RESUMABILITY={}".format("RESUMABLE_SISO_STATE" if meta.get("siso_state_sha256") and meta.get("siso_journal_sha256") else "OUTPUTS_ONLY_INCREMENTAL"))


def restore(root: Path) -> int:
    root = root.resolve()
    archive, manifest = root / "sandium-checkpoint.tar.zst", root / "sandium-checkpoint.meta"
    if not archive.is_file() or not manifest.is_file():
        print("SANDIUM_CHECKPOINT_RESTORE=MISS; preserving any legacy raw output cache")
        return 0
    temp_root: Path | None = None
    try:
        meta = read_json(manifest)
        if meta.get("schema") != SCHEMA or meta.get("output_rel_path") != OUTPUT_REL.as_posix():
            raise CheckpointError("Checkpoint schema/output path is incompatible")
        if meta.get("archive_bytes") != archive.stat().st_size:
            raise CheckpointError("Checkpoint archive size differs from metadata")
        if meta.get("archive_sha256") != sha256_file(archive):
            raise CheckpointError("Checkpoint archive SHA-256 differs from metadata")
        list_members(archive)
        temp_root = Path(tempfile.mkdtemp(prefix=".sandium-checkpoint-restore-", dir=root))
        try:
            subprocess.run(["tar", "-x", "-I", "zstd -T1 -d", "-f", str(archive), "-C", str(temp_root)],
                           check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CheckpointError("Checkpoint extraction failed: {}".format(exc)) from exc
        restored = temp_root / OUTPUT_REL
        state_file, args_file, ninja_file = restored / STATE_NAME, restored / "args.gn", restored / "build.ninja"
        for path in (state_file, args_file, ninja_file):
            required_file(path)
        state = read_json(state_file)
        validate_state(state)
        validate_siso_checkpoint_pair(restored)
        for field in ("source_commit", "chromium_commit", "chromium_version", "target",
                      "build_config", "build_input_fingerprint", "gn_args_sha256"):
            if state.get(field) != meta.get(field):
                raise CheckpointError("Checkpoint metadata mismatch for {}".format(field))
        check_files = {
            "state_sha256": state_file, "args_gn_sha256": args_file,
            "build_ninja_sha256": ninja_file,
            "siso_state_sha256": restored / ".siso_fs_state",
            "siso_journal_sha256": restored / ".siso_fs_state.journal",
        }
        for field, path in check_files.items():
            expected = meta.get(field)
            if expected is None:
                if field.startswith("siso_") and path.exists():
                    raise CheckpointError("Unexpected Siso checkpoint file: {}".format(path.name))
            elif not path.is_file() or sha256_file(path) != expected:
                raise CheckpointError("Checkpoint file failed integrity validation: {}".format(path.name))
        destination = root / "src" / OUTPUT_REL
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup = temp_root / "previous-output"
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(restored, destination)
        except OSError:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        print("SANDIUM_CHECKPOINT_RESTORE=PASS")
        print("SANDIUM_CHECKPOINT_STAGE={}".format(meta.get("stage", "unknown")))
        print("SANDIUM_CHECKPOINT_RESTORED_SHA256={}".format(meta["archive_sha256"]))
        # Explicitly report whether the restored checkpoint carries resumable
        # Siso filesystem metadata. Restore PASS alone permits both files to be
        # absent; it must not be misread as proof that Siso state was restored.
        siso_state = destination / ".siso_fs_state"
        siso_journal = destination / ".siso_fs_state.journal"
        print("SANDIUM_SISO_STATE_PRESENT=" + str(siso_state.is_file() and not siso_state.is_symlink()).lower())
        print("SANDIUM_SISO_JOURNAL_PRESENT=" + str(siso_journal.is_file() and not siso_journal.is_symlink()).lower())
        if siso_state.is_file() and not siso_state.is_symlink():
            print("SANDIUM_SISO_STATE_BYTES=" + str(siso_state.stat().st_size))
            print("SANDIUM_SISO_STATE_SHA256=" + sha256_file(siso_state))
        if siso_journal.is_file() and not siso_journal.is_symlink():
            print("SANDIUM_SISO_JOURNAL_BYTES=" + str(siso_journal.stat().st_size))
            print("SANDIUM_SISO_JOURNAL_SHA256=" + sha256_file(siso_journal))
        return 0
    except (CheckpointError, OSError, ValueError) as exc:
        print("SANDIUM_CHECKPOINT_RESTORE=REJECTED ({})".format(exc))
        print("SANDIUM_CHECKPOINT_RESTORE=MISS; invalid archive did not replace existing output")
        return 0
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pack", "restore"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--stage", choices=ALLOWED_STAGES, default=None)
    parser.add_argument("--require-siso-state", action="store_true", help="fail pack unless both nonempty Siso state and journal files are present")
    args = parser.parse_args()
    try:
        if args.action == "pack":
            if not args.stage:
                parser.error("--stage is required for pack")
            pack(args.root, args.stage, require_siso_state=args.require_siso_state)
            return 0
        return restore(args.root)
    except (CheckpointError, OSError, ValueError) as exc:
        print("SANDIUM_CHECKPOINT_PACK=FAIL ({})".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
