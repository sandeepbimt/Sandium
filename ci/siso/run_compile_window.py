#!/usr/bin/env python3
"""Run one bounded, resumable Brave/Siso build window with SANDFOX-style gates."""
from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import selectors
import shutil
import signal
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
PROGRESS_LINE = re.compile(r"^\[[0-9]+/[0-9]+\]\s+\S+\s+F\s+(.*)$")


def emit(log: Any, message: str) -> None:
    print(message, flush=True)
    log.write(message + "\n")
    log.flush()


def has_remote_signature(text: str) -> bool:
    return any(pattern.search(text) for pattern in REMOTE_SIGNATURES)


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


def parse_metrics(path: Path) -> dict[str, int]:
    values = {
        "records": 0,
        "action_records": 0,
        "cached_action_records": 0,
        "local_action_records": 0,
        "remote_executed_action_records": 0,
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
            values["cached_action_records"] += int(cached)
            values["local_action_records"] += int(local)
            values["no_exec_action_records"] += int(no_exec)
            # Siso emits positive boolean fields; is_local=false is commonly omitted.
            # Prefer its explicit is_remote signal, keeping the explicit-false fallback
            # for compatible metric emitters that provide both fields.
            remote = record.get("is_remote") is True or record.get("is_local") is False
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
    parser.add_argument("--max-stage-seconds", type=int, default=3900)
    parser.add_argument("--handover-reserve-seconds", type=int, default=300)
    parser.add_argument("--max-compile-seconds", type=int, default=1800)
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
    remaining = min(args.max_compile_seconds, args.max_stage_seconds - elapsed_before_compile - args.handover_reserve_seconds)

    if apk.is_file() and apk.stat().st_size >= 1_000_000:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            emit(log, "COMPILE_WINDOW_SKIPPED=APK_ALREADY_PRESENT_IN_RESTORED_OUTPUT")
            emit(log, "APK_PRESENT_AFTER_BUILD=true")
        return 0
    if remaining <= 0:
        print("STAGE_TIME_BUDGET_EXHAUSTED=true; preserving output and handover margin")
        return 5
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
    remote_preflight_passed = True

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
    state_start: dict[str, tuple[bool, int]] = {}
    for state_name in (".siso_fs_state", ".siso_fs_state.journal"):
        state_path = out_dir / state_name
        try:
            present = state_path.is_file() and not state_path.is_symlink()
            size = state_path.stat().st_size if present else 0
        except OSError:
            present, size = False, 0
        state_start[state_name] = (present, size)
    current_frontier: set[str] = set()
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
        emit(log, f"RESTORED_OBJECT_COUNT={objects_before}")
        emit(log, f"RESTORED_BUILD_OUTPUT_COUNT={outputs_before}")
        emit(log, f"ACTION_FRONTIER_PRIOR_FILE_PRESENT={str(prior_frontier_file_present).lower()}")
        emit(log, f"ACTION_FRONTIER_PRIOR_FILE_BYTES={prior_frontier_file_bytes}")
        for state_name, (present, size) in state_start.items():
            state_label = state_name.upper().replace(".", "_")
            emit(log, f"STAGE_START_{state_label}_PRESENT={str(present).lower()}")
            emit(log, f"STAGE_START_{state_label}_BYTES={size}")
        state_pair_valid = all(present and size > 0 for present, size in state_start.values())
        emit(log, f"STAGE_START_SISO_FS_STATE_PAIR_VALID={str(state_pair_valid).lower()}")
        checkpoint_archive = root / "sandium-checkpoint.tar.zst"
        checkpoint_bytes = checkpoint_archive.stat().st_size if checkpoint_archive.is_file() else 0
        emit(log, f"RESTORED_CHECKPOINT_ARCHIVE_BYTES={checkpoint_bytes}")
        emit(log, "SOURCE_TIMESTAMP_POLICY=epoch_946684800_full_tree_v1")
        emit(log, "SOURCE_TIMESTAMP_VERIFY=PASS")
        emit(log, "SANDIUM_REMOTE_PREFLIGHT=PASS source=current-workflow-uncached-cxx-action")
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
        last_activity = time.monotonic()
        child_exit_seen_at: float | None = None
        last_info_poll = 0.0
        last_metrics_poll = 0.0
        remote_gate_pass_logged = False

        def process_text(text: str) -> None:
            nonlocal remote_seen, regeneration_seen, partial, rolling, last_activity
            if not text:
                return
            last_activity = time.monotonic()
            sys.stdout.write(text)
            sys.stdout.flush()
            log.write(text)
            log.flush()
            lower = text.lower()
            rolling = (rolling + lower)[-8192:]
            if REGENERATE_NINJA.search(rolling):
                regeneration_seen = True
            partial += text
            while "\n" in partial:
                line, partial = partial.split("\n", 1)
                match = PROGRESS_LINE.match(line.strip())
                if match:
                    label = match.group(1).strip()
                    if label and label.lower() != "regenerating ninja files":
                        current_frontier.add(label)
                if any(pattern.search(line) for pattern in REMOTE_SIGNATURES):
                    remote_signatures.add(line.strip())

        try:
            while True:
                now = time.monotonic()
                elapsed = now - started
                if proc.poll() is None and not remote_seen and now - last_metrics_poll >= max(1, args.metrics_poll_interval_seconds):
                    last_metrics_poll = now
                    try:
                        metrics_now = parse_metrics(metrics_path)
                    except ValueError as exc:
                        gate_failed = True
                        stop_reason = f"invalid Siso metrics during remote gate: {exc}"
                        emit(log, "ACTUAL_REMOTE_EXECUTION_METRICS_GATE=FAIL invalid_metrics=true")
                        stop_process_group(proc)
                        metrics_now = {"remote_executed_action_records": 0}
                    if metrics_now.get("remote_executed_action_records", 0) > 0:
                        remote_seen = True
                if proc.poll() is None and elapsed >= remaining:
                    timeout_seen = True
                    stop_reason = "stage compile time budget exhausted"
                    emit(log, f"COMPILE_WINDOW_TIMEOUT=PASS timeout_seconds={remaining}")
                    stop_process_group(proc)
                elif regeneration_seen and proc.poll() is None:
                    gate_failed = True
                    stop_reason = "Ninja attempted to regenerate the restored/prepared graph"
                    emit(log, "INCREMENTAL_HANDOVER_GATE=FAIL; Ninja attempted to regenerate files after source timestamp normalization")
                    stop_process_group(proc)

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
                    # Log text and cache messages are not execution evidence. The
                    # only authoritative early-gate signal is an uncached Siso
                    # action whose metrics explicitly report is_remote=true (or the
                    # explicit is_local=false compatibility field).
                    try:
                        metrics_now = parse_metrics(metrics_path)
                    except ValueError as exc:
                        gate_failed = True
                        stop_reason = f"invalid Siso metrics during remote gate: {exc}"
                        emit(log, "ACTUAL_REMOTE_EXECUTION_METRICS_GATE=FAIL invalid_metrics=true")
                        stop_process_group(proc)
                        metrics_now = {"remote_executed_action_records": 0}
                    if metrics_now.get("remote_executed_action_records", 0) > 0:
                        remote_seen = True
                    if info_delta:
                        for line in info_delta.splitlines():
                            if any(pattern.search(line) for pattern in REMOTE_SIGNATURES):
                                remote_signatures.add(line.strip())
                if remote_seen and not gate_failed and not regeneration_seen and not remote_gate_pass_logged:
                    emit(log, f"EARLY_REMOTE_EXECUTION_GATE=PASS seconds={int(time.monotonic()-started)}")
                    remote_gate_pass_logged = True
                if proc.poll() is not None and child_exit_seen_at is None:
                    child_exit_seen_at = time.monotonic()
                if proc.poll() is not None and eof:
                    break
                if proc.poll() is not None and child_exit_seen_at is not None and time.monotonic() - child_exit_seen_at > 120:
                    stop_reason = "build process exited but child output/Siso did not shut down cleanly"
                    stop_process_group(proc, grace_seconds=5)
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
            gate_failed = True
        if not remote_seen and not remote_preflight_passed and not (apk.is_file() and apk.stat().st_size >= 1_000_000):
            gate_failed = True
            stop_reason = stop_reason or "build finished without remote execution evidence and did not produce APK"

        # Wait for Siso to close its state/journal before writing a checkpoint.
        siso_forced_kill = False
        siso_wait_started = time.monotonic()
        while count_siso_processes() and time.monotonic() - siso_wait_started < 120:
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
        if metrics.get("remote_executed_action_records", 0) == 0 and not (apk.is_file() and apk.stat().st_size >= 1_000_000):
            if remote_preflight_passed:
                emit(log, "CURRENT_WINDOW_REMOTE_EXECUTION=NOT_OBSERVED; preflight proof is reported separately")
                emit(log, "ACTUAL_REMOTE_EXECUTION_METRICS_GATE=PASS source=validated_current_workflow_preflight remote_executed_action_records=0")
            else:
                gate_failed = True
                stop_reason = stop_reason or "no actual remote-executed Siso actions were recorded in this compile window"
                emit(log, "ACTUAL_REMOTE_EXECUTION_METRICS_GATE=FAIL remote_executed_action_records=0")
        else:
            emit(log, f"ACTUAL_REMOTE_EXECUTION_METRICS_GATE=PASS remote_executed_action_records={metrics.get('remote_executed_action_records', 0)}")
        for key, value in metrics.items():
            emit(log, f"SISO_{key.upper()}={value}")
        emit(log, f"REMOTE_EXEC_LOG_SIGNATURE_COUNT={len(remote_signatures)}")
        if regeneration_seen:
            emit(log, "INCREMENTAL_HANDOVER_GATE=FAIL")
        elif remote_seen:
            emit(log, "INCREMENTAL_HANDOVER_GATE=PASS")
        if siso_forced_kill:
            gate_failed = True
        if stop_reason:
            emit(log, "COMPILE_WINDOW_STOP_REASON=" + stop_reason)

        objects_after, outputs_after = count_outputs(out_dir)
        emit(log, f"STAGE_END_OBJECT_COUNT={objects_after}")
        emit(log, f"STAGE_OBJECT_COUNT_DELTA={objects_after - objects_before}")
        emit(log, f"STAGE_END_BUILD_OUTPUT_COUNT={outputs_after}")
        emit(log, f"STAGE_BUILD_OUTPUT_COUNT_DELTA={outputs_after - outputs_before}")
        emit(log, f"ACTION_FRONTIER_PRIOR_UNIQUE_LABEL_COUNT={len(prior_frontier)}")
        emit(log, f"ACTION_FRONTIER_CURRENT_UNIQUE_LABEL_COUNT={len(current_frontier)}")
        new_frontier = current_frontier - prior_frontier
        emit(log, f"ACTION_FRONTIER_NEW_UNIQUE_LABEL_COUNT={len(new_frontier)}")
        prior_frontier_path.write_text(
            "".join(label + "\n" for label in sorted(prior_frontier | current_frontier)),
            encoding="utf-8",
        )
        emit(log, "SISO_STATE_PAIR_MEASUREMENT_PHASE=AFTER_SHUTDOWN")
        for state_name in (".siso_fs_state", ".siso_fs_state.journal"):
            state_path = out_dir / state_name
            state_label = state_name.upper().replace(".", "_")
            if state_path.is_file() and not state_path.is_symlink():
                size = state_path.stat().st_size
                emit(log, f"{state_label}_BYTES={size}")
                emit(log, f"STAGE_END_{state_label}_PRESENT=true")
                emit(log, f"STAGE_END_{state_label}_BYTES={size}")
            else:
                emit(log, f"{state_label}_PRESENT=false")
                emit(log, f"STAGE_END_{state_label}_PRESENT=false")
                emit(log, f"STAGE_END_{state_label}_BYTES=0")
        emit(log, f"COMPILE_CHILD_EXIT_CODE={build_return_code}")
        emit(log, f"METRICS_JSON_BYTES={metrics_path.stat().st_size if metrics_path.is_file() else 0}")

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
