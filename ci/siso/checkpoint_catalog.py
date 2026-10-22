#!/usr/bin/env python3
"""Rank saved SANDIUM checkpoint manifests by validated build progress.

This module deliberately ranks manifests only. It does not pretend CircleCI
cache-prefix restoration can enumerate candidates or fetch arbitrary archives.
A storage adapter must discover candidate manifests and validate each archive
before the returned candidate is restored.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

IDENTITY_FIELDS = (
    "source_commit", "chromium_commit", "chromium_version", "target",
    "build_config", "build_input_fingerprint", "gn_args_sha256",
)
STAGE_RE = re.compile(r"^stage-([1-5])$")


class CatalogError(ValueError):
    pass


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogError(f"manifest is not an object: {path}")
    return value


def compatible(candidate: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Only compare candidates with a complete, exact build identity."""
    if candidate.get("schema") != 1:
        return False
    if not STAGE_RE.fullmatch(str(candidate.get("stage", ""))):
        return False
    for field in IDENTITY_FIELDS:
        value = expected.get(field)
        if not isinstance(value, str) or not value or candidate.get(field) != value:
            return False
    progress = candidate.get("progress")
    if not isinstance(progress, dict):
        return False
    for field in ("compile_complete", "artifact_verified"):
        if not isinstance(progress.get(field), bool):
            return False
    for field in ("object_file_count", "linked_artifact_count", "output_bytes"):
        if not isinstance(progress.get(field), int) or progress[field] < 0:
            return False
    return True


def rank(candidate: dict[str, Any]) -> tuple[int, int, int, int, int, str]:
    """Rank by real completion/progress; stage and timestamp are tie-breakers."""
    progress = candidate["progress"]
    stage_match = STAGE_RE.fullmatch(candidate["stage"])
    stage = int(stage_match.group(1)) if stage_match else 0
    return (
        int(progress["artifact_verified"]),
        int(progress["compile_complete"]),
        progress["object_file_count"],
        progress["linked_artifact_count"],
        progress["output_bytes"],
        f"{stage:01d}:{candidate.get('created_at_utc', '')}",
    )


def choose(candidates: list[dict[str, Any]], expected: dict[str, Any]) -> dict[str, Any] | None:
    """Return the highest-progress compatible manifest; ignore malformed entries."""
    valid = [item for item in candidates if compatible(item, expected)]
    return max(valid, key=rank) if valid else None


def catalog(directory: Path, expected: dict[str, Any]) -> dict[str, Any] | None:
    """Read *.meta candidates from a local catalog directory and select the best."""
    candidates: list[dict[str, Any]] = []
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.meta")):
        try:
            candidates.append(load_manifest(path))
        except CatalogError:
            continue
    return choose(candidates, expected)
