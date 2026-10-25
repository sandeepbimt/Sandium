#!/usr/bin/env bash
set -Eeuo pipefail
set +x
STAGE="$1"; [[ "$STAGE" =~ ^[1-5]$ ]] || exit 2
ROOT="$CIRCLE_WORKING_DIRECTORY"; CONTROL="$ROOT/sandium-control"; TS_EPOCH=946684800; TS_POLICY=epoch_946684800_full_tree_v1
HANDOFF="$ROOT/ci-handoff/stage-$STAGE"; LEDGER="$HANDOFF/ledger.json"; STATUS="$HANDOFF/status.txt"
PREV="$ROOT/ci-handoff/stage-$((STAGE-1))/ledger.json"; PREV_STATUS="$ROOT/ci-handoff/stage-$((STAGE-1))/status.txt"
if [[ "$STAGE" -gt 1 ]]; then
  test -s "$PREV" || { echo "PREVIOUS_LEDGER_MISSING=$PREV" >&2; exit 41; }
  if grep -qx BUILD_FINISHED=true "$PREV_STATUS" 2>/dev/null; then echo APK_FAST_PATH=PASS; exit 0; fi
fi
mkdir -p "$HANDOFF" "$ROOT/sandium-output"
DRIVER="$ROOT/stage-$STAGE-driver.log"; LOG="$ROOT/stage-$STAGE-siso.log"; METRICS="$ROOT/stage-$STAGE-metrics.jsonl"
: > "$LOG"; : > "$METRICS"
WALL=2820; [[ "$STAGE" -eq 5 ]] && WALL=3300
exec > >(tee -a "$DRIVER") 2>&1
trap 'rc=$?; if [[ ! -s "$STATUS" ]]; then printf "BUILD_FINISHED=false\nSTAGE=%s\nFAILURE_EXIT_CODE=%s\n" "$STAGE" "$rc" > "$STATUS"; fi' EXIT
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
export PATH="$DEPOT:$PATH" DEPOT_TOOLS_UPDATE=0 SISO_LIMITS=local=8,remote=80 SANDIUM_ALLOW_CHROMIUM_SYNC=true
echo "SANDIUM_STAGE=$STAGE"; echo "SANDIUM_MODE=LONG-Full-APK"; echo "SANDIUM_REAPI_INSTANCE=sandium/android-arm64/ci"
if [[ "$STAGE" -gt 1 && ! -d "$ROOT/src/.git" ]]; then
  echo "SANDIUM_STAGE_HANDOFF_MISSING=Chromium source tree was not restored from the prior stage; refusing to repeat source sync." >&2
  exit 44
fi
if [[ "$STAGE" -gt 1 && ! -s "$ROOT/src/out/android_Debug_arm64/build.ninja" ]]; then
  echo "SANDIUM_STAGE_HANDOFF_MISSING=Generated GN/Ninja output was not restored from the prior stage; refusing to regenerate or rebuild from scratch." >&2
  exit 44
fi
bash "$CONTROL/ci/bare/bootstrap_chromium.sh" source
SRC="$ROOT/src"; OUT="$SRC/out/android_Debug_arm64"; SDIR="$SRC/build/config/siso"
test -s "$SDIR/.sisoenv"; printf '%s\n' 'ninja -re_cache_enable_write=true' > "$SDIR/.sisorc"
find "$SRC" \( -name .git -o -path "$SRC/out" \) -prune -o -exec touch -h -d "@$TS_EPOCH" {} +
for f in "$SRC" "$SRC/DEPS" "$SRC/chrome/VERSION" "$SRC/build/config/compiler/compiler.gni"; do test -e "$f" && test "$(stat -c %Y "$f")" = "$TS_EPOCH" || { echo "SOURCE_TIMESTAMP_VERIFY=FAIL $f" >&2; exit 45; }; done
echo "SOURCE_TIMESTAMP_POLICY=$TS_POLICY"; echo SOURCE_TIMESTAMP_VERIFY=PASS
bash "$CONTROL/ci/bare/bootstrap_chromium.sh" configure
test -s "$OUT/build.ninja"; test -s "$OUT/args.gn"
if [[ "$STAGE" -gt 1 ]]; then
  test -s "$ROOT/sandium-bare-checkpoint.tar.zst" || { echo "SANDIUM_CHECKPOINT_MISSING=previous stage cache did not restore checkpoint archive" >&2; exit 50; }
  test -s "$ROOT/sandium-bare-checkpoint.meta" || { echo "SANDIUM_CHECKPOINT_METADATA_MISSING=previous stage cache did not restore metadata" >&2; exit 51; }
  python3 "$CONTROL/ci/bare/checkpoint.py" restore --root "$ROOT"
fi
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
if [[ -d "$OUT" ]]; then
  python3 "$CONTROL/ci/bare/checkpoint.py" pack --root "$ROOT"
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
trap - EXIT
exit 0
