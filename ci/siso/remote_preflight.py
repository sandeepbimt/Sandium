#!/usr/bin/env python3
"""Prove one fresh C++ action completes remotely before starting the full browser compile."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_TARGET = "obj/base/test/test_support/mock_log.o"
PROBE_OUTPUT_SUFFIX = "obj/base/test/test_support/mock_log.o"
PROBE_TIMEOUT_SECONDS = 90
MAX_STAGE_SECONDS = 3000
HANDOVER_RESERVE_SECONDS = 600
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def has_probe_rule(fragment: Path) -> bool:
    """Check the generated GN subninja that owns the test_support C++ rule."""
    if not fragment.is_file() or fragment.stat().st_size == 0:
        return False
    with fragment.open(encoding="utf-8", errors="replace") as stream:
        return any(
            line.startswith("build " + PROBE_TARGET + " ")
            and "../../base/test/mock_log.cc" in line
            for line in stream
        )


def parse_remote_probe_metrics(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise ValueError("Siso metrics JSONL was not produced: " + str(path))
    records = targets = remote_targets = 0
    with path.open(encoding="utf-8", errors="replace") as stream:
        for n, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("Invalid Siso metrics JSONL line " + str(n)) from exc
            if not isinstance(item, dict) or "step_id" not in item or not isinstance(item.get("outputs"), list):
                continue
            records += 1
            if not any(isinstance(x, str) and x.replace("\\", "/").endswith(PROBE_OUTPUT_SUFFIX)
                       for x in item["outputs"]):
                continue
            targets += 1
            remote = item.get("is_remote") is True or item.get("is_local") is False
            if remote and item.get("cached") is not True and item.get("no_exec") is not True:
                remote_targets += 1
    return {"action_records": records, "target_action_records": targets,
            "uncached_remote_target_actions": remote_targets}


def load_valid_marker(path: Path, workflow_id: str, source_commit: str,
                      chromium_commit: str, chromium_version: str) -> dict[str, Any]:
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Remote preflight marker missing or invalid: " + str(exc)) from exc
    if not isinstance(marker, dict):
        raise ValueError("Remote preflight marker is not an object")
    expected = {
        "schema": 1, "status": "PASS", "workflow_id": workflow_id,
        "source_commit": source_commit, "chromium_commit": chromium_commit,
        "chromium_version": chromium_version, "probe_target": PROBE_TARGET,
        "probe_output_suffix": PROBE_OUTPUT_SUFFIX, "siso_state_pair_valid": True,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise ValueError("Remote preflight mismatch for " + key)
    if int(marker.get("probe_remote_action_records", 0)) < 1:
        raise ValueError("Remote preflight does not prove an uncached remote C++ action")
    if not isinstance(marker.get("metrics_sha256"), str) or not DIGEST_RE.fullmatch(marker["metrics_sha256"]):
        raise ValueError("Remote preflight metrics digest is invalid")
    return marker


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def run(root: Path) -> int:
    root = root.resolve()
    source, brave = root / "src", root / "src/brave"
    out = source / "out/android_Debug_arm64"
    diagnostics = root / "build-diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    marker_path = out / ".sandium-remote-preflight.json"
    metrics_path = diagnostics / "remote-preflight-metrics.jsonl"
    log_path = diagnostics / "remote-preflight.log"
    marker_path.unlink(missing_ok=True)
    metrics_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)

    workflow_id = os.environ.get("CIRCLE_WORKFLOW_ID", "")
    source_commit = os.environ.get("CIRCLE_SHA1", "")
    api_key = os.environ.get("BUILDBUDDY_API_KEY", "")
    if not workflow_id or not source_commit or not api_key:
        raise ValueError("CIRCLE_WORKFLOW_ID, CIRCLE_SHA1 and BUILDBUDDY_API_KEY are required")

    helper = brave / "ci/siso/buildbuddy-credential-helper.py"
    for path in (helper, brave / "ci/pins/chromium-tag.txt",
                 source / "base/test/mock_log.cc", out / "build.ninja",
                 out / "args.gn", out / "args_generated.gni",
                 source / "build/config/siso/.sisoenv",
                 source / "build/config/siso/.sisorc"):
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Required preflight input missing or empty: " + str(path))
    if not os.access(helper, os.X_OK):
        raise ValueError("BuildBuddy credential helper is not executable: " + str(helper))
    gclient = root / ".gclient"
    if not gclient.is_file() or str(helper) not in gclient.read_text(encoding="utf-8"):
        raise ValueError(".gclient does not contain the expected BuildBuddy credential-helper path")
    sisoenv = (source / "build/config/siso/.sisoenv").read_text(encoding="utf-8", errors="replace")
    if "remote.buildbuddy.io:443" not in sisoenv or "sandium/android-arm64/ci" not in sisoenv:
        raise ValueError("Generated Siso environment does not select the configured BuildBuddy endpoint/instance")

    env = os.environ.copy()
    env["SISO_CREDENTIAL_HELPER"] = str(helper)
    auth = subprocess.run(
        [sys.executable, str(helper), "get"],
        input=json.dumps({"uri": "https://remote.buildbuddy.io:443"}),
        text=True, capture_output=True, check=False, timeout=10, env=env,
    )
    if auth.returncode:
        raise ValueError("BuildBuddy credential-helper protocol failed: " + auth.stderr[:1000])
    try:
        auth_json = json.loads(auth.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("BuildBuddy credential helper returned invalid JSON") from exc
    if auth_json.get("headers", {}).get("x-buildbuddy-api-key") != [api_key]:
        raise ValueError("BuildBuddy credential helper returned an unexpected auth header")
    print("SANDIUM_CREDENTIAL_HELPER_PROTOCOL=PASS secret_value_redacted=true")
    print("SISO_CREDENTIAL_HELPER=" + str(helper))

    chromium_commit = git(source, "rev-parse", "HEAD")
    chromium_version = git(source, "describe", "--tags", "--exact-match", "HEAD")
    pins = [x.strip() for x in (brave / "ci/pins/chromium-tag.txt").read_text().splitlines()
            if x.strip() and not x.lstrip().startswith("#")]
    if pins != [chromium_version]:
        raise ValueError("Resolved Chromium tag does not exactly match the manual pin")

    clock = Path.home() / ".sandium-stage-start-epoch"
    try:
        started = int(clock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise ValueError("Stage budget clock is unavailable: " + str(exc)) from exc
    elapsed = max(0, int(time.time()) - started)
    available = MAX_STAGE_SECONDS - elapsed - HANDOVER_RESERVE_SECONDS
    print("REMOTE_PREFLIGHT_STAGE_ELAPSED_SECONDS=" + str(elapsed))
    print("REMOTE_PREFLIGHT_SECONDS_AVAILABLE_BEFORE_HANDOVER=" + str(available))
    if available < PROBE_TIMEOUT_SECONDS + 30:
        raise ValueError("Insufficient stage budget for probe plus handover; full compile will not start")

    target = out / PROBE_TARGET
    target.unlink(missing_ok=True)
    Path(str(target) + ".dwo").unlink(missing_ok=True)
    # GN emits per-target rules in subninja fragments; this object is in
    # obj/base/test/test_support.ninja, not necessarily in root build.ninja.
    target_fragment = out / "obj/base/test/test_support.ninja"
    if not has_probe_rule(target_fragment):
        raise ValueError(
            "Pinned GN subninja does not contain the expected test_support "
            "C++ rule for obj/base/test/test_support/mock_log.o"
        )

    autoninja = shutil.which("autoninja") or str(brave / "vendor/depot_tools/autoninja")
    if not Path(autoninja).is_file() or not os.access(autoninja, os.X_OK):
        raise ValueError("autoninja is not executable in the configured build environment")
    timeout_bin = shutil.which("timeout")
    if not timeout_bin:
        raise ValueError("GNU timeout is required for the bounded remote probe")
    env["PATH"] = str(brave / "vendor/depot_tools") + os.pathsep + str(source / "buildtools/linux64") + os.pathsep + env.get("PATH", "")
    salt = "sandium-remote-preflight-" + uuid.uuid4().hex
    command = [
        timeout_bin, "--signal=INT", "--kill-after=10s", str(PROBE_TIMEOUT_SECONDS) + "s",
        autoninja, "-C", str(out), PROBE_TARGET, "-strict_remote",
        "-remote_jobs=1", "-local_jobs=1", "-action_salt=" + salt,
        "-metrics_json", str(metrics_path),
    ]
    print("REMOTE_PREFLIGHT_COMMAND=autoninja single-file C++ target, strict remote, unique action salt, one remote job")
    try:
        result = subprocess.run(command, cwd=root, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                check=False, timeout=PROBE_TIMEOUT_SECONDS + 20)
        output, status = result.stdout or "", result.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        log_path.write_text(output.replace(api_key, "[REDACTED]"), encoding="utf-8")
        raise ValueError("Strict-remote C++ preflight exceeded its hard timeout") from exc
    sanitized = output.replace(api_key, "[REDACTED]")
    log_path.write_text(sanitized, encoding="utf-8", errors="replace")
    if sanitized:
        print("REMOTE_PREFLIGHT_DIAGNOSTICS_BEGIN")
        print(sanitized[-24000:])
        print("REMOTE_PREFLIGHT_DIAGNOSTICS_END")
    if status:
        raise ValueError("Strict-remote C++ probe failed with exit code " + str(status))
    if not target.is_file() or target.stat().st_size == 0:
        raise ValueError("Strict-remote probe did not produce its expected object file")

    counts = parse_remote_probe_metrics(metrics_path)
    for name, value in counts.items():
        print("REMOTE_PREFLIGHT_" + name.upper() + "=" + str(value))
    if counts["uncached_remote_target_actions"] < 1:
        raise ValueError("No uncached remote result for the probe object; full Chromium compile is blocked")

    state, journal = out / ".siso_fs_state", out / ".siso_fs_state.journal"
    if not state.is_file() or not journal.is_file() or state.stat().st_size == 0 or journal.stat().st_size == 0:
        raise ValueError("Remote probe ended without a complete, nonempty Siso state/journal pair")

    marker = {
        "schema": 1, "status": "PASS", "workflow_id": workflow_id,
        "source_commit": source_commit, "chromium_commit": chromium_commit,
        "chromium_version": chromium_version, "probe_target": PROBE_TARGET,
        "probe_output_suffix": PROBE_OUTPUT_SUFFIX,
        "probe_remote_action_records": counts["uncached_remote_target_actions"],
        "probe_action_records": counts["action_records"],
        "metrics_sha256": hashlib.sha256(metrics_path.read_bytes()).hexdigest(),
        "action_salt_sha256": hashlib.sha256(salt.encode("utf-8")).hexdigest(),
        "siso_state_pair_valid": True,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    temporary = marker_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    temporary.replace(marker_path)
    print("SANDIUM_REMOTE_PREFLIGHT=PASS")
    print("SANDIUM_PREFLIGHT_REMOTE_EXECUTED_ACTION_RECORDS=" + str(marker["probe_remote_action_records"]))
    print("SANDIUM_PREFLIGHT_SISO_STATE_PAIR=PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        return run(args.root)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("SANDIUM_REMOTE_PREFLIGHT=FAIL", file=sys.stderr)
        print("SANDIUM_REMOTE_PREFLIGHT_REASON=" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
