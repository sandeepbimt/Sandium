#!/usr/bin/env bash
set -Eeuo pipefail
set +x
STAGE="$1"; [[ "$STAGE" =~ ^[1-5]$ ]] || exit 2
ROOT="${CIRCLE_WORKING_DIRECTORY:-$HOME/project}"; ROOT="${ROOT/#\~/$HOME}"; CONTROL="$ROOT/sandium-control"; TS_EPOCH=946684800; TS_POLICY=epoch_946684800_full_tree_v1
STAGE_START_TS="$(date +%s)"
HANDOFF="$ROOT/ci-handoff/stage-$STAGE"; LEDGER="$HANDOFF/ledger.json"; STATUS="$HANDOFF/status.txt"
PREV="$ROOT/ci-handoff/stage-$((STAGE-1))/ledger.json"; PREV_STATUS="$ROOT/ci-handoff/stage-$((STAGE-1))/status.txt"
mkdir -p "$ROOT/sandium-output"
: > "$ROOT/sandium-fetch.log"
: > "$ROOT/sandium-gclient-sync.log"
: > "$ROOT/sandium-gclient-config.log"
: > "$ROOT/sandium-git-trace.log"
mkdir -p "$HANDOFF" "$ROOT/sandium-output"
DRIVER="$ROOT/stage-$STAGE-driver.log"; LOG="$ROOT/stage-$STAGE-siso.log"; METRICS="$ROOT/stage-$STAGE-metrics.jsonl"
STATE_TOOL="$CONTROL/ci/bare/state_handoff.py"
STATE_ARCHIVE="$ROOT/sandium-stage-state.tar.zst"; STATE_META="$ROOT/sandium-stage-state.meta"
STATE_REPORT="$ROOT/sandium-state-handoff-report.json"
: > "$LOG"; : > "$METRICS"
WALL=2820; [[ "$STAGE" -eq 5 ]] && WALL=3300
exec > >(tee -a "$DRIVER") 2>&1
echo "DIAG_STAGE_START_UTC=$(date -u +%FT%TZ)"
echo "DIAG_HOST=$(hostname)"
echo "DIAG_KERNEL=$(uname -a)"
echo "DIAG_CPU_COUNT=$(nproc)"
echo "DIAG_MEMORY_BEGIN"; free -h; echo "DIAG_MEMORY_END"
echo "DIAG_DISK_BEGIN"; df -h "$ROOT"; echo "DIAG_DISK_END"
echo "DIAG_CIRCLE_BRANCH=${CIRCLE_BRANCH:-unknown}"
echo "DIAG_WORKFLOW_ID=${CIRCLE_WORKFLOW_ID:-unknown}"
echo "DIAG_BUILD_NUM=${CIRCLE_BUILD_NUM:-unknown}"
echo "DIAG_CONTROL_HEAD=$(git -C "$CONTROL" rev-parse HEAD)"
# Cross-run caches may contain a previous stage's ledger at the same path.
# Never let stale status suppress this stage's failure status.
rm -f "$LEDGER" "$STATUS" "$STATE_REPORT"
printf '{"schema":1,"SANDIUM_STATE_HANDOFF":"NOT_RUN","current_stage":%s,"current_workflow_id":"%s","reason":"runner exited before state validation"}\n' "$STAGE" "${CIRCLE_WORKFLOW_ID:-unknown}" > "$STATE_REPORT"
trap 'rc=$?; if [[ ! -s "$STATUS" ]]; then printf "BUILD_FINISHED=false\nSTAGE=%s\nFAILURE_EXIT_CODE=%s\n" "$STAGE" "$rc" > "$STATUS"; fi' EXIT
echo "SANDIUM_STATIC_CHECK_BEGIN_UTC=$(date -u +%FT%TZ)"
for script in "$CONTROL/ci/bare/run_stage.sh" "$CONTROL/ci/bare/bootstrap_chromium.sh" "$CONTROL/ci/bare/prepare_source.sh" "$CONTROL/ci/bare/package_artifact.sh"; do
  bash -n "$script"
done
python3 -m py_compile "$CONTROL/ci/bare/configure_siso.py" "$CONTROL/ci/bare/checkpoint.py" "$CONTROL/ci/bare/build_identity.py" "$CONTROL/ci/bare/write_cache_identity.py" "$CONTROL/ci/bare/stage_ledger.py" "$CONTROL/ci/bare/stage_contract.py" "$STATE_TOOL" "$CONTROL/ci/bare/test_state_handoff.py"
python3 "$CONTROL/ci/bare/test_build_identity.py"
python3 "$CONTROL/ci/bare/test_stage_ledger.py"
python3 "$CONTROL/ci/bare/test_state_handoff.py"
python3 "$CONTROL/ci/bare/stage_contract.py"
echo "SANDIUM_STATIC_CHECK=PASS"
echo "SANDIUM_STATIC_CHECK_END_UTC=$(date -u +%FT%TZ)"
if [[ "$STAGE" -gt 1 ]]; then
  test -s "$PREV" || { echo "PREVIOUS_LEDGER_MISSING=$PREV" >&2; exit 41; }
  if grep -qx BUILD_FINISHED=true "$PREV_STATUS" 2>/dev/null; then
    # A green prior status alone is insufficient: the cache must contain the
    # exact verified APK/manifest and the matching identity-bound ledger.
    python3 - "$PREV" "$PREV_STATUS" "$ROOT/sandium-output/Sandium-debug-arm64.apk" "$ROOT/sandium-output/build-manifest.json" <<'PY'
import hashlib,json,sys,zipfile
from pathlib import Path
ledger_path,status_path,apk_path,manifest_path=map(Path,sys.argv[1:5])
ledger=json.loads(ledger_path.read_text(encoding="utf-8"))
status={}
for line in status_path.read_text(encoding="utf-8").splitlines():
 if "=" in line:
  k,v=line.split("=",1); status[k]=v
if not apk_path.is_file() or apk_path.is_symlink() or not manifest_path.is_file() or manifest_path.is_symlink():
 raise SystemExit("APK_FAST_PATH=FAIL cached APK or manifest missing/unsafe")
manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
h=hashlib.sha256()
with apk_path.open("rb") as f:
 for block in iter(lambda:f.read(1024*1024),b""): h.update(block)
digest=h.hexdigest(); size=apk_path.stat().st_size
with zipfile.ZipFile(apk_path) as z:
 if z.testzip() is not None or "AndroidManifest.xml" not in z.namelist() or not any(n.startswith("classes") and n.endswith(".dex") for n in z.namelist()):
  raise SystemExit("APK_FAST_PATH=FAIL cached APK ZIP/content invalid")
if size < 1_000_000 or manifest.get("artifact_verified") is not True or manifest.get("apksigner_verification")!="PASS":
 raise SystemExit("APK_FAST_PATH=FAIL cached manifest lacks verified artifact proof")
if manifest.get("artifact_sha256")!=digest or manifest.get("artifact_bytes")!=size:
 raise SystemExit("APK_FAST_PATH=FAIL cached APK differs from manifest")
if ledger.get("artifact_verified") is not True or ledger.get("build_identity_sha256")!=manifest.get("build_identity_sha256"):
 raise SystemExit("APK_FAST_PATH=FAIL cached ledger/manifest identity mismatch")
if status.get("APK_SHA256")!=digest or status.get("APK_SIZE_BYTES")!=str(size) or status.get("BUILD_IDENTITY_SHA256")!=manifest.get("build_identity_sha256"):
 raise SystemExit("APK_FAST_PATH=FAIL cached status/manifest mismatch")
if status.get("APK_ARCHIVE_VALID")!="true" or status.get("APK_SIGNATURE_VERIFIED")!="true":
 raise SystemExit("APK_FAST_PATH=FAIL cached status lacks archive/signature proof")
print("APK_FAST_PATH_ARTIFACT_REVALIDATION=PASS")
PY
    python3 - "$PREV" "$LEDGER" "$PREV_STATUS" "$STATUS" "$STATE_REPORT" "$STAGE" "${CIRCLE_WORKFLOW_ID:-unknown}" <<'PY'
import hashlib,json,sys
from pathlib import Path
prev,ledger_out,prev_status,status_out,report_out=map(Path,sys.argv[1:6])
stage=int(sys.argv[6]); workflow=sys.argv[7]
ledger=json.loads(prev.read_text(encoding="utf-8"))
prior_status={}
for line in prev_status.read_text(encoding="utf-8").splitlines():
 if "=" in line:
  k,v=line.split("=",1); prior_status[k]=v
identity_hash=ledger.get("build_identity_sha256","")
ledger["previous_stage_ledger_sha256"]=hashlib.sha256(prev.read_bytes()).hexdigest()
ledger["stage"]=stage
ledger["evidence"]="VERIFIED_APK_COMPLETE_FAST_PATH"
ledger["state_handoff"]={
 "schema":1,"SANDIUM_STATE_HANDOFF":"SKIPPED_APK_ALREADY_VERIFIED",
 "source_stage":stage-1,"source_workflow_id":workflow,
 "current_stage":stage,"current_workflow_id":workflow,
 "build_identity_sha256":identity_hash,
 "local_compiled_outputs_restored":False,
 "reason":"Cached APK, manifest, status and ledger revalidated; no compilation work remains"
}
ledger_out.write_text(json.dumps(ledger,indent=2,sort_keys=True)+"\n",encoding="utf-8")
prior_status["STAGE"]=str(stage)
status_out.write_text("".join(f"{k}={v}\n" for k,v in prior_status.items()),encoding="utf-8")
report_out.write_text(json.dumps(ledger["state_handoff"],indent=2,sort_keys=True)+"\n",encoding="utf-8")
PY
    echo "APK_FAST_PATH=PASS propagated_previous_stage=$((STAGE-1))"
    echo "SANDIUM_BUILD_WORK_REMAINING=no"
    exit 0
  fi
fi

# A compatible prior useful run may already contain a verified APK. Validate the
# cache identity, saved manifest, SHA-256, ZIP structure, and recorded signature
# verification before skipping any source/build work.
if [[ "$STAGE" -eq 1 && -s "$STATE_ARCHIVE" && -s "$STATE_META" && -s "$ROOT/sandium-output/Sandium-debug-arm64.apk" && -s "$ROOT/sandium-output/build-manifest.json" ]]; then
  set +e
  python3 "$STATE_TOOL" reuse-apk --root "$ROOT" --workflow-id "${CIRCLE_WORKFLOW_ID:-unknown}" --archive "$STATE_ARCHIVE" --meta "$STATE_META" --apk "$ROOT/sandium-output/Sandium-debug-arm64.apk" --manifest "$ROOT/sandium-output/build-manifest.json" --ledger "$LEDGER" --status "$STATUS" --report "$STATE_REPORT"
  REUSE_RC=$?
  set -e
  if [[ "$REUSE_RC" -eq 0 && -s "$STATUS" ]] && grep -qx 'BUILD_FINISHED=true' "$STATUS"; then
    echo "SANDIUM_PRIOR_APK_REUSE=PASS"
    echo "SANDIUM_SOURCE_SYNC_RUN=no"
    echo "SANDIUM_GN_GENERATION_RUN=no"
    echo "SANDIUM_COMPILATION_RUN=no"
    exit 0
  fi
fi

# Validate the restored prior-run candidate before toolchain/source/network work.
# Stage 1 records a clean miss and may proceed cold; stages 2-5 must refuse to
# spend setup time unless the immediately preceding stage saved a valid capsule.
if [[ "$STAGE" -eq 1 ]]; then
  python3 "$STATE_TOOL" preflight --root "$ROOT" --stage "$STAGE" --workflow-id "${CIRCLE_WORKFLOW_ID:-unknown}" --archive "$STATE_ARCHIVE" --meta "$STATE_META"
else
  python3 "$STATE_TOOL" preflight --root "$ROOT" --stage "$STAGE" --workflow-id "${CIRCLE_WORKFLOW_ID:-unknown}" --archive "$STATE_ARCHIVE" --meta "$STATE_META" --required
fi

test -n "$(printenv BUILDBUDDY_API_KEY 2>/dev/null || true)" || { echo MISSING_REQUIRED_SECRET=BUILDBUDDY_API_KEY >&2; exit 42; }

DEPOT="$HOME/depot_tools"; PIN=""
if [[ "$STAGE" -gt 1 ]]; then
  PIN="$(python3 - "$PREV" <<'PY'
import json,sys
print(json.load(open(sys.argv[1],encoding="utf-8")).get("build_identity",{}).get("toolchain_depot_tools_commit",""))
PY
)"
  [[ "$PIN" =~ ^[0-9a-f]{40}$ ]] || { echo PREVIOUS_DEPOT_TOOLS_PIN_INVALID >&2; exit 43; }
elif [[ -s "$STATE_META" ]]; then
  # A prior useful run's manifest pins depot_tools before the state capsule is
  # validated against the current source/GN identity.
  PIN="$(python3 "$STATE_TOOL" toolchain-pin --meta "$STATE_META" 2>/dev/null || true)"
  if [[ ! "$PIN" =~ ^[0-9a-f]{40}$ ]]; then PIN=""; echo "SANDIUM_PRIOR_TOOLCHAIN_PIN=UNAVAILABLE"; else echo "SANDIUM_PRIOR_TOOLCHAIN_PIN=$PIN"; fi
fi
if [[ ! -d "$DEPOT/.git" ]]; then git clone --no-tags --depth=1 https://chromium.googlesource.com/chromium/tools/depot_tools.git "$DEPOT"; fi
if [[ -n "$PIN" && "$(git -C "$DEPOT" rev-parse HEAD)" != "$PIN" ]]; then git -C "$DEPOT" fetch --depth=1 origin "$PIN"; git -C "$DEPOT" checkout --force --detach "$PIN"; fi
# Bootstrap a fresh depot_tools clone before disabling its updater.
export PATH="$DEPOT:$PATH"
if [[ ! -s "$DEPOT/python3_bin_reldir.txt" ]]; then
  echo "DEPOT_TOOLS_BOOTSTRAP_BEGIN=$(date -u +%FT%TZ)"
  if [[ -x "$DEPOT/update_depot_tools" ]]; then
    (cd "$DEPOT" && ./update_depot_tools)
  elif [[ -x "$DEPOT/ensure_bootstrap" ]]; then
    (cd "$DEPOT" && ./ensure_bootstrap)
  else
    echo "DEPOT_TOOLS_BOOTSTRAP_HELPER_MISSING" >&2
    find "$DEPOT" -maxdepth 2 \( -iname "*bootstrap*" -o -iname "*update*depot*" \) -print | head -n 80 >&2 || true
    exit 46
  fi
  test -s "$DEPOT/python3_bin_reldir.txt" || { echo "DEPOT_TOOLS_BOOTSTRAP_MARKER_MISSING" >&2; exit 46; }
  echo "DEPOT_TOOLS_BOOTSTRAP=PASS"
  echo "DEPOT_TOOLS_BOOTSTRAP_END=$(date -u +%FT%TZ)"
fi
export DEPOT_TOOLS_UPDATE=0 SISO_LIMITS=local=8,remote=80 SANDIUM_ALLOW_CHROMIUM_SYNC=true
echo "SANDIUM_STAGE=$STAGE"; echo "SANDIUM_MODE=LONG-Full-APK"; echo "SANDIUM_REAPI_INSTANCE=sandium/android-arm64/ci"
# Each stage recreates the exact Bare-pinned source/build graph. CircleCI's
# source-identity cache discovers a prior useful run and hands off a small,
# hash-verified Siso state capsule plus ledger/verified APK when available.
# Compiled objects are never archived here; Siso must prove BuildBuddy cache hits.
bash "$CONTROL/ci/bare/bootstrap_chromium.sh" source
SRC="$ROOT/src"; OUT="$SRC/out/android_Debug_arm64"; SDIR="$SRC/build/config/siso"
test -s "$SDIR/.sisoenv"; printf '%s\n' 'ninja -re_cache_enable_write=true' > "$SDIR/.sisorc"
find "$SRC" \( -name .git -o -path "$SRC/out" \) -prune -o -exec touch -h -d "@$TS_EPOCH" {} +
for f in "$SRC" "$SRC/DEPS" "$SRC/chrome/VERSION" "$SRC/build/config/compiler/compiler.gni"; do test -e "$f" && test "$(stat -c %Y "$f")" = "$TS_EPOCH" || { echo "SOURCE_TIMESTAMP_VERIFY=FAIL $f" >&2; exit 45; }; done
echo "SOURCE_TIMESTAMP_POLICY=$TS_POLICY"; echo SOURCE_TIMESTAMP_VERIFY=PASS
bash "$CONTROL/ci/bare/bootstrap_chromium.sh" configure
test -s "$OUT/build.ninja"; test -s "$OUT/args.gn"
grep -Eq '^[[:space:]]*use_siso[[:space:]]*=[[:space:]]*true$' "$OUT/args.gn"
grep -Eq '^[[:space:]]*use_reclient[[:space:]]*=[[:space:]]*false$' "$OUT/args.gn"
grep -Eq '^[[:space:]]*use_remoteexec[[:space:]]*=[[:space:]]*true$' "$OUT/args.gn"
python3 "$CONTROL/ci/bare/write_cache_identity.py"
if [[ "$STAGE" -eq 1 ]]; then
  python3 "$STATE_TOOL" restore --root "$ROOT" --stage "$STAGE" --workflow-id "${CIRCLE_WORKFLOW_ID:-unknown}" --archive "$STATE_ARCHIVE" --meta "$STATE_META"
else
  python3 "$STATE_TOOL" restore --root "$ROOT" --stage "$STAGE" --workflow-id "${CIRCLE_WORKFLOW_ID:-unknown}" --archive "$STATE_ARCHIVE" --meta "$STATE_META" --required
fi
ELAPSED=$(( $(date +%s) - STAGE_START_TS )); REMAIN=$(( WALL - ELAPSED ))
echo "SANDIUM_SETUP_SECONDS=$ELAPSED"; echo "SANDIUM_REMAINING_COMPILE_SECONDS=$REMAIN"
[[ "$REMAIN" -gt 120 ]] || { echo STAGE_SETUP_CONSUMED_BUDGET >&2; exit 47; }

rm -f "$METRICS" "$LOG"; : > "$LOG"
set +e
setsid -w timeout --signal=INT --kill-after=60s "$REMAIN"s bash -c 'set -o pipefail; cd "$1"; autoninja -strict_remote -metrics_json "$2" -C "$3" -j80 chrome_public_apk 2>&1 | tee "$4"' _ "$SRC" "$METRICS" "$OUT" "$LOG" &
PID=$!
set -e
WAIT=0; EVIDENCE=false
while [[ "$WAIT" -lt 240 ]]; do
  grep -Fq 'Regenerating ninja files' "$LOG" 2>/dev/null && { echo NINJA_REGEN_GATE=FAIL >&2; break; }
  if grep -Eiq '"is_local"[[:space:]]*:[[:space:]]*false|"is_remote"[[:space:]]*:[[:space:]]*true|"cached"[[:space:]]*:[[:space:]]*true|step state:[[:space:]]*remote exec|exec remote execute' "$LOG" "$METRICS" 2>/dev/null; then EVIDENCE=true; echo SISO_REMOTE_OR_CACHE_EVIDENCE=PASS; break; fi
  kill -0 "$PID" 2>/dev/null || break; sleep 15; WAIT=$((WAIT+15))
done
if [[ "$EVIDENCE" != true ]] && kill -0 "$PID" 2>/dev/null; then
  echo SISO_REMOTE_OR_CACHE_EVIDENCE=FAIL >&2; kill -INT -- "-$PID" 2>/dev/null || true; sleep 5; kill -TERM -- "-$PID" 2>/dev/null || true; sleep 5; kill -KILL -- "-$PID" 2>/dev/null || true
fi
set +e; wait "$PID"; RET=$?; set -e; echo AUTONINJA_EXIT_CODE=$RET
# The build can finish before the polling loop observes evidence, or Siso may
# flush its final metrics only at shutdown. Recheck the completed logs, then
# fail closed: an APK alone must never bypass the remote/cache execution gate.
if [[ "$EVIDENCE" != true ]] && grep -Eiq '"is_local"[[:space:]]*:[[:space:]]*false|"is_remote"[[:space:]]*:[[:space:]]*true|"cached"[[:space:]]*:[[:space:]]*true|step state:[[:space:]]*remote exec|exec remote execute' "$LOG" "$METRICS" 2>/dev/null; then
  EVIDENCE=true
  echo SISO_REMOTE_OR_CACHE_EVIDENCE=PASS_FINAL_METRICS
fi
if [[ "$EVIDENCE" != true ]]; then
  echo SISO_REMOTE_OR_CACHE_EVIDENCE=FAIL_NO_ACTION_PROOF >&2
  exit 50
fi
echo "DIAG_METRICS_LINES=$(wc -l < "$METRICS" 2>/dev/null || echo 0)"
echo "DIAG_METRICS_HEAD_BEGIN"; head -n 10 "$METRICS" 2>/dev/null || true; echo "DIAG_METRICS_HEAD_END"
echo "DIAG_SISO_LOG_TAIL_BEGIN"; tail -n 160 "$LOG" 2>/dev/null || true; echo "DIAG_SISO_LOG_TAIL_END"
for i in $(seq 1 30); do pgrep -x siso >/dev/null 2>&1 || break; sleep 2; done
if pgrep -x siso >/dev/null 2>&1; then echo SISO_SHUTDOWN=FAILED >&2; pkill -TERM -x siso 2>/dev/null || true; sleep 5; pkill -KILL -x siso 2>/dev/null || true; exit 48; fi
[[ "$RET" -eq 0 || "$RET" -eq 124 ]] || { echo "BUILD_SLICE_FAILED=$RET" >&2; exit 49; }

grep -oE '\[[[:space:]]*[0-9]+/[0-9]+\]' "$LOG" 2>/dev/null | sed -E 's/\[//;s/\]//;s/[[:space:]]//g' > "$ROOT/progress-$STAGE.txt" || true
DONE="$(awk -F/ 'BEGIN{m=0} NF==2 && $1+0>m {m=$1+0} END{print m}' "$ROOT/progress-$STAGE.txt")"
TOTAL="$(awk -F/ 'BEGIN{m=0} NF==2 && $2+0>m {m=$2+0} END{print m}' "$ROOT/progress-$STAGE.txt")"
APK="$OUT/apks/ChromePublic.apk"; COMPLETE=false
if [[ -s "$APK" ]]; then bash "$CONTROL/ci/bare/package_artifact.sh"; COMPLETE=true; fi
if [[ "$STAGE" -eq 1 ]]; then
  PREV_ARGS=()
else
  PREV_ARGS=(--previous "$PREV")
fi
if [[ "$COMPLETE" == true ]]; then
  python3 "$CONTROL/ci/bare/stage_ledger.py" --stage "$STAGE" --metrics "$METRICS" --siso-log "$LOG" --output "$LEDGER" --root "$ROOT" --completed-actions "$DONE" --total-actions "$TOTAL" --source-timestamp-policy "$TS_POLICY" --state-handoff-report "$STATE_REPORT" "${PREV_ARGS[@]}" --apk "$ROOT/sandium-output/Sandium-debug-arm64.apk" --manifest "$ROOT/sandium-output/build-manifest.json"
else
  python3 "$CONTROL/ci/bare/stage_ledger.py" --stage "$STAGE" --metrics "$METRICS" --siso-log "$LOG" --output "$LEDGER" --root "$ROOT" --completed-actions "$DONE" --total-actions "$TOTAL" --source-timestamp-policy "$TS_POLICY" --state-handoff-report "$STATE_REPORT" "${PREV_ARGS[@]}"
fi
if [[ "$COMPLETE" == true ]]; then
  HASH="$(sha256sum "$ROOT/sandium-output/Sandium-debug-arm64.apk" | awk '{print $1}')"; BYTES="$(stat -c %s "$ROOT/sandium-output/Sandium-debug-arm64.apk")"
  ID="$(python3 - "$ROOT/sandium-output/build-manifest.json" <<'PY'
import json,sys
print(json.load(open(sys.argv[1],encoding="utf-8"))["build_identity_sha256"])
PY
)"
  printf 'BUILD_FINISHED=true\nAPK_ARCHIVE_VALID=true\nAPK_SIGNATURE_VERIFIED=true\nAPK_SHA256=%s\nAPK_SIZE_BYTES=%s\nBUILD_IDENTITY_SHA256=%s\nSTAGE=%s\n' "$HASH" "$BYTES" "$ID" "$STAGE" > "$STATUS"
  echo "SANDIUM_APK_READY=true"; echo "SANDIUM_APK_SHA256=$HASH"; echo "SANDIUM_APK_BYTES=$BYTES"
else
  printf 'BUILD_FINISHED=false\nSTAGE=%s\nBUILD_EXIT_CODE=%s\nMAX_COMPLETED_ACTIONS=%s\nTOTAL_ACTION_COUNT=%s\n' "$STAGE" "$RET" "$DONE" "$TOTAL" > "$STATUS"
  echo SANDIUM_APK_READY=false
fi
python3 "$STATE_TOOL" pack --root "$ROOT" --stage "$STAGE" --workflow-id "${CIRCLE_WORKFLOW_ID:-unknown}" --ledger "$LEDGER" --archive "$STATE_ARCHIVE" --meta "$STATE_META"
echo "DIAG_STAGE_OUT_SIZE=$(du -sh "$OUT" 2>/dev/null | cut -f1 || true)"
echo "DIAG_SISO_STATE_CAPSULE_SIZE=$(stat -c %s "$STATE_ARCHIVE" 2>/dev/null || echo 0)"
echo "DIAG_STAGE_END_UTC=$(date -u +%FT%TZ)"
trap - EXIT
exit 0
