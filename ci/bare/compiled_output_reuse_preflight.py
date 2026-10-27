#!/usr/bin/env python3
"""Stabilize Siso input mtimes and prove restored object targets are up to date early."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path
from typing import Any

OUTPUT_RELATIVE = Path("src/out/android_Debug_arm64")
SISO_CONFIG_RELATIVE = Path("src/build/config/siso")
SISO_INPUT_NAMES = (".sisoenv", ".sisorc")
COMPILED_SUFFIXES = {".o", ".a", ".so"}
MIN_SAMPLE_COUNT = 8

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
    samples = report.get("compiled_output_action_samples")
    if not isinstance(samples, list) or len(samples) < MIN_SAMPLE_COUNT:
        raise ReusePreflightError(
            f"Restored checkpoint has too few action-output samples: {len(samples) if isinstance(samples, list) else 0} < {MIN_SAMPLE_COUNT}"
        )
    paths = []
    for item in samples:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ReusePreflightError("Invalid previous-stage compiled-output sample record")
        relative = item["path"]
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != "obj":
            raise ReusePreflightError(f"Unsafe previous-stage object target: {relative}")
        if rel.suffix.lower() not in COMPILED_SUFFIXES:
            raise ReusePreflightError(f"Unsupported prior object target suffix: {relative}")
        path = output / rel
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0 or sha256_file(path) != item.get("sha256"):
            raise ReusePreflightError(f"Restored sample output missing or hash mismatch: {relative}")
        paths.append(rel.as_posix())
    paths = list(dict.fromkeys(paths))
    if len(paths) < MIN_SAMPLE_COUNT:
        raise ReusePreflightError(f"Too few unique restored object targets: {len(paths)}")
    return paths

def scheduled_sample_targets(output_text: str, targets: list[str]) -> dict[str, list[str]]:
    scheduled = {}
    for target in targets:
        pattern = re.compile(r"(?<![A-Za-z0-9_./-])" + re.escape(target) + r"(?![A-Za-z0-9_./-])")
        for line in output_text.splitlines():
            clean = line.strip()
            if not clean or clean.startswith(("ninja:", "Siso finished", "Build Succeeded:", "SANDIUM_")):
                continue
            if pattern.search(clean):
                scheduled.setdefault(target, []).append(clean[:300])
    return scheduled

def verify_restored_output_reuse(*, root: Path, source_root: Path, stage: int,
                                 report_path: Path, epoch: int, timeout_seconds: int = 180) -> dict[str, Any]:
    """Run a target-only no-op preflight before allowing a full stage compile."""
    config_before = normalize_siso_input_timestamps(source_root, epoch)
    if not report_path.is_file() or report_path.is_symlink():
        if stage > 1:
            raise ReusePreflightError("Continuation stage lacks a state-handoff report before the build window")
        print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT=SKIP_NO_RESTORED_OUTPUTS")
        return {"status": "SKIP_NO_RESTORED_OUTPUTS", "sample_count": 0}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReusePreflightError(f"State-handoff report is unreadable: {exc}") from exc
    if not isinstance(report, dict):
        raise ReusePreflightError("State-handoff report must be a JSON object")
    targets = safe_sample_paths(report, root / OUTPUT_RELATIVE)
    if not targets:
        if stage > 1:
            raise ReusePreflightError("Continuation stage has no restored compiled-output checkpoint to reuse")
        print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT=SKIP_NO_RESTORED_OUTPUTS")
        return {"status": "SKIP_NO_RESTORED_OUTPUTS", "sample_count": 0}
    output = root / OUTPUT_RELATIVE
    graph = output / "build.ninja"
    if not graph.is_file() or graph.is_symlink() or graph.stat().st_size == 0:
        raise ReusePreflightError("Restored build.ninja missing/unsafe before reuse preflight")
    graph_before = sha256_file(graph)
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
    graph_after = sha256_file(graph)
    config_after = normalize_siso_input_timestamps(source_root, epoch)
    if config_after != config_before:
        raise ReusePreflightError("Siso config bytes changed during targeted dry-run")
    if graph_after != graph_before:
        raise ReusePreflightError("Restored build.ninja changed during targeted dry-run; graph is unstable; full build was not started")
    scheduled = scheduled_sample_targets(log, targets)
    if scheduled:
        details = "; ".join(f"{target}: {lines[0]}" for target, lines in list(scheduled.items())[:5])
        raise ReusePreflightError(f"Restored outputs are scheduled for rebuild ({len(scheduled)}/{len(targets)} samples); full build was not started: {details}; see {log_path}")
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT=PASS")
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_SAMPLES=" + str(len(targets)))
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_BUILD_NINJA_SHA256=" + graph_after)
    print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT_LOG=" + str(log_path))
    return {"status":"PASS","sample_count":len(targets),"sample_paths":targets,
            "build_ninja_sha256":graph_after,"siso_config_sha256":config_after,"log":str(log_path)}

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
        verify_restored_output_reuse(root=root,source_root=source_root,stage=args.stage,
            report_path=report,epoch=args.epoch,timeout_seconds=args.timeout_seconds)
        return 0
    except (ReusePreflightError,OSError,ValueError) as exc:
        print("SANDIUM_LOCAL_OUTPUT_REUSE_PREFLIGHT=FAIL "+str(exc),file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
