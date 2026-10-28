#!/usr/bin/env python3
"""Cheap filesystem-only preflight for a restored Siso/Ninja output tree.

This helper deliberately does not invoke GN or Siso: it validates the restored
build identity and generated files before modifying the checkpoint. The separate
remaining-work measurement runs GN gen explicitly before autoninja -n. A
regeneration-only, zero-step Siso result is not treated as a valid measurement.
The real compile remains responsible for executing build actions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import IdentityError, source_identity


class ManifestPreflightError(RuntimeError):
    pass


def identity_digest(control: Path, root: Path) -> str:
    identity = source_identity(control, root, require_gn=True)
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def regular_file_digest(path: Path) -> tuple[str, int]:
    if not path.is_file() or path.is_symlink():
        raise ManifestPreflightError(f"Required build graph file missing/unsafe: {path.name}")
    size = path.stat().st_size
    if size <= 0:
        raise ManifestPreflightError(f"Required build graph file is empty: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest(), size


def run_preflight(
    control: Path,
    root: Path,
    source_root: Path,
    output_dir: Path,
    stage: int,
    log_path: Path,
    timeout_seconds: int = 180,
) -> int:
    # Keep the CLI argument compatible with run_stage.sh, but no external
    # command is executed here, so it is intentionally not a runtime timeout.
    if timeout_seconds < 1:
        raise ManifestPreflightError("timeout-seconds must be positive")
    if stage not in range(1, 6):
        raise ManifestPreflightError(f"invalid stage: {stage}")
    if not source_root.is_dir() or source_root.is_symlink():
        raise ManifestPreflightError(f"Unsafe/missing Chromium source root: {source_root}")
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise ManifestPreflightError(f"Unsafe/missing GN output directory: {output_dir}")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    before_identity = identity_digest(control, root)
    args_digest_before, args_bytes_before = regular_file_digest(output_dir / "args.gn")
    graph_digest_before, graph_bytes_before = regular_file_digest(output_dir / "build.ninja")

    # Check identity and generated graph once more to catch concurrent mutation
    # between restore and the build's first real Siso invocation.
    after_identity = identity_digest(control, root)
    args_digest_after, args_bytes_after = regular_file_digest(output_dir / "args.gn")
    graph_digest_after, graph_bytes_after = regular_file_digest(output_dir / "build.ninja")
    if after_identity != before_identity:
        raise ManifestPreflightError("Source/toolchain/GN build identity changed during preflight")
    if (args_digest_after, args_bytes_after) != (args_digest_before, args_bytes_before):
        raise ManifestPreflightError("args.gn changed during filesystem-only preflight")
    if (graph_digest_after, graph_bytes_after) != (graph_digest_before, graph_bytes_before):
        raise ManifestPreflightError("build.ninja changed during filesystem-only preflight")

    text = (
        "mode=filesystem-only\n"
        f"stage={stage}\n"
        f"build_identity_sha256={before_identity}\n"
        f"args_gn_bytes={args_bytes_before}\n"
        f"args_gn_sha256={args_digest_before}\n"
        f"build_ninja_bytes={graph_bytes_before}\n"
        f"build_ninja_sha256={graph_digest_before}\n"
        "gn_generation_in_preflight=no\n"
        "siso_dry_run_in_preflight=no\n"
        "manifest_regeneration_policy=handled_by_real_siso_build\n"
    )
    log_path.write_text(text, encoding="utf-8")
    print("SANDIUM_NINJA_MANIFEST_PREFLIGHT=PASS mode=filesystem-only")
    print("SANDIUM_NINJA_MANIFEST_PREFLIGHT_GN_GENERATION=NOT_RUN")
    print("SANDIUM_NINJA_MANIFEST_PREFLIGHT_SISO_DRY_RUN=NOT_RUN")
    print(f"SANDIUM_NINJA_MANIFEST_BUILD_IDENTITY_SHA256={before_identity}")
    print(f"SANDIUM_NINJA_MANIFEST_BUILDDOTNINJA_BYTES={graph_bytes_before}")
    print(f"SANDIUM_NINJA_MANIFEST_PREFLIGHT_LOG={log_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180,
                        help="compatibility argument; no subprocess is launched")
    args = parser.parse_args()
    try:
        return run_preflight(
            control=args.control.resolve(),
            root=args.root.resolve(),
            source_root=args.source_root.resolve(),
            output_dir=args.output_dir.resolve(),
            stage=args.stage,
            log_path=args.log.absolute(),
            timeout_seconds=args.timeout_seconds,
        )
    except (ManifestPreflightError, IdentityError, OSError, ValueError) as exc:
        print(f"SANDIUM_NINJA_MANIFEST_PREFLIGHT=FAIL reason={exc}", file=sys.stderr)
        if args.log.exists():
            tail = args.log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            for line in tail:
                print(line, file=sys.stderr)
        return 52


if __name__ == "__main__":
    raise SystemExit(main())
