#!/usr/bin/env python3
"""Stabilize Siso input mtimes and prove restored object targets are up to date early."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import output_checkpoint as OC

OUTPUT_RELATIVE = Path("src/out/android_Debug_arm64")
SISO_CONFIG_RELATIVE = Path("src/build/config/siso")
SISO_INPUT_NAMES = (".sisoenv", ".sisorc")
MIN_SAMPLE_COUNT = OC.MIN_ACTION_OUTPUT_SAMPLES

class ReusePreflightError(RuntimeError):
    pass

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def normalize_siso_input_timestamps(source_root: Path, epoch: int) -> dict[str, str]:
    """Normalize generated Siso inputs after GN without modifying their bytes."""
    siso_dir = source_root / SISO_CONFIG_RELATIVE.relative_to("src")
    hashes = {}
    for name in SISO_INPUT_NAMES:
        path = siso_dir / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise ReusePreflightError(f"Required regular nonempty Siso input missing/unsafe: {path}")
        before = sha256_file(path)
        os.utime(path, (epoch, epoch), follow_symlinks=False)
        if int(path.stat().st_mtime) != epoch or sha256_file(path) != before:
            raise ReusePreflightError(f"Siso input timestamp normalization failed: {path}")
        hashes[name] = before
    print(f"SANDIUM_SISO_INPUT_TIMESTAMP_POLICY=stable_epoch_{epoch}")
    print("SANDIUM_SISO_INPUT_TIMESTAMP_FILES=" + ",".join(SISO_INPUT_NAMES))
    return hashes

def safe_sample_paths(report: dict[str, Any], output: Path) -> list[str]:
    if report.get("local_compiled_outputs_restored") is not True:
        return []
    try:
        samples = OC.normalize_compiled_output_action_samples(
            report.get("compiled_output_action_samples"),
            report.get("compiled_output_action_sample_schema"),
        )
    except OC.CheckpointError as exc:
        raise ReusePreflightError(str(exc)) from exc
    if len(samples) < MIN_SAMPLE_COUNT:
        raise ReusePreflightError(
            f"Restored checkpoint has too few verified leaf-object action samples: "
            f"{len(samples)} < {MIN_SAMPLE_COUNT}"
        )
    paths = []
    for item in samples:
        relative = item["path"]
        rel = Path(relative)
        path = output / rel
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != item["bytes"]
                or sha256_file(path) != item["sha256"]):
            raise ReusePreflightError(f"Restored sample output missing or hash mismatch: {relative}")
        paths.append(rel.as_posix())
    return paths

def is_siso_dry_run_invocation(clean: str) -> bool:
    """Identify Siso's echoed ninja -n invocation, not a scheduled build action.

    The wrapper may print one command line containing every requested target.
    Matching those command-line arguments as if they were output actions makes
    the reuse preflight report every sample dirty even when the dry run is idle.
    """
    if not re.search(r"(?:^|[/\\])siso\.py:\s*ninja\b", clean, re.IGNORECASE):
        return False
    return bool(re.search(r"(?:^|\s)-n(?:\s|$)", clean))


def scheduled_sample_targets(output_text: str, targets: list[str]) -> dict[str, list[str]]:
    scheduled = {}
    for target in targets:
        pattern = re.compile(r"(?<![A-Za-z0-9_./-])" + re.escape(target) + r"(?![A-Za-z0-9_./-])")
        for line in output_text.splitlines():
            clean = line.strip()
            if not clean or clean.startswith(("ninja:", "Siso finished", "Build Succeeded:", "SANDIUM_")):
                continue
            # Siso can echo the full dry-run command with all requested targets.
            # Those arguments are not build actions and must not count as rebuilds.
            if is_siso_dry_run_invocation(clean):
                continue
            if pattern.search(clean):
                scheduled.setdefault(target, []).append(clean[:300])
    return scheduled

def verify_restored_output_reuse(*, root: Path, source_root: Path, stage: int,
                                 report_path: Path, epoch: int, timeout_seconds: int = 180) -> dict[str, Any]:
    """Run a target-only no-op preflight before allowing a full stage compile."""
    config_before = normalize_siso_input_timestamps(source_root, epoch)
    output = root / OUTPUT_RELATIVE
    graph = output / "build.ninja"
    stamp = output / "build.ninja.stamp"

    def generator_output_signature(path: Path) -> tuple[str, int, int]:
        stat = path.stat()
        return sha256_file(path), stat.st_size, stat.st_mtime_ns

    if not graph.is_file() or graph.is_symlink() or graph.stat().st_size == 0:
        raise ReusePreflightError("Current build.ninja missing/unsafe before reuse preflight")
    if not stamp.is_file() or stamp.is_symlink():
        raise ReusePreflightError("Current build.ninja.stamp missing/unsafe before reuse preflight")
    graph_before = generator_output_signature(graph)
    stamp_before = generator_output_signature(stamp)

    # A cold Stage 1 legitimately has no prior-output evidence. Still report the
    # real current graph hash so the shell never infers it from console logs.
    if not report_path.is_file() or report_path.is_symlink():
        if stage > 1:
            raise ReusePreflightError("Continuation stage lacks a state-handoff report before the build window")
        print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT=SKIP_NO_RESTORED_OUTPUTS")
        print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_BUILD_NINJA_SHA256=" + graph_before[0])
        return {"status": "SKIP_NO_RESTORED_OUTPUTS", "sample_count": 0,
                "build_ninja_sha256": graph_before[0], "build_ninja_mtime_ns": graph_before[2]}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReusePreflightError(f"State-handoff report is unreadable: {exc}") from exc
    if not isinstance(report, dict):
        raise ReusePreflightError("State-handoff report must be a JSON object")
    targets = safe_sample_paths(report, output)
    if not targets:
        if stage > 1:
            raise ReusePreflightError("Continuation stage has no restored compiled-output checkpoint to reuse")
        print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT=SKIP_NO_RESTORED_OUTPUTS")
        print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_BUILD_NINJA_SHA256=" + graph_before[0])
        return {"status": "SKIP_NO_RESTORED_OUTPUTS", "sample_count": 0,
                "build_ninja_sha256": graph_before[0], "build_ninja_mtime_ns": graph_before[2]}
    autoninja = shutil.which("autoninja")
    if not autoninja:
        raise ReusePreflightError("autoninja not found on PATH for compiled-output reuse preflight")
    log_path = root / f"stage-{stage}-reuse-preflight.log"
    command = [autoninja, "-n", "-C", str(output), *targets]
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_COMMAND=" + " ".join(command))
    try:
        completed = subprocess.run(command, cwd=source_root, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", timeout=timeout_seconds, check=False)
        log = completed.stdout or ""
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        log_path.write_text(str(partial) + f"\nREUSE_PREFLIGHT_TIMEOUT_SECONDS={timeout_seconds}\n", encoding="utf-8")
        raise ReusePreflightError(f"Targeted dry-run timed out after {timeout_seconds}s; full build was not started") from exc
    log_path.write_text(log, encoding="utf-8")
    if completed.returncode != 0:
        raise ReusePreflightError(f"Targeted dry-run failed with exit {completed.returncode}; full build was not started")
    graph_after = generator_output_signature(graph)
    stamp_after = generator_output_signature(stamp)
    config_after = normalize_siso_input_timestamps(source_root, epoch)
    if config_after != config_before:
        raise ReusePreflightError("Siso config bytes changed during targeted dry-run")
    if graph_after != graph_before:
        raise ReusePreflightError("Restored build.ninja changed during targeted dry-run; graph is unstable; full build was not started")
    if stamp_after != stamp_before:
        raise ReusePreflightError("Restored build.ninja.stamp changed during targeted dry-run; GN graph regenerated; full build was not started")
    scheduled = scheduled_sample_targets(log, targets)
    if scheduled:
        details = "; ".join(f"{target}: {lines[0]}" for target, lines in list(scheduled.items())[:5])
        # Capture Ninja/Siso's own dirty-reason explanations in a second no-build
        # invocation. The normal dry-run proves the checkpoint is not currently
        # reusable; this diagnostic identifies which input/mtime made it dirty.
        explain_path = root / f"stage-{stage}-reuse-explain.log"
        explain_command = [autoninja, "-n", "-d", "explain", "-C", str(output), *targets]
        print("SANDIUM_LOCAL_OUTPUT_REUSE_EXPLAIN_COMMAND=" + " ".join(explain_command))
        try:
            explain = subprocess.run(explain_command, cwd=source_root, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace",
                timeout=timeout_seconds, check=False)
            explain_text = explain.stdout or ""
            explain_path.write_text(explain_text, encoding="utf-8")
            print(f"SANDIUM_LOCAL_OUTPUT_REUSE_EXPLAIN_EXIT={explain.returncode}")
            print(f"SANDIUM_LOCAL_OUTPUT_REUSE_EXPLAIN_LOG={explain_path}")
            for line in explain_text.splitlines()[:24]:
                print("SANDIUM_REUSE_EXPLAIN: " + line[:500])
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            explain_path.write_text(str(partial) + f"\nREUSE_EXPLAIN_TIMEOUT_SECONDS={timeout_seconds}\n", encoding="utf-8")
            print(f"SANDIUM_LOCAL_OUTPUT_REUSE_EXPLAIN=TIMEOUT after {timeout_seconds}s")
        raise ReusePreflightError(f"Restored outputs are scheduled for rebuild ({len(scheduled)}/{len(targets)} samples); full build was not started: {details}; see {log_path} and {explain_path}")
    # No matching target row is not proof of a no-op. Siso may emit wrapper or
    # summary output that the target matcher cannot interpret. Only accept an
    # explicit no-work response; otherwise stop before the compile window.
    if not re.search(r"(?:ninja: no work to do[.]|no work to do)", log, re.IGNORECASE):
        preview = " | ".join(line.strip()[:220] for line in log.splitlines()[:8] if line.strip())
        raise ReusePreflightError(
            "Targeted dry-run had no matching rebuild rows and no explicit no-work marker; "
            f"output format is unverified, full build was not started (sample: {preview or '<empty>'}); "
            f"see {log_path}"
        )
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT=PASS")
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_SAMPLES=" + str(len(targets)))
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_BUILD_NINJA_SHA256=" + graph_after[0])
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_LOG=" + str(log_path))
    return {"status":"PASS","sample_count":len(targets),"sample_paths":targets,
            "build_ninja_sha256":graph_after[0],"siso_config_sha256":config_after,"log":str(log_path)}

def write_result_report(path: Path, result: dict[str, Any]) -> None:
    """Publish this invocation's structured result atomically."""
    if path.is_symlink():
        raise ReusePreflightError(f"Unsafe reuse-preflight result-report path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    if temporary.is_symlink():
        raise ReusePreflightError(f"Unsafe temporary reuse-preflight result-report path: {temporary}")
    try:
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest="mode",required=True)
    normalize=subs.add_parser("normalize")
    normalize.add_argument("--source-root",type=Path,required=True)
    normalize.add_argument("--epoch",type=int,required=True)
    verify=subs.add_parser("verify-reuse")
    verify.add_argument("--root",type=Path,default=Path.home()/"project")
    verify.add_argument("--source-root",type=Path,default=None)
    verify.add_argument("--stage",type=int,choices=range(1,6),required=True)
    verify.add_argument("--report",type=Path,default=None)
    verify.add_argument("--result-report",type=Path,default=None,
                        help="Write this invocation's structured result JSON")
    verify.add_argument("--epoch",type=int,required=True)
    verify.add_argument("--timeout-seconds",type=int,default=180)
    args=parser.parse_args()
    try:
        if args.epoch < 0:
            raise ReusePreflightError("Timestamp epoch must be nonnegative")
        if args.mode=="normalize":
            normalize_siso_input_timestamps(args.source_root.resolve(),args.epoch)
            return 0
        root=args.root.resolve()
        source_root=args.source_root.resolve() if args.source_root else root/"src"
        report=args.report.resolve() if args.report else root/"sandium-state-handoff-report.json"
        if args.timeout_seconds <= 0:
            raise ReusePreflightError("Reuse-preflight timeout must be positive")
        result = verify_restored_output_reuse(root=root,source_root=source_root,stage=args.stage,
            report_path=report,epoch=args.epoch,timeout_seconds=args.timeout_seconds)
        if args.result_report:
            write_result_report(args.result_report, result)
        return 0
    except (ReusePreflightError,OSError,ValueError) as exc:
        print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT=FAIL "+str(exc),file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
