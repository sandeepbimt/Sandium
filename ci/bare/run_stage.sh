#!/usr/bin/env bash
set -Eeuo pipefail
set +x
STAGE="$1"; [[ "$STAGE" =~ ^[1-5]$ ]] || exit 2
ROOT="${CIRCLE_WORKING_DIRECTORY:-$HOME/project}"; ROOT="${ROOT/#\~/$HOME}"; CONTROL="$ROOT/sandium-control"; TS_EPOCH=946684800; TS_POLICY=epoch_946684800_full_tree_v1
HANDOFF="$ROOT/ci-handoff/stage-$STAGE"; LEDGER="$HANDOFF/ledger.json"; STATUS="$HANDOFF/status.txt"
PREV="$ROOT/ci-handoff/stage-$((STAGE-1))/ledger.json"; PREV_STATUS="$ROOT/ci-handoff/stage-$((STAGE-1))/status.txt"
mkdir -p "$HANDOFF" "$ROOT/sandium-output"
DRIVER="$ROOT/stage-$STAGE-driver.log"; LOG="$ROOT/stage-$STAGE-siso.log"; METRICS="$ROOT/stage-$STAGE-metrics.jsonl"
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
trap 'rc=$?; if [[ ! -s "$STATUS" ]]; then printf "BUILD_FINISHED=false\nSTAGE=%s\nFAILURE_EXIT_CODE=%s\n" "$STAGE" "$rc" > "$STATUS"; fi' EXIT
if [[ "$STAGE" -gt 1 ]]; then
  test -s "$PREV" || { echo "PREVIOUS_LEDGER_MISSING=$PREV" >&2; exit 41; }
  if grep -qx BUILD_FINISHED=true "$PREV_STATUS" 2>/dev/null; then
    cp "$PREV" "$LEDGER"
    cp "$PREV_STATUS" "$STATUS"
    echo "APK_FAST_PATH=PASS propagated_previous_stage=$((STAGE-1))"
    exit 0
  fi
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
# Each stage recreates the exact Bare-pinned source/build graph. Compiled action
# reuse must come from the existing BuildBuddy/Siso remote cache; CircleCI persists
# only the small source-bound stage ledger, not Chromium output checkpoints.
echo "SANDIUM_STATIC_CHECK_BEGIN_UTC=$(date -u +%FT%TZ)"
for script in "$CONTROL/ci/bare/bootstrap_chromium.sh" "$CONTROL/ci/bare/prepare_source.sh" "$CONTROL/ci/bare/package_artifact.sh"; do
  bash -n "$script"
done
python3 -m py_compile "$CONTROL/ci/bare/configure_siso.py" "$CONTROL/ci/bare/checkpoint.py" "$CONTROL/ci/bare/build_identity.py" "$CONTROL/ci/bare/write_cache_identity.py" "$CONTROL/ci/bare/stage_ledger.py"
echo "SANDIUM_STATIC_CHECK=PASS"
echo "SANDIUM_STATIC_CHECK_END_UTC=$(date -u +%FT%TZ)"
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
  python3 "$CONTROL/ci/bare/stage_ledger.py" --stage "$STAGE" --metrics "$METRICS" --siso-log "$LOG" --output "$LEDGER" --root "$ROOT" --completed-actions "$DONE" --total-actions "$TOTAL" --source-timestamp-policy "$TS_POLICY" "${PREV_ARGS[@]}" --apk "$ROOT/sandium-output/Sandium-debug-arm64.apk" --manifest "$ROOT/sandium-output/build-manifest.json"
else
  python3 "$CONTROL/ci/bare/stage_ledger.py" --stage "$STAGE" --metrics "$METRICS" --siso-log "$LOG" --output "$LEDGER" --root "$ROOT" --completed-actions "$DONE" --total-actions "$TOTAL" --source-timestamp-policy "$TS_POLICY" "${PREV_ARGS[@]}"
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
echo "DIAG_STAGE_OUT_SIZE=$(du -sh "$OUT" 2>/dev/null | cut -f1 || true)"
echo "DIAG_CHECKPOINT_SIZE=$(stat -c %s "$ROOT/sandium-bare-checkpoint.tar.zst" 2>/dev/null || echo 0)"
echo "DIAG_STAGE_END_UTC=$(date -u +%FT%TZ)"
trap - EXIT
exit 0
