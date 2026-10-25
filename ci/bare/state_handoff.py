#!/usr/bin/env python3
"""Discover a useful prior run and hand off identity-bound Siso state between stages."""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_identity import IdentityError, control_and_workspace, read_pins, source_identity

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
    # The ledger's artifact_verified field is emitted only after build_identity.py
    # verified the APK against the manifest, ZIP structure and apksigner result.
    apk = ledger.get("artifact_verified") is True
    return (progress > 0 and total > 0 and remote) or apk

def control_inputs(control: Path) -> dict[str, str]:
    paths = (
        "ci/bare/source-pin.txt",
        "ci/bare/gn_args.gn",
        "ci/bare/prepare_source.sh",
        "ci/bare/configure_siso.py",
        "ci/siso/buildbuddy_backend.star",
        "ci/siso/buildbuddy-credential-helper.py",
    )
    result = {}
    for rel in paths:
        path = control / rel
        if not path.is_file() or path.is_symlink():
            raise IdentityError("Build input missing/unsafe: " + rel)
        result[rel] = sha256(path)
    return result

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
        "control_inputs": control_inputs(control),
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
            # A same-workflow rerun can also resume a successfully saved earlier
            # stage; the immutable build-number cache key prevents self-reading.
            pass
        elif saved_stage != stage - 1 or saved_workflow != workflow_id:
            return miss(f"expected_stage_{stage-1}_same_workflow_got_{saved_stage}_{saved_workflow}")

        if meta.get("control_inputs") != control_inputs(control):
            return miss("control_input_hash_mismatch")
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


def reuse_cached_apk(root: Path, control: Path, archive: Path, meta_path: Path,
                     apk: Path, manifest_path: Path, ledger_path: Path,
                     status_path: Path, report_path: Path, workflow_id: str) -> int:
    def miss(reason: str) -> int:
        write_json(report_path, {
            "schema": SCHEMA, "SANDIUM_STATE_HANDOFF": "MISS",
            "SANDIUM_PRIOR_USEFUL_RUN": "REJECTED", "reason": reason,
            "current_stage": 1, "current_workflow_id": workflow_id or "unknown",
            "state_files_restored": [], "local_compiled_outputs_restored": False,
        })
        print("SANDIUM_PRIOR_APK_REUSE=MISS")
        print("SANDIUM_PRIOR_APK_REUSE_REASON=" + reason)
        # Preserve an invalid cache candidate for forensic review, but keep it
        # out of the canonical artifact path before a fresh build begins.
        for path in (apk, manifest_path):
            if path.exists() and not path.is_symlink():
                rejected = path.with_name(path.name + ".rejected-cache-candidate")
                if not rejected.exists():
                    os.replace(path, rejected)
        return 1
    try:
        if not archive.is_file() or not meta_path.is_file() or not apk.is_file() or not manifest_path.is_file():
            return miss("complete_prior_apk_candidate_not_found")
        meta = load_json(meta_path)
        if meta.get("schema") != SCHEMA or meta.get("format") != "siso-state-only":
            return miss("unsupported_state_capsule")
        if meta.get("archive_name") != archive.name or meta.get("archive_sha256") != sha256(archive):
            return miss("state_capsule_checksum_mismatch")
        if not useful(meta):
            return miss("prior_run_not_useful")
        if meta.get("control_inputs") != control_inputs(control):
            return miss("control_input_hash_mismatch")
        ident = meta.get("build_identity", {})
        ident_hash = digest(ident)
        if meta.get("build_identity_sha256") != ident_hash:
            return miss("cached_build_identity_hash_mismatch")
        pins = read_pins(control)
        expected = {
            "bare_commit": pins["bare_commit"],
            "chromium_commit": pins["chromium_commit"],
            "chromium_version": pins["chromium_version"],
            "target_abi": pins["target_abi"],
            "build_config": pins["build_config"],
            "bare_repository": pins["bare_repository"],
        }
        for key, value in expected.items():
            if ident.get(key) != value:
                return miss("pinned_input_mismatch:" + key)
        manifest = load_json(manifest_path)
        if manifest.get("build_identity_sha256") != ident_hash:
            return miss("cached_manifest_build_identity_mismatch")
        if {key: manifest.get(key) for key in ident} != ident:
            return miss("cached_manifest_source_identity_mismatch")
        if manifest.get("artifact_verified") is not True or manifest.get("apksigner_verification") != "PASS":
            return miss("cached_apk_lacks_prior_signature_verification")
        if manifest.get("artifact_sha256") != sha256(apk) or manifest.get("artifact_bytes") != apk.stat().st_size:
            return miss("cached_apk_hash_or_size_mismatch")
        if apk.stat().st_size < 1_000_000:
            return miss("cached_apk_unexpectedly_small")
        with zipfile.ZipFile(apk) as zf:
            if zf.testzip() is not None:
                return miss("cached_apk_zip_integrity_failed")
            names = set(zf.namelist())
            if "AndroidManifest.xml" not in names or not any(n.startswith("classes") and n.endswith(".dex") for n in names):
                return miss("cached_apk_missing_manifest_or_dex")
        ledger = meta.get("stage_ledger", {})
        if not isinstance(ledger, dict) or not ledger.get("artifact_verified"):
            return miss("prior_ledger_does_not_record_verified_apk")
        current_ledger = {
            **ledger,
            "schema": 3,
            "stage": 1,
            "evidence": "VERIFIED_APK_REUSED_FROM_PRIOR_USEFUL_RUN",
            "prior_useful_run": {
                "workflow_id": meta.get("workflow_id", "unknown"),
                "stage": meta.get("stage"),
                "state_capsule_sha256": meta.get("archive_sha256"),
            },
            "state_handoff": {
                "SANDIUM_STATE_HANDOFF": "REUSED_VERIFIED_APK",
                "SANDIUM_PRIOR_USEFUL_RUN": "ACCEPTED",
                "source_workflow_id": meta.get("workflow_id", "unknown"),
                "source_stage": meta.get("stage"),
                "current_workflow_id": workflow_id or "unknown",
                "current_stage": 1,
                "build_identity_sha256": ident_hash,
                "local_compiled_outputs_restored": False,
                "compiled_output_reuse": "Verified APK reused; no compile needed",
            },
        }
        write_json(ledger_path, current_ledger)
        write_json(report_path, current_ledger["state_handoff"])
        status = {
            "BUILD_FINISHED": "true",
            "APK_ARCHIVE_VALID": "true",
            "APK_SIGNATURE_VERIFIED": "true",
            "APK_SHA256": sha256(apk),
            "APK_SIZE_BYTES": str(apk.stat().st_size),
            "BUILD_IDENTITY_SHA256": ident_hash,
            "STAGE": "1",
            "REUSED_PRIOR_VERIFIED_APK": "true",
            "SOURCE_WORKFLOW_ID": str(meta.get("workflow_id", "unknown")),
            "SOURCE_STAGE": str(meta.get("stage", "unknown")),
        }
        status_path.write_text("".join(f"{k}={v}\n" for k, v in status.items()), encoding="utf-8")
        print("SANDIUM_PRIOR_APK_REUSE=PASS")
        print("SANDIUM_PRIOR_APK_SHA256=" + sha256(apk))
        print("SANDIUM_PRIOR_APK_BYTES=" + str(apk.stat().st_size))
        print("SANDIUM_PRIOR_USEFUL_WORKFLOW_ID=" + str(meta.get("workflow_id", "unknown")))
        return 0
    except (IdentityError, OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
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
    p = subs.add_parser("reuse-apk")
    p.add_argument("--root", type=Path); p.add_argument("--workflow-id", default="unknown")
    p.add_argument("--archive", type=Path); p.add_argument("--meta", type=Path)
    p.add_argument("--apk", type=Path, required=True); p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--ledger", type=Path, required=True); p.add_argument("--status", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    control, inferred_root = control_and_workspace(Path(__file__))
    root = args.root.resolve() if args.root else inferred_root
    if args.mode == "toolchain-pin":
        toolchain_pin(args.meta); return 0
    archive = args.archive.resolve() if args.archive else root / ARCHIVE_NAME
    meta = args.meta.resolve() if args.meta else root / META_NAME
    if args.mode == "pack":
        return pack(root, control, args.stage, args.workflow_id, args.ledger, archive, meta)
    if args.mode == "reuse-apk":
        return reuse_cached_apk(root, control, archive, meta, args.apk.resolve(), args.manifest.resolve(),
                                args.ledger.resolve(), args.status.resolve(), args.report.resolve(), args.workflow_id)
    return restore(root, control, archive, meta, args.stage, args.workflow_id, args.required)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IdentityError, OSError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise SystemExit("SANDIUM_STATE_HANDOFF_ERROR=" + str(exc))
