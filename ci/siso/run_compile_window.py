#!/usr/bin/env python3
"""Run one bounded, resumable Brave/Siso build window with SANDFOX-style gates."""
from __future__ import annotations

import argparse
import codecs
import json
import hashlib
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Resolve sibling helper for direct execution and importlib-based CI tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from remote_preflight import load_valid_marker

REMOTE_SIGNATURES = (
    re.compile(r"is_remote=true", re.IGNORECASE),
    re.compile(r"is_local=false", re.IGNORECASE),  # Compatibility with emitters that include false booleans.
    re.compile(r"step state: remote exec", re.IGNORECASE),
    re.compile(r"exec remote execute", re.IGNORECASE),
)
REGENERATE_NINJA = re.compile(r"regenerating ninja files", re.IGNORECASE)
REMOTE_WORKDIR_INPUT_TREE_ERROR = re.compile(
    r"(?:working directory.{0,100}(?:not an input directory|not found in input tree)|"
    r"(?:not an input directory|not found in input tree).{0,100}working directory)",
    re.IGNORECASE,
)
PROGRESS_LINE = re.compile(r"^\[[0-9]+/[0-9]+\]\s+\S+\s+F\s+(.*)$")
TASK_PROGRESS = re.compile(r"^\[(\d+)/(\d+)\]")


def emit(log: Any, message: str) -> None:
    print(message, flush=True)
    log.write(message + "\n")
    log.flush()


def has_remote_signature(text: str) -> bool:
    return any(pattern.search(text) for pattern in REMOTE_SIGNATURES)


def state_file_snapshot(path: Path) -> dict[str, Any]:
    """Hash a Siso state file for before/after persistence diagnostics."""
    result: dict[str, Any] = {
        "entry_exists": False, "present": False, "bytes": 0,
        "sha256": None, "regular_file": False,
    }
    try:
        # A missing journal after clean shutdown is normal. Distinguish absence
        # from an empty file, symlink, directory, or other invalid journal entry.
        result["entry_exists"] = path.exists() or path.is_symlink()
        if path.is_symlink() or not path.is_file():
            return result
        size = path.stat().st_size
        result["bytes"] = size
        result["present"] = size > 0
        result["regular_file"] = True
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        result["sha256"] = digest.hexdigest()
    except OSError as exc:
        result["error"] = type(exc).__name__
    return result


def emit_state_snapshot(log: Any, out_dir: Path, phase: str) -> list[dict[str, Any]]:
    snapshots = []
    for name in (".siso_fs_state", ".siso_fs_state.journal"):
        snap = state_file_snapshot(out_dir / name)
        label = name.upper().replace(".", "_")
        for field, value in snap.items():
            emit(log, f"{phase}_{label}_{field.upper()}={value if value is not None else 'null'}")
        snapshots.append(snap)
    state, journal = snapshots
    state_valid = bool(state.get("present") and state.get("sha256") and state.get("regular_file"))
    journal_entry = bool(journal.get("entry_exists"))
    journal_valid = bool(journal.get("present") and journal.get("sha256") and journal.get("regular_file"))
    state_safe = state_valid and (not journal_entry or journal_valid)
    emit(log, f"{phase}_SISO_BASE_STATE_VALID={str(state_valid).lower()}")
    emit(log, f"{phase}_SISO_JOURNAL_ENTRY_EXISTS={str(journal_entry).lower()}")
    emit(log, f"{phase}_SISO_JOURNAL_VALID={str(journal_valid).lower()}")
    emit(log, f"{phase}_SISO_CHECKPOINT_STATE_SAFE={str(state_safe).lower()}")
    return snapshots


def count_outputs(out_dir: Path) -> tuple[int, int]:
    object_count = 0
    output_count = 0
    roots = [out_dir / "obj", out_dir / "gen", out_dir / "apks"]
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Siso/Ninja outputs are ordinary files; do not follow symlinks.
            output_count += len(filenames)
            if root.name == "obj":
                object_count += sum(name.endswith(".o") for name in filenames)
    return object_count, output_count


def metrics_prove_remote_execution(metrics: dict[str, int]) -> bool:
    """Accept only final structured Siso evidence of uncached remote execution."""
    return metrics.get("remote_executed_action_records", 0) > 0


def parse_metrics(path: Path) -> dict[str, int]:
    values = {
        "records": 0,
        "action_records": 0,
        "cached_action_records": 0,
        "local_action_records": 0,
        "explicit_remote_action_records": 0,
        "remote_cached_action_records": 0,
        "remote_executed_action_records": 0,
        "fast_local_action_records": 0,
        "local_fallback_action_records": 0,
        "no_exec_action_records": 0,
    }
    if not path.is_file():
        return values
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            values["records"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid Siso metrics JSONL line {line_number}: {exc}") from exc
            if not isinstance(record, dict) or "step_id" not in record or not isinstance(record.get("outputs"), list):
                continue
            values["action_records"] += 1
            cached = record.get("cached") is True
            local = record.get("is_local") is True
            no_exec = record.get("no_exec") is True
            fast_local = record.get("fast_local") is True
            fallback = record.get("fallback") is True
            values["cached_action_records"] += int(cached)
            values["local_action_records"] += int(local)
            values["fast_local_action_records"] += int(fast_local)
            values["local_fallback_action_records"] += int(fallback)
            values["no_exec_action_records"] += int(no_exec)
            # Siso omits false booleans in JSON. Keep reported remote results
            # distinct from true executions: cached remote results are useful
            # cache evidence, but cannot satisfy the execution gate.
            remote = record.get("is_remote") is True or record.get("is_local") is False
            values["explicit_remote_action_records"] += int(remote)
            values["remote_cached_action_records"] += int(remote and cached)
            if remote and not cached and not no_exec:
                values["remote_executed_action_records"] += 1
    return values


def verify_source_timestamp_baseline(root: Path, out_dir: Path) -> None:
    epoch = 946684800
    source = root / "src"
    inputs = (
        source,
        source / "DEPS",
        source / "build" / "config" / "compiler" / "compiler.gni",
        source / "chrome" / "VERSION",
        source / "brave" / "build" / "commands" / "lib" / "build.ts",
    )
    for path in inputs:
        if not path.exists():
            raise ValueError(f"Source timestamp baseline input is missing: {path}")
        actual = int(path.stat().st_mtime)
        if actual != epoch:
            raise ValueError(f"Source timestamp baseline is not normalized: {path} has {actual}, expected {epoch}")
    stamp = out_dir / "build.ninja.stamp"
    stamp_time = int(stamp.stat().st_mtime)
    if stamp_time <= epoch:
        raise ValueError(f"GN build.ninja.stamp must remain newer than normalized source: {stamp_time}")
    print(f"SOURCE_TIMESTAMP_POLICY=epoch_946684800_full_tree_v1")
    print(f"SOURCE_TIMESTAMP_VERIFY=PASS source_epoch={epoch} build_ninja_stamp_epoch={stamp_time}")


def siso_runtime_identity(path_env: str | None = None) -> dict[str, str]:
    """Report the Siso executable actually resolvable by the compile process."""
    executable = shutil.which("siso", path=path_env)
    if not executable:
        return {"path": "not-found", "version": "not-probed"}
    version = "unavailable"
    for args in ([executable, "version"], [executable, "--version"]):
        try:
            result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            version = f"probe-error:{type(exc).__name__}"
            continue
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0 and output:
            version = output.splitlines()[0][:500]
            break
        if output:
            version = f"exit-{result.returncode}:{output.splitlines()[0][:400]}"
    return {"path": str(Path(executable).resolve()), "version": version}


def preserve_failure_diagnostics(diagnostics: Path, out_dir: Path, stage: str, siso_info: Path, metrics_path: Path, failed: bool) -> list[str]:
    """Preserve Siso logs and failed-command scripts when a window fails."""
    if not failed:
        return []
    preserved: list[str] = []
    candidates = [
        (siso_info, diagnostics / f"siso.INFO.failure-{stage}"),
        (metrics_path, diagnostics / f"siso-metrics.failure-{stage}.jsonl"),
        (out_dir / "siso_failed_commands.sh", diagnostics / f"siso_failed_commands.failure-{stage}.sh"),
        (out_dir / "siso_output", diagnostics / f"siso_output.failure-{stage}"),
    ]
    for source, destination in candidates:
        try:
            if source.is_file() and not source.is_symlink():
                shutil.copy2(source, destination)
                preserved.append(str(destination))
        except OSError:
            continue
    return preserved


def count_siso_processes() -> int:
    if not shutil.which("pgrep"):
        return 0
    result = subprocess.run(["pgrep", "-x", "siso"], capture_output=True, text=True, check=False)
    return len([line for line in result.stdout.splitlines() if line.strip()])


def stop_process_group(proc: subprocess.Popen[bytes], grace_seconds: int = 30) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def ensure_remote_wrapper_executable(wrapper_path: Path) -> bool:
    """Ensure the generated Python wrapper is shipped as an executable REAPI input.

    Returns True when execute bits had to be repaired. This is deliberately
    fail-closed for missing, non-regular, or symlink paths; silently proceeding
    would repeat the BuildBuddy 'exists but is not executable' action failure.
    """
    if wrapper_path.is_symlink() or not wrapper_path.is_file():
        raise ValueError(
            f"generated remote Python wrapper is missing or not a regular file: {wrapper_path}"
        )
    previous_mode = stat.S_IMODE(wrapper_path.stat().st_mode)
    required_execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    repaired = (previous_mode & required_execute_bits) != required_execute_bits
    if repaired:
        wrapper_path.chmod(previous_mode | required_execute_bits)
    final_mode = stat.S_IMODE(wrapper_path.stat().st_mode)
    if (final_mode & required_execute_bits) != required_execute_bits:
        raise ValueError(
            f"generated remote Python wrapper is not executable after repair: {wrapper_path}"
        )
    return repaired


def reset_siso_info(siso_info: Path, diagnostics: Path, stage: str) -> None:
    """Start a fresh regular Siso info log, preserving unexpected restored paths."""
    if siso_info.is_symlink():
        # A restored Siso log can be a symlink (including a dangling one).
        # Unlink only the link itself; never truncate or follow its target.
        siso_info.unlink()
        print("SISO_INFO_SYMLINK_REPLACED=true")
    elif siso_info.exists():
        if siso_info.is_file():
            # Preserve checkpoint-era diagnostics, but don't let a previous
            # stage's remote signature satisfy the current stage's remote gate.
            shutil.copy2(siso_info, diagnostics / f"siso.INFO.before-{stage}")
            print(f"SISO_INFO_PREVIOUS_STAGE_SAVED={diagnostics / f'siso.INFO.before-{stage}'}")
        else:
            # A restored cache can leave a directory or another non-regular
            # filesystem object at the log path. Quarantine it instead of
            # failing the full build; preserve the object for diagnosis.
            candidate = diagnostics / f"siso.INFO.unexpected-{stage}"
            suffix = 1
            while candidate.exists() or candidate.is_symlink():
                candidate = diagnostics / f"siso.INFO.unexpected-{stage}-{suffix}"
                suffix += 1
            shutil.move(str(siso_info), str(candidate))
            print(f"SISO_INFO_UNEXPECTED_PATH_PRESERVED={candidate}")
    siso_info.write_text("", encoding="utf-8")
    if not siso_info.is_file() or siso_info.is_symlink():
        raise ValueError(f"failed to initialize a regular Siso info log: {siso_info}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--stage", choices=("stage-1", "stage-2", "stage-3", "stage-4", "stage-5"), required=True)
    parser.add_argument("--stage-start-epoch-file", type=Path, required=True)
    parser.add_argument("--max-stage-seconds", type=int, default=3300)
    parser.add_argument("--handover-reserve-seconds", type=int, default=600)
    parser.add_argument("--shutdown-grace-seconds", type=int, default=360,
                        help="allow Siso to flush state/journal after SIGINT before escalating")
    parser.add_argument("--metrics-poll-interval-seconds", type=int, default=15)
    args = parser.parse_args()

    root = args.root.resolve()
    source = root / "src"
    brave = source / "brave"
    out_dir = source / "out" / "android_Debug_arm64"
    apk = out_dir / "apks" / "Bravearm64.apk"
    diagnostics = root / "build-diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    stage_number = args.stage.split("-")[-1]
    log_path = diagnostics / f"compile-window-{stage_number}.log"
    metrics_path = diagnostics / f"compile-window-{stage_number}-metrics.jsonl"
    metrics_path.unlink(missing_ok=True)

    if not brave.is_dir() or not out_dir.is_dir():
        print("SANDIUM_COMPILE_SETUP=FAIL; expected Brave checkout or Android output directory is missing", file=sys.stderr)
        return 23
    for filename in ("build.ninja", "args.gn", "args_generated.gni"):
        path = out_dir / filename
        if not path.is_file() or path.stat().st_size == 0:
            print(f"SANDIUM_COMPILE_SETUP=FAIL; missing prepared graph input: {path}", file=sys.stderr)
            return 23
    if not (out_dir / "build.ninja.stamp").is_file():
        print("SANDIUM_COMPILE_SETUP=FAIL; GN build.ninja.stamp is missing", file=sys.stderr)
        return 23
    try:
        verify_source_timestamp_baseline(root, out_dir)
    except (OSError, ValueError) as exc:
        print(f"SANDIUM_SOURCE_TIMESTAMP_VERIFY=FAIL ({exc})", file=sys.stderr)
        return 24

    try:
        stage_started = int(args.stage_start_epoch_file.read_text().strip())
    except (OSError, ValueError) as exc:
        print(f"SANDIUM_STAGE_BUDGET=FAIL; cannot read stage start timestamp: {exc}", file=sys.stderr)
        return 5
    elapsed_before_compile = max(0, int(time.time()) - stage_started)
    # Do not impose a separate fixed compile timeout. Use all time available
    # in this CircleCI stage while reserving a bounded handover window for
    # graceful Siso/Ninja shutdown, checkpoint packaging, and artifact upload.
    remaining = args.max_stage_seconds - elapsed_before_compile - args.handover_reserve_seconds

    if apk.is_file() and apk.stat().st_size >= 1_000_000:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            emit(log, "COMPILE_WINDOW_SKIPPED=APK_ALREADY_PRESENT_IN_RESTORED_OUTPUT")
            emit(log, "APK_PRESENT_AFTER_BUILD=true")
        return 0
    if remaining <= 0:
        print("STAGE_TIME_BUDGET_EXHAUSTED=true; preserving output and handover margin")
        return 5
    # Brave's remote Python rules declare this generated wrapper as an
    # executable input. Some runhooks/cache restores leave the mode non-executable,
    # which BuildBuddy rejects during input-tree materialization. Repair the
    # workspace mode before Siso computes the remote action input digest.
    remote_python_wrapper = source / "buildtools/reclient_cfgs/python/python_remote_wrapper"
    try:
        wrapper_mode_repaired = ensure_remote_wrapper_executable(remote_python_wrapper)
    except (OSError, ValueError) as exc:
        print("SANDIUM_REMOTE_PYTHON_WRAPPER=FAIL " + str(exc), file=sys.stderr)
        return 23
    print("SANDIUM_REMOTE_PYTHON_WRAPPER_EXECUTABLE=PASS")
    print("SANDIUM_REMOTE_PYTHON_WRAPPER_MODE_REPAIRED=" + str(wrapper_mode_repaired).lower())

    try:
        chromium_commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        chromium_version = subprocess.check_output(
            ["git", "-C", str(source), "describe", "--tags", "--exact-match", "HEAD"], text=True
        ).strip()
        remote_preflight = load_valid_marker(
            out_dir / ".sandium-remote-preflight.json",
            workflow_id=os.environ.get("CIRCLE_WORKFLOW_ID", ""),
            source_commit=os.environ.get("CIRCLE_SHA1", ""),
            chromium_commit=chromium_commit,
            chromium_version=chromium_version,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("SANDIUM_REMOTE_PREFLIGHT=FAIL", file=sys.stderr)
        print("SANDIUM_REMOTE_PREFLIGHT_REASON=" + str(exc), file=sys.stderr)
        return 126

    prior_frontier_path = root / "sandium-action-frontier.txt"
    prior_frontier_file_present = prior_frontier_path.is_file()
    try:
        prior_frontier_file_bytes = prior_frontier_path.stat().st_size if prior_frontier_file_present else 0
    except OSError:
        prior_frontier_file_present = False
        prior_frontier_file_bytes = 0
    try:
        prior_frontier = {
            line.rstrip("\n")
            for line in prior_frontier_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except OSError:
        prior_frontier = set()
    prior_frontier_path.touch(exist_ok=True)

    objects_before, outputs_before = count_outputs(out_dir)
    state_start_snapshots = [
        state_file_snapshot(out_dir / ".siso_fs_state"),
        state_file_snapshot(out_dir / ".siso_fs_state.journal"),
    ]
    current_frontier: set[str] = set()
    # Ninja's [completed/total] denominator is not the remaining work. Track both
    # values and report the residual (total - completed); never use the raw graph
    # denominator as a success ceiling. Reuse is evaluated from restored outputs,
    # newly produced outputs, and the persisted action frontier instead.
    latest_progress_total: int | None = None
    latest_progress_completed: int | None = None
    first_progress_at: float | None = None
    last_progress_total_change_at: float | None = None
    metrics: dict[str, int] = {}
    remote_signatures: set[str] = set()
    remote_seen = False
    regeneration_seen = False
    timeout_seen = False
    gate_failed = False
    build_return_code: int | None = None
    started = time.monotonic()
    siso_info = out_dir / "siso.INFO"
    # Create the diagnostic log before any reset attempt. If a restored path is
    # malformed, the wrapper can still collect a useful log instead of failing
    # a second time because compile-window-N.log does not exist.
    log_path.write_text("", encoding="utf-8")
    try:
        reset_siso_info(siso_info, diagnostics, args.stage)
    except (OSError, ValueError) as exc:
        message = f"SANDIUM_SISO_INFO_RESET=FAIL; {exc}"
        print(message, file=sys.stderr)
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            emit(log, message)
        return 23
    try:
        initial_info_stat = siso_info.stat()
        siso_info_identity: tuple[int, int] | None = (initial_info_stat.st_dev, initial_info_stat.st_ino)
        siso_info_offset = 0
    except OSError:
        siso_info_identity = None
        siso_info_offset = 0

    def read_new_siso_info() -> str:
        nonlocal siso_info_identity, siso_info_offset
        try:
            info_stat = siso_info.stat()
        except OSError:
            return ""
        identity = (info_stat.st_dev, info_stat.st_ino)
        if identity != siso_info_identity or info_stat.st_size < siso_info_offset:
            # Siso can rotate/truncate its info log on startup; consume the new
            # file from offset zero, never reuse lines from the old checkpoint.
            siso_info_identity = identity
            siso_info_offset = 0
        if info_stat.st_size <= siso_info_offset:
            return ""
        try:
            with siso_info.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(siso_info_offset)
                appended = stream.read()
                siso_info_offset = stream.tell()
                return appended
        except OSError:
            return ""

    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        emit(log, f"SANDIUM_STAGE={args.stage}")
        emit(log, f"STAGE_ELAPSED_BEFORE_COMPILE_SECONDS={elapsed_before_compile}")
        emit(log, f"COMPILE_WINDOW_SECONDS={remaining}")
        emit(log, f"STAGE_START_OBJECT_COUNT={objects_before}")
        emit(log, f"STAGE_START_BUILD_OUTPUT_COUNT={outputs_before}")
        emit(log, "TASK_COUNT_POLICY=REPORT_RESIDUAL_NOT_RAW_DENOMINATOR")
        emit(log, f"RESTORED_OBJECT_COUNT={objects_before}")
        emit(log, f"RESTORED_BUILD_OUTPUT_COUNT={outputs_before}")
        emit(log, f"ACTION_FRONTIER_PRIOR_FILE_PRESENT={str(prior_frontier_file_present).lower()}")
        emit(log, f"ACTION_FRONTIER_PRIOR_FILE_BYTES={prior_frontier_file_bytes}")
        start_state_snapshots = emit_state_snapshot(log, out_dir, "STAGE_START")
        # The state file is the clean-shutdown snapshot. The journal is expected
        # to disappear after Siso folds it into the state file on close.
        emit(log, "STAGE_START_STATE_SNAPSHOT_RECHECKED=" + str(bool(state_start_snapshots[0].get("sha256"))).lower())
        checkpoint_archive = root / "sandium-checkpoint.tar.zst"
        checkpoint_bytes = checkpoint_archive.stat().st_size if checkpoint_archive.is_file() else 0
        emit(log, f"RESTORED_CHECKPOINT_ARCHIVE_BYTES={checkpoint_bytes}")
        emit(log, "SOURCE_TIMESTAMP_POLICY=epoch_946684800_full_tree_v1")
        emit(log, "SOURCE_TIMESTAMP_VERIFY=PASS")
        emit(log, "SANDIUM_REMOTE_PREFLIGHT=PASS source=current-workflow-uncached-cxx-action")
        siso_identity = siso_runtime_identity(os.environ.get("PATH"))
        emit(log, "SISO_RUNTIME_PATH=" + siso_identity["path"])
        emit(log, "SISO_RUNTIME_VERSION=" + siso_identity["version"])
        emit(log, "SISO_PIN_EXPECTED=git_revision:bc45e8f67ae0f37d337190ad64aa8bb440c791eb")
        emit(log, "SISO_PIN_VERIFIED=" + str("bc45e8f67ae0f37d337190ad64aa8bb440c791eb" in siso_identity["version"]).lower())
        emit(log, "SANDIUM_PREFLIGHT_REMOTE_EXECUTED_ACTION_RECORDS=" + str(remote_preflight["probe_remote_action_records"]))
        emit(log, "EARLY_REMOTE_EXECUTION_GATE=PREPROVEN_BY_CURRENT_WORKFLOW_PREFLIGHT")
        emit(log, "REMOTE_EXECUTION_MONITORING_POLICY=NO_SEPARATE_TIME_DEADLINE")

        env = os.environ.copy()
        env.update({
            "SANDIUM_CI_RESUME_PREPARED_GRAPH": "true",
            "SANDIUM_CI_STRICT_REMOTE": "true",
            "SANDIUM_CI_SISO_METRICS_JSON": str(metrics_path),
            "SANDIUM_CI_NINJA_JOBS": "80",
            "SISO_LIMITS": "local=8,remote=80",
            "SISO_CREDENTIAL_HELPER": str(brave / "ci/siso/buildbuddy-credential-helper.py"),
        })
        autoninja = shutil.which("autoninja", path=env.get("PATH", ""))
        if not autoninja:
            emit(log, "SANDIUM_AUTONINJA_PREFLIGHT=FAIL; autoninja is not resolvable in the compile process PATH")
            emit(log, "SANDIUM_AUTONINJA_EFFECTIVE_PATH=" + env.get("PATH", ""))
            return 127
        emit(log, "SANDIUM_AUTONINJA_PREFLIGHT=PASS path=" + autoninja)
        command = [
            "pnpm", "run", "build", "Debug",
            "--target_os=android",
            "--target_arch=arm64",
            "--target_android_output_format=apk",
            "--use_remoteexec", "true",
            "--gn", "use_siso:true",
            "--gn", "use_reclient:false",
            "--gn", 'clang_diagnostic_dir:"clang-crashreports"',
        ]
        emit(log, "SANDIUM_CI_RESUME_PREPARED_GRAPH=true")
        emit(log, "STRICT_REMOTE=ENABLED")
        emit(log, "SISO_LIMITS=local=8,remote=80")
        emit(log, "SISO_METRICS_JSON=" + str(metrics_path))
        emit(log, "AUTONINJA_TARGET=Brave Android ARM64 Debug APK (Brave wrapper target)")
        emit(log, "COMMAND=" + " ".join(command))
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        selector = selectors.DefaultSelector()
        try:
            proc = subprocess.Popen(
                command,
                cwd=brave,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as exc:
            emit(log, f"SANDIUM_COMPILE_SPAWN_FAILED={exc}")
            return 127
        assert proc.stdout is not None
        selector.register(proc.stdout, selectors.EVENT_READ)
        partial = ""
        rolling = ""
        stop_reason: str | None = None
        eof = False
        shutdown_requested_at: float | None = None
        shutdown_escalated_at: float | None = None
        shutdown_kill_sent = False
        last_activity = time.monotonic()
        child_exit_seen_at: float | None = None
        last_info_poll = 0.0
        last_metrics_poll = 0.0
        remote_gate_pass_logged = False

        def request_graceful_shutdown(reason: str) -> None:
            nonlocal shutdown_requested_at, stop_reason
            if shutdown_requested_at is not None:
                return
            shutdown_requested_at = time.monotonic()
            stop_reason = stop_reason or reason
            emit(log, "SISO_GRACEFUL_SHUTDOWN_REQUESTED=SIGINT reason=" + reason)
            try:
                # Signal the build process group, but keep draining stdout/stderr
                # in the selector loop so shutdown diagnostics and output flushing
                # cannot deadlock on a full pipe.
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                emit(log, "SISO_GRACEFUL_SHUTDOWN_SIGNAL=PROCESS_GROUP_ALREADY_EXITED")

        def process_text(text: str) -> None:
            nonlocal remote_seen, regeneration_seen, partial, rolling, last_activity, gate_failed, stop_reason
            nonlocal latest_progress_total, latest_progress_completed, first_progress_at, last_progress_total_change_at
            if not text:
                return
            last_activity = time.monotonic()
            sys.stdout.write(text)
            sys.stdout.flush()
            log.write(text)
            log.flush()
            lower = text.lower()
            rolling = (rolling + lower)[-8192:]
            if REGENERATE_NINJA.search(rolling) and not regeneration_seen:
                regeneration_seen = True
                # Ninja may legitimately need to refresh the generated graph
                # after restoring outputs onto a fresh runner. Graph generation
                # is not a Chromium compile and does not delete existing objects.
                # Allow graph refresh; raw Ninja denominator is diagnostic only.
                emit(log, "NINJA_GRAPH_REGENERATION=DETECTED; graph refresh allowed; reuse is evaluated from restored outputs and incremental deltas")
            partial += text
            while "\n" in partial:
                line, partial = partial.split("\n", 1)
                task_match = TASK_PROGRESS.match(line.strip())
                if task_match:
                    observed_completed = int(task_match.group(1))
                    observed_total = int(task_match.group(2))
                    latest_progress_completed = observed_completed
                    if latest_progress_total != observed_total:
                        latest_progress_total = observed_total
                        last_progress_total_change_at = time.monotonic()
                    if first_progress_at is None:
                        first_progress_at = time.monotonic()
                match = PROGRESS_LINE.match(line.strip())
                if match:
                    label = match.group(1).strip()
                    if label and label.lower() != "regenerating ninja files":
                        current_frontier.add(label)
                if any(pattern.search(line) for pattern in REMOTE_SIGNATURES):
                    # Retain remote-looking lines for diagnostics only. They are not
                    # authoritative execution evidence; only parsed uncached remote
                    # action records from this run's Siso metrics can open the gate.
                    remote_signatures.add(line.strip())
                if REMOTE_WORKDIR_INPUT_TREE_ERROR.search(line):
                    gate_failed = True
                    stop_reason = "REAPI rejected action because working directory is absent from the remote input tree"
                    emit(log, "REAPI_WORKDIR_INPUT_TREE_GATE=FAIL")
                    emit(log, "REAPI_WORKDIR_INPUT_TREE_DIAGNOSTIC=" + line.strip()[:1000])
                    # Stop this compile window rather than spending the remaining
                    # stage budget on an action-level REAPI precondition failure.
                    request_graceful_shutdown(stop_reason)

        # A workflow-bound uncached C++ probe already proved this REAPI backend,
        # and the build uses --strict_remote. A second wall-clock deadline can
        # falsely abort before Siso flushes its metrics JSONL; judge the actual
        # compile from final structured metrics after Siso exits.
        emit(log, "REMOTE_EXECUTION_STARTUP_DEADLINE_ACTIVE=false")
        emit(log, "REMOTE_EXECUTION_POLICY=WORKFLOW_PREFLIGHT_PLUS_STRICT_REMOTE_AND_FINAL_METRICS")
        emit(log, f"SISO_SHUTDOWN_GRACE_SECONDS={args.shutdown_grace_seconds}")
        try:
            while True:
                now = time.monotonic()
                elapsed = now - started
                if (proc.poll() is None and not remote_seen and shutdown_requested_at is None
                        and now - last_metrics_poll >= max(1, args.metrics_poll_interval_seconds)):
                    last_metrics_poll = now
                    try:
                        metrics_now = parse_metrics(metrics_path)
                    except ValueError as exc:
                        gate_failed = True
                        emit(log, "ACTUAL_REMOTE_EXECUTION_METRICS_GATE=FAIL invalid_metrics=true")
                        request_graceful_shutdown(f"invalid Siso metrics during remote gate: {exc}")
                        metrics_now = {"remote_executed_action_records": 0}
                    if metrics_prove_remote_execution(metrics_now):
                        remote_seen = True
                if proc.poll() is None and shutdown_requested_at is None and elapsed >= remaining:
                    timeout_seen = True
                    emit(log, f"COMPILE_WINDOW_TIMEOUT=PASS timeout_seconds={remaining}")
                    request_graceful_shutdown("stage compile time budget exhausted")

                # Keep the pipe drained while Siso shuts down. A 300s filesystem
                # flush setting is configured in .sisorc; killing the whole process
                # group after 30s can leave only the journal and lose the state file.
                if shutdown_requested_at is not None:
                    shutdown_elapsed = now - shutdown_requested_at
                    if shutdown_elapsed >= args.shutdown_grace_seconds and shutdown_escalated_at is None:
                        emit(log, f"SISO_SHUTDOWN_ESCALATION=SIGTERM after_seconds={int(shutdown_elapsed)}")
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        shutdown_escalated_at = now
                    elif (shutdown_escalated_at is not None and not shutdown_kill_sent
                          and now - shutdown_escalated_at >= 15):
                        emit(log, "SISO_SHUTDOWN_ESCALATION=SIGKILL after_sigterm_seconds=15")
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        shutdown_kill_sent = True

                events = selector.select(timeout=0.25)
                if events:
                    for key, _ in events:
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            eof = True
                            tail = decoder.decode(b"", final=True)
                            process_text(tail)
                            continue
                        process_text(decoder.decode(chunk))

                if now - last_info_poll >= max(1, args.metrics_poll_interval_seconds):
                    info_delta = read_new_siso_info()
                    last_info_poll = now
                    # Log text and cache messages are diagnostic only. A real remote
                    # action must be present in this run's structured Siso metrics.
                    try:
                        metrics_now = parse_metrics(metrics_path)
                    except ValueError as exc:
                        gate_failed = True
                        emit(log, "ACTUAL_REMOTE_EXECUTION_METRICS_GATE=FAIL invalid_metrics=true")
                        request_graceful_shutdown(f"invalid Siso metrics during remote gate: {exc}")
                        metrics_now = {"remote_executed_action_records": 0}
                    if metrics_prove_remote_execution(metrics_now):
                        remote_seen = True
                    if info_delta:
                        for line in info_delta.splitlines():
                            if any(pattern.search(line) for pattern in REMOTE_SIGNATURES):
                                remote_signatures.add(line.strip())

                if remote_seen and not gate_failed and not remote_gate_pass_logged:
                    emit(log, f"EARLY_REMOTE_EXECUTION_GATE=PASS seconds={int(time.monotonic()-started)}")
                    remote_gate_pass_logged = True
                if proc.poll() is not None and child_exit_seen_at is None:
                    child_exit_seen_at = time.monotonic()
                if proc.poll() is not None and eof and count_siso_processes() == 0:
                    break
                if (proc.poll() is not None and child_exit_seen_at is not None
                        and time.monotonic() - child_exit_seen_at > args.shutdown_grace_seconds
                        and not eof):
                    if shutdown_requested_at is None:
                        request_graceful_shutdown("build wrapper exited but child/Siso output remained open")
                    elif shutdown_escalated_at is not None and time.monotonic() - shutdown_escalated_at > 15:
                        break
        finally:
            selector.close()
            if proc.poll() is None:
                stop_process_group(proc)

        build_return_code = proc.returncode
        if partial:
            match = PROGRESS_LINE.match(partial.strip())
            if match:
                current_frontier.add(match.group(1).strip())
        if regeneration_seen:
            emit(log, "NINJA_GRAPH_REGENERATION=ALLOWED_FOR_GRAPH_REFRESH; compile still requires task-budget, remote-execution, and incremental-progress gates")
        # Wait for Siso to close its state/journal before writing a checkpoint.
        siso_forced_kill = False
        siso_wait_started = time.monotonic()
        while count_siso_processes() and time.monotonic() - siso_wait_started < args.shutdown_grace_seconds:
            time.sleep(2)
        remaining_siso = count_siso_processes()
        if remaining_siso:
            emit(log, f"SISO_SHUTDOWN=FAILED remaining_processes={remaining_siso}")
            pids = subprocess.run(["pgrep", "-x", "siso"], capture_output=True, text=True, check=False).stdout.split()
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
            siso_forced_kill = True
        else:
            emit(log, "SISO_SHUTDOWN=CLEAN")

        try:
            metrics = parse_metrics(metrics_path)
        except ValueError as exc:
            emit(log, f"SISO_METRICS_PARSE=FAIL error={exc}")
            gate_failed = True
            metrics = {}
        remote_metric_records = metrics.get("remote_executed_action_records", 0)
        remote_log_records = len(remote_signatures)
        apk_ready = apk.is_file() and apk.stat().st_size >= 1_000_000
        # The metrics writer may flush its final JSONL records only as Siso exits.
        # Final parsed metrics are authoritative even if the live poll never saw
        # them; do not emit a premature FAIL before this final parse.
        if metrics_prove_remote_execution(metrics):
            if not remote_seen:
                emit(log, f"EARLY_REMOTE_EXECUTION_GATE=PASS source=final_metrics_after_shutdown remote_executed_action_records={remote_metric_records}")
            remote_seen = True
            emit(log, f"ACTUAL_REMOTE_EXECUTION_METRICS_GATE=PASS source=siso_metrics_json remote_executed_action_records={remote_metric_records}")
        elif apk_ready:
            # A previously verified APK is a completed artifact; this exception
            # does not authorize an incomplete compile window without metrics.
            emit(log, "ACTUAL_REMOTE_EXECUTION_METRICS_GATE=PASS source=verified_apk remote_execution_metrics_unavailable=true")
        else:
            gate_failed = True
            stop_reason = stop_reason or "incomplete compile window has no uncached remote-executed Siso action records in structured metrics"
            emit(log, "ACTUAL_REMOTE_EXECUTION_METRICS_GATE=FAIL remote_executed_action_records=0 remote_log_signatures_diagnostic_only=true preflight_is_not_execution_proof=true")
        for key, value in metrics.items():
            emit(log, f"SISO_{key.upper()}={value}")
        emit(log, f"REMOTE_EXEC_LOG_SIGNATURE_COUNT={len(remote_signatures)}")
        if remote_seen:
            emit(log, "INCREMENTAL_HANDOVER_GATE=PASS")
        elif regeneration_seen:
            emit(log, "INCREMENTAL_HANDOVER_GATE=WAITING_FOR_REMOTE_EXECUTION")
        if siso_forced_kill:
            gate_failed = True
        if stop_reason:
            emit(log, "COMPILE_WINDOW_STOP_REASON=" + stop_reason)

        if latest_progress_total is not None and latest_progress_completed is not None:
            residual = max(0, latest_progress_total - latest_progress_completed)
            emit(log, f"NINJA_PROGRESS_COMPLETED={latest_progress_completed}")
            emit(log, f"NINJA_PROGRESS_TOTAL={latest_progress_total}")
            emit(log, f"NINJA_ESTIMATED_REMAINING={residual}")
            emit(log, "NINJA_REMAINING_ESTIMATE_NOTE=total_minus_completed; this is progress telemetry, not a standalone success gate")
        objects_after, outputs_after = count_outputs(out_dir)
        emit(log, f"STAGE_END_OBJECT_COUNT={objects_after}")
        emit(log, f"STAGE_OBJECT_COUNT_DELTA={objects_after - objects_before}")
        emit(log, f"STAGE_END_BUILD_OUTPUT_COUNT={outputs_after}")
        emit(log, f"STAGE_BUILD_OUTPUT_COUNT_DELTA={outputs_after - outputs_before}")
        emit(log, f"ACTION_FRONTIER_PRIOR_UNIQUE_LABEL_COUNT={len(prior_frontier)}")
        emit(log, f"ACTION_FRONTIER_CURRENT_UNIQUE_LABEL_COUNT={len(current_frontier)}")
        new_frontier = current_frontier - prior_frontier
        overlap_frontier = current_frontier & prior_frontier
        cumulative_frontier = prior_frontier | current_frontier
        emit(log, f"ACTION_FRONTIER_OVERLAP_UNIQUE_LABEL_COUNT={len(overlap_frontier)}")
        emit(log, f"ACTION_FRONTIER_NEW_UNIQUE_LABEL_COUNT={len(new_frontier)}")
        emit(log, f"ACTION_FRONTIER_CUMULATIVE_UNIQUE_LABEL_COUNT={len(cumulative_frontier)}")
        emit(log, f"ACTION_FRONTIER_NEW_LABEL_SHARE_PERCENT={100.0 * len(new_frontier) / max(1, len(current_frontier)):.2f}")
        output_delta = outputs_after - outputs_before
        apk_ready = apk.is_file() and apk.stat().st_size >= 1_000_000
        if not apk_ready and not new_frontier and output_delta <= 0:
            gate_failed = True
            stop_reason = stop_reason or "no incremental progress: no new action labels, no new build outputs, and no APK"
            emit(log, "INCREMENTAL_PROGRESS_GATE=FAIL new_action_labels=0 build_output_delta=0 apk_present=false")
        else:
            emit(
                log,
                "INCREMENTAL_PROGRESS_GATE=PASS "
                f"new_action_labels={len(new_frontier)} build_output_delta={output_delta} "
                f"apk_present={str(apk_ready).lower()}",
            )
        prior_frontier_path.write_text(
            "".join(label + "\n" for label in sorted(cumulative_frontier)),
            encoding="utf-8",
        )
        emit(log, "SISO_STATE_PAIR_MEASUREMENT_PHASE=AFTER_SHUTDOWN")
        end_state_snapshots = emit_state_snapshot(log, out_dir, "STAGE_END")
        for index, before in enumerate(start_state_snapshots):
            after = end_state_snapshots[index]
            emit(log, f"SISO_STATE_TRANSITION_{index}_BEFORE_SHA256={before.get('sha256') or 'null'}")
            emit(log, f"SISO_STATE_TRANSITION_{index}_AFTER_SHA256={after.get('sha256') or 'null'}")
            emit(log, f"SISO_STATE_TRANSITION_{index}_CHANGED={str(before.get('sha256') != after.get('sha256')).lower()}")
        emit(log, f"SISO_SHUTDOWN_FORCED_KILL={str(siso_forced_kill).lower()}")
        emit(log, f"SISO_SHUTDOWN_STATUS={'FORCED_KILL' if siso_forced_kill else 'CLEAN'}")
        final_state, final_journal = end_state_snapshots
        initial_state, initial_journal = start_state_snapshots
        final_state_valid = bool(final_state.get("present") and final_state.get("sha256") and final_state.get("regular_file"))
        final_journal_entry = bool(final_journal.get("entry_exists"))
        final_journal_valid = bool(final_journal.get("present") and final_journal.get("sha256") and final_journal.get("regular_file"))
        if not final_state_valid:
            emit(log, "SISO_STATE_PERSISTENCE_RESULT=BASE_STATE_MISSING_OR_INVALID_AFTER_SHUTDOWN")
        elif final_journal_entry and not final_journal_valid:
            emit(log, "SISO_STATE_PERSISTENCE_RESULT=BASE_STATE_SAVED_BUT_JOURNAL_INVALID")
        elif siso_forced_kill:
            emit(log, "SISO_STATE_PERSISTENCE_RESULT=STATE_PRESENT_AFTER_FORCED_SHUTDOWN_JOURNAL_PRESENT=" + str(final_journal_valid).lower())
        elif not final_journal_entry:
            if not initial_state.get("sha256"):
                result = "STATE_SAVED_CLEAN_SHUTDOWN_JOURNAL_NOT_REQUIRED"
            elif initial_state.get("sha256") == final_state.get("sha256"):
                result = "STATE_UNCHANGED_CLEAN_SHUTDOWN_JOURNAL_NOT_REQUIRED"
            else:
                result = "STATE_UPDATED_CLEAN_SHUTDOWN_JOURNAL_NOT_REQUIRED"
            emit(log, "SISO_STATE_PERSISTENCE_RESULT=" + result)
        else:
            emit(log, "SISO_STATE_PERSISTENCE_RESULT=STATE_AND_RECOVERY_JOURNAL_AVAILABLE")
        emit(log, f"COMPILE_CHILD_EXIT_CODE={build_return_code}")
        emit(log, f"METRICS_JSON_BYTES={metrics_path.stat().st_size if metrics_path.is_file() else 0}")
        failed_window = bool(gate_failed or timeout_seen or (build_return_code not in (None, 0)))
        preserved = preserve_failure_diagnostics(
            diagnostics, out_dir, args.stage, siso_info, metrics_path, failed_window,
        )
        emit(log, "SISO_FAILURE_DIAGNOSTICS_PRESERVED_COUNT=" + str(len(preserved)))
        for path in preserved:
            emit(log, "SISO_FAILURE_DIAGNOSTIC_FILE=" + path)

        if apk.is_file() and apk.stat().st_size >= 1_000_000:
            emit(log, "APK_PRESENT_AFTER_BUILD=true")
            return 0
        if gate_failed:
            return 125
        if timeout_seen:
            return 124
        return 0 if build_return_code == 0 else (build_return_code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
