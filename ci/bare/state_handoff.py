#!/usr/bin/env python3
"""Discover a useful prior run and hand off identity-bound Siso state between stages."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import IdentityError, control_and_workspace, source_identity

SCHEMA = 1
ARCHIVE_NAME = "sandium-stage-state.tar.zst"
META_NAME = "sandium-stage-state.meta"
REPORT_NAME = "sandium-state-handoff-report.json"
OUT_RELATIVE = Path("src/out/android_Debug_arm64")
STATE_NAMES = (
    ".siso_fs_state", ".siso_fs_state.0", ".ninja_log", ".ninja_deps",
    ".siso_config", ".siso_deps", ".siso_filegroups", ".siso_failed_targets",
)
VALID_NAMES = set(STATE_NAMES)

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def load_json(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise IdentityError(f"Required handoff file missing/unsafe: {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise IdentityError(f"Handoff JSON must be an object: {path}")
    return obj

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def useful(meta: dict) -> bool:
    ledger = meta.get("stage_ledger")
    if not isinstance(ledger, dict):
        return False
    progress = int(ledger.get("max_completed_actions", 0) or 0)
    total = int(ledger.get("total_action_count", 0) or 0)
    metrics = ledger.get("metrics", {})
    remote = bool(
        ledger.get("remote_execution_proven")
        or ledger.get("remote_action_cache_reuse_proven")
        or (isinstance(metrics, dict) and (
            metrics.get("remote_execution_proven") or metrics.get("remote_action_cache_reuse_proven")
        ))
    )
    apk = ledger.get("artifact_verified") is True and ledger.get("apksigner_verification") == "PASS"
    return (progress > 0 and total > 0 and remote) or apk

def toolchain_pin(meta_path: Path) -> None:
    meta = load_json(meta_path)
    if meta.get("schema") != SCHEMA or meta.get("format") != "siso-state-only":
        raise IdentityError("Unsupported prior Siso state metadata")
    ident = meta.get("build_identity", {})
    pin = ident.get("toolchain_depot_tools_commit", "") if isinstance(ident, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", str(pin)):
        raise IdentityError("Prior Siso state has no valid depot_tools commit")
    print(pin)

def pack(root: Path, control: Path, stage: int, workflow_id: str, ledger_path: Path,
         archive: Path, meta_path: Path) -> int:
    out = root / OUT_RELATIVE
    if not out.is_dir() or out.is_symlink():
        raise IdentityError(f"GN output directory missing/unsafe: {out}")
    ledger = load_json(ledger_path)
    if ledger.get("stage") != stage:
        raise IdentityError("Stage ledger number differs from state capsule stage")
    ident = source_identity(control, root, require_gn=True)
    ident_hash = digest(ident)
    if ledger.get("build_identity_sha256") != ident_hash:
        raise IdentityError("Stage ledger identity differs from current build identity")
    if not useful({"stage_ledger": ledger}):
        raise IdentityError("Refusing to publish a cache candidate without useful progress and remote/cache proof, or a verified APK")
    files = []
    for name in STATE_NAMES:
        path = out / name
        if path.is_file() and not path.is_symlink():
            files.append({"name": name, "size": path.stat().st_size, "sha256": sha256(path)})
    if not any(f["name"] in (".siso_fs_state", ".siso_fs_state.0") for f in files):
        raise IdentityError("Siso filesystem state file missing; cannot hand off stage state")
    archive.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".sandium-state.", suffix=".tar.zst", dir=str(archive.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        subprocess.run(
            ["tar", "--use-compress-program=zstd", "-cf", str(tmp), "-C", str(out)]
            + [f["name"] for f in files],
            check=True,
        )
        archive_hash = sha256(tmp)
        os.replace(tmp, archive)
    finally:
        tmp.unlink(missing_ok=True)
    payload = {
        "schema": SCHEMA, "format": "siso-state-only",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "workflow_id": workflow_id or "unknown", "stage": stage, "source": "Bare",
        "build_identity": ident, "build_identity_sha256": ident_hash,
        "stage_ledger_sha256": sha256(ledger_path), "stage_ledger": ledger,
        "archive_name": archive.name, "archive_sha256": archive_hash, "state_files": files,
        "local_compiled_outputs_archived": False,
        "compiled_output_reuse": "BuildBuddy/Siso remote action cache; verify per-stage metrics",
    }
    write_json(meta_path, payload)
    print("SANDIUM_STATE_HANDOFF_PACK=PASS")
    print("SANDIUM_STATE_HANDOFF_STAGE=" + str(stage))
    print("SANDIUM_STATE_HANDOFF_FILES=" + ",".join(f["name"] for f in files))
    print("SANDIUM_STATE_HANDOFF_ARCHIVE_BYTES=" + str(archive.stat().st_size))
    print("SANDIUM_STATE_HANDOFF_BUILD_IDENTITY_SHA256=" + ident_hash)
    return 0

def restore(root: Path, control: Path, archive: Path, meta_path: Path,
            stage: int, workflow_id: str, required: bool) -> int:
    def miss(reason: str) -> int:
        report = {
            "schema": SCHEMA, "SANDIUM_STATE_HANDOFF": "MISS",
            "SANDIUM_PRIOR_USEFUL_RUN": "REJECTED" if meta_path.exists() else "NOT_FOUND",
            "reason": reason, "current_stage": stage,
            "current_workflow_id": workflow_id or "unknown",
            "state_files_restored": [], "local_compiled_outputs_restored": False,
            "compiled_output_reuse": "BuildBuddy/Siso remote action cache",
        }
        write_json(root / REPORT_NAME, report)
        print("SANDIUM_STATE_HANDOFF=MISS")
        print("SANDIUM_STATE_HANDOFF_REASON=" + reason)
        if required:
            raise IdentityError("Required previous-stage handoff missing/incompatible: " + reason)
        return 0
    try:
        if not archive.is_file() or not meta_path.is_file():
            return miss("archive_or_metadata_not_found")
        meta = load_json(meta_path)
        if meta.get("schema") != SCHEMA or meta.get("format") != "siso-state-only":
            return miss("unsupported_schema_or_format")
        if meta.get("archive_name") != archive.name or meta.get("archive_sha256") != sha256(archive):
            return miss("archive_hash_or_name_mismatch")
        if not useful(meta):
            return miss("prior_run_not_proven_useful")
        saved_stage = int(meta.get("stage", 0))
        saved_workflow = str(meta.get("workflow_id", "unknown"))
        if stage == 1:
            if saved_workflow == workflow_id:
                return miss("stage1_candidate_not_from_a_prior_workflow")
        elif saved_stage != stage - 1 or saved_workflow != workflow_id:
            return miss(f"expected_stage_{stage-1}_same_workflow_got_{saved_stage}_{saved_workflow}")

        ident = source_identity(control, root, require_gn=True)
        ident_hash = digest(ident)
        saved_ident = meta.get("build_identity", {})
        if meta.get("build_identity_sha256") != ident_hash:
            changed = sorted(k for k, v in ident.items()
                             if not isinstance(saved_ident, dict) or saved_ident.get(k) != v)
            return miss("build_identity_mismatch:" + ",".join(changed))
        ledger = meta.get("stage_ledger", {})
        if not isinstance(ledger, dict) or ledger.get("build_identity_sha256") != ident_hash:
            return miss("embedded_stage_ledger_identity_mismatch")
        entries = meta.get("state_files")
        if not isinstance(entries, list) or not entries:
            return miss("state_file_manifest_missing")
        names, expected = [], {}
        for item in entries:
            if not isinstance(item, dict):
                return miss("invalid_state_file_manifest_entry")
            name = str(item.get("name", ""))
            if name not in VALID_NAMES or "/" in name or "\\" in name or name in names:
                return miss("unsafe_or_duplicate_state_file_name")
            names.append(name)
            expected[name] = str(item.get("sha256", ""))
        if not any(n in (".siso_fs_state", ".siso_fs_state.0") for n in names):
            return miss("state_manifest_has_no_siso_state")

        out = root / OUT_RELATIVE
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sandium-state-restore.", dir=str(root)) as temp:
            tmp_root = Path(temp)
            members = subprocess.check_output(
                ["tar", "--use-compress-program=zstd", "-tf", str(archive)],
                text=True, stderr=subprocess.STDOUT,
            ).splitlines()
            if sorted(members) != sorted(names):
                return miss("archive_members_do_not_match_manifest")
            subprocess.run(["tar", "--use-compress-program=zstd", "-xf", str(archive), "-C", str(tmp_root)], check=True)
            for name in names:
                source = tmp_root / name
                if not source.is_file() or source.is_symlink() or sha256(source) != expected[name]:
                    return miss("state_file_hash_mismatch:" + name)
            restored = []
            for name in names:
                source, dest = tmp_root / name, out / name
                if dest.is_file() and sha256(dest) == expected[name]:
                    restored.append(name)
                    continue
                backup = dest.with_name(dest.name + ".pre-handoff-backup")
                if dest.exists():
                    if backup.exists():
                        return miss("preserved_backup_already_exists:" + name)
                    os.replace(dest, backup)
                shutil.copy2(source, dest)
                restored.append(name)

        payload = {
            "schema": SCHEMA, "SANDIUM_STATE_HANDOFF": "RESTORED",
            "SANDIUM_PRIOR_USEFUL_RUN": "ACCEPTED" if stage == 1 else "CURRENT_WORKFLOW_PREVIOUS_STAGE",
            "source_workflow_id": saved_workflow, "source_stage": saved_stage,
            "current_workflow_id": workflow_id or "unknown", "current_stage": stage,
            "build_identity_sha256": ident_hash, "state_files_restored": restored,
            "source_max_completed_actions": int(ledger.get("max_completed_actions", 0)),
            "source_total_action_count": int(ledger.get("total_action_count", 0)),
            "source_remote_execution_proven": bool(ledger.get("remote_execution_proven")),
            "source_remote_action_cache_reuse_proven": bool(ledger.get("remote_action_cache_reuse_proven")),
            "local_compiled_outputs_restored": False,
            "compiled_output_reuse": "BuildBuddy/Siso remote action cache; current-stage metrics must prove hits",
        }
        write_json(root / REPORT_NAME, payload)
        print("SANDIUM_STATE_HANDOFF=RESTORED")
        print("SANDIUM_PRIOR_USEFUL_RUN=" + payload["SANDIUM_PRIOR_USEFUL_RUN"])
        print("SANDIUM_STATE_FILES_RESTORED=" + ",".join(restored))
        print("SANDIUM_STATE_HANDOFF_BUILD_IDENTITY_SHA256=" + ident_hash)
        return 0
    except (IdentityError, OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        return miss("validation_error:" + str(exc))

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="mode", required=True)
    p = subs.add_parser("toolchain-pin"); p.add_argument("--meta", type=Path, required=True)
    p = subs.add_parser("pack")
    p.add_argument("--root", type=Path); p.add_argument("--stage", type=int, choices=range(1,6), required=True)
    p.add_argument("--workflow-id", default="unknown"); p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--archive", type=Path); p.add_argument("--meta", type=Path)
    p = subs.add_parser("restore")
    p.add_argument("--root", type=Path); p.add_argument("--stage", type=int, choices=range(1,6), required=True)
    p.add_argument("--workflow-id", default="unknown"); p.add_argument("--archive", type=Path); p.add_argument("--meta", type=Path)
    p.add_argument("--required", action="store_true")
    args = parser.parse_args()
    control, inferred_root = control_and_workspace(Path(__file__))
    root = args.root.resolve() if args.root else inferred_root
    if args.mode == "toolchain-pin":
        toolchain_pin(args.meta); return 0
    archive = args.archive.resolve() if args.archive else root / ARCHIVE_NAME
    meta = args.meta.resolve() if args.meta else root / META_NAME
    if args.mode == "pack":
        return pack(root, control, args.stage, args.workflow_id, args.ledger, archive, meta)
    return restore(root, control, archive, meta, args.stage, args.workflow_id, args.required)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IdentityError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise SystemExit("SANDIUM_STATE_HANDOFF_ERROR=" + str(exc))
