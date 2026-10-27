#!/usr/bin/env python3
"""Static five-stage contract. This validator never syncs or builds Chromium."""
import json, sys
from pathlib import Path

STAGES = [
 {"stage":1,"requires":[],"proof":["pinned_bare_chromium_identity","discover_prior_useful_run_or_record_miss","full_output_checkpoint_or_safe_siso_fallback","remote_or_cache_evidence","forward_progress_or_verified_apk"]},
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
    progress_normalizer_path = here / "normalize_progress_total.py"
    progress_normalizer = progress_normalizer_path.read_text(encoding="utf-8") if progress_normalizer_path.is_file() else ""
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
    if 'output_checkpoint.py" verify-reuse' not in run_stage:
        errors.append("runner must verify sampled local compiled-output reuse after each build window")
    if "compiled_output_inventory" not in output_checkpoint or "COMPILED_OUTPUT_MIN_FILES = 100" not in output_checkpoint:
        errors.append("full-output checkpoints must inventory actual compiled object files")
    if "compiled_output_action_samples" not in output_checkpoint or "MIN_REUSED_ACTION_SAMPLES = 3" not in output_checkpoint:
        errors.append("checkpoint must bind Siso-reported object outputs to content hashes")
    if "compiled_output_inventory_missing_from_predecessor_checkpoint" not in output_checkpoint:
        errors.append("continuation stages must reject legacy checkpoints without compiled-output inventory")
    if "SANDIUM_COMPILED_OUTPUT_REUSE_EVIDENCE=" not in output_checkpoint or "VERIFIED_LOCAL_OUTPUT_REUSE" not in output_checkpoint:
        errors.append("the reuse verifier must emit explicit evidence, separate from checkpoint restore")
    if not manifest_preflight:
        errors.append("runner must have a dedicated manifest preflight helper")
    if 'manifest_preflight.py"' not in run_stage:
        errors.append("runner must prime the restored Siso manifest before compilation")
    elif run_stage.index('manifest_preflight.py"') > run_stage.index('timeout --signal=INT --kill-after=60s'):
        errors.append("manifest preflight must run before the timed compile window")
    if "NINJA_REGEN_GATE=FAIL" in run_stage:
        errors.append("one supported Siso manifest regeneration must not discard a completed stage window")
    if "mode=filesystem-only" not in manifest_preflight or "siso_dry_run_in_preflight=no" not in manifest_preflight:
        errors.append("preflight must not run the known-repeating Siso no-op regeneration path")
    if "handled_by_real_siso_build" not in manifest_preflight:
        errors.append("manifest regeneration must be delegated to the actual Siso build after filesystem validation")
    if not progress_normalizer:
        errors.append("small action-count variance helper is missing")
    if 'normalize_progress_total.py"' not in run_stage:
        errors.append("runner must normalize progress count only with the identity-bound helper")
    elif run_stage.index('python3 "$CONTROL/ci/bare/normalize_progress_total.py" --stage') > run_stage.index('stage_ledger.py" --stage'):
        errors.append("progress denominator must be normalized before strict stage-ledger validation")
    if "build identity differs" not in progress_normalizer or "MAX_GRAPH_TOTAL_DRIFT = 0.01" not in progress_normalizer:
        errors.append("progress normalization must require exact build identity and a 1% maximum drift")
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
    if "candidate_is_not_a_green_completed_stage" not in quality or "checkpoint_size_mismatch" not in quality:
        errors.append("checkpoint quality must require green-stage metadata and match declared/actual size")
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
    if "discover_prior_useful_run_or_record_miss" not in STAGES[0]["proof"]:
        errors.append("stage 1 must discover a prior useful run or record an explicit miss")
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
