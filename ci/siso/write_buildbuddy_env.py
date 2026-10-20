#!/usr/bin/env python3
"""Generate ignored runtime Brave .env settings for Sandium's BuildBuddy adapter.

This script performs local file operations only. It does not invoke Siso,
gclient, Chromium, BuildBuddy, or any network service.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Mapping

BEGIN_MARKER = "# BEGIN SANDIUM BUILDBUDDY RUNTIME CONFIG"
END_MARKER = "# END SANDIUM BUILDBUDDY RUNTIME CONFIG"
EXPECTED_ADDRESS = "remote.buildbuddy.io:443"
BACKEND_CONFIG_RELATIVE = Path("ci/siso/buildbuddy_backend.star")
CHROMIUM_PIN_RELATIVE = Path("ci/pins/chromium-tag.txt")
CHROMIUM_TAG_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
SISO_VERSION_PIN = "git_revision:bc45e8f67ae0f37d337190ad64aa8bb440c791eb"
SISO_VERSION_LABEL = "v1.5.28"


def validate_instance(instance: str) -> None:
    if not isinstance(instance, str) or not re.fullmatch(
        r"sandium/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", instance
    ):
        raise ValueError(
            "SANDIUM_REAPI_INSTANCE must use a stable Sandium-prefixed "
            "namespace such as sandium/android-arm64/ci"
        )
    if any(part in (".", "..") for part in instance.split("/")):
        raise ValueError("SANDIUM_REAPI_INSTANCE contains an unsafe path component")


def validate_siso_cache_dir(value: str) -> str:
    """Validate a stable absolute Siso cache directory shared by CI."""
    if not isinstance(value, str) or not value:
        raise ValueError("SANDIUM_SISO_CACHE_DIR is required")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError("SANDIUM_SISO_CACHE_DIR must be an absolute path")
    resolved = str(candidate.resolve())
    if " " in resolved:
        raise ValueError("SANDIUM_SISO_CACHE_DIR cannot contain spaces")
    return resolved


def read_manual_chromium_tag(core_dir: Path) -> str:
    """Read the explicitly approved Chromium source tag for Sandium."""
    pin_path = core_dir / CHROMIUM_PIN_RELATIVE
    if not pin_path.is_file():
        raise FileNotFoundError(
            f"Manual Chromium pin file is missing: {pin_path}"
        )
    tags = [
        line.strip()
        for line in pin_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(tags) != 1 or not CHROMIUM_TAG_PATTERN.fullmatch(tags[0]):
        raise ValueError(
            f"Manual Chromium pin must contain exactly one four-part numeric "
            f"tag in {pin_path}"
        )
    return tags[0]


def render_block(
    core_dir: Path,
    address: str,
    instance: str,
    siso_cache_dir: str,
) -> str:
    if address != EXPECTED_ADDRESS:
        raise ValueError(
            "SANDIUM_REAPI_ADDRESS must equal the reviewed candidate "
            + EXPECTED_ADDRESS
        )
    validate_instance(instance)
    siso_cache_dir = validate_siso_cache_dir(siso_cache_dir)
    chromium_tag = read_manual_chromium_tag(core_dir)

    helper_path = (core_dir / "ci/siso/buildbuddy-credential-helper.py").resolve()
    if not helper_path.is_file():
        raise FileNotFoundError(f"Credential helper not found: {helper_path}")
    backend_path = (core_dir / BACKEND_CONFIG_RELATIVE).resolve()
    if not backend_path.is_file():
        raise FileNotFoundError(f"Siso backend config not found: {backend_path}")
    if " " in str(core_dir.resolve()):
        raise ValueError("Brave build root cannot contain spaces")

    values = [
        BEGIN_MARKER,
        f"rbe_service={address}",
        f"projects_chrome_custom_vars_reapi_address={address}",
        f"projects_chrome_custom_vars_reapi_instance={instance}",
        f"projects_chrome_custom_vars_reapi_backend_config_path={backend_path}",
        f"projects_chrome_custom_vars_reapi_credential_helper={helper_path}",
        f"projects_chrome_custom_vars_siso_version={SISO_VERSION_PIN}",
        f"sandium_chromium_tag={chromium_tag}",
        f"siso_cache_dir={siso_cache_dir}",
        "siso_use_googlechrome_config=false",
        "use_remoteexec=false",
        END_MARKER,
    ]
    return "\n".join(values)


def replace_managed_block(existing: str, block: str) -> str:
    lines = existing.splitlines()
    begin_indices = [i for i, line in enumerate(lines) if line == BEGIN_MARKER]
    end_indices = [i for i, line in enumerate(lines) if line == END_MARKER]

    if not begin_indices and not end_indices:
        preserved = existing.rstrip()
    else:
        if len(begin_indices) != 1 or len(end_indices) != 1:
            raise ValueError("Malformed managed SANDIUM BuildBuddy block in .env")
        start, end = begin_indices[0], end_indices[0]
        if start >= end:
            raise ValueError("Malformed managed SANDIUM BuildBuddy block in .env")
        preserved = "\n".join(lines[:start] + lines[end + 1 :]).rstrip()

    if preserved:
        return preserved + "\n\n" + block + "\n"
    return block + "\n"


def apply_config(core_dir: Path, environ: Mapping[str, str]) -> Path:
    core_dir = core_dir.resolve()
    address = environ.get("SANDIUM_REAPI_ADDRESS", "")
    instance = environ.get("SANDIUM_REAPI_INSTANCE", "")
    # Default to a stable project-root path so every CircleCI job resolves
    # the same cache even if a caller omits the explicit override.
    siso_cache_dir = environ.get("SANDIUM_SISO_CACHE_DIR") or str(
        core_dir.parents[1] / "sandium-siso-cache"
    )
    block = render_block(core_dir, address, instance, siso_cache_dir)

    env_file = core_dir / ".env"
    existing = env_file.read_text(encoding="utf-8") if env_file.exists() else (
        "# Local runtime configuration; do not commit generated settings.\n"
    )
    updated = replace_managed_block(existing, block)
    env_file.write_text(updated, encoding="utf-8")

    helper_path = core_dir / "ci/siso/buildbuddy-credential-helper.py"
    helper_path.chmod(
        helper_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return env_file


def main() -> int:
    core_dir = Path(__file__).resolve().parents[2]
    try:
        env_file = apply_config(core_dir, os.environ)
    except (OSError, ValueError) as exc:
        print(f"Sandium BuildBuddy config generation failed: {exc}", file=sys.stderr)
        return 2

    print(f"SANDIUM_RUNTIME_ENV={env_file}")
    print(f"SANDIUM_REAPI_ADDRESS={os.environ['SANDIUM_REAPI_ADDRESS']}")
    print(f"SANDIUM_REAPI_INSTANCE={os.environ['SANDIUM_REAPI_INSTANCE']}")
    print(f"SANDIUM_SISO_CACHE_DIR={os.environ.get('SANDIUM_SISO_CACHE_DIR') or (core_dir.parents[1] / 'sandium-siso-cache')}")
    print(f"SANDIUM_SISO_VERSION_PIN={SISO_VERSION_PIN}")
    print(f"SANDIUM_SISO_VERSION_EXPECTED={SISO_VERSION_LABEL}")
    print("SANDIUM_CREDENTIAL_HELPER_EXECUTABLE=yes")
    print("SANDIUM_REMOTE_EXECUTION_ENABLED=no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
