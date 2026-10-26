#!/usr/bin/env python3
"""Prime and validate the Siso/Ninja manifest before a timed compile window.

A single initial "Regenerating ninja files" event is a supported GN/Ninja/Siso
recovery path. Run a no-op autoninja pass before the expensive compile so that
any required manifest regeneration happens outside the compile window. If the
manifest regenerates on both consecutive dry runs, fail early instead of
spending most of a stage budget before discovering an unstable graph.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
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


def has_manifest_regeneration(text: str) -> bool:
    # Siso emits this same event in several progress/summary lines. Treat all
    # occurrences in one invocation as one regeneration event, not many.
    return "Regenerating ninja files" in text


def build_command(output_dir: Path, timeout_seconds: int) -> list[str]:
    return [
        "timeout",
        "--signal=INT",
        "--kill-after=15s",
        f"{timeout_seconds}s",
        "autoninja",
        "-n",
        "-C",
        str(output_dir),
        "-j80",
        "chrome_public_apk",
    ]


def run_preflight(
    control: Path,
    root: Path,
    source_root: Path,
    output_dir: Path,
    stage: int,
    log_path: Path,
    timeout_seconds: int = 180,
) -> int:
    if timeout_seconds < 15:
        raise ManifestPreflightError("timeout must be at least 15 seconds")
    if not source_root.is_dir() or source_root.is_symlink():
        raise ManifestPreflightError(f"Unsafe/missing Chromium source root: {source_root}")
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise ManifestPreflightError(f"Unsafe/missing GN output directory: {output_dir}")

    before_identity = identity_digest(control, root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    command = build_command(output_dir, timeout_seconds)

    for attempt in (1, 2):
        attempt_path = log_path.with_name(f".{log_path.name}.attempt-{attempt}.tmp")
        with attempt_path.open("w", encoding="utf-8", errors="replace") as output:
            result = subprocess.run(
                command,
                cwd=source_root,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
        text = attempt_path.read_text(encoding="utf-8", errors="replace")
        with log_path.open("a", encoding="utf-8") as aggregate:
            aggregate.write(f"===== SISO MANIFEST PREFLIGHT ATTEMPT {attempt} =====\n")
            aggregate.write(text)
            if text and not text.endswith("\n"):
                aggregate.write("\n")
            aggregate.write(f"===== ATTEMPT {attempt} EXIT CODE {result.returncode} =====\n")
        attempt_path.unlink(missing_ok=True)

        if result.returncode != 0:
            raise ManifestPreflightError(
                f"autoninja dry-run failed on attempt {attempt} with exit code {result.returncode}"
            )
        after_identity = identity_digest(control, root)
        if after_identity != before_identity:
            raise ManifestPreflightError(
                "Source/toolchain/GN build identity changed during manifest preflight"
            )

        regenerated = has_manifest_regeneration(text)
        print(
            f"SANDIUM_NINJA_MANIFEST_PREFLIGHT_ATTEMPT={attempt} "
            f"REGENERATED={'yes' if regenerated else 'no'}"
        )
        if regenerated:
            if attempt == 1:
                # GN has refreshed the graph. Verify the refreshed graph is stable
                # on a second no-op pass before starting remote compilation.
                continue
            raise ManifestPreflightError(
                "build.ninja regenerated on two consecutive dry runs; refusing to "
                "spend the build window on an unstable graph"
            )

        print(f"SANDIUM_NINJA_MANIFEST_PREFLIGHT=PASS attempts={attempt}")
        print(f"SANDIUM_NINJA_MANIFEST_PREFLIGHT_LOG={log_path}")
        print(f"SANDIUM_NINJA_MANIFEST_BUILD_IDENTITY_SHA256={before_identity}")
        return 0

    raise ManifestPreflightError("Manifest preflight exhausted without reaching a stable graph")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--root", type=Path, default=Path.home() / "project")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stage", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    try:
        return run_preflight(
            control=args.control.resolve(),
            root=args.root.resolve(),
            source_root=args.source_root.resolve(),
            output_dir=args.output_dir.resolve(),
            stage=args.stage,
            log_path=args.log.resolve(),
            timeout_seconds=args.timeout_seconds,
        )
    except (ManifestPreflightError, IdentityError, OSError, subprocess.SubprocessError) as exc:
        print(f"SANDIUM_NINJA_MANIFEST_PREFLIGHT=FAIL reason={exc}", file=sys.stderr)
        if args.log.exists():
            tail = args.log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
            for line in tail:
                print(line, file=sys.stderr)
        return 52


if __name__ == "__main__":
    raise SystemExit(main())
