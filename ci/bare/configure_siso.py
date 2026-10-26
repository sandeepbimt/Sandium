#!/usr/bin/env python3
"""Inspect or write Chromium-native Siso/REAPI settings for Sandium's Bare build.

This script edits only the generated workspace .gclient file. It never reads,
prints or writes the BuildBuddy API key, and never runs gclient, Siso or a build.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import pprint
import tempfile
from pathlib import Path
from typing import Any

REAPI_ADDRESS = "remote.buildbuddy.io:443"
REAPI_INSTANCE = "sandium/android-arm64/ci"
CUSTOM_VAR_VALUES = {
    "reapi_address": REAPI_ADDRESS,
    "reapi_instance": REAPI_INSTANCE,
}


class ConfigError(RuntimeError):
    pass


def control_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workspace_root(control: Path) -> Path:
    # CircleCI keeps all job state beneath $HOME/project, even if the control
    # repository is moved into a subdirectory. A local checkout defaults to its
    # own root; no new CI variable is required.
    expected = (Path.home() / "project").resolve()
    try:
        control.resolve().relative_to(expected)
    except ValueError:
        return control
    return expected


def parse_gclient(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Generated .gclient file is missing: {path}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            raise ConfigError("Unexpected .gclient syntax; refusing to rewrite it")
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            raise ConfigError("Unexpected .gclient assignment target")
        try:
            values[target.id] = ast.literal_eval(node.value)
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                f".gclient assignment {target.id!r} is not a literal; refusing to rewrite it"
            ) from exc
    return values


def chromium_solution(values: dict[str, Any]) -> dict[str, Any]:
    solutions = values.get("solutions")
    if not isinstance(solutions, list):
        raise ConfigError(".gclient must contain a solutions list")
    matches = [
        item for item in solutions
        if isinstance(item, dict)
        and (
            item.get("name") == "src"
            or "chromium.googlesource.com/chromium/src" in str(item.get("url", ""))
        )
    ]
    if len(matches) != 1:
        raise ConfigError("Expected exactly one Chromium src solution in .gclient")
    solution = matches[0]
    if not isinstance(solution.get("custom_vars", {}), dict):
        raise ConfigError("Chromium solution custom_vars must be a dictionary")
    solution.setdefault("custom_vars", {})
    return solution


def expected_values(control: Path) -> dict[str, str]:
    backend = (control / "ci" / "siso" / "buildbuddy_backend.star").resolve()
    helper = (control / "ci" / "siso" / "buildbuddy-credential-helper.py").resolve()
    if not backend.is_file():
        raise ConfigError(f"Existing Sandium BuildBuddy backend file is missing: {backend}")
    if not helper.is_file():
        raise ConfigError(f"Existing Sandium credential helper is missing: {helper}")
    if not os.access(helper, os.X_OK):
        raise ConfigError(
            f"Existing Sandium credential helper is not executable: {helper}; "
            "the repository file mode must be 100755"
        )
    return {
        **CUSTOM_VAR_VALUES,
        "reapi_backend_config_path": str(backend),
        "reapi_credential_helper": str(helper),
    }


def render(values: dict[str, Any]) -> str:
    # .gclient is Python literal syntax. Preserve every existing literal assignment
    # and change only the Chromium solution's custom_vars values.
    lines = []
    for name in sorted(values):
        lines.append(f"{name} = {pprint.pformat(values[name], sort_dicts=True)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the expected non-secret REAPI settings to the generated .gclient file",
    )
    args = parser.parse_args()

    control = control_root()
    workspace = args.workspace.resolve() if args.workspace else workspace_root(control)
    gclient = workspace / ".gclient"
    values = parse_gclient(gclient)
    solution = chromium_solution(values)
    expected = expected_values(control)
    current = solution["custom_vars"]
    drift = {
        key: {"current": current.get(key), "expected": value}
        for key, value in expected.items()
        if current.get(key) != value
    }

    print("SANDIUM_SOURCE=BARE")
    print(f"SANDIUM_GCLIENT_PATH={gclient}")
    print(f"SANDIUM_REAPI_ADDRESS={REAPI_ADDRESS}")
    print(f"SANDIUM_REAPI_INSTANCE={REAPI_INSTANCE}")
    print(f"SANDIUM_BACKEND_CONFIG={expected['reapi_backend_config_path']}")
    print(f"SANDIUM_CREDENTIAL_HELPER={expected['reapi_credential_helper']}")
    print(f"SANDIUM_GCLIENT_SHA256_BEFORE={hashlib.sha256(gclient.read_bytes()).hexdigest()}")
    print(f"SANDIUM_GCLIENT_REAPI_DRIFT_KEYS={','.join(sorted(drift)) or 'none'}")

    if not args.write:
        print("SANDIUM_GCLIENT_WRITE=NOT_REQUESTED")
        print("SANDIUM_GCLIENT_READY=" + ("PASS" if not drift else "NEEDS_WRITE"))
        return 0

    current.update(expected)
    payload = render(values)
    # Write atomically in the same directory so a partial write cannot corrupt .gclient.
    fd, temp_name = tempfile.mkstemp(prefix=".gclient.sandium.", dir=str(workspace))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, gclient)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass

    verified = parse_gclient(gclient)
    verified_solution = chromium_solution(verified)
    for key, value in expected.items():
        if verified_solution["custom_vars"].get(key) != value:
            raise ConfigError(f"Generated .gclient setting did not persist: {key}")
    print(f"SANDIUM_GCLIENT_SHA256_AFTER={hashlib.sha256(gclient.read_bytes()).hexdigest()}")
    print("SANDIUM_GCLIENT_WRITE=PASS")
    print("SANDIUM_GCLIENT_READY=PASS")
    print("GCLIENT_SYNC_RUN=no")
    print("SISO_RUN=no")
    print("CHROMIUM_BUILD_RUN=no")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        raise SystemExit(f"SANDIUM_SISO_CONFIG_ERROR={exc}")
