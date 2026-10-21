#!/usr/bin/env python3
"""Prove one isolated C++ action completes remotely before the full browser compile."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_TARGET = "sandium_remote_probe.o"
PROBE_OUTPUT_SUFFIX = PROBE_TARGET
PROBE_SOURCE = "sandium_remote_probe.cc"
PROBE_NINJA = ".sandium-remote-probe.ninja"
MAX_STAGE_SECONDS = 3000
PROBE_TIMEOUT_SECONDS = 60
HANDOVER_RESERVE_SECONDS = 600
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def write_probe_files(out: Path, compiler: Path) -> tuple[Path, Path]:
    """Create a two-input, one-action Ninja graph isolated from Chromium's GN graph."""
    if not compiler.is_file() or not os.access(compiler, os.X_OK):
        raise ValueError("Pinned Chromium clang++ is missing or not executable: " + str(compiler))
    source_path = out / PROBE_SOURCE
    ninja_path = out / PROBE_NINJA
    # Declare the compiler executable and its resolved target as explicit action
    # inputs. The remote executor must stage both the clang++ symlink and the
    # binary it resolves to; mentioning the compiler in the command or as an
    # implicit dependency did not stage it in the previous preflight.
    compiler_rel = os.path.relpath(compiler, out).replace(os.sep, "/")
    real_compiler = compiler.resolve(strict=True)
    real_compiler_rel = os.path.relpath(real_compiler, out).replace(os.sep, "/")
    compiler_inputs = list(dict.fromkeys((compiler_rel, real_compiler_rel)))
    source_path.write_text(
        'extern "C" int sandium_remote_execution_probe() { return 42; }\n',
        encoding="utf-8",
    )
    # Hard-code the source in the command because $in includes the compiler
    # executable(s), which are explicit dependencies for remote input staging.
    ninja_path.write_text(
        "ninja_required_version = 1.10\n"
        "rule cxx\n"
        f"  command = {compiler_rel} -c -x c++ -fno-exceptions -fno-rtti "
        f"-fno-stack-protector -o $out {PROBE_SOURCE}\n"
        "  description = SANDIUM_REMOTE_CXX_PROBE\n"
        f"build {PROBE_TARGET}: cxx {PROBE_SOURCE} {' '.join(compiler_inputs)}\n"
        f"default {PROBE_TARGET}\n",
        encoding="utf-8",
    )
    return source_path, ninja_path


def validate_generated_siso_environment(sisoenv: str) -> None:
    """Check only the endpoint represented in .sisoenv; instance is in Brave .env."""
    if "remote.buildbuddy.io:443" not in sisoenv:
        raise ValueError("Generated Siso environment does not select remote.buildbuddy.io:443")


def validate_buildbuddy_runtime_config(runtime_env: str) -> None:
    """Validate the authoritative Brave .env values written by Sandium CI."""
    required = (
        "rbe_service=remote.buildbuddy.io:443",
        "projects_chrome_custom_vars_reapi_address=remote.buildbuddy.io:443",
        "projects_chrome_custom_vars_reapi_instance=default",
        "use_remoteexec=true",
    )
    missing = [value for value in required if value not in runtime_env.splitlines()]
    if missing:
        raise ValueError("Brave runtime BuildBuddy configuration is missing: " + ", ".join(missing))


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
        "probe_output_suffix": PROBE_OUTPUT_SUFFIX,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise ValueError("Remote preflight mismatch for " + key)
    if int(marker.get("probe_remote_action_records", 0)) < 1:
        raise ValueError("Remote preflight does not prove an uncached remote C++ action")
    if not isinstance(marker.get("metrics_sha256"), str) or not DIGEST_RE.fullmatch(marker["metrics_sha256"]):
        raise ValueError("Remote preflight metrics digest is invalid")
    return marker


def snapshot_siso_state(out: Path, backup_dir: Path) -> dict[str, Path | None]:
    """Copy pre-existing Siso state files so the isolated probe cannot poison a checkpoint."""
    snapshot: dict[str, Path | None] = {}
    for name in (".siso_fs_state", ".siso_fs_state.journal"):
        original = out / name
        if original.exists():
            if not original.is_file():
                raise ValueError("Siso state path is not a regular file: " + str(original))
            backup = backup_dir / name
            shutil.copy2(original, backup)
            snapshot[name] = backup
        else:
            snapshot[name] = None
    print(
        "REMOTE_PREFLIGHT_SISO_STATE_SNAPSHOT="
        + ",".join(name + (":present" if path else ":absent") for name, path in snapshot.items())
    )
    return snapshot


def restore_siso_state(out: Path, snapshot: dict[str, Path | None]) -> None:
    """Restore exactly the pre-probe state/journal presence and contents."""
    for name, backup in snapshot.items():
        destination = out / name
        if backup is None:
            destination.unlink(missing_ok=True)
        else:
            shutil.copy2(backup, destination)
    print("REMOTE_PREFLIGHT_SISO_STATE_RESTORED=true")


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
    runtime_env = (brave / ".env").read_text(encoding="utf-8", errors="replace")
    validate_buildbuddy_runtime_config(runtime_env)
    sisoenv = (source / "build/config/siso/.sisoenv").read_text(encoding="utf-8", errors="replace")
    validate_generated_siso_environment(sisoenv)

    env = os.environ.copy()
    env["SISO_CREDENTIAL_HELPER"] = str(helper)
    auth = subprocess.run(
        [sys.executable, str(helper), "get"],
        input=json.dumps({"uri": "https://remote.buildbuddy.io:443"}),
        text=True, capture_output=True, check=False, env=env,
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
    if available <= 0:
        raise ValueError("Stage build budget is exhausted before the remote preflight; full compile will not start")

    target = out / PROBE_TARGET
    target.unlink(missing_ok=True)
    Path(str(target) + ".dwo").unlink(missing_ok=True)
    # Do not probe a GN target: even one object can pull in thousands of
    # generated-header and library actions. Use a private Ninja file containing
    # exactly one trivial C++ compile, independent of Chromium's default graph.
    compiler = source / "third_party/llvm-build/Release+Asserts/bin/clang++"
    _, probe_ninja = write_probe_files(out, compiler)

    autoninja = shutil.which("autoninja") or str(brave / "vendor/depot_tools/autoninja")
    if not Path(autoninja).is_file() or not os.access(autoninja, os.X_OK):
        raise ValueError("autoninja is not executable in the configured build environment")
    env["PATH"] = str(brave / "vendor/depot_tools") + os.pathsep + str(source / "buildtools/linux64") + os.pathsep + env.get("PATH", "")
    salt = "sandium-remote-preflight-" + uuid.uuid4().hex
    command = [
        autoninja, "-C", str(out), "-f", probe_ninja.name, PROBE_TARGET,
        "-strict_remote", "-remote_jobs=1", "-local_jobs=1",
        "-action_salt=" + salt, "-metrics_json", str(metrics_path),
    ]
    print("REMOTE_PREFLIGHT_COMMAND=isolated one-action Ninja graph, strict remote, unique action salt, one remote job")
    # This graph has one source and one compile output, not Chromium's GN
    # dependency graph. Keep the whole proof to one short attempt so a remote
    # CAS failure cannot consume minutes before the APK compile is even allowed.
    probe_timeout = min(PROBE_TIMEOUT_SECONDS, max(1, available // 3))
    # Keep a temporary backup alive until the probe has finished.
    state_backup_context = tempfile.TemporaryDirectory(prefix="sandium-siso-state-", dir=diagnostics)
    state_snapshot = snapshot_siso_state(out, Path(state_backup_context.name))
    status = None
    max_manifest_attempts = 1
    for attempt in range(1, max_manifest_attempts + 1):
        attempt_started = time.monotonic()
        deadline = attempt_started + probe_timeout
        decoder = __import__("codecs").getincrementaldecoder("utf-8")("replace")
        attempt_log_offset = log_path.stat().st_size if log_path.exists() else 0
        mode = "w" if attempt == 1 else "a"
        with log_path.open(mode, encoding="utf-8", errors="replace") as log:
            print("REMOTE_PREFLIGHT_ATTEMPT=" + str(attempt), flush=True)
            print("REMOTE_PREFLIGHT_TIMEOUT_SECONDS=" + str(probe_timeout), flush=True)
            print("REMOTE_PREFLIGHT_DIAGNOSTICS_BEGIN", flush=True)
            proc = subprocess.Popen(
                command, cwd=root, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0, start_new_session=True,
            )
            assert proc.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            pending = ""
            last_heartbeat = time.monotonic()
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, probe_timeout)
                    events = selector.select(min(1.0, remaining))
                    if events:
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            selector.unregister(proc.stdout)
                            break
                        chunk_text = decoder.decode(chunk).replace(api_key, "[REDACTED]")
                        log.write(chunk_text)
                        log.flush()
                        pending += chunk_text
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            print(line, flush=True)
                    elif time.monotonic() - last_heartbeat >= 30:
                        heartbeat = "REMOTE_PREFLIGHT_HEARTBEAT elapsed_seconds=" + str(
                            int(time.monotonic() - attempt_started)
                        )
                        log.write(heartbeat + "\n")
                        log.flush()
                        print(heartbeat, flush=True)
                        last_heartbeat = time.monotonic()
                tail = decoder.decode(b"", final=True).replace(api_key, "[REDACTED]")
                if tail:
                    log.write(tail)
                    pending += tail
                if pending:
                    print(pending, end="", flush=True)
                status = proc.wait(timeout=max(1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                print("REMOTE_PREFLIGHT_TIMEOUT=EXPIRED; sending SIGINT to probe process group", flush=True)
                log.write("\nREMOTE_PREFLIGHT_TIMEOUT=EXPIRED\n")
                log.flush()
                try:
                    os.killpg(proc.pid, signal.SIGINT)
                    proc.wait(timeout=3)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                status = proc.returncode if proc.returncode is not None else 124
            finally:
                selector.close()
                proc.stdout.close()
            print("REMOTE_PREFLIGHT_DIAGNOSTICS_END", flush=True)
        if status == 0:
            break
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as saved_log:
                saved_log.seek(attempt_log_offset)
                attempt_log = saved_log.read()
        except OSError:
            attempt_log = ""
        # A failed preflight is intentionally not retried: this is a fast
        # infrastructure gate, not a compilation window. Preserve the attempt
        # log so the actual CAS/REAPI failure remains diagnosable.
        break
    try:
        restore_siso_state(out, state_snapshot)
    finally:
        state_backup_context.cleanup()

    if status:
        raise ValueError("Strict-remote C++ probe failed with exit code " + str(status))
    if not target.is_file() or target.stat().st_size == 0:
        raise ValueError("Strict-remote probe did not produce its expected object file")

    counts = parse_remote_probe_metrics(metrics_path)
    for name, value in counts.items():
        print("REMOTE_PREFLIGHT_" + name.upper() + "=" + str(value))
    if counts["uncached_remote_target_actions"] < 1:
        raise ValueError("No uncached remote result for the probe object; full Chromium compile is blocked")

    # This isolated one-action graph proves remote execution, not persistence
    # of Chromium's normal GN graph state. Siso does not guarantee that an
    # isolated graph creates the output-directory filesystem-state pair.
    # State/cache persistence is validated by the dedicated CI checkpoint steps.
    print("REMOTE_PREFLIGHT_SISO_STATE_PAIR=NOT_REQUIRED_FOR_ISOLATED_PROBE")

    marker = {
        "schema": 1, "status": "PASS", "workflow_id": workflow_id,
        "source_commit": source_commit, "chromium_commit": chromium_commit,
        "chromium_version": chromium_version, "probe_target": PROBE_TARGET,
        "probe_output_suffix": PROBE_OUTPUT_SUFFIX,
        "probe_remote_action_records": counts["uncached_remote_target_actions"],
        "probe_action_records": counts["action_records"],
        "metrics_sha256": hashlib.sha256(metrics_path.read_bytes()).hexdigest(),
        "action_salt_sha256": hashlib.sha256(salt.encode("utf-8")).hexdigest(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    temporary = marker_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    temporary.replace(marker_path)
    print("SANDIUM_REMOTE_PREFLIGHT=PASS")
    print("SANDIUM_PREFLIGHT_REMOTE_EXECUTED_ACTION_RECORDS=" + str(marker["probe_remote_action_records"]))
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
