#!/usr/bin/env python3
"""Static five-stage contract. This validator never syncs or builds Chromium."""
import json, sys
STAGES = [
 {"stage":1,"requires":[],"proof":["pinned_bare_chromium_identity","discover_prior_useful_run_or_record_miss","identity_checked_siso_state_restore_or_record_miss","remote_or_cache_evidence","forward_progress_or_verified_apk"]},
 {"stage":2,"requires":[1],"proof":["same_build_identity","same_timestamp_policy","restore_stage_1_siso_state","remote_cache_or_remote_execution","forward_progress_or_verified_apk"]},
 {"stage":3,"requires":[2],"proof":["same_build_identity","same_timestamp_policy","restore_stage_2_siso_state","remote_cache_or_remote_execution","forward_progress_or_verified_apk"]},
 {"stage":4,"requires":[3],"proof":["same_build_identity","same_timestamp_policy","restore_stage_3_siso_state","remote_cache_or_remote_execution","forward_progress_or_verified_apk"]},
 {"stage":5,"requires":[4],"proof":["same_build_identity","same_timestamp_policy","restore_stage_4_siso_state","apk_zip_integrity","apk_signature","sha256"]},
]
def validate():
 errors=[]
 if [x["stage"] for x in STAGES] != [1,2,3,4,5]: errors.append("stages must be exactly 1..5")
 for i,x in enumerate(STAGES):
  expected=[] if i==0 else [i]
  if x["requires"] != expected: errors.append("stage dependency mismatch at "+str(x["stage"]))
  if not x["proof"]: errors.append("stage has no proof requirements: "+str(x["stage"]))
 if not any("remote_or_cache_evidence" in x["proof"] for x in STAGES): errors.append("no remote evidence gate")
 if "discover_prior_useful_run_or_record_miss" not in STAGES[0]["proof"]: errors.append("stage 1 must discover a prior useful run or record an explicit miss")
 if "identity_checked_siso_state_restore_or_record_miss" not in STAGES[0]["proof"]: errors.append("stage 1 must validate prior Siso state or record an explicit miss")
 for x in STAGES[1:]:
  required = f"restore_stage_{x['stage']-1}_siso_state"
  if required not in x["proof"]: errors.append(f"stage {x['stage']} must restore stage {x['stage']-1} Siso state")
 return errors
if __name__=="__main__":
 e=validate()
 if e:
  print("\n".join("SANDIUM_STAGE_CONTRACT_ERROR="+x for x in e)); sys.exit(1)
 if "--json" in sys.argv: print(json.dumps({"schema":3,"stages":STAGES},indent=2))
 else: print("SANDIUM_STAGE_CONTRACT=PASS\nSANDIUM_STAGE_COUNT=5\nSANDIUM_BUILD_STARTED=no")
