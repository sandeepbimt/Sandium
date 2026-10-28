#!/usr/bin/env python3
"""Static five-stage contract. This validator never syncs or builds Chromium."""
import json, sys
from pathlib import Path

STAGES = [
 {"stage":1,"requires":[],"proof":["pinned_bare_chromium_identity","restore_integrity_validated_prior_output_checkpoint","measured_pending_work_after_reuse","verified_local_object_reuse","remote_or_cache_evidence","forward_progress_or_verified_apk"]},
 {"stage":2,"requires":[1],"proof":["same_build_identity","same_timestamp_policy","restore_stage_1_full_output_checkpoint","remote_cache_or_remote_execution","forward_progress_or_verified_apk"]},
 {"stage":3,"requires":[2],"proof":["same_build_identity","same_timestamp_policy","restore_stage_2_full_output_checkpoint","remote_cache_or_remote_execution","forward_progress_or_verified_apk"]},
 {"stage":4,"requires":[3],"proof":["same_build_identity","same_timestamp_policy","restore_stage_3_full_output_checkpoint","remote_cache_or_remote_execution","forward_progress_or_verified_apk"]},
 {"stage":5,"requires":[4],"proof":["same_build_identity","same_timestamp_policy","restore_stage_4_full_output_checkpoint","apk_zip_integrity","apk_signature","sha256"]},
]

def validate():
    errors = []
    here = Path(__file__).resolve().parent
    run_stage = (here / "run_stage.sh").read_text(encoding="utf-8")
    output_checkpoint = (here / "output_checkpoint.py").read_text(encoding="utf-8")
    fast_apk = (here / "fast_apk.py").read_text(encoding="utf-8")
    quality_path = here / "checkpoint_quality.py"
    quality = quality_path.read_text(encoding="utf-8") if quality_path.is_file() else ""
    manifest_preflight_path = here / "manifest_preflight.py"
    manifest_preflight = manifest_preflight_path.read_text(encoding="utf-8") if manifest_preflight_path.is_file() else ""
    timestamp = "epoch_946684800_regular_files_v1"

    if "SISO_REMOTE_OR_CACHE_EVIDENCE=FAIL_NO_ACTION_PROOF" not in run_stage:
        errors.append("APK build must fail closed without Siso remote/cache action proof")
    if "SISO_REMOTE_OR_CACHE_EVIDENCE=PASS_AFTER_BUILD_WINDOW" not in run_stage:
        errors.append("Siso evidence must be evaluated after the bounded build window, not by an early watchdog")
    if "WAIT -lt 240" in run_stage or "kill -INT -- \"$PID\"" in run_stage:
        errors.append("the 240-second remote-evidence watchdog must not kill slow-starting Siso")
    if "find \"$SRC\" -path \"$SRC/.git\" -prune -o -path \"$SRC/out\" -prune -o -type f -exec touch" not in run_stage:
        errors.append("timestamp normalization must touch regular source files only, excluding .git and out")
    if timestamp not in run_stage:
        errors.append("runner must record the regular-files-only timestamp policy")
    if 'output_checkpoint.py" restore' not in run_stage or 'output_checkpoint.py" pack' not in run_stage:
        errors.append("runner must restore and pack the full GN output tree")
    if "SANDIUM_OUTPUT_CHECKPOINT_RESTORE_MODE=INTACT_SNAPSHOT_BEFORE_GN" not in run_stage:
        errors.append("compatible checkpoints must be restored intact before GN generation")
    if "SANDIUM_STAGE1_REUSE_GATE=FAIL" not in run_stage:
        errors.append("Stage 1 must stop before compilation when no valid prior output checkpoint exists")
    if 'bootstrap_chromium.sh" configure' in run_stage:
        errors.append("cold Stage 1 GN/bootstrap path violates the no-cold-build acceptance gate")
    remaining_work = (here / "remaining_work.py").read_text(encoding="utf-8")
    # A restored snapshot can carry a stale/missing GN generator stamp. In that
    # state the observed Siso dry-run schedules only GN regeneration and reports
    # zero compile steps. Refresh GN once before measuring, but only accept it if
    # the graph/args bytes remain identical, their mtimes are preserved, and the
    # regenerated stamp is then stable throughout the Siso dry-run.
    if "SANDIUM_PENDING_WORK_GN_GEN=COMPLETED_BEFORE_MEASUREMENT" not in remaining_work:
        errors.append("pending-work measurement must settle GN regeneration before the Siso dry run")
    if "SANDIUM_PENDING_WORK_GN_REFRESH=PASS_GRAPH_AND_ARGS_UNCHANGED" not in remaining_work:
        errors.append("GN refresh must prove the restored build graph and args are byte-identical")
    if "SANDIUM_PENDING_WORK_GN_GRAPH_MTIME_PRESERVED=PASS" not in remaining_work:
        errors.append("GN refresh must preserve build.ninja mtime when graph bytes are unchanged")
    if "SANDIUM_PENDING_WORK_GN_ARGS_MTIME_PRESERVED=PASS" not in remaining_work:
        errors.append("GN refresh must preserve args.gn mtime when args bytes are unchanged")
    if "stamp_after != stamp_before" not in remaining_work:
        errors.append("Siso dry-run must prove the regenerated GN stamp stayed unchanged")
    if "restored build.ninja, args.gn, or build.ninja.stamp changed during dry run" not in remaining_work:
        errors.append("Siso dry-run must fail closed if it regenerates the restored graph/stamp")
    if '"graph_action_count"' not in remaining_work:
        errors.append("pending-work report must expose the independently measured full graph action count")
    if '"pending_actions"' not in remaining_work:
        errors.append("pending-work report must expose pending work separately from graph size")
    if "mode=filesystem-only" not in manifest_preflight or "siso_dry_run_in_preflight=no" not in manifest_preflight:
        errors.append("preflight must not run the known-repeating Siso no-op regeneration path")
    if "handled_by_real_siso_build" not in manifest_preflight:
        errors.append("manifest regeneration must be delegated to the actual Siso build after filesystem validation")
    if "Full identity-validated Chromium GN output tree restored" not in output_checkpoint:
        errors.append("small action-count variance helper is missing")
    if 'normalize_progress_total.py" --stage' in run_stage:
        errors.append("runner must not rewrite graph totals; pending work and full graph size are reported independently")
    if '"graph_action_count"' not in run_stage or '"pending_actions"' not in run_stage:
        errors.append("runner must consume separate pending-work and full-graph counters")
    if "Full identity-validated Chromium GN output tree restored" not in output_checkpoint:
        errors.append("full output checkpoint must write explicit handoff evidence")
    if not quality:
        errors.append("compiled-output checkpoint quality validator is missing")
    if 'checkpoint_quality.py" --root "$ROOT"' not in run_stage:
        errors.append("runner must reject invalid/tiny cached checkpoints before restore")
    if 'checkpoint_quality.py" --root "$ROOT"' not in run_stage or "--produced" not in run_stage:
        errors.append("runner must validate checkpoint quality before publishing")
    if "MIN_GOOD_OUTPUT_BYTES = 512 * 1024 * 1024" not in quality:
        errors.append("checkpoint selection must reject metadata-only and undersized output trees")
    if "candidate_is_not_a_green_completed_stage_or_authorized_partial" not in quality or "checkpoint_size_mismatch" not in quality:
        errors.append("checkpoint quality must default to green-stage metadata and match declared/actual size")
    if "--allow-partial" not in quality or "allow_partial" not in quality:
        errors.append("partial checkpoint recovery must be explicit and still pass checkpoint integrity checks")
    if "SANDIUM_APK_FAST_PATH=PASS" not in fast_apk:
        errors.append("cached APK fast path must verify and propagate the current stage")
    if "APK_SIGNATURE_CHECK=REUSED_SAVED_VERIFICATION_RECORD" not in fast_apk:
        errors.append("APK fast path must distinguish saved signature evidence from a fresh apksigner run")
    if [x["stage"] for x in STAGES] != [1,2,3,4,5]:
        errors.append("stages must be exactly 1..5")
    for i, x in enumerate(STAGES):
        expected = [] if i == 0 else [i]
        if x["requires"] != expected:
            errors.append("stage dependency mismatch at " + str(x["stage"]))
        if not x["proof"]:
            errors.append("stage has no proof requirements: " + str(x["stage"]))
    if not any("remote_or_cache_evidence" in x["proof"] for x in STAGES):
        errors.append("no remote evidence gate")
    if "restore_integrity_validated_prior_output_checkpoint" not in STAGES[0]["proof"]:
        errors.append("Stage 1 cannot proceed on a cache miss; it must restore prior compiled output")
    for x in STAGES[1:]:
        required = f"restore_stage_{x['stage']-1}_full_output_checkpoint"
        if required not in x["proof"]:
            errors.append(f"stage {x['stage']} must restore the prior full output checkpoint")
    return errors

if __name__ == "__main__":
    e = validate()
    if e:
        print("\n".join("SANDIUM_STAGE_CONTRACT_ERROR=" + x for x in e))
        sys.exit(1)
    if "--json" in sys.argv:
        print(json.dumps({"schema": 4, "stages": STAGES}, indent=2))
    else:
        print("SANDIUM_STAGE_CONTRACT=PASS\nSANDIUM_STAGE_COUNT=5\nSANDIUM_BUILD_STARTED=no")
